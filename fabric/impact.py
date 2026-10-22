"""Impact analysis: given a semantic-model change, name what breaks.

Three sources are stitched together here, because no single one is enough:

  1. `fabric.lineage` gives the tenant edges -- which reports, dashboards and
     chained datasets consume the model being changed. That is dataset->report
     granularity only; the Scanner API does not return report internals.
  2. The Fabric item-definition API (`getDefinition`) pulls the actual PBIR for
     each downstream report, which upgrades "this report might be affected" to
     "visual 'Revenue by Region' on page 'Summary' binds to the field you are
     dropping". Fetched only for reports downstream of the change, never for
     the whole tenant.
  3. Scanner-supplied measure DAX on *chained* datasets, scanned for
     references to the removed fields, so composite-model breakage is caught
     too.

The honesty rule this module enforces: anything we could not read is reported
as unchecked, never as safe. A safety net with a silent hole is worse than no
safety net, because engineers stop looking themselves.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field

import requests

from auth.token_manager import get_fabric_token
from config import LINEAGE_MAX_REPORT_FETCH
from fabric.lineage import (
    DASHBOARD,
    DATASET,
    REPORT,
    LineageGraph,
    LineageNode,
)
from model.tool_guards import _walk_field_refs

logger = logging.getLogger(__name__)

_FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
_TIMEOUT = 30

CONFIRMED = "CONFIRMED"
POTENTIAL = "POTENTIAL"


@dataclass
class ModelSurface:
    """The referenceable surface of a semantic model.

    Only what a downstream artifact can bind to: tables, columns and measures.
    Expressions and formatting live elsewhere -- changing them breaks nothing
    by reference.
    """

    tables: set[str] = field(default_factory=set)
    columns: dict[str, str] = field(default_factory=dict)
    measures: set[str] = field(default_factory=set)

    @classmethod
    def from_xmla_metadata(cls, meta: dict) -> ModelSurface:
        """Build from `fabric.pbi_xmla.fetch_model_metadata()` output."""
        surface = cls(tables=set(meta.get("tables", []) or []))
        for tname, cols in (meta.get("columns_by_table", {}) or {}).items():
            for entry in cols or []:
                if isinstance(entry, (tuple, list)) and entry:
                    cname = entry[0]
                    dtype = str(entry[1]) if len(entry) > 1 else ""
                else:
                    cname, dtype = str(entry), ""
                if cname:
                    surface.columns[f"{tname}[{cname}]"] = dtype
        for tname, meas in (meta.get("measures_by_table", {}) or {}).items():
            for mname in meas or []:
                if mname:
                    surface.measures.add(f"{tname}[{mname}]")
        return surface

    @classmethod
    def from_lineage_node(cls, node: LineageNode) -> ModelSurface:
        """Build from a scanned dataset node (no XMLA connection required)."""
        return cls(
            tables=set(node.tables),
            columns=dict(node.columns),
            measures=set(node.measures.keys()),
        )

    def is_empty(self) -> bool:
        return not (self.tables or self.columns or self.measures)


@dataclass
class SurfaceDiff:
    """What a change removed, renamed or retyped. Additions never break anything."""

    removed_tables: list[str] = field(default_factory=list)
    removed_columns: list[str] = field(default_factory=list)
    removed_measures: list[str] = field(default_factory=list)
    retyped_columns: list[tuple[str, str, str]] = field(default_factory=list)
    added_columns: list[str] = field(default_factory=list)
    added_measures: list[str] = field(default_factory=list)

    @property
    def breaking_refs(self) -> list[str]:
        """Every `Table[Field]` reference a downstream artifact could now miss."""
        refs = list(self.removed_columns) + list(self.removed_measures)
        refs += [ref for ref, _, _ in self.retyped_columns]
        return sorted(set(refs))

    def is_empty(self) -> bool:
        return not (
            self.removed_tables
            or self.removed_columns
            or self.removed_measures
            or self.retyped_columns
        )

    def rename_candidates(self) -> list[tuple[str, str]]:
        """Pair each removal with a same-table addition -- a likely rename.

        A rename and a delete are indistinguishable in a before/after diff, but
        they need different fixes: a rename can be repointed, a delete cannot.
        Flagging the candidate pair is more useful than guessing.
        """
        pairs: list[tuple[str, str]] = []
        added = self.added_columns + self.added_measures
        for gone in self.removed_columns + self.removed_measures:
            table = gone.split("[", 1)[0]
            same = [a for a in added if a.split("[", 1)[0] == table]
            if len(same) == 1:
                pairs.append((gone, same[0]))
        return pairs


def diff_surface(before: ModelSurface, after: ModelSurface) -> SurfaceDiff:
    diff = SurfaceDiff()
    diff.removed_tables = sorted(before.tables - after.tables)
    diff.removed_columns = sorted(set(before.columns) - set(after.columns))
    diff.removed_measures = sorted(before.measures - after.measures)
    diff.added_columns = sorted(set(after.columns) - set(before.columns))
    diff.added_measures = sorted(after.measures - before.measures)
    for ref, old_type in before.columns.items():
        new_type = after.columns.get(ref)
        if new_type is not None and old_type and new_type and new_type != old_type:
            diff.retyped_columns.append((ref, old_type, new_type))
    diff.retyped_columns.sort()
    return diff


@dataclass
class ImpactHit:
    """One downstream artifact affected by the change."""

    node: LineageNode
    confidence: str
    depth: int
    details: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class ImpactReport:
    dataset: LineageNode | None
    diff: SurfaceDiff
    hits: list[ImpactHit] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    graph_age_s: float = 0.0
    graph_scanned_at: float = 0.0
    graph_gaps: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> list[ImpactHit]:
        return [h for h in self.hits if h.confidence == CONFIRMED]

    @property
    def potential(self) -> list[ImpactHit]:
        return [h for h in self.hits if h.confidence == POTENTIAL]


def _ref_patterns(ref: str) -> list[re.Pattern]:
    """Regexes matching a `Table[Field]` reference as DAX would spell it."""
    table, _, rest = ref.partition("[")
    field_name = rest.rstrip("]")
    if not field_name:
        return []
    tbl = re.escape(table)
    fld = re.escape(field_name)
    return [
        re.compile(rf"'{tbl}'\s*\[\s*{fld}\s*\]", re.IGNORECASE),
        re.compile(rf"(?<!['\w]){tbl}\s*\[\s*{fld}\s*\]", re.IGNORECASE),
        re.compile(rf"(?<![\w'])\[\s*{fld}\s*\]", re.IGNORECASE),
    ]


def _fetch_report_definition(workspace_id: str, report_id: str) -> dict | None:
    """Pull a report's PBIR parts. Handles the long-running-operation dance."""
    url = f"{_FABRIC_BASE}/workspaces/{workspace_id}/items/{report_id}/getDefinition"
    headers = {"Authorization": f"Bearer {get_fabric_token()}"}
    try:
        r = requests.post(url, headers=headers, timeout=_TIMEOUT)
    except Exception as e:
        logger.info(f"getDefinition transport error for {report_id}: {e}")
        return None

    if r.status_code == 202:
        op_id = r.headers.get("x-ms-operation-id", "")
        if not op_id:
            return None
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(float(r.headers.get("Retry-After", "2") or 2))
            try:
                s = requests.get(
                    f"{_FABRIC_BASE}/operations/{op_id}",
                    headers=headers,
                    timeout=_TIMEOUT,
                )
                state = (s.json() or {}).get("status", "")
            except Exception:
                return None
            if state == "Succeeded":
                try:
                    res = requests.get(
                        f"{_FABRIC_BASE}/operations/{op_id}/result",
                        headers=headers,
                        timeout=_TIMEOUT,
                    )
                    return res.json() if res.status_code < 400 else None
                except Exception:
                    return None
            if state in ("Failed", "Undefined"):
                return None
        return None

    if r.status_code >= 400:
        logger.info(f"getDefinition {report_id} -> {r.status_code}")
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _bindings_from_definition(payload: dict) -> list[tuple[str, str, str]]:
    """(page, visual, 'Table[Field]') for every field reference in a report.

    Reuses the PBIR walker the local report tools already use, so service
    reports and on-disk reports are read by exactly the same logic.
    """
    out: list[tuple[str, str, str]] = []
    parts = ((payload or {}).get("definition", {}) or {}).get("parts", []) or []
    for part in parts:
        path = part.get("path", "") or ""
        if not path.endswith(".json"):
            continue
        try:
            raw = base64.b64decode(part.get("payload", "") or "")
            data = json.loads(raw.decode("utf-8-sig"))
        except Exception as e:
            logger.debug("skipping unreadable part %s: %s", path, e)
            continue
        segments = path.replace("\\", "/").split("/")
        page = visual = ""
        if "pages" in segments:
            idx = segments.index("pages")
            if idx + 1 < len(segments):
                page = segments[idx + 1]
        if "visuals" in segments:
            idx = segments.index("visuals")
            if idx + 1 < len(segments):
                visual = segments[idx + 1]
        if isinstance(data, dict):
            visual = data.get("name") or visual
        refs: list = []
        _walk_field_refs(data, refs)
        for _kind, entity, prop in refs:
            if entity and prop:
                out.append((page or "?", visual or "?", f"{entity}[{prop}]"))
    return out


def _match_report(
    node: LineageNode, breaking: set[str], broken_tables: set[str]
) -> tuple[bool, ImpactHit | None]:
    """Returns (definition_was_readable, hit_if_affected).

    The two None cases must stay distinguishable: a report we could not read
    is unknown, a report we read and cleared is safe. Collapsing them into one
    "no hit" is how a safety net grows a silent hole.
    """
    payload = _fetch_report_definition(node.workspace_id, node.id)
    if payload is None:
        return False, None
    bindings = _bindings_from_definition(payload)
    if not bindings:
        # Parts came back but carried no field references at all. A textbox-only
        # report is possible, but so is a PBIR layout this walker cannot parse
        # -- and we cannot tell which, so it does not count as cleared.
        return False, None
    lowered = {b.lower() for b in breaking}
    tables_lower = {t.lower() for t in broken_tables}
    details: list[str] = []
    for page, visual, ref in bindings:
        table = ref.split("[", 1)[0]
        if ref.lower() in lowered or table.lower() in tables_lower:
            line = f"page '{page}' - visual '{visual}' binds {ref}"
            if line not in details:
                details.append(line)
    if not details:
        return True, None
    return True, ImpactHit(
        node=node,
        confidence=CONFIRMED,
        depth=1,
        details=sorted(details),
        reason="report definition read; these visuals reference the changed fields",
    )


def _match_chained_dataset(
    node: LineageNode, breaking: set[str], depth: int
) -> ImpactHit | None:
    """Scan a downstream dataset's measure DAX for references to removed fields."""
    patterns = {ref: _ref_patterns(ref) for ref in breaking}
    details: list[str] = []
    for mref, expression in node.measures.items():
        if not expression:
            continue
        for ref, pats in patterns.items():
            if any(p.search(expression) for p in pats):
                details.append(f"measure {mref} references {ref}")
                break
    if not details:
        return None
    return ImpactHit(
        node=node,
        confidence=CONFIRMED,
        depth=depth,
        details=sorted(details),
        reason="chained dataset; its DAX references the changed fields",
    )


def analyze(
    graph: LineageGraph,
    dataset_id: str,
    diff: SurfaceDiff,
    deep: bool = True,
    max_report_fetch: int = 0,
) -> ImpactReport:
    """Walk everything downstream of `dataset_id` and classify the damage."""
    dataset = graph.nodes.get(dataset_id)
    report = ImpactReport(
        dataset=dataset,
        diff=diff,
        graph_age_s=graph.age_s,
        graph_scanned_at=graph.scanned_at,
        graph_gaps=list(graph.gaps),
    )
    if diff.is_empty():
        return report

    breaking = set(diff.breaking_refs)
    broken_tables = set(diff.removed_tables)
    budget = max_report_fetch or LINEAGE_MAX_REPORT_FETCH
    fetched = 0

    for node, depth in graph.downstream_of(dataset_id):
        if node.kind == REPORT:
            if not deep:
                report.unchecked.append(
                    f"Report '{node.label}' - deep inspection disabled"
                )
                continue
            if fetched >= budget:
                report.unchecked.append(
                    f"Report '{node.label}' - not inspected (fetch budget of "
                    f"{budget} reports reached; raise LINEAGE_MAX_REPORT_FETCH)"
                )
                continue
            fetched += 1
            readable, hit = _match_report(node, breaking, broken_tables)
            if hit is not None:
                hit.depth = depth
                report.hits.append(hit)
            elif not readable:
                report.unchecked.append(
                    f"Report '{node.label}' - definition could not be read "
                    f"(needs Item.ReadAll, or the report is not in PBIR format)"
                )
            # Readable with no matching binding means genuinely unaffected;
            # staying silent there is what keeps the alert worth reading.
        elif node.kind == DASHBOARD:
            report.hits.append(
                ImpactHit(
                    node=node,
                    confidence=POTENTIAL,
                    depth=depth,
                    details=[f"tile '{t}'" for t in node.tiles[:10]],
                    reason=(
                        "dashboard tiles are pinned snapshots; the API exposes no "
                        "field bindings for them, so they need a human check"
                    ),
                )
            )
        elif node.kind == DATASET:
            hit = _match_chained_dataset(node, breaking, depth)
            if hit is not None:
                report.hits.append(hit)
            elif not node.measures:
                report.unchecked.append(
                    f"{node.kind} '{node.label}' - no measure DAX in the scan"
                )
        else:
            report.hits.append(
                ImpactHit(
                    node=node,
                    confidence=POTENTIAL,
                    depth=depth,
                    reason="downstream artifact of the changed model",
                )
            )

    report.hits.sort(key=lambda h: (h.confidence != CONFIRMED, h.depth, h.node.label))
    return report


def _fmt_change_list(diff: SurfaceDiff) -> list[str]:
    lines: list[str] = []
    if diff.removed_tables:
        lines.append(f"Tables removed: {', '.join(diff.removed_tables)}")
    if diff.removed_measures:
        lines.append(f"Measures removed: {', '.join(diff.removed_measures)}")
    if diff.removed_columns:
        lines.append(f"Columns removed: {', '.join(diff.removed_columns)}")
    for ref, old, new in diff.retyped_columns:
        lines.append(f"Column retyped: {ref} ({old} -> {new})")
    return lines


def _headline(n_confirmed: int, n_potential: int) -> str:
    if n_confirmed:
        return (
            f"IMPACT ALERT - {n_confirmed} downstream item(s) will break, "
            f"{n_potential} more may be affected."
        )
    if n_potential:
        return (
            f"IMPACT WARNING - no confirmed breakage, but {n_potential} "
            f"downstream item(s) could not be cleared."
        )
    return "No downstream impact found for this change."


def _render_confirmed(hits: list[ImpactHit]) -> list[str]:
    out = ["\nWILL BREAK:"]
    for hit in hits:
        hop = "" if hit.depth <= 1 else f"  [{hit.depth} hops downstream]"
        out.append(f"  {hit.node.kind}: {hit.node.label}{hop}")
        out.extend(f"      {d}" for d in hit.details[:12])
        if len(hit.details) > 12:
            out.append(f"      ...and {len(hit.details) - 12} more")
    return out


def _render_potential(hits: list[ImpactHit]) -> list[str]:
    out = ["\nNEEDS A HUMAN CHECK:"]
    for hit in hits:
        out.append(f"  {hit.node.kind}: {hit.node.label} - {hit.reason}")
        out.extend(f"      {d}" for d in hit.details[:5])
    return out


def _render_gaps(gap_lines: list[str]) -> list[str]:
    out = [f"\nNOT CHECKED ({len(gap_lines)}) - treat as unknown, not safe:"]
    out.extend(f"  - {g}" for g in gap_lines[:15])
    if len(gap_lines) > 15:
        out.append(f"  - ...and {len(gap_lines) - 15} more")
    return out


def _render_age(report: ImpactReport) -> str:
    if report.graph_scanned_at <= 0:
        return (
            "\nBased on a lineage scan of unknown age -- the graph carries no scan "
            "timestamp, so treat its coverage as unverified and rescan."
        )
    return (
        f"\nBased on a lineage scan {int(report.graph_age_s // 60)} minute(s) old. "
        f"Anything created or rebound since then is not represented -- rescan for "
        f"a fresh view."
    )


def format_alert(report: ImpactReport) -> str:
    """Render the alert an engineer reads before shipping the change."""
    ds_label = report.dataset.label if report.dataset else "(unknown model)"

    if report.diff.is_empty():
        return (
            f"No breaking change detected in {ds_label}. Additions and expression "
            f"edits do not break downstream bindings."
        )

    confirmed, potential = report.confirmed, report.potential
    out: list[str] = [_headline(len(confirmed), len(potential))]

    out.append(f"\nModel changed: {ds_label}")
    out.extend(f"  - {line}" for line in _fmt_change_list(report.diff))

    pairs = report.diff.rename_candidates()
    if pairs:
        out.append("\nPossible renames rather than deletions:")
        out.extend(f"  - {gone}  ->  {maybe}?" for gone, maybe in pairs)

    if confirmed:
        out.extend(_render_confirmed(confirmed))
    if potential:
        out.extend(_render_potential(potential))

    gap_lines = list(report.unchecked) + list(report.graph_gaps)
    if gap_lines:
        out.extend(_render_gaps(gap_lines))

    out.append(_render_age(report))
    return "\n".join(out)


def diff_from_removals(
    node: LineageNode, refs: list[str]
) -> tuple[SurfaceDiff, list[str]]:
    """Turn a planned removal list into a diff, reporting refs that don't exist.

    A typo'd field name would otherwise analyse to "nothing breaks", which is
    the most dangerous possible answer to give.
    """
    surface = ModelSurface.from_lineage_node(node)
    diff = SurfaceDiff()
    unknown: list[str] = []
    lower_cols = {k.lower(): k for k in surface.columns}
    lower_meas = {m.lower(): m for m in surface.measures}
    lower_tabs = {t.lower(): t for t in surface.tables}
    for raw in refs:
        ref = (raw or "").strip()
        if not ref:
            continue
        key = ref.lower()
        if key in lower_meas:
            diff.removed_measures.append(lower_meas[key])
        elif key in lower_cols:
            diff.removed_columns.append(lower_cols[key])
        elif key in lower_tabs:
            diff.removed_tables.append(lower_tabs[key])
        else:
            unknown.append(ref)
    diff.removed_measures.sort()
    diff.removed_columns.sort()
    diff.removed_tables.sort()
    return diff, unknown


def _resolve_dataset(
    graph: LineageGraph, model: str, workspace: str
) -> tuple[LineageNode | None, str]:
    """Find one dataset by name, or explain why we cannot."""
    if not model:
        return None, "ERROR - no semantic model name given."
    hits = graph.find_datasets(model, workspace)
    if not hits:
        known = sorted({n.label for n in graph.by_kind(DATASET)})[:25]
        hint = "\n".join(f"  - {k}" for k in known) or "  (none in the graph)"
        return None, (
            f"ERROR - no semantic model named '{model}'"
            + (f" in workspace '{workspace}'" if workspace else "")
            + f" is in the lineage graph. Known models:\n{hint}\n"
            f"If the model is new, rescan with refresh=true."
        )
    if len(hits) > 1:
        opts = "\n".join(f"  - {h.label}" for h in hits)
        return None, (
            f"ERROR - '{model}' is ambiguous; {len(hits)} models share that name. "
            f"Pass `workspace` to disambiguate:\n{opts}"
        )
    return hits[0], ""


def run_impact_analysis(
    model: str,
    removed_fields: list[str],
    workspace: str = "",
    refresh: bool = False,
    deep: bool = True,
) -> str:
    """Tool entry point: what breaks if these fields leave this model?"""
    from fabric.lineage import get_last_error, scan_tenant

    graph = scan_tenant(force=refresh)
    if not graph.nodes:
        return (
            "ERROR - the lineage graph is empty, so no impact analysis is "
            "possible. Do NOT report this as 'nothing will break'.\n"
            f"Cause: {get_last_error() or 'unknown'}"
        )

    node, err = _resolve_dataset(graph, model, workspace)
    if err or node is None:
        return err or "ERROR - could not resolve semantic model."

    if not removed_fields:
        return (
            "ERROR - `removed_fields` is empty. Pass the fields the change will "
            "drop or rename, each as Table[Field] (or a bare table name)."
        )

    diff, unknown = diff_from_removals(node, removed_fields)
    alert = format_alert(analyze(graph, node.id, diff, deep=deep))
    if unknown:
        alert += (
            f"\n\nNOT FOUND IN THE MODEL ({len(unknown)}): "
            + ", ".join(unknown)
            + "\nThese were not analysed. Check the spelling -- a mistyped field "
            "analyses as 'nothing breaks', which is not the same as safe."
        )
    return alert


def run_lineage_map(model: str, workspace: str = "", refresh: bool = False) -> str:
    """Tool entry point: what consumes this model, transitively?"""
    from fabric.lineage import get_last_error, scan_tenant, summarize

    graph = scan_tenant(force=refresh)
    if not graph.nodes:
        return (
            "ERROR - the lineage graph is empty.\n"
            f"Cause: {get_last_error() or 'unknown'}"
        )
    if not model:
        return summarize(graph)

    node, err = _resolve_dataset(graph, model, workspace)
    if err or node is None:
        return err or "ERROR - could not resolve semantic model."

    rows = graph.downstream_of(node.id)
    if not rows:
        return (
            f"{node.label} has no downstream consumers in the lineage graph.\n"
            f"{summarize(graph)}"
        )
    out = [f"Downstream of {node.label} - {len(rows)} consumer(s):"]
    for consumer, depth in rows:
        out.append(f"  {'  ' * (depth - 1)}[{depth}] {consumer.kind}: {consumer.label}")
    out.append("")
    out.append(summarize(graph))
    return "\n".join(out)
