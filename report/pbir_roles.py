
from __future__ import annotations

import json
import os
from typing import Any

# Roles verified against Desktop-authored reports under storage/ —
# treemap→Group, gauge→TargetValue, kpi→Indicator/Goal/TrendLine and the
# Category/Y cartesian pattern were all read off files Power BI itself wrote.
# Where a role is plausible but unevidenced it is included only if omitting it
# would cause a false "unsupported role" report; see `Legend` below.
_CARTESIAN = {
    "Category",
    "Y",
    "Series",
    # pbi_cli emits `Legend` for the legend well. The vendored catalog
    # reference says the PBIR role is `Series` and that "Legend" is only its
    # display name — but no report on disk has a legend bound, so there is no
    # positive evidence either way. It stays accepted here rather than being
    # reported as broken on a guess; resolve it by binding a legend in Desktop
    # and reading back the role it writes.
    "Legend",
    "Rows",
    "Columns",
    "Tooltips",
}

ROLES: dict[str, set[str]] = {
    # --- cartesian ---------------------------------------------------------
    "barChart": _CARTESIAN,
    "columnChart": _CARTESIAN,
    "clusteredBarChart": _CARTESIAN,
    "clusteredColumnChart": _CARTESIAN,
    "stackedBarChart": _CARTESIAN,
    "stackedColumnChart": _CARTESIAN,
    "hundredPercentStackedBarChart": _CARTESIAN,
    "hundredPercentStackedColumnChart": _CARTESIAN,
    "lineChart": _CARTESIAN | {"Y2"},
    "areaChart": _CARTESIAN | {"Y2"},
    "stackedAreaChart": _CARTESIAN | {"Y2"},
    "ribbonChart": _CARTESIAN,
    # Combo charts: Y is the column values well, Y2 the line values well.
    # NOT ColumnY/LineY — those are pbi_cli inventions Power BI ignores.
    "lineStackedColumnComboChart": _CARTESIAN | {"Y2"},
    "lineClusteredColumnComboChart": _CARTESIAN | {"Y2"},
    # --- part-to-whole -----------------------------------------------------
    "donutChart": {"Category", "Y", "Legend", "Details", "Tooltips"},
    "pieChart": {"Category", "Y", "Legend", "Details", "Tooltips"},
    "funnelChart": {"Category", "Y", "Tooltips"},
    "waterfallChart": {"Category", "Y", "Breakdown", "Tooltips"},
    # treemap groups on Group, not Category — verified against two
    # Desktop-authored treemaps in the MavenMarket report.
    "treemap": {"Group", "Details", "Values", "Tooltips"},
    # --- scatter / map -----------------------------------------------------
    "scatterChart": {"Category", "Details", "X", "Y", "Size", "Legend", "Play", "Tooltips"},
    "map": {"Category", "Series", "Size", "Longitude", "Latitude", "Tooltips"},
    "filledMap": {"Category", "Series", "Size", "Longitude", "Latitude", "Tooltips"},
    "azureMap": {"Category", "Series", "Size", "Longitude", "Latitude", "Tooltips"},
    # --- tabular -----------------------------------------------------------
    "tableEx": {"Values"},
    "pivotTable": {"Rows", "Columns", "Values"},
    # --- single value ------------------------------------------------------
    "cardVisual": {"Data"},
    "card": {"Values"},
    "multiRowCard": {"Values"},
    "kpi": {"Indicator", "TrendLine", "Goal"},
    "gauge": {"Y", "MinValue", "MaxValue", "TargetValue"},
    # --- slicers -----------------------------------------------------------
    "slicer": {"Values"},
    "textSlicer": {"Values"},
    "listSlicer": {"Values"},
    "advancedSlicerVisual": {"Values"},
    # --- no data roles -----------------------------------------------------
    "textbox": set(),
    "shape": set(),
    "image": set(),
    "actionButton": set(),
    "pageNavigator": set(),
}

# Visual types that legitimately carry no bindings, so an empty queryState is
# not a defect for them.
DATALESS_TYPES = frozenset(
    t for t, roles in ROLES.items() if not roles
)


def known_roles(visual_type: str) -> set[str] | None:
    return ROLES.get(visual_type)


def unsupported_roles(visual_type: str, query_state: dict) -> list[str]:
    valid = known_roles(visual_type)
    if valid is None:
        return []
    bad = []
    for role, body in (query_state or {}).items():
        if not isinstance(body, dict) or not body.get("projections"):
            continue
        if role not in valid:
            bad.append(role)
    return sorted(bad)


def _projections(body: Any) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    if not isinstance(body, dict):
        return out
    for proj in body.get("projections", []) or []:
        field = (proj or {}).get("field") or {}
        for kind in ("Measure", "Column", "Aggregation", "HierarchyLevel"):
            node = field.get(kind)
            if not isinstance(node, dict):
                continue
            entity = (
                (node.get("Expression") or {}).get("SourceRef", {}).get("Entity", "?")
            )
            out.append((kind, entity, node.get("Property", "?")))
            break
        else:
            out.append(("?", "?", proj.get("queryRef", "?")))
    return out


def summarize_roles(visual_type: str, query_state: dict) -> str:
    parts: list[str] = []
    valid = known_roles(visual_type)
    for role, body in (query_state or {}).items():
        refs = [f"{ent}[{prop}] ({kind})" for kind, ent, prop in _projections(body)]
        if not refs:
            continue
        flag = ""
        if valid is not None and role not in valid:
            flag = f" !! NOT a role of {visual_type} — Power BI renders this EMPTY"
        parts.append(f"{role}={', '.join(refs)}{flag}")

    if parts:
        return "; ".join(parts)
    if visual_type in DATALESS_TYPES:
        return "(no field bindings — textbox/shape/image)"
    return "(NO BINDINGS — renders empty)"


def describe_query_state(visual_json_path: str) -> str:
    try:
        with open(visual_json_path, encoding="utf-8") as f:
            vdata = json.load(f)
    except Exception as e:  # unreadable file is itself worth reporting
        return f"{os.path.basename(os.path.dirname(visual_json_path))}: ERROR {e}"

    vis = vdata.get("visual", {}) or {}
    vtype = vis.get("visualType", "?")
    name = vdata.get("name") or os.path.basename(os.path.dirname(visual_json_path))
    qs = (vis.get("query", {}) or {}).get("queryState", {}) or {}
    valid = known_roles(vtype)

    lines = [f"{name} ({vtype}):"]
    if not qs:
        note = "" if vtype in DATALESS_TYPES else "  !! no bindings — renders empty"
        lines.append(f"  (no queryState){note}")
        return "\n".join(lines)

    for role, body in qs.items():
        refs = [f"{ent}[{prop}] ({kind})" for kind, ent, prop in _projections(body)]
        flag = ""
        if refs and valid is not None and role not in valid:
            flag = (
                f"   !! '{role}' is not a role of {vtype} — Power BI DROPS it "
                f"and renders this visual EMPTY. Valid: {sorted(valid)}"
            )
        lines.append(f"  {role:<14} <- {', '.join(refs) or '(empty)'}{flag}")
    return "\n".join(lines)
