"""Tenant-wide lineage graph, sourced from the Power BI Scanner API.

`fabric_browse` lists what exists in one workspace. Nothing could answer "if I
drop this measure, what breaks?", because that needs the *edges*, not the
nodes: which report binds to which dataset, which dashboard tile binds to
which dataset, which dataset chains off another in a composite model.

The Scanner API is the only tenant-wide source for those edges. It is an async
three-call dance (getInfo -> scanStatus -> scanResult) over batches of 100
workspaces, so a large tenant takes minutes, not seconds -- results are cached
on disk and reused until stale.

Prerequisites the *tenant admin* must grant. This module cannot grant them and
reports a plain-language error when they are missing:
  * The app registration needs `Tenant.Read.All` with admin consent.
  * For service-principal auth, the Fabric admin portal setting "Service
    principals can access read-only admin APIs" must be on, with the SP in the
    allowed security group.

Documented per-tenant throttles: 30 modified-workspace calls/hour, 500 getInfo
calls/hour (16 concurrent scans), 500 scanResult calls/hour. The cache exists
as much to respect those as for speed.

Read-only: every call here is a GET or a scan POST. Nothing mutates the tenant.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import requests

from auth.token_manager import get_token_cached
from config import (
    LINEAGE_CACHE_TTL_S,
    LINEAGE_EXCLUDE_PERSONAL,
    LINEAGE_SCAN_TIMEOUT_S,
)

logger = logging.getLogger(__name__)

_ADMIN_BASE = "https://api.powerbi.com/v1.0/myorg/admin"
_TIMEOUT = 60
_BATCH = 100
_CACHE_FILE = Path.home() / ".pbi_agent" / "lineage_cache.json"

DATASET = "Dataset"
REPORT = "Report"
DASHBOARD = "Dashboard"
DATAFLOW = "Dataflow"
DATAMART = "Datamart"

_last_error: str = ""


def get_last_error() -> str:
    return _last_error


def _set_error(msg: str) -> None:
    global _last_error
    _last_error = msg
    logger.warning(msg)


def _clear_error() -> None:
    global _last_error
    _last_error = ""


@dataclass
class LineageNode:
    """One artifact in the tenant. Field inventory is populated for datasets."""

    id: str
    name: str
    kind: str
    workspace_id: str = ""
    workspace_name: str = ""
    binds_to: str = ""
    columns: dict[str, str] = field(default_factory=dict)
    measures: dict[str, str] = field(default_factory=dict)
    tables: list[str] = field(default_factory=list)
    tiles: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.workspace_name:
            return f"{self.workspace_name} > {self.name}"
        return self.name

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
            "binds_to": self.binds_to,
            "columns": self.columns,
            "measures": self.measures,
            "tables": self.tables,
            "tiles": self.tiles,
        }

    @classmethod
    def from_dict(cls, d: dict) -> LineageNode:
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            kind=d.get("kind", ""),
            workspace_id=d.get("workspace_id", ""),
            workspace_name=d.get("workspace_name", ""),
            binds_to=d.get("binds_to", ""),
            columns=d.get("columns", {}) or {},
            measures=d.get("measures", {}) or {},
            tables=d.get("tables", []) or [],
            tiles=d.get("tiles", []) or [],
        )


@dataclass
class LineageGraph:
    """Nodes plus directed upstream -> downstream edges.

    `gaps` is load-bearing: it records every workspace or artifact the scan
    could not read. An impact alert that silently omits an unreadable
    workspace reads as "nothing else is affected", which is precisely the
    failure mode this feature exists to prevent.
    """

    nodes: dict[str, LineageNode] = field(default_factory=dict)
    edges: dict[str, set[str]] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    scanned_at: float = 0.0
    workspace_count: int = 0

    def add_edge(self, upstream: str, downstream: str) -> None:
        if not upstream or not downstream or upstream == downstream:
            return
        self.edges.setdefault(upstream, set()).add(downstream)

    def downstream_of(
        self, node_id: str, max_depth: int = 6
    ) -> list[tuple[LineageNode, int]]:
        """Breadth-first transitive closure, nearest first.

        Depth matters downstream of a composite model: a report on a chained
        dataset is genuinely affected, but it is one hop further from the
        change and is worth showing as such.
        """
        seen = {node_id}
        out: list[tuple[LineageNode, int]] = []
        frontier = [node_id]
        depth = 0
        while frontier and depth < max_depth:
            depth += 1
            nxt: list[str] = []
            for nid in frontier:
                for child in sorted(self.edges.get(nid, ())):
                    if child in seen:
                        continue
                    seen.add(child)
                    node = self.nodes.get(child)
                    if node:
                        out.append((node, depth))
                    nxt.append(child)
            frontier = nxt
        return out

    def by_kind(self, kind: str) -> list[LineageNode]:
        return [n for n in self.nodes.values() if n.kind == kind]

    def find_datasets(self, name: str, workspace: str = "") -> list[LineageNode]:
        want = (name or "").strip().lower()
        ws = (workspace or "").strip().lower()
        hits = []
        for n in self.by_kind(DATASET):
            if n.name.strip().lower() != want:
                continue
            if ws and n.workspace_name.strip().lower() != ws:
                continue
            hits.append(n)
        return hits

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.scanned_at)

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "scanned_at": self.scanned_at,
            "workspace_count": self.workspace_count,
            "gaps": self.gaps,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": {k: sorted(v) for k, v in self.edges.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> LineageGraph:
        g = cls(
            gaps=d.get("gaps", []) or [],
            scanned_at=float(d.get("scanned_at", 0.0) or 0.0),
            workspace_count=int(d.get("workspace_count", 0) or 0),
        )
        for raw in d.get("nodes", []) or []:
            node = LineageNode.from_dict(raw)
            if node.id:
                g.nodes[node.id] = node
        for k, v in (d.get("edges", {}) or {}).items():
            g.edges[k] = set(v)
        return g


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_token_cached()}"}


def _explain_admin_error(resp: requests.Response) -> str:
    """Turn the two failure modes that actually happen into fixable advice."""
    body = (resp.text or "")[:300]
    if resp.status_code == 401:
        return (
            "Scanner API returned 401 Unauthorized. The token carries no admin "
            "scope -- grant the app registration `Tenant.Read.All` and complete "
            "admin consent."
        )
    if resp.status_code == 403:
        return (
            "Scanner API returned 403 Forbidden. This is almost always the tenant "
            "setting, not the code: in the Fabric admin portal enable 'Service "
            "principals can access read-only admin APIs' and add this service "
            "principal to the allowed security group. A tenant admin has to do "
            "this -- FabricBI cannot grant it to itself."
        )
    if resp.status_code == 429:
        return (
            "Scanner API returned 429 Too Many Requests -- the tenant hit its "
            "hourly scan throttle. Reuse the cached graph, or retry in an hour."
        )
    return f"Scanner API returned {resp.status_code}: {body}"


def _get(url: str) -> requests.Response | None:
    try:
        r = requests.get(url, headers=_bearer(), timeout=_TIMEOUT)
    except Exception as e:
        _set_error(f"GET {url} transport error: {e}")
        return None
    if r.status_code >= 400:
        _set_error(_explain_admin_error(r))
        return None
    return r


def list_modified_workspace_ids(modified_since: str = "") -> list[str]:
    """Workspace IDs the tenant will let us scan.

    `modified_since` is an ISO8601 UTC stamp (e.g. 2026-08-01T00:00:00Z); pass
    it for an incremental rescan instead of walking the whole tenant again.
    """
    url = f"{_ADMIN_BASE}/workspaces/modified"
    params = []
    if LINEAGE_EXCLUDE_PERSONAL:
        params.append("excludePersonalWorkspaces=True")
    if modified_since:
        params.append(f"modifiedSince={modified_since}")
    if params:
        url = f"{url}?{'&'.join(params)}"
    r = _get(url)
    if r is None:
        return []
    try:
        raw = r.json()
    except ValueError:
        _set_error("Scanner API /workspaces/modified returned non-JSON.")
        return []
    ids = [w.get("id", "") for w in raw if isinstance(w, dict) and w.get("id")]
    if ids:
        _clear_error()
    return ids


def _start_scan(workspace_ids: list[str]) -> str:
    url = (
        f"{_ADMIN_BASE}/workspaces/getInfo"
        "?lineage=True&datasourceDetails=True"
        "&datasetSchema=True&datasetExpressions=True"
    )
    try:
        r = requests.post(
            url,
            headers={**_bearer(), "Content-Type": "application/json"},
            json={"workspaces": workspace_ids},
            timeout=_TIMEOUT,
        )
    except Exception as e:
        _set_error(f"POST getInfo transport error: {e}")
        return ""
    if r.status_code >= 400:
        _set_error(_explain_admin_error(r))
        return ""
    try:
        return r.json().get("id", "")
    except ValueError:
        _set_error("getInfo returned non-JSON.")
        return ""


def _await_scan(scan_id: str, timeout_s: int) -> bool:
    """Poll scanStatus with gentle backoff until Succeeded, or give up."""
    deadline = time.time() + timeout_s
    delay = 2.0
    while time.time() < deadline:
        r = _get(f"{_ADMIN_BASE}/workspaces/scanStatus/{scan_id}")
        if r is None:
            return False
        try:
            status = r.json().get("status", "")
        except ValueError:
            _set_error(f"scanStatus/{scan_id} returned non-JSON.")
            return False
        if status == "Succeeded":
            return True
        if status in ("Failed", "Cancelled"):
            _set_error(f"Scan {scan_id} ended with status {status}.")
            return False
        time.sleep(delay)
        delay = min(delay * 1.5, 15.0)
    _set_error(
        f"Scan {scan_id} did not finish within {timeout_s}s. Raise "
        f"LINEAGE_SCAN_TIMEOUT_S, or scan fewer workspaces at a time."
    )
    return False


def _scan_result(scan_id: str) -> dict:
    r = _get(f"{_ADMIN_BASE}/workspaces/scanResult/{scan_id}")
    if r is None:
        return {}
    try:
        return r.json() or {}
    except ValueError:
        _set_error("scanResult returned non-JSON.")
        return {}


def _ingest_datasets(ws: dict, ws_id: str, ws_name: str, graph: LineageGraph) -> None:
    for ds in ws.get("datasets", []) or []:
        ds_id = ds.get("id", "")
        if not ds_id:
            continue
        node = LineageNode(
            id=ds_id,
            name=ds.get("name", "") or "(unnamed dataset)",
            kind=DATASET,
            workspace_id=ws_id,
            workspace_name=ws_name,
        )
        tables = ds.get("tables") or []
        if not tables:
            graph.gaps.append(
                f"dataset '{ws_name} > {node.name}' returned no schema "
                f"(field-level impact unavailable for it)"
            )
        for tbl in tables:
            tname = tbl.get("name", "")
            if not tname:
                continue
            node.tables.append(tname)
            for col in tbl.get("columns", []) or []:
                cname = col.get("name", "")
                if cname:
                    node.columns[f"{tname}[{cname}]"] = col.get("dataType", "") or ""
            for mea in tbl.get("measures", []) or []:
                mname = mea.get("name", "")
                if mname:
                    node.measures[f"{tname}[{mname}]"] = mea.get("expression", "") or ""
        graph.nodes[ds_id] = node

        for up in ds.get("upstreamDatasets", []) or []:
            graph.add_edge(up.get("targetDatasetId", "") or up.get("id", ""), ds_id)
        for up in ds.get("upstreamDataflows", []) or []:
            graph.add_edge(up.get("targetDataflowId", "") or up.get("id", ""), ds_id)


def _ingest_plain_items(
    ws: dict, ws_id: str, ws_name: str, graph: LineageGraph
) -> None:
    """Dataflows and datamarts: nodes with no field surface of their own."""
    for df in ws.get("dataflows", []) or []:
        df_id = df.get("objectId", "") or df.get("id", "")
        if not df_id:
            continue
        graph.nodes[df_id] = LineageNode(
            id=df_id,
            name=df.get("name", "") or "(unnamed dataflow)",
            kind=DATAFLOW,
            workspace_id=ws_id,
            workspace_name=ws_name,
        )

    for dm in ws.get("datamarts", []) or []:
        dm_id = dm.get("id", "")
        if not dm_id:
            continue
        graph.nodes[dm_id] = LineageNode(
            id=dm_id,
            name=dm.get("name", "") or "(unnamed datamart)",
            kind=DATAMART,
            workspace_id=ws_id,
            workspace_name=ws_name,
        )


def _ingest_reports(ws: dict, ws_id: str, ws_name: str, graph: LineageGraph) -> None:
    for rep in ws.get("reports", []) or []:
        rep_id = rep.get("id", "")
        if not rep_id:
            continue
        ds_id = rep.get("datasetId", "") or ""
        graph.nodes[rep_id] = LineageNode(
            id=rep_id,
            name=rep.get("name", "") or "(unnamed report)",
            kind=REPORT,
            workspace_id=ws_id,
            workspace_name=ws_name,
            binds_to=ds_id,
        )
        if ds_id:
            graph.add_edge(ds_id, rep_id)
        else:
            graph.gaps.append(
                f"report '{ws_name} > {rep.get('name', '?')}' reported no bound "
                f"dataset (cannot trace its impact)"
            )


def _ingest_dashboards(ws: dict, ws_id: str, ws_name: str, graph: LineageGraph) -> None:
    for dash in ws.get("dashboards", []) or []:
        dash_id = dash.get("id", "")
        if not dash_id:
            continue
        node = LineageNode(
            id=dash_id,
            name=dash.get("displayName", "") or dash.get("name", "") or "(unnamed)",
            kind=DASHBOARD,
            workspace_id=ws_id,
            workspace_name=ws_name,
        )
        for tile in dash.get("tiles", []) or []:
            title = tile.get("title", "") or "(untitled tile)"
            for src in (tile.get("datasetId", ""), tile.get("reportId", "")):
                if src:
                    graph.add_edge(src, dash_id)
                    if title not in node.tiles:
                        node.tiles.append(title)
        graph.nodes[dash_id] = node


def _ingest_workspace(ws: dict, graph: LineageGraph) -> None:
    """Fold one scanned workspace into the graph.

    Every edge points the way breakage travels: an upstream artifact points at
    everything that consumes it.
    """
    ws_id = ws.get("id", "")
    ws_name = ws.get("name", "") or "(unnamed workspace)"
    state = ws.get("state", "")
    if state and state != "Active":
        graph.gaps.append(f"workspace '{ws_name}' skipped (state={state})")
        return
    _ingest_datasets(ws, ws_id, ws_name, graph)
    _ingest_plain_items(ws, ws_id, ws_name, graph)
    _ingest_reports(ws, ws_id, ws_name, graph)
    _ingest_dashboards(ws, ws_id, ws_name, graph)


def _load_cache() -> LineageGraph | None:
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    try:
        return LineageGraph.from_dict(data)
    except Exception:
        return None


def _save_cache(graph: LineageGraph) -> None:
    try:
        _CACHE_FILE.parent.mkdir(exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(graph.to_dict()), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not write lineage cache: {e}")


def _batched(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def scan_tenant(
    force: bool = False,
    modified_since: str = "",
    workspace_ids: list[str] | None = None,
    progress=None,
) -> LineageGraph:
    """Build (or reuse) the tenant lineage graph.

    Returns a graph even on partial failure -- a batch that 403s becomes an
    entry in `graph.gaps` rather than an exception, so a scan that reads 9 of
    10 workspaces still produces a usable, honestly-labelled answer.
    """
    if not force:
        cached = _load_cache()
        if cached and cached.age_s < LINEAGE_CACHE_TTL_S and cached.nodes:
            logger.info(
                f"lineage: reusing cache ({len(cached.nodes)} nodes, "
                f"{int(cached.age_s)}s old)"
            )
            return cached

    if workspace_ids is not None:
        ids = workspace_ids
    else:
        ids = list_modified_workspace_ids(modified_since)

    graph = LineageGraph(scanned_at=time.time())
    if not ids:
        graph.gaps.append(
            get_last_error() or "No workspaces returned by the Scanner API."
        )
        return graph

    graph.workspace_count = len(ids)
    batches = list(_batched(ids, _BATCH))
    for i, batch in enumerate(batches, start=1):
        if progress:
            progress(f"Scanning workspaces {i}/{len(batches)} ({len(batch)} in batch)")
        scan_id = _start_scan(batch)
        if not scan_id:
            graph.gaps.append(
                f"batch {i}/{len(batches)} ({len(batch)} workspaces) could not "
                f"start: {get_last_error()}"
            )
            continue
        if not _await_scan(scan_id, LINEAGE_SCAN_TIMEOUT_S):
            graph.gaps.append(
                f"batch {i}/{len(batches)} ({len(batch)} workspaces) did not "
                f"complete: {get_last_error()}"
            )
            continue
        result = _scan_result(scan_id)
        if not result:
            graph.gaps.append(
                f"batch {i}/{len(batches)} returned no result: {get_last_error()}"
            )
            continue
        for ws in result.get("workspaces", []) or []:
            try:
                _ingest_workspace(ws, graph)
            except Exception as e:
                graph.gaps.append(
                    f"workspace '{ws.get('name', '?')}' failed to parse: {e}"
                )

    graph.scanned_at = time.time()
    if graph.nodes:
        _clear_error()
        _save_cache(graph)
    logger.info(
        f"lineage: {len(graph.nodes)} nodes, "
        f"{sum(len(v) for v in graph.edges.values())} edges, "
        f"{len(graph.gaps)} gap(s)"
    )
    return graph


def cached_graph() -> LineageGraph | None:
    """The last scan, however old. Callers decide whether the age is tolerable."""
    return _load_cache()


def clear_cache() -> None:
    try:
        _CACHE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def summarize(graph: LineageGraph) -> str:
    counts: dict[str, int] = {}
    for n in graph.nodes.values():
        counts[n.kind] = counts.get(n.kind, 0) + 1
    parts = ", ".join(f"{v} {k.lower()}(s)" for k, v in sorted(counts.items()))
    edges = sum(len(v) for v in graph.edges.values())
    line = (
        f"Lineage graph - {graph.workspace_count} workspace(s), {parts or 'nothing'}; "
        f"{edges} edge(s); scanned {int(graph.age_s)}s ago."
    )
    if graph.gaps:
        line += f"\nCoverage gaps ({len(graph.gaps)}):\n" + "\n".join(
            f"  - {g}" for g in graph.gaps[:20]
        )
        if len(graph.gaps) > 20:
            line += f"\n  - ...and {len(graph.gaps) - 20} more"
    return line
