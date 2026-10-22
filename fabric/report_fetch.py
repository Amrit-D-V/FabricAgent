"""Pull a Fabric report onto local disk so the on-disk report tools can read it.

Every `pbi_*` report tool in this repo works on PBIP/PBIR files on disk --
`pbi_report_info`, `pbi_visual_list`, `pbi_visual_bindings`, `pbi_dashboard_audit`
all take a `report_path`. That left a gap: a report living in the Fabric service
could not be analysed at all, because nothing brought it down.

This module closes that gap. `fetch_report()` resolves a report by name, pulls
its definition (and its semantic model's TMDL) through the Fabric item API, and
writes a real PBIP project under FABRICBI_TEMP_DIR. The returned local path is
what you hand to the analysis tools.

Credentials come from the stored Fabric connection (`fabric_connection` row,
decrypted via `auth.crypto_store`), falling back to .env.

Read-only against the tenant: GET plus the getDefinition scan. Nothing is
written back to Fabric; the only writes are local files.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from config import FABRICBI_TEMP_DIR
from fabric.fabric_connection import get_fabric_connection

logger = logging.getLogger(__name__)

_PBI_BASE = "https://api.powerbi.com/v1.0/myorg"
_FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
_PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
_FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
_TIMEOUT = 120
_OP_TIMEOUT = 300

_INVALID = re.compile(r'[<>:"/\\|?*]')

# Power BI Desktop validates this exact string against
#   ^https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/
#    1.[0-9]+.[0-9]+/schema.json$
# and refuses to open the project otherwise ("UnrecognizedSchemaVersion:
# Path: PBIP Shortcut"). Note .../fabric/pbip/pbipProperties/..., NOT the
# .../fabric/item/... prefix the report and model *part* schemas use.
PBIP_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/pbip/"
    "pbipProperties/1.0.0/schema.json"
)
_PBIP_SCHEMA_RE = re.compile(
    r"^https://developer\.microsoft\.com/json-schemas/fabric/pbip/"
    r"pbipProperties/1\.[0-9]+\.[0-9]+/schema\.json$"
)


class FetchError(RuntimeError):
    """Raised when the fetch cannot proceed at all (auth, config, no such report)."""


@dataclass
class FetchResult:
    name: str = ""
    path: Path | None = None
    report_parts: int = 0
    model_parts: int = 0
    legacy: bool = False
    pbix: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.report_parts > 0 or self.model_parts > 0


def safe_name(name: str) -> str:
    return _INVALID.sub("_", (name or "report").strip()).rstrip(". ") or "report"


def longpath(p: Path) -> str:
    r"""Windows MAX_PATH escape hatch.

    A PBIP tree is deep -- <Name>/<Name>.SemanticModel/definition/tables/
    <TableName>.tmdl -- and the storage root is already a long OneDrive path.
    Together they cross 260 characters and mkdir dies with WinError 206 partway
    through, leaving half a project on disk. The \\?\ prefix lifts the limit;
    it needs a fully resolved, backslash-separated path.
    """
    s = str(p.resolve())
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s


def write_bytes(target: Path, data: bytes) -> None:
    os.makedirs(longpath(target.parent), exist_ok=True)
    with open(longpath(target), "wb") as fh:
        fh.write(data)


def connection():
    conn = get_fabric_connection()
    if not conn.has_sp:
        raise FetchError(
            "no service principal configured. Set it on the Fabric Connection "
            "admin page, or put AZURE_TENANT_ID / AZURE_CLIENT_ID / "
            "AZURE_CLIENT_SECRET in .env."
        )
    if not conn.workspace_id:
        raise FetchError(
            f"the stored connection has no workspace_id (workspace is "
            f"{conn.workspace!r}). Re-save it on the Fabric Connection page, or "
            f"set FABRIC_WORKSPACE_ID in .env."
        )
    return conn


def token(conn, scope: str) -> str:
    """Explicit client-credentials token.

    Deliberately not `auth.token_manager.get_token_cached()`: that chains
    AzureCliCredential first, so on a machine with `az login` active it can
    return a different identity than the service principal in the DB.
    """
    from azure.identity import ClientSecretCredential

    cred = ClientSecretCredential(
        tenant_id=conn.tenant_id,
        client_id=conn.client_id,
        client_secret=conn.client_secret,
    )
    return cred.get_token(scope).token


def temp_root() -> Path:
    """The scratch area reports are downloaded into."""
    root = Path(FABRICBI_TEMP_DIR)
    parent = root.parent
    if not parent.exists():
        raise FetchError(
            f"the storage root does not exist: {parent}\n"
            f"config.py falls back to a hardcoded PROJECTS_DIR default when it "
            f"is unset in .env. Set PROJECTS_DIR (or FABRICBI_TEMP_DIR) to a real "
            f"folder."
        )
    os.makedirs(longpath(root), exist_ok=True)
    return root


def list_reports(conn, tok: str) -> list[dict]:
    url = f"{_PBI_BASE}/groups/{conn.workspace_id}/reports"
    r = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=_TIMEOUT)
    if r.status_code >= 400:
        raise FetchError(
            f"listing reports returned {r.status_code}: {r.text[:300]}. A 401/403 "
            f"usually means the tenant setting 'Service principals can use Fabric "
            f"APIs' is off, or the SP has no access to the workspace."
        )
    return r.json().get("value", []) or []


def get_definition(
    tok: str, workspace_id: str, item_id: str, fmt: str, notes: list[str]
) -> dict | None:
    """POST getDefinition, following the long-running-operation dance."""
    headers = {"Authorization": f"Bearer {tok}"}
    url = f"{_FABRIC_BASE}/workspaces/{workspace_id}/items/{item_id}/getDefinition"
    if fmt:
        url += f"?format={fmt}"
    r = requests.post(url, headers=headers, timeout=_TIMEOUT)

    if r.status_code == 202:
        # Poll the Location header, NOT api.fabric.microsoft.com/operations.
        # Fabric returns a region-specific redirect host (e.g.
        # wabi-india-central-a-primary-redirect.analysis.windows.net); polling
        # the global host finds no such operation and the fetch silently yields
        # nothing.
        location = r.headers.get("Location", "")
        op = r.headers.get("x-ms-operation-id", "")
        if not location and op:
            location = f"{_FABRIC_BASE}/operations/{op}"
        if not location:
            notes.append("getDefinition accepted (202) but returned no Location.")
            return None

        delay = float(r.headers.get("Retry-After", "5") or 5)
        deadline = time.time() + _OP_TIMEOUT
        while time.time() < deadline:
            time.sleep(delay)
            s = requests.get(location, headers=headers, timeout=_TIMEOUT)
            if s.status_code >= 400:
                notes.append(f"operation poll failed ({s.status_code}).")
                return None
            body = s.json() or {}
            state = body.get("status", "")
            if state == "Succeeded":
                res = requests.get(
                    f"{location.rstrip('/')}/result", headers=headers, timeout=_TIMEOUT
                )
                if res.status_code >= 400:
                    notes.append(f"result fetch failed ({res.status_code}).")
                    return None
                return res.json()
            if state in ("Failed", "Undefined"):
                err = (body.get("error") or {}).get("message", "")
                notes.append(f"operation {state}: {err[:200]}")
                return None
            delay = float(s.headers.get("Retry-After", delay) or delay)
        notes.append(f"operation timed out after {_OP_TIMEOUT}s.")
        return None

    if r.status_code >= 400:
        notes.append(f"getDefinition failed ({r.status_code}): {r.text[:200].strip()}")
        return None
    return r.json()


def write_parts(payload: dict, dest: Path) -> int:
    """Base64-decode the definition parts into a real folder tree."""
    parts = ((payload or {}).get("definition", {}) or {}).get("parts", []) or []
    written = 0
    for part in parts:
        rel = (part.get("path") or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in rel.split("/"):
            continue  # never let a returned path escape the output folder
        write_bytes(dest / rel, base64.b64decode(part.get("payload", "") or ""))
        written += 1
    return written


def write_pbip_file(root: Path, name: str) -> None:
    """The top-level .pbip that makes Power BI Desktop open the folder."""
    if not _PBIP_SCHEMA_RE.match(PBIP_SCHEMA):
        raise FetchError("internal: PBIP_SCHEMA does not satisfy Desktop's pattern")
    write_bytes(
        root / f"{name}.pbip",
        json.dumps(
            {
                "$schema": PBIP_SCHEMA,
                "version": "1.0",
                "artifacts": [{"report": {"path": f"{name}.Report"}}],
                "settings": {"enableAutoRecovery": True},
            },
            indent=2,
        ).encode("utf-8"),
    )


def export_pbix(conn, tok: str, report_id: str, dest: Path, notes: list[str]) -> bool:
    """Download the binary .pbix. Not every report is exportable."""
    url = f"{_PBI_BASE}/groups/{conn.workspace_id}/reports/{report_id}/Export"
    r = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=_TIMEOUT)
    if r.status_code >= 400:
        # Common and not fatal: live-connected reports, or reports with
        # incremental refresh, refuse export.
        notes.append(
            f".pbix export unavailable ({r.status_code}): {r.text[:160].strip()}"
        )
        return False
    write_bytes(dest, r.content)
    return True


def resolve_report(reports: list[dict], name: str) -> dict:
    want = (name or "").strip().lower()
    if not want:
        raise FetchError("no report name given.")
    hits = [r for r in reports if (r.get("name") or "").strip().lower() == want]
    if not hits:
        known = ", ".join(sorted((r.get("name") or "?") for r in reports)[:25])
        raise FetchError(f"no report named {name!r}. Available: {known}")
    if len(hits) > 1:
        raise FetchError(
            f"{len(hits)} reports are named {name!r}; cannot tell them apart by "
            f"name alone."
        )
    return hits[0]


def fetch_report(
    report_name: str,
    workspace: str = "",
    include_pbix: bool = False,
    dest_root: Path | None = None,
) -> FetchResult:
    """Download one report (and its model) into local temp storage as PBIP."""
    conn = connection()
    if workspace and workspace.strip().lower() != (conn.workspace or "").lower():
        raise FetchError(
            f"the stored connection points at workspace {conn.workspace!r}, not "
            f"{workspace!r}. Only the connected workspace can be fetched from."
        )

    pbi_tok = token(conn, _PBI_SCOPE)
    report = resolve_report(list_reports(conn, pbi_tok), report_name)

    name = report.get("name") or "report"
    safe = safe_name(name)
    root = (Path(dest_root) if dest_root else temp_root()) / safe
    result = FetchResult(name=name, path=root)
    fab_tok = token(conn, _FABRIC_SCOPE)
    rid = report.get("id", "")

    payload = get_definition(fab_tok, conn.workspace_id, rid, "PBIR", result.notes)
    if not payload:
        # Fall back to PBIR-Legacy format if PBIR export is unavailable
        result.notes.append("PBIR unavailable; retried as PBIR-Legacy.")
        payload = get_definition(
            fab_tok, conn.workspace_id, rid, "PBIR-Legacy", result.notes
        )
        result.legacy = bool(payload)

    if payload:
        result.report_parts = write_parts(payload, root / f"{safe}.Report")
        write_pbip_file(root, safe)

    ds_id = report.get("datasetId") or ""
    if ds_id:
        ds = get_definition(fab_tok, conn.workspace_id, ds_id, "TMDL", result.notes)
        if ds:
            result.model_parts = write_parts(ds, root / f"{safe}.SemanticModel")
        else:
            result.notes.append("semantic model definition unavailable.")
    else:
        result.notes.append("report reports no datasetId; no model fetched.")

    if include_pbix:
        dest = root / f"{safe}.pbix"
        if export_pbix(conn, pbi_tok, rid, dest, result.notes):
            result.pbix = dest

    logger.info(
        "report_fetch: %s -> %s (report=%d parts, model=%d parts, legacy=%s)",
        name,
        root,
        result.report_parts,
        result.model_parts,
        result.legacy,
    )
    return result


def format_result(res: FetchResult) -> str:
    """Render a fetch for the assistant, including what it can and cannot analyse."""
    if not res.ok:
        lines = [f"FAILED to fetch '{res.name}' - nothing was written."]
        lines += [f"  - {n}" for n in res.notes]
        lines.append("Do NOT describe this report's contents; nothing was downloaded.")
        return "\n".join(lines)

    report_dir = res.path / f"{safe_name(res.name)}.Report" if res.path else None
    lines = [
        f"Fetched '{res.name}' into local storage.",
        f"  project:   {res.path}",
        f"  report:    {report_dir}  ({res.report_parts} part(s))",
        f"  model:     {res.model_parts} TMDL part(s)",
    ]
    if res.pbix:
        lines.append(f"  .pbix:     {res.pbix}")

    if res.legacy:
        lines.append(
            f"\nPBIR-Legacy format - this report is a single report.json with no "
            f"definition/pages/ tree. pbi_report_info and pbi_visual_list glob "
            f"for PBIR and WILL RETURN EMPTY on it; that is a tool limitation, "
            f"NOT evidence the report has no visuals.\n"
            f"USE pbi_report_inspect with report_path={str(report_dir)!r} - it "
            f"reads this format and returns the real pages, visuals, types and "
            f"field bindings. pbi_visual_bindings also falls back to it.\n"
            f"(Optional, for full PBIR: open the .pbip in Power BI Desktop with "
            f"the 'enhanced report format (PBIR)' preview on, save, re-fetch.)"
        )
    else:
        lines.append(
            f"\nPBIR format - ready to analyse. Pass report_path="
            f"{str(report_dir)!r} to pbi_report_info / pbi_visual_list / "
            f"pbi_visual_bindings / pbi_dashboard_audit."
        )

    if res.notes:
        lines.append("\nNotes:")
        lines += [f"  - {n}" for n in res.notes]
    return "\n".join(lines)
