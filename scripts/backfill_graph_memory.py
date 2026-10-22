"""Seed the Neo4j org memory from everything FabricBI has already recorded locally.

Sources, in order of how much they're trusted:
  project_data.db  clients, projects, semantic_models, audit_log
  projects.metadata.fabricbi_memory  the per-project notes (same format the live
                                  turn-writer produces, so no duplication)
  .pending_model_edits.json       queue DAX for edits whose status is applied
  .created_model_objects_*.json   applied measures — the only place with model

Everything is MERGE, so re-running is safe and picks up whatever is new.

    python scripts/backfill_graph_memory.py [--dry-run]
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import GRAPH_MEMORY_NOTES_PER_PROJECT, PROJECTS_DIR
from db import graph_memory as gm

DRY = "--dry-run" in sys.argv
DB = "project_data.db"


def _rows(sql: str, *args) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, args))
    finally:
        conn.close()


def _json(raw) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


APPLIED_STATUSES = ("applied",)


def applied_measure_dax(entries: list) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in entries or []:
        if e.get("object_type") != "measure" or e.get("status") not in APPLIED_STATUSES:
            continue
        name = (e.get("name") or "").strip()
        if name:
            out[name] = {
                "dax": str(e.get("definition") or ""),
                "table": e.get("table") or "",
                "reason": e.get("reason") or "",
            }
    return out


def load_dax_by_name() -> dict[str, dict]:
    return applied_measure_dax(_json_file(".pending_model_edits.json") or [])


def _json_file(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_measures() -> list[dict]:
    dax = load_dax_by_name()
    by_key: dict[tuple[str, str], dict] = {}

    for path in glob.glob(".created_model_objects_*.json"):
        for row in _json_file(path) or []:
            name, model = (row.get("name") or "").strip(), (
                row.get("model") or ""
            ).strip()
            if not name or not model or row.get("type") != "measure":
                continue
            d = dax.get(name, {})
            by_key[(model, name)] = {
                "name": name,
                "model": model,
                "table": row.get("table") or d.get("table", ""),
                "dax": d.get("dax", ""),
                "reason": row.get("reason") or d.get("reason", ""),
                "project_id": 0,
            }

    for r in _rows(
        "SELECT target, details, project_id FROM audit_log "
        "WHERE event_type = 'model.measure_create'"
    ):
        name = (r["target"] or "").strip()
        model = (_json(r["details"]).get("model") or "").strip()
        if not name or not model:
            continue
        d = dax.get(name, {})
        rec = by_key.setdefault(
            (model, name),
            {
                "name": name,
                "model": model,
                "table": d.get("table", ""),
                "dax": d.get("dax", ""),
                "reason": _json(r["details"]).get("reason", ""),
            },
        )
        rec["project_id"] = r["project_id"] or rec.get("project_id", 0)
    return list(by_key.values())


def load_model_measures() -> list[dict]:
    from model.model_docs import _all_measures, _load_model

    out: list[dict] = []
    for sm in glob.glob(
        os.path.join(str(PROJECTS_DIR), "*", "**", "*.SemanticModel"), recursive=True
    ):
        if not os.path.isdir(sm):
            continue
        try:
            title, tables, _ = _load_model(sm)
        except Exception as e:
            print(f"  ! skipped {os.path.basename(sm)}: {type(e).__name__}: {e}")
            continue
        for table, name, expr, _fmt, desc in _all_measures(tables):
            if name and expr:
                out.append(
                    {
                        "name": name,
                        "model": title,
                        "table": table,
                        "dax": expr,
                        "reason": desc,
                        "project_id": 0,
                    }
                )
    return out


def merge_measures(disk: list[dict], queue: list[dict]) -> list[dict]:
    _counts: dict[str, int] = {}
    for m in disk:
        if m["dax"]:
            _counts[m["name"]] = _counts.get(m["name"], 0) + 1
    dax_by_name = {m["name"]: m for m in disk if m["dax"] and _counts[m["name"]] == 1}
    by_key = {(m["model"], m["name"]): m for m in disk}
    for m in queue:
        disk_rec = by_key.get((m["model"], m["name"])) or dax_by_name.get(m["name"], {})
        if disk_rec.get("dax"):
            m["dax"] = disk_rec["dax"]
        m["table"] = m["table"] or disk_rec.get("table", "")
        by_key[(m["model"], m["name"])] = m
    return list(by_key.values())


def main() -> int:
    if not gm.enabled():
        print("Org memory is disabled — set NEO4J_URI/NEO4J_PASSWORD in .env.")
        return 1
    if gm._driver() is None:
        print("Neo4j unreachable — is the Aura instance paused?")
        return 1

    clients = {r["id"]: r["name"] for r in _rows("SELECT id, name FROM clients")}
    projects = _rows("SELECT id, client_id, name, metadata FROM projects")
    models = _rows(
        "SELECT DISTINCT project_id, model_name, server_fqdn, db_name "
        "FROM semantic_models WHERE model_name <> ''"
    )
    users = _rows(
        "SELECT DISTINCT project_id, user_email FROM audit_log "
        "WHERE project_id IS NOT NULL AND user_email <> ''"
    )
    measures = merge_measures(load_model_measures(), load_measures())

    models_by_project: dict[int, list[str]] = {}
    for m in models:
        models_by_project.setdefault(m["project_id"], []).append(m["model_name"])
    users_by_project: dict[int, list[str]] = {}
    for u in users:
        users_by_project.setdefault(u["project_id"], []).append(u["user_email"])

    counts = {"projects": 0, "notes": 0, "measures": 0, "users": 0, "models": 0}
    for p in projects:
        pid, pname = p["id"], p["name"]
        notes = [
            ln.strip().lstrip("- ").strip()
            for ln in (_json(p["metadata"]).get("fabricbi_memory") or "").splitlines()
            if ln.strip()
        ][-GRAPH_MEMORY_NOTES_PER_PROJECT:]
        pmodels = models_by_project.get(pid, [])
        pusers = users_by_project.get(pid, [])
        print(
            f"  #{pid} {pname:22} {len(notes):3} notes  {len(pmodels)} model(s)  "
            f"{len(pusers)} user(s)"
        )
        if DRY:
            continue

        # Notes are CREATEd (every live turn is a distinct event), so re-running
        gm._run(
            "MATCH (:Project {id:$pid})-[:NOTED]->(n:Note) DETACH DELETE n", pid=pid
        )

        gm.remember_turn(
            pid,
            project_name=pname,
            client_name=clients.get(p["client_id"], ""),
            models=pmodels,
            user_email=pusers[0] if pusers else "",
            note=notes[0] if notes else "",
        )
        for email in pusers[1:]:
            gm.remember_turn(pid, project_name=pname, user_email=email)
        for note in notes[1:]:
            gm.remember_turn(pid, project_name=pname, note=note)
        counts["projects"] += 1
        counts["notes"] += len(notes)
        counts["users"] += len(pusers)
        counts["models"] += len(pmodels)

    for m in measures:
        print(
            f"  measure {m['model']}::{m['name']}"
            f"{'  (no DAX on file)' if not m['dax'] else ''}"
        )
        if DRY:
            continue
        gm.remember_measure(
            m.get("project_id") or 0,
            model=m["model"],
            name=m["name"],
            table=m["table"],
            dax=m["dax"],
            reason=m["reason"],
        )
        counts["measures"] += 1

    if not DRY:
        for m in models:
            if m["server_fqdn"] or m["db_name"]:
                gm._run(
                    "MERGE (mo:Model {name: $n}) SET mo.source_server = $s, mo.source_db = $d",
                    n=m["model_name"],
                    s=m["server_fqdn"] or "",
                    d=m["db_name"] or "",
                )

    print("\n" + ("DRY RUN — nothing written." if DRY else f"written: {counts}"))
    print(gm.stats())
    gm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
