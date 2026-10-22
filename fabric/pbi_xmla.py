import os
import re
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIB = os.path.join(ROOT, "lib")

def _adomd_search_dirs() -> list:
    dirs = [_LIB]
    try:
        import pbi_cli

        dirs.append(os.path.join(os.path.dirname(pbi_cli.__file__), "dlls"))
    except Exception:
        pass
    return dirs

def _load_adomd() -> str:
    try:
        import clr as _clr
    except Exception as e:
        return f"pythonnet (clr) unavailable: {e}"

    add_reference = getattr(_clr, "AddReference", None)
    if not callable(add_reference):
        return "pythonnet clr.AddReference unavailable"

    attempts = []
    for d in _adomd_search_dirs():
        if not os.path.isdir(d):
            attempts.append(f"{d} (no such directory)")
            continue
        if d not in sys.path:
            sys.path.insert(0, d)
        try:
            add_reference("Microsoft.AnalysisServices.AdomdClient")
            return ""
        except Exception as e:
            attempts.append(f"{d} ({type(e).__name__}: {e})")
    return "; ".join(attempts) or "no candidate directories"

_ADOMD_LOAD_ERROR = _load_adomd()

_SKIP_TABLE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_", "sys ")
_SKIP_TABLE_NAMES = {"sys dm_db_external_tables_log_status"}

# ── endpoint normalisation ───────────────────────────────────────────────
#
# ADOMD reads an https:// Data Source as a legacy HTTP XMLA pump: it POSTs SOAP
# and demands XML back. Hand it a portal URL and the Fabric web app answers
# with its HTML shell, which surfaces as the unhelpful "Unsupported data
# format : text/html". Everything a user could plausibly paste into the XMLA
# endpoint box is rewritten here into the powerbi:// form the engine speaks.

class XmlaEndpointError(ValueError):
    """The given value cannot address an XMLA endpoint."""

_XMLA_SCHEMES = (
    "powerbi://",
    "pbiazure://",
    "pbidedicated://",
    "asazure://",
    "link://",
)
_PBI_HOST_SUFFIXES = ("powerbi.com", "fabric.microsoft.com", "analysis.windows.net")
_GUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)
# A local/on-prem SSAS instance: localhost, HOST:port, or SERVER\INSTANCE.
_LOCAL_RE = re.compile(r"^(localhost\b|\.$|[\w.\-]+\\[\w.\-]+$|[\w.\-]+:\d+$)", re.IGNORECASE)
_WORKSPACE_NAMES: dict[str, str] = {}

def _xmla_base() -> str:
    from config import XMLA_ENDPOINT_BASE

    return XMLA_ENDPOINT_BASE.rstrip("/")

def _workspace_ref(ws: str) -> str:
    """Resolve a workspace GUID lifted from a portal URL to its name."""
    if not _GUID_RE.match(ws):
        return ws
    if ws.lower() not in _WORKSPACE_NAMES:
        try:
            from fabric.fabric_browse import list_workspaces

            for w in list_workspaces():
                if w.id:
                    _WORKSPACE_NAMES[w.id.lower()] = w.name
        except Exception:
            pass  # best effort — fall back to the GUID, which the engine may take
    return _WORKSPACE_NAMES.get(ws.lower(), ws)

def normalize_xmla_endpoint(server: str) -> str:
    """Return `server` as an XMLA endpoint, or raise XmlaEndpointError."""
    s = (server or "").strip().strip('"').strip("'").rstrip("/")
    if not s:
        return ""

    low = s.lower()
    if low.startswith(_XMLA_SCHEMES) or _LOCAL_RE.match(s):
        return s

    if low.startswith(("http://", "https://")):
        parts = urlsplit(s)
        host = (parts.hostname or "").lower()
        segs = [p for p in parts.path.split("/") if p]
        if not host.endswith(_PBI_HOST_SUFFIXES):
            raise XmlaEndpointError(
                f"'{server}' is not a Power BI or Fabric address. An XMLA endpoint "
                f"looks like {_xmla_base()}/<Workspace name>."
            )
        # Portal URL — https://app.fabric.microsoft.com/groups/<workspace-id>/…
        if segs and segs[0].lower() in ("groups", "workspaces"):
            if len(segs) < 2 or segs[1].lower() == "me":
                return _xmla_base()  # My workspace carries no name segment
            return f"{_xmla_base()}/{_workspace_ref(segs[1])}"
        # REST URL — https://api.powerbi.com/v1.0/myorg/<Workspace>
        if host.startswith("api.") and segs:
            return "powerbi://" + host + "/" + "/".join(segs)
        raise XmlaEndpointError(
            f"'{server}' is a Power BI web page, not an XMLA endpoint. Copy the "
            "endpoint from Workspace settings → License info → Connection link, or "
            f"just type the workspace name and FabricBI expands it to "
            f"{_xmla_base()}/<name>."
        )

    if "/v1.0/myorg" in low:  # endpoint pasted without its scheme
        return "powerbi://" + s

    return f"{_xmla_base()}/{s}"  # a bare workspace name

def explain_xmla_error(exc: BaseException, server: str = "") -> str:
    """Turn ADOMD's stack-shaped failures into something actionable."""
    msg = str(exc).strip()
    low = msg.lower()
    where = f"'{server}'" if server else "That endpoint"

    if "unsupported data format" in low and "text/html" in low:
        return (
            f"{where} answered with a web page instead of XMLA. That address points "
            "at the Power BI portal (or a sign-in page), not the analysis-services "
            f"endpoint. Use {_xmla_base()}/<Workspace name> — copy it from Workspace "
            "settings → License info → Connection link, or type the workspace name "
            "on its own and FabricBI will expand it."
        )
    if "xmla" in low and ("not enabled" in low or "disabled" in low):
        return (
            "The XMLA endpoint is turned off for this capacity. A Fabric or Premium "
            "capacity admin has to set XMLA Endpoint to Read (or Read Write) in the "
            f"capacity settings before {where} can be read.\n  Server said: {msg}"
        )
    if "cannot be found" in low or "was not found" in low or "cannot find" in low:
        return (
            "The workspace or model name did not resolve. Check that the model name "
            "matches the semantic model exactly, and that the workspace in the "
            f"endpoint is the one holding it.\n  Server said: {msg}"
        )
    if "unauthorized" in low or "401" in low or "does not have permission" in low:
        return (
            f"Authentication to {where} was refused. The signed-in user or service "
            "principal needs at least Viewer plus Build permission on the model, and "
            "the tenant must allow service principals to use XMLA endpoints."
            f"\n  Server said: {msg}"
        )
    return msg

def _build_conn_str(server, database, conn, roles="", effective_user=""):
    server = normalize_xmla_endpoint(server)
    s = f"Provider=MSOLAP;Data Source={server};"
    if database:
        s += f"Initial Catalog={database};"
    if roles:
        s += f"Roles={roles};"
    if effective_user:
        s += f"EffectiveUserName={effective_user};"
    if server.lower().startswith("powerbi://") and conn.has_sp:
        s += f"User ID=app:{conn.client_id}@{conn.tenant_id};Password={conn.client_secret};"
    return s

def _new_conn(conn_str: str):
    if _ADOMD_LOAD_ERROR:
        raise RuntimeError(
            "ADOMD.NET client library could not be loaded, so XMLA is "
            "unavailable (this is not a credential, workspace or model-name "
            "problem).\n"
            f"  Tried: {_ADOMD_LOAD_ERROR}\n"
            "  Fix: copy Microsoft.AnalysisServices.*.dll into "
            f"{_LIB}, or install the Analysis Services client libraries."
        )

    from pyadomd import Pyadomd

    return Pyadomd(conn_str)

def _close_quietly(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass

class _PooledConn:

    def __init__(self, key: tuple, conn, opened_at: float):
        self._key = key
        self._conn = conn
        self._opened_at = opened_at

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        with _POOL_LOCK:
            _IN_USE.discard(self._key)
            if exc_type is None:
                _POOL[self._key] = (self._conn, self._opened_at)
                return False
        _close_quietly(self._conn)
        return False

_POOL: dict[tuple, tuple] = {}
_IN_USE: set = set()
_POOL_LOCK = threading.Lock()

def _conn(server: str, database: str, *, roles: str = "", effective_user: str = ""):
    from config import XMLA_CONN_TTL_S
    from fabric.fabric_connection import get_fabric_connection

    conn_str = _build_conn_str(
        server, database, get_fabric_connection(), roles, effective_user
    )
    if XMLA_CONN_TTL_S <= 0:
        return _new_conn(conn_str)

    key = (conn_str,)
    now = time.time()
    stale = None
    with _POOL_LOCK:
        pooled = _POOL.pop(key, None)
        if pooled and key not in _IN_USE:
            if now - pooled[1] < XMLA_CONN_TTL_S:
                _IN_USE.add(key)
                return _PooledConn(key, pooled[0], pooled[1])
            stale = pooled[0]
        busy = key in _IN_USE
        if not busy:
            _IN_USE.add(key)
    if stale is not None:
        _close_quietly(stale)
    if busy:
        return _new_conn(conn_str)  # busy in another thread — one-shot
    raw = _new_conn(conn_str)
    opened = raw.__enter__()  # open once; wrapper hands it out from then on
    return _PooledConn(key, opened, time.time())

def _idx(description, *names: str) -> list[int]:
    col_names = [d[0] for d in description]
    return [col_names.index(n) for n in names]

def fetch_model_metadata(server: str, database: str) -> dict:
    endpoint = normalize_xmla_endpoint(server)
    try:
        return _read_model_metadata(endpoint, database)
    except XmlaEndpointError:
        raise
    except Exception as exc:
        raise RuntimeError(explain_xmla_error(exc, endpoint)) from exc

def _opt(description, *names: str) -> dict[str, int]:
    """Map the DMV columns we want to their indexes, tolerating absent ones.

    TMSCHEMA column sets vary by engine version, and a missing column must
    degrade one rule rather than fail the whole read.
    """
    col_names = [d[0] for d in description]
    return {n: col_names.index(n) for n in names if n in col_names}

def _pick(row, idx: dict, name: str, default=None):
    i = idx.get(name)
    return row[i] if i is not None else default

def fetch_bpa_metadata(server: str, database: str) -> dict:
    endpoint = normalize_xmla_endpoint(server)
    try:
        return _read_bpa_metadata(endpoint, database)
    except XmlaEndpointError:
        raise
    except Exception as exc:
        raise RuntimeError(explain_xmla_error(exc, endpoint)) from exc

def relationship_label(r: dict, arrow: str = "→") -> str:
    return (
        f"{r.get('from_table', '?')}[{r.get('from_column', '?')}] {arrow} "
        f"{r.get('to_table', '?')}[{r.get('to_column', '?')}]"
    )


def _read_relationships(conn, tables_by_id: dict, columns_by_id: dict) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM $SYSTEM.TMSCHEMA_RELATIONSHIPS")
    i = _opt(
        cur.description,
        "FromTableID",
        "ToTableID",
        "FromColumnID",
        "ToColumnID",
        "IsActive",
        "CrossFilteringBehavior",
        "FromCardinality",
        "ToCardinality",
    )
    out = [
        {
            "from_table": tables_by_id.get(_pick(row, i, "FromTableID"), "?"),
            "from_column": columns_by_id.get(_pick(row, i, "FromColumnID"), "?"),
            "to_table": tables_by_id.get(_pick(row, i, "ToTableID"), "?"),
            "to_column": columns_by_id.get(_pick(row, i, "ToColumnID"), "?"),
            "is_active": bool(_pick(row, i, "IsActive", True)),
            "cross_filter": str(_pick(row, i, "CrossFilteringBehavior") or ""),
            "from_cardinality": str(_pick(row, i, "FromCardinality") or ""),
            "to_cardinality": str(_pick(row, i, "ToCardinality") or ""),
        }
        for row in cur.fetchall()
    ]
    cur.close()
    return out


def _read_bpa_metadata(server: str, database: str) -> dict:
    tables_by_id: dict = {}
    tables: list[dict] = []
    measures: list[dict] = []
    columns: list[dict] = []
    relationships: list[dict] = []
    col_id_to: dict = {}

    with _conn(server, database) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM $SYSTEM.TMSCHEMA_TABLES")
        i = _opt(cur.description, "ID", "Name", "IsHidden", "IsPrivate", "Description")
        for row in cur.fetchall():
            name = _pick(row, i, "Name") or ""
            tables_by_id[_pick(row, i, "ID")] = name
            if any(name.startswith(p) for p in _SKIP_TABLE_PREFIXES):
                continue
            if name in _SKIP_TABLE_NAMES:
                continue
            tables.append(
                {
                    "name": name,
                    "is_hidden": bool(
                        _pick(row, i, "IsHidden") or _pick(row, i, "IsPrivate")
                    ),
                    "description": _pick(row, i, "Description") or "",
                }
            )
        cur.close()

        cur = conn.cursor()
        cur.execute("SELECT * FROM $SYSTEM.TMSCHEMA_MEASURES")
        i = _opt(
            cur.description,
            "TableID",
            "Name",
            "Expression",
            "FormatString",
            "DisplayFolder",
            "IsHidden",
            "Description",
        )
        for row in cur.fetchall():
            name = _pick(row, i, "Name")
            if not name:
                continue
            tname = tables_by_id.get(_pick(row, i, "TableID"), "")
            if any(tname.startswith(p) for p in _SKIP_TABLE_PREFIXES):
                continue
            measures.append(
                {
                    "table": tname,
                    "name": name,
                    "expression": _pick(row, i, "Expression") or "",
                    "format_string": _pick(row, i, "FormatString") or "",
                    "display_folder": _pick(row, i, "DisplayFolder") or "",
                    "is_hidden": bool(_pick(row, i, "IsHidden")),
                    "description": _pick(row, i, "Description") or "",
                }
            )
        cur.close()

        cur = conn.cursor()
        cur.execute("SELECT * FROM $SYSTEM.TMSCHEMA_COLUMNS")
        i = _opt(
            cur.description,
            "ID",
            "TableID",
            "ExplicitName",
            "ExplicitDataType",
            "IsHidden",
            "IsKey",
            "SummarizeBy",
            "Description",
        )
        for row in cur.fetchall():
            name = _pick(row, i, "ExplicitName")
            if not name:
                continue
            col_id_to[_pick(row, i, "ID")] = name
            tname = tables_by_id.get(_pick(row, i, "TableID"), "")
            if any(tname.startswith(p) for p in _SKIP_TABLE_PREFIXES):
                continue
            # Hidden columns are KEPT here, unlike fetch_model_metadata: several
            # rules are specifically about hidden-vs-visible.
            columns.append(
                {
                    "table": tname,
                    "name": name,
                    "data_type": str(_pick(row, i, "ExplicitDataType") or ""),
                    "is_hidden": bool(_pick(row, i, "IsHidden")),
                    "is_key": bool(_pick(row, i, "IsKey")),
                    "summarize_by": str(_pick(row, i, "SummarizeBy") or ""),
                    "description": _pick(row, i, "Description") or "",
                }
            )
        cur.close()

        relationships = _read_relationships(conn, tables_by_id, col_id_to)

    return {
        "tables": tables,
        "measures": measures,
        "columns": columns,
        "relationships": relationships,
    }

def _read_model_metadata(server: str, database: str) -> dict:
    tables_map: dict = {}
    visible: dict[str, bool] = {}
    measures_by_table: dict[str, list[str]] = {}
    measure_errors: list[str] = []
    columns_by_table: dict[str, list[tuple[str, object]]] = {}
    col_id_to_name: dict[object, str] = {}
    relationships: list[dict] = []
    row_counts: dict[str, object] = {}

    with _conn(server, database) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM $SYSTEM.TMSCHEMA_TABLES")
        id_i, nm_i, hid_i, priv_i = _idx(
            cur.description, "ID", "Name", "IsHidden", "IsPrivate"
        )
        for row in cur.fetchall():
            tid, name = row[id_i], row[nm_i]
            tables_map[tid] = name
            if row[hid_i] or row[priv_i]:
                continue
            if any(name.startswith(p) for p in _SKIP_TABLE_PREFIXES):
                continue
            if name in _SKIP_TABLE_NAMES:
                continue
            visible[name] = True
        cur.close()

        cur = conn.cursor()
        cur.execute("SELECT * FROM $SYSTEM.TMSCHEMA_MEASURES")
        names = [d[0] for d in cur.description]
        tid_i, nm_i = names.index("TableID"), names.index("Name")
        err_i = names.index("ErrorMessage") if "ErrorMessage" in names else None
        for row in cur.fetchall():
            mname = row[nm_i]
            if not mname:
                continue
            tname = tables_map.get(row[tid_i], f"table_{row[tid_i]}")
            if any(tname.startswith(p) for p in _SKIP_TABLE_PREFIXES):
                continue
            if tname not in visible:
                # Table itself was filtered as hidden/private (see the
                # TMSCHEMA_TABLES loop above) — a measure defined on it
                # shouldn't appear here either, or it reads as a measure on
                # a table that "doesn't exist" per the TABLES section.
                continue
            measures_by_table.setdefault(tname, []).append(mname)
            if err_i is not None and row[err_i]:
                measure_errors.append(f"{tname}[{mname}]: {row[err_i]}")
        cur.close()

        cur = conn.cursor()
        cur.execute("SELECT * FROM $SYSTEM.TMSCHEMA_COLUMNS")
        cid_i, tid_i, nm_i, dt_i, hid_i = _idx(
            cur.description,
            "ID",
            "TableID",
            "ExplicitName",
            "ExplicitDataType",
            "IsHidden",
        )
        for row in cur.fetchall():
            col = row[nm_i]
            if col:
                col_id_to_name[row[cid_i]] = col
            tname = tables_map.get(row[tid_i], "")
            if tname not in visible or row[hid_i] or not col:
                continue
            columns_by_table.setdefault(tname, []).append((col, row[dt_i]))
        cur.close()

        relationships = _read_relationships(conn, tables_map, col_id_to_name)

        def _count(tables: list[str]) -> dict:
            parts = ", ".join(
                f'ROW("T", "{t}", "N", COUNTROWS(\'{t}\'))' for t in tables
            )
            dax = f"UNION({parts})" if len(tables) > 1 else parts
            cur = conn.cursor()
            cur.execute(f"EVALUATE {dax}")
            out = {str(r[0]): r[1] for r in cur.fetchall() if r[0] is not None}
            cur.close()
            return out

        safe = [t for t in visible if "'" not in t]
        if safe:
            try:
                row_counts = _count(safe)
            except Exception:
                failed: list[str] = []
                for t in safe:
                    try:
                        row_counts.update(_count([t]))
                    except Exception:
                        failed.append(t)
                if failed:
                    print(
                        f"   [pbi_xmla] row count unavailable for {len(failed)} "
                        f"table(s): {', '.join(failed[:10])}"
                    )

    return {
        "tables": list(visible.keys()),
        "row_counts": row_counts,
        "measures_by_table": measures_by_table,
        "measure_errors": measure_errors,
        "columns_by_table": columns_by_table,
        "relationships": relationships,
    }

def run_dax(server: str, database: str, dax: str) -> str:
    if not server or not database:
        return "Error: not connected to any model."
    try:
        with _conn(server, database) as conn:
            cur = conn.cursor()
            cur.execute(dax)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(200)
            cur.close()

        if not rows:
            return "(no rows returned)"

        col_widths = [
            max(len(str(c)), max((len(str(r[i])) for r in rows), default=0))
            for i, c in enumerate(cols)
        ]
        sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"

        def fmt(r):
            return (
                "|"
                + "|".join(f" {v!s:<{col_widths[i]}} " for i, v in enumerate(r))
                + "|"
            )

        lines = [sep, fmt(cols), sep] + [fmt(r) for r in rows] + [sep]
        if len(rows) == 200:
            lines.append("(capped at 200 rows)")
        return "\n".join(lines)

    except Exception as exc:
        return f"DAX error: {explain_xmla_error(exc, server)}"

def dax_result_columns(server: str, database: str, dax: str) -> tuple[list[str], str]:
    if not server or not database:
        return [], "not connected to any model"
    try:
        with _conn(server, database) as conn:
            cur = conn.cursor()
            cur.execute(dax)
            cols = [d[0] for d in (cur.description or [])]
            cur.fetchmany(1)
            cur.close()
        return cols, ""
    except Exception as e:  # the provider's message names the bad table/column
        return [], f"{type(e).__name__}: {e}"
