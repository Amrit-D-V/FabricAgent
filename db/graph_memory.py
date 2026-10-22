from __future__ import annotations

import logging
import threading
import time

from config import (
    GRAPH_MEMORY_ENABLED,
    GRAPH_MEMORY_NOTES_PER_PROJECT,
    GRAPH_MEMORY_RECALL_LIMIT,
    GRAPH_MEMORY_RETRY_AFTER_S,
    GRAPH_MEMORY_SOURCE_COLUMNS,
    GRAPH_MEMORY_SOURCE_TABLES,
    GRAPH_MEMORY_TIMEOUT_S,
    NEO4J_DATABASE,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USERNAME,
)

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_DRIVER = None
_DEAD_UNTIL = 0.0

_CONSTRAINTS = (
    "CREATE CONSTRAINT project_id IF NOT EXISTS FOR (p:Project) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT client_name IF NOT EXISTS FOR (c:Client) REQUIRE c.name IS UNIQUE",
    "CREATE CONSTRAINT model_name IF NOT EXISTS FOR (m:Model) REQUIRE m.name IS UNIQUE",
    "CREATE CONSTRAINT tool_name IF NOT EXISTS FOR (t:Tool) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT measure_key IF NOT EXISTS FOR (m:Measure) REQUIRE m.key IS UNIQUE",
    "CREATE CONSTRAINT user_email IF NOT EXISTS FOR (u:User) REQUIRE u.email IS UNIQUE",
    "CREATE CONSTRAINT source_key IF NOT EXISTS FOR (s:Source) REQUIRE s.key IS UNIQUE",
    "CREATE CONSTRAINT table_key IF NOT EXISTS FOR (t:Table) REQUIRE t.key IS UNIQUE",
    "CREATE CONSTRAINT column_key IF NOT EXISTS FOR (c:Column) REQUIRE c.key IS UNIQUE",
    "CREATE CONSTRAINT kpi_key IF NOT EXISTS FOR (k:Kpi) REQUIRE k.key IS UNIQUE",
    "CREATE CONSTRAINT req_key IF NOT EXISTS FOR (r:Requirement) REQUIRE r.key IS UNIQUE",
)


def enabled() -> bool:
    return bool(GRAPH_MEMORY_ENABLED and NEO4J_URI and NEO4J_PASSWORD)


def _driver():
    global _DRIVER, _DEAD_UNTIL
    if not enabled() or time.time() < _DEAD_UNTIL:
        return None
    with _LOCK:
        if _DRIVER is not None:
            return _DRIVER
        # Re-check under the lock: without this, every thread that passed the
        # check above pays the full connection timeout and logs its own
        # failure, instead of one thread failing and the rest short-circuiting.
        if time.time() < _DEAD_UNTIL:
            return None
        try:
            from neo4j import GraphDatabase

            drv = GraphDatabase.driver(
                NEO4J_URI,
                auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
                connection_timeout=GRAPH_MEMORY_TIMEOUT_S,
                max_transaction_retry_time=GRAPH_MEMORY_TIMEOUT_S,
            )
            drv.verify_connectivity()
            for stmt in _CONSTRAINTS:
                drv.execute_query(stmt, database_=NEO4J_DATABASE)
            _DRIVER = drv
            logger.info("[graph_memory] connected to %s", NEO4J_URI)
            return _DRIVER
        except Exception as e:
            _DEAD_UNTIL = time.time() + GRAPH_MEMORY_RETRY_AFTER_S
            logger.warning(
                "[graph_memory] unavailable (%s: %s) — memory disabled for %ss",
                type(e).__name__,
                str(e)[:160],
                GRAPH_MEMORY_RETRY_AFTER_S,
            )
            return None


def _run(cypher: str, **params) -> list:
    drv = _driver()
    if drv is None:
        return []
    try:
        records, _, _ = drv.execute_query(cypher, database_=NEO4J_DATABASE, **params)
        return list(records)
    except Exception as e:
        logger.warning(
            "[graph_memory] query failed: %s: %s", type(e).__name__, str(e)[:200]
        )
        return []


def close() -> None:
    global _DRIVER
    with _LOCK:
        if _DRIVER is not None:
            try:
                _DRIVER.close()
            except Exception:
                pass
            _DRIVER = None


_REMEMBER_TURN = """
MERGE (p:Project {id: $pid})
  ON CREATE SET p.created = datetime()
SET p.name = $pname, p.last_seen = datetime()
FOREACH (_ IN CASE WHEN $client <> '' THEN [1] ELSE [] END |
  MERGE (c:Client {name: $client})
  MERGE (c)-[:OWNS]->(p))
FOREACH (m IN $models |
  MERGE (mo:Model {name: m})
  MERGE (p)-[:USES]->(mo))
FOREACH (t IN $tools |
  MERGE (tl:Tool {name: t})
  MERGE (p)-[:USED_TOOL]->(tl))
FOREACH (_ IN CASE WHEN $user <> '' THEN [1] ELSE [] END |
  MERGE (u:User {email: $user})
  MERGE (u)-[:WORKED_ON]->(p))
FOREACH (_ IN CASE WHEN $note <> '' THEN [1] ELSE [] END |
  CREATE (p)-[:NOTED]->(:Note {text: $note, ts: datetime()}))
"""

_TRIM_NOTES = """
MATCH (:Project {id: $pid})-[:NOTED]->(n:Note)
WITH n ORDER BY n.ts DESC SKIP $keep
DETACH DELETE n
"""


def remember_turn(
    project_id: int,
    project_name: str = "",
    client_name: str = "",
    models: list[str] | None = None,
    tools: list[str] | None = None,
    note: str = "",
    user_email: str = "",
) -> None:
    if not enabled() or not project_id:
        return
    _run(
        _REMEMBER_TURN,
        pid=int(project_id),
        pname=(project_name or f"project {project_id}").strip(),
        client=(client_name or "").strip(),
        models=[m for m in (models or []) if m],
        tools=[t for t in (tools or []) if t],
        note=(note or "").strip(),
        user=(user_email or "").strip().lower(),
    )
    if note:
        _run(_TRIM_NOTES, pid=int(project_id), keep=GRAPH_MEMORY_NOTES_PER_PROJECT)


_REMEMBER_MEASURE = """
MERGE (mo:Model {name: $model})
MERGE (ms:Measure {key: $key})
  ON CREATE SET ms.created = datetime()
SET ms.name = $name, ms.table = $table, ms.dax = $dax, ms.model = $model,
    ms.reason = $reason
MERGE (ms)-[:IN_MODEL]->(mo)
FOREACH (_ IN CASE WHEN $pid > 0 THEN [1] ELSE [] END |
  MERGE (p:Project {id: $pid})
  MERGE (p)-[:CREATED]->(ms)
  MERGE (p)-[:USES]->(mo))
"""


def remember_measure(
    project_id: int,
    model: str,
    name: str,
    table: str = "",
    dax: str = "",
    reason: str = "",
) -> None:
    if not enabled() or not name or not model:
        return
    _run(
        _REMEMBER_MEASURE,
        pid=int(project_id or 0),
        key=f"{model}::{name}",
        model=model,
        name=name,
        table=table or "",
        dax=(dax or "")[:4000],
        reason=(reason or "")[:500],
    )


_RECALL_MEASURES = """
MATCH (ms:Measure)
WHERE NOT (:Project {id: $pid})-[:CREATED]->(ms)
RETURN ms.model AS model, ms.table AS table, ms.name AS name
// A measure with no expression on file can't be reused — show it last.
ORDER BY ms.dax = '', ms.created DESC LIMIT $limit
"""

_RECALL_SIBLINGS = """
MATCH (other:Project)-[:USES]->(mo:Model)
WHERE other.id <> $pid
OPTIONAL MATCH (c:Client)-[:OWNS]->(other)
WITH other, c, collect(DISTINCT mo.name) AS models
RETURN other.name AS project, c.name AS client, models
ORDER BY other.last_seen DESC LIMIT $limit
"""


def recall(project_id: int, limit: int = 0) -> str:
    if not enabled():
        return ""
    limit = limit or GRAPH_MEMORY_RECALL_LIMIT
    pid = int(project_id or 0)
    lines: list[str] = []

    sibs = _run(_RECALL_SIBLINGS, pid=pid, limit=limit)
    if sibs:
        lines.append("Other projects in this org:")
        for r in sibs:
            models = ", ".join(r["models"][:4]) or "no model yet"
            owner = f" [{r['client']}]" if r.get("client") else ""
            lines.append(f"  - {r['project']}{owner} -> {models}")

    measures = _run(_RECALL_MEASURES, pid=pid, limit=limit)
    if measures:
        lines.append("Measures already built elsewhere (reuse the pattern, and the")
        lines.append("name, unless this project's spec says otherwise):")
        for r in measures:
            tbl = f"{r['table']}[{r['name']}]" if r.get("table") else r["name"]
            lines.append(f"  - {tbl}  (model: {r['model']})")

    return "\n".join(lines)


_REMEMBER_SOURCE = """
MERGE (s:Source {key: $key})
  ON CREATE SET s.created = datetime()
SET s.server = $server, s.database = $database, s.kind = $kind,
    s.last_seen = datetime()
MERGE (p:Project {id: $pid})
SET p.name = coalesce($pname, p.name), p.last_seen = datetime()
MERGE (p)-[:READS_FROM]->(s)
FOREACH (t IN $tables |
  MERGE (tb:Table {key: $key + '::' + t.name})
    ON CREATE SET tb.created = datetime()
  SET tb.name = t.name, tb.source = $key
  MERGE (s)-[:HAS_TABLE]->(tb)
  FOREACH (c IN t.columns |
    MERGE (co:Column {key: $key + '::' + t.name + '::' + c})
    SET co.name = c, co.table = t.name
    MERGE (tb)-[:HAS_COLUMN]->(co)))
"""


def remember_source(
    project_id: int,
    kind: str,
    server: str,
    database: str,
    tables: list[dict] | None = None,
    project_name: str = "",
) -> None:
    if not enabled() or not project_id or not server or not database:
        return
    payload = []
    for t in (tables or [])[:GRAPH_MEMORY_SOURCE_TABLES]:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        cols = [c for c in (t.get("columns") or []) if c][:GRAPH_MEMORY_SOURCE_COLUMNS]
        payload.append({"name": name, "columns": cols})
    _run(
        _REMEMBER_SOURCE,
        pid=int(project_id),
        pname=(project_name or "").strip() or None,
        key=f"{server}/{database}".lower(),
        server=server,
        database=database,
        kind=kind,
        tables=payload,
    )


_RECALL_SOURCES = """
MATCH (:Project {id: $pid})-[:READS_FROM]->(s:Source)<-[:READS_FROM]-(other:Project)
WHERE other.id <> $pid
RETURN s.database AS database, s.kind AS kind,
       collect(DISTINCT other.name) AS projects
LIMIT $limit
"""

_RECALL_SHARED_TABLES = """
MATCH (:Project {id: $pid})-[:READS_FROM]->(:Source)-[:HAS_TABLE]->(tb:Table)
MATCH (other:Project)-[:READS_FROM]->(:Source)-[:HAS_TABLE]->(tb2:Table)
WHERE other.id <> $pid AND tb2.name = tb.name
RETURN tb.name AS table, collect(DISTINCT other.name) AS projects
ORDER BY size(projects) DESC LIMIT $limit
"""


def recall_sources(project_id: int, limit: int = 0) -> str:
    if not enabled():
        return ""
    limit = limit or GRAPH_MEMORY_RECALL_LIMIT
    pid = int(project_id or 0)
    lines: list[str] = []

    for r in _run(_RECALL_SOURCES, pid=pid, limit=limit):
        who = ", ".join(p for p in r["projects"] if p)
        lines.append(f"  - {r['database']} ({r['kind']}) is also used by: {who}")
    if lines:
        lines.insert(0, "This project's data sources are shared with:")

    shared = _run(_RECALL_SHARED_TABLES, pid=pid, limit=limit)
    if shared:
        lines.append("Tables this org has modelled before (reuse grain and naming):")
        for r in shared:
            who = ", ".join(p for p in r["projects"] if p)
            lines.append(f"  - {r['table']}  (in {who})")

    return "\n".join(lines)


# Requirements are scoped to their project: every node hangs off (:Project {id})
# and its key is prefixed with the project id, so two projects can each have a
# "Revenue" KPI without colliding. Cross-project reuse queries match on .name.
_CLEAR_REQUIREMENTS = """
MATCH (:Project {id: $pid})-[:REQUIRES]->(r)
DETACH DELETE r
"""

_REMEMBER_REQUIREMENTS = """
MERGE (p:Project {id: $pid})
SET p.name = coalesce($pname, p.name),
    p.objective = $objective,
    p.business_understanding = $business,
    p.audience = $audience,
    p.requirements_updated = datetime(),
    p.last_seen = datetime()
FOREACH (k IN $kpis |
  MERGE (n:Kpi {key: toString($pid) + '::' + k.name})
  SET n.name = k.name, n.definition = k.definition, n.backing = k.backing,
      n.project_id = $pid
  MERGE (p)-[:REQUIRES]->(n))
FOREACH (r IN $items |
  MERGE (n:Requirement {key: toString($pid) + '::' + r.kind + '::' + r.text})
  SET n.text = r.text, n.kind = r.kind, n.project_id = $pid
  MERGE (p)-[:REQUIRES]->(n))
"""


def remember_requirements(
    project_id: int,
    project_name: str = "",
    objective: str = "",
    business: str = "",
    audience: str = "",
    kpis: list[dict] | None = None,
    items: list[dict] | None = None,
) -> None:
    if not enabled() or not project_id:
        return
    pid = int(project_id)
    # Replace rather than accumulate: a re-extraction supersedes the old spec.
    _run(_CLEAR_REQUIREMENTS, pid=pid)
    clean_kpis = [
        {
            "name": (k.get("name") or "").strip()[:200],
            "definition": (k.get("definition") or "").strip()[:500],
            "backing": (k.get("backing") or "").strip()[:200],
        }
        for k in (kpis or [])
        if (k.get("name") or "").strip()
    ]
    clean_items = [
        {
            "kind": (i.get("kind") or "item").strip()[:40],
            "text": (i.get("text") or "").strip()[:400],
        }
        for i in (items or [])
        if (i.get("text") or "").strip()
    ]
    _run(
        _REMEMBER_REQUIREMENTS,
        pid=pid,
        pname=(project_name or "").strip() or None,
        objective=(objective or "").strip()[:2000],
        business=(business or "").strip()[:4000],
        audience=(audience or "").strip()[:1000],
        kpis=clean_kpis,
        items=clean_items,
    )


_RECALL_OWN_REQUIREMENTS = """
MATCH (p:Project {id: $pid})
OPTIONAL MATCH (p)-[:REQUIRES]->(k:Kpi)
WITH p, collect(DISTINCT {name: k.name, backing: k.backing}) AS kpis
OPTIONAL MATCH (p)-[:REQUIRES]->(r:Requirement)
RETURN p.objective AS objective, p.business_understanding AS business,
       p.audience AS audience, kpis,
       collect(DISTINCT {kind: r.kind, text: r.text}) AS items
"""


def recall_project_requirements(project_id: int) -> str:
    if not enabled() or not project_id:
        return ""
    rows = _run(_RECALL_OWN_REQUIREMENTS, pid=int(project_id))
    if not rows:
        return ""
    r = rows[0]
    lines: list[str] = []
    if r.get("objective"):
        lines.append(f"Objective: {r['objective']}")
    if r.get("audience"):
        lines.append(f"Audience: {r['audience']}")
    if r.get("business"):
        lines.append(f"Business understanding: {r['business']}")
    kpis = [k for k in (r.get("kpis") or []) if k.get("name")]
    if kpis:
        lines.append("KPIs this project requires:")
        for k in kpis:
            backing = f"  <- {k['backing']}" if k.get("backing") else ""
            lines.append(f"  - {k['name']}{backing}")
    by_kind: dict[str, list[str]] = {}
    for i in r.get("items") or []:
        if i.get("text"):
            by_kind.setdefault(i.get("kind") or "item", []).append(i["text"])
    for kind, texts in by_kind.items():
        lines.append(f"{kind}:")
        for t in texts:
            lines.append(f"  - {t}")
    return "\n".join(lines)


_RECALL_OTHER_KPIS = """
MATCH (other:Project)-[:REQUIRES]->(k:Kpi)
WHERE other.id <> $pid
RETURN k.name AS name, collect(DISTINCT other.name) AS projects,
       head(collect(k.backing)) AS backing
ORDER BY size(projects) DESC LIMIT $limit
"""


def recall_kpis(project_id: int, limit: int = 0) -> str:
    if not enabled():
        return ""
    rows = _run(
        _RECALL_OTHER_KPIS,
        pid=int(project_id or 0),
        limit=limit or GRAPH_MEMORY_RECALL_LIMIT,
    )
    if not rows:
        return ""
    lines = ["KPIs specified on other projects (reuse the name where it fits):"]
    for r in rows:
        who = ", ".join(p for p in r["projects"] if p)
        backing = f"  <- {r['backing']}" if r.get("backing") else ""
        lines.append(f"  - {r['name']}{backing}  (in {who})")
    return "\n".join(lines)


_SEARCH = """
MATCH (ms:Measure)
WHERE toLower(ms.name) CONTAINS $t OR toLower(ms.dax) CONTAINS $t
OPTIONAL MATCH (p:Project)-[:CREATED]->(ms)
RETURN 'measure' AS kind, ms.name AS title,
       ms.model + ' | ' + coalesce(ms.table, '') AS ctx,
       ms.dax AS body, collect(DISTINCT p.name) AS projects
LIMIT $limit
UNION
MATCH (p:Project)-[:NOTED]->(n:Note)
WHERE toLower(n.text) CONTAINS $t
RETURN 'note' AS kind, p.name AS title, toString(n.ts) AS ctx,
       n.text AS body, [] AS projects
LIMIT $limit
"""


def search(term: str, limit: int = 0) -> str:
    term = (term or "").strip()
    if not term:
        return "Give a search term (a measure name, KPI, table, or keyword)."
    if not enabled():
        return "Org memory is not configured (no NEO4J_URI) — nothing to search."
    rows = _run(_SEARCH, t=term.lower(), limit=limit or GRAPH_MEMORY_RECALL_LIMIT)
    if not rows:
        return f"No org memory matches '{term}'."
    out = [
        f"Org memory matches for '{term}':",
        "(historical: verify against live metadata before relying on it)",
    ]
    for r in rows:
        if r["kind"] == "measure":
            who = ", ".join(p for p in r["projects"] if p)
            out.append(
                f"\n[measure] {r['title']}  ({r['ctx']})"
                + (f" — built for {who}" if who else "")
            )
            if r.get("body"):
                out.append((r["body"] or "").strip()[:800])
        else:
            out.append(f"\n[note] {r['title']} @ {r['ctx'][:16]}: {r['body']}")
    return "\n".join(out)


def stats() -> str:
    if not enabled():
        return "Org memory: disabled (set NEO4J_URI/NEO4J_PASSWORD in .env)."
    if _driver() is None:
        return f"Org memory: UNREACHABLE ({NEO4J_URI}) — is the Aura instance paused?"
    rows = _run("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c ORDER BY c DESC")
    if not rows:
        return f"Org memory: connected but empty ({NEO4J_URI})."
    parts = ", ".join(f"{r['c']} {r['label']}" for r in rows if r["label"])
    return f"Org memory ({NEO4J_URI}): {parts}"
