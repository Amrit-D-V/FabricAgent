
from __future__ import annotations

import glob
import json
import os
import re

from config import ALT_TEXT_MAX_CHARS, PROJECT_ROOT, PROJECTS_DIR, REPORT_LAYOUT
from model.build_trace import _build_trace, _trace_file_after_write
from model.json_io import _atomic_write_json


def _list_page_visual_names(report_path: str, page_name: str) -> list[str]:
    if not report_path or not page_name:
        return []
    pages_dir = os.path.join(report_path, "definition", "pages")
    if not os.path.isdir(pages_dir):
        pages_dir = os.path.join(report_path, "Report", "definition", "pages")
        if not os.path.isdir(pages_dir):
            return []
    target = page_name.lower()
    visuals_dir = None
    for entry in os.listdir(pages_dir):
        if entry.lower() == target:
            candidate = os.path.join(pages_dir, entry, "visuals")
            if os.path.isdir(candidate):
                visuals_dir = candidate
                break
    if not visuals_dir:
        return []
    return sorted(
        d
        for d in os.listdir(visuals_dir)
        if os.path.isdir(os.path.join(visuals_dir, d))
    )


# Visual mutation tools that require the visual to already exist
_VISUAL_EDIT_TOOLS = {
    "pbi_visual_bind",
    "pbi_visual_format",
    "pbi_visual_update",
    "pbi_visual_delete",
    "pbi_visual_set_container",
    "pbi_card_set_image",
    "pbi_visual_color_by_category",
    "pbi_visual_color_by_measure",
    "pbi_visual_conditional_color",
}

_BIND_ROLE_KEYS = (
    "field",
    "category",
    "value",
    "row",
    "column",
    "line",
    "legend",
    "indicator",
    "trend",
    "goal",
    "max",
)


def _validate_visual_bind_combo(tool_input: dict) -> str | None:
    present = {k for k in _BIND_ROLE_KEYS if (tool_input.get(k) or "").strip()}
    if not present:
        return (
            "ERROR — refused: pbi_visual_bind called with NO binding fields. "
            "Required combos: card/slicer→field; chart→category+value; "
            "matrix→row+value; combo→category+value+line; kpi→indicator. "
            "Call pbi_visual_list / measure_operations / column_operations "
            "first, then retry with the right field roles."
        )
    return None


_OPTION_GUARD_FIELDS = (
    "path",
    "report_path",
    "theme_file",
    "name",
    "page",
    "visual_name",
    "display_name",
)
_PATH_FIELDS = ("path", "report_path", "theme_file")
_ALLOWED_PATH_ROOTS = (
    str(PROJECT_ROOT),
    os.path.realpath(str(PROJECTS_DIR)),
    os.path.realpath("C:/tmp"),
)


def _path_is_confined(p: str) -> bool:
    if not p:
        return False
    try:
        rp = os.path.realpath(p)
    except (OSError, ValueError):
        return False
    for root in _ALLOWED_PATH_ROOTS:
        try:
            if os.path.commonpath([root, rp]) == root:
                return True
        except ValueError:
            continue
    return False


def _guard_tool_inputs(tool_name: str, tool_input: dict) -> str | None:
    for f in _OPTION_GUARD_FIELDS:
        v = tool_input.get(f)
        if isinstance(v, str) and v.lstrip().startswith("-"):
            return (
                f"ERROR — refused {tool_name}: '{f}' must not start with '-' "
                f"(argument-injection guard). Got {v!r}."
            )
    for f in _PATH_FIELDS:
        v = tool_input.get(f)
        if isinstance(v, str) and v and not _path_is_confined(v):
            return (
                f"ERROR — refused {tool_name}: '{f}'={v!r} is outside the allowed "
                f"project directory. Paths must be within {PROJECT_ROOT} (or "
                f"C:/tmp). This is a PATH problem, not a missing file — do not "
                f"recreate anything. Check the spelling against the path you "
                f"were given, or list the files first."
                + _existing_paths_hint(tool_name)
            )
    return None


def _existing_paths_hint(tool_name: str) -> str:
    if "paginated" not in tool_name:
        return ""
    try:
        from report.paginated_store import all_rdls

        found = all_rdls()
        if found:
            return "\nExisting .rdl files:\n  " + "\n  ".join(found)
    except Exception:
        pass
    return ""


def _pbi_exit_error(tool_name: str, result, output: str) -> str | None:
    if result.returncode == 0:
        return None
    return (
        f"ERROR — tool '{tool_name}' failed (exit {result.returncode}): "
        f"{output or '(no output)'}. Nothing may have been written — "
        f"verify with pbi_visual_list / pbi_report_info before continuing."
    )


def _wire_tooltip_page(
    report_path: str,
    tooltip_page: str,
    source_page: str,
    source_visual: str,
    width: float = 360,
    height: float = 260,
) -> str:
    tp = glob.glob(f"{report_path}/**/pages/{tooltip_page}/page.json", recursive=True)
    if not tp:
        return (
            f"ERROR — tooltip page '{tooltip_page}' not found. Create it with "
            f"pbi_report_add_page (and add its visual) before wiring."
        )
    with open(tp[0], encoding="utf-8") as f:
        page = json.load(f)
    page["type"] = "Tooltip"
    page["displayOption"] = "ActualSize"
    page["visibility"] = "HiddenInViewMode"
    page["width"] = int(width)
    page["height"] = int(height)
    _atomic_write_json(tp[0], page, indent=2)

    sv = glob.glob(
        f"{report_path}/**/pages/{source_page}/visuals/{source_visual}/visual.json",
        recursive=True,
    )
    if not sv:
        return (
            f"ERROR — visual '{source_visual}' not found on page '{source_page}'. "
            f"The tooltip page WAS marked, but the source visual was NOT wired — "
            f"check the name with pbi_visual_list and retry."
        )
    with open(sv[0], encoding="utf-8") as f:
        visual = json.load(f)
    vco = visual.setdefault("visual", {}).setdefault("visualContainerObjects", {})
    vco["visualTooltip"] = [
        {
            "properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "type": {"expr": {"Literal": {"Value": "'Canvas'"}}},
                "section": {"expr": {"Literal": {"Value": f"'{tooltip_page}'"}}},
            }
        }
    ]
    _atomic_write_json(sv[0], visual, indent=2)

    return (
        f"Tooltip wired: page '{tooltip_page}' → Tooltip ({int(width)}x{int(height)}, "
        f"hidden from tabs); visual '{source_visual}' on '{source_page}' now shows it "
        f"on hover. Run pbi_report_health_check, then open Desktop and hover "
        f"'{source_visual}' to confirm it loads."
    )


def _snap_px(value) -> int:
    g = REPORT_LAYOUT["grid_px"]
    return round(float(value) / g) * g


def _require_xywh(tool_name: str, tool_input: dict) -> str | None:
    missing = [k for k in ("x", "y", "width", "height") if tool_input.get(k) is None]
    if missing:
        return f"ERROR — {tool_name} missing required field(s): {missing}."
    return None


def _registered_theme(report_path: str) -> dict:
    from report.dashboard_polish import THEME_NAME

    try:
        for tp in glob.glob(
            f"{report_path}/**/RegisteredResources/{THEME_NAME}.json", recursive=True
        ):
            with open(tp, encoding="utf-8") as tf:
                return json.load(tf)
    except Exception:
        pass
    return {}


_SORT_BY_MEASURE_TYPES = {
    "barChart",
    "clusteredBarChart",
    "stackedBarChart",
    "columnChart",
    "clusteredColumnChart",
    "stackedColumnChart",
    "hundredPercentStackedBarChart",
    "hundredPercentStackedColumnChart",
    "donutChart",
    "pieChart",
    "treemap",
    "funnel",
}
_SORT_BY_CATEGORY_TYPES = {"lineChart", "areaChart", "stackedAreaChart", "ribbonChart"}
_DATE_LIKE_RE = re.compile(r"date|year|month|quarter|week|day", re.IGNORECASE)


def _apply_default_sort(report_path: str, page: str, visual_name: str) -> str:
    matches = glob.glob(
        f"{report_path}/**/pages/{page}/visuals/{visual_name}/visual.json",
        recursive=True,
    )
    if not matches:
        return ""
    try:
        with open(matches[0], encoding="utf-8") as f:
            vdata = json.load(f)
        visual = vdata.get("visual", {})
        vtype = visual.get("visualType", "")
        query = visual.get("query")
        if not isinstance(query, dict) or query.get("sortDefinition"):
            return ""
        qs = query.get("queryState", {})

        def _projections(*roles):
            for role in roles:
                for p in (qs.get(role) or {}).get("projections", []) or []:
                    if isinstance(p.get("field"), dict):
                        yield p["field"]

        cat_field = next(iter(_projections("Category")), None)
        cat_prop = (cat_field or {}).get("Column", {}).get("Property", "")
        cat_is_dateish = bool(cat_prop and _DATE_LIKE_RE.search(cat_prop))

        sort_field, direction = None, "Descending"
        if vtype in _SORT_BY_CATEGORY_TYPES or (
            vtype in _SORT_BY_MEASURE_TYPES and cat_is_dateish
        ):
            sort_field, direction = cat_field, "Ascending"
        elif vtype in _SORT_BY_MEASURE_TYPES:
            sort_field = next(
                (f for f in _projections("Y", "Values") if "Measure" in f), None
            )
        if not sort_field:
            return ""
        query["sortDefinition"] = {
            "sort": [{"field": sort_field, "direction": direction}],
            "isDefaultSort": True,
        }
        _atomic_write_json(matches[0], vdata, indent=2)
        _trace_file_after_write(matches[0], "pbi_visual_add_bound:default-sort")
        target = "category" if direction == "Ascending" else "value"
        return f" [sorted by {target} {direction.lower()}]"
    except Exception as e:
        _build_trace("default_sort_error", visual=visual_name, error=str(e))
        return ""


def _apply_alt_text(
    report_path: str, page: str, visual_name: str, title: str, bindings: list
) -> str:
    matches = glob.glob(
        f"{report_path}/**/pages/{page}/visuals/{visual_name}/visual.json",
        recursive=True,
    )
    if not matches:
        return ""
    try:
        with open(matches[0], encoding="utf-8") as f:
            vdata = json.load(f)
        vco = vdata.setdefault("visual", {}).setdefault("visualContainerObjects", {})
        general = vco.setdefault("general", [{"properties": {}}])
        props = general[0].setdefault("properties", {})
        if props.get("altText"):
            return ""
        fields = ", ".join(
            str(val) for b in (bindings or []) for val in (b or {}).values()
        )
        text = (title or visual_name) + (f" — {fields}" if fields else "")
        text = text.replace("'", "")[:ALT_TEXT_MAX_CHARS]
        props["altText"] = {"expr": {"Literal": {"Value": f"'{text}'"}}}
        _atomic_write_json(matches[0], vdata, indent=2)
        _trace_file_after_write(matches[0], "pbi_visual_add_bound:alt-text")
        return " [alt text set]"
    except Exception as e:
        _build_trace("alt_text_error", visual=visual_name, error=str(e))
        return ""


def _walk_field_refs(node, out: list) -> None:
    if isinstance(node, dict):
        for kind in ("Measure", "Column"):
            sub = node.get(kind)
            if isinstance(sub, dict) and "Property" in sub:
                ent = sub.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
                out.append((kind, ent, sub.get("Property", "")))
        for v in node.values():
            _walk_field_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_field_refs(v, out)


def _pbi_visual_bindings(report_path: str, page: str | None = None) -> str:
    from report.pbir_roles import summarize_roles

    pattern = os.path.join(
        report_path, "**", "pages", "*", "visuals", "*", "visual.json"
    )
    rows: list[str] = []
    for vp in sorted(glob.glob(pattern, recursive=True)):
        parts = vp.replace("\\", "/").split("/")
        try:
            pg = parts[parts.index("pages") + 1]
        except (ValueError, IndexError):
            pg = "?"
        if page and pg != page:
            continue
        try:
            with open(vp, encoding="utf-8") as f:
                vdata = json.load(f)
        except Exception as e:
            rows.append(f"[{pg}] {os.path.basename(os.path.dirname(vp))}: ERROR {e}")
            continue
        vis = vdata.get("visual", {}) or {}
        vtype = vis.get("visualType", "?")
        vname = vdata.get("name") or os.path.basename(os.path.dirname(vp))
        qs = (vis.get("query", {}) or {}).get("queryState", {}) or {}

        rows.append(f"[{pg}] {vname} ({vtype}): {summarize_roles(vtype, qs)}")
    if not rows:
        # Fall back to legacy report reader if PBIR tree is not present
        try:
            from report.legacy_reader import format_bindings
            from report.legacy_reader import read as _read_legacy

            _legacy = _read_legacy(report_path)
            if _legacy is not None:
                return format_bindings(_legacy, page)
        except Exception:
            pass
        _where = f" on page '{page}'" if page else ""
        return f"No visuals found{_where} under {report_path}."
    _scope = f"page '{page}'" if page else "all pages"
    return (
        f"Field bindings — {os.path.basename(report_path)} · {_scope} "
        f"({len(rows)} visual(s)):\n" + "\n".join(rows)
    )
