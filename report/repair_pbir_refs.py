from __future__ import annotations

import glob
import json
import os
import sys
from collections.abc import Iterable

_CARD_LIKE = {"card", "cardVisual", "kpi", "kpiNew", "gauge"}
_SLICER_LIKE = {"slicer", "listSlicer", "textSlicer", "advancedSlicerVisual"}

_MEASURE_ROLES = {
    "Values",
    "Y",
    # Y2 is the combo/line chart's secondary (line) values role, and
    # TargetValue is the gauge's target. Both were missing, so this fallback
    # left their fields unclassified. "LineY"/"ColumnY" are pbi_cli's invented
    # combo roles — kept only so an already-written legacy file is still
    # classified correctly; new binds are normalized to Y/Y2 upstream.
    "Y2",
    "LineY",
    "ColumnY",
    "Indicator",
    "Goal",
    "TargetValue",
    "MaxValue",
    "MinValue",
    "Data",
    "Series",
    "LineValues",
    "ColumnValues",
}
_COLUMN_ROLES = {
    "Category",
    "Rows",
    "Columns",
    "Legend",
    "Trend",
    "TrendLine",
    "Details",
    "Axis",
}


def _flip(node: dict, want_kind: str) -> bool:
    for src_kind in ("Measure", "Column"):
        if src_kind in node and isinstance(node[src_kind], dict):
            if src_kind != want_kind:
                node[want_kind] = node.pop(src_kind)
                return True
            return False
    return False


def _registry_classify(
    entity: str,
    prop: str,
    measure_reg: dict,
    column_reg: dict,
) -> str | None:
    key = f"{entity}.{prop}"
    if key in measure_reg:
        return "Measure"
    if key in column_reg:
        return "Column"
    if any(k == f"*.{prop}" or k.endswith(f".{prop}") for k in measure_reg):
        return "Measure"
    return None


def _fix_projection(
    proj: dict,
    role_name: str,
    visual_type: str,
    measure_reg: dict,
    column_reg: dict,
) -> int:
    field = proj.get("field")
    if not isinstance(field, dict):
        return 0

    entity = prop = ""
    for src_kind in ("Measure", "Column"):
        if src_kind in field and isinstance(field[src_kind], dict):
            entity = (
                field[src_kind]
                .get("Expression", {})
                .get("SourceRef", {})
                .get("Entity", "")
            )
            prop = field[src_kind].get("Property", "")
            break

    if entity and prop:
        kind = _registry_classify(entity, prop, measure_reg, column_reg)
        if kind:
            return 1 if _flip(field, kind) else 0

    if visual_type in _SLICER_LIKE:
        return 1 if _flip(field, "Column") else 0
    if visual_type in _CARD_LIKE and role_name == "Values":
        return 1 if _flip(field, "Measure") else 0

    if role_name in _MEASURE_ROLES:
        return 1 if _flip(field, "Measure") else 0
    if role_name in _COLUMN_ROLES:
        return 1 if _flip(field, "Column") else 0
    return 0


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_MEASURE_REG = os.path.join(_ROOT, ".measure_registry.json")
_DEFAULT_COLUMN_REG = os.path.join(_ROOT, ".column_registry.json")


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def repair_visual_file(
    visual_path: str,
    measure_reg: dict | None = None,
    column_reg: dict | None = None,
) -> dict:
    if measure_reg is None:
        measure_reg = _load_json(_DEFAULT_MEASURE_REG)
    if column_reg is None:
        column_reg = _load_json(_DEFAULT_COLUMN_REG)

    with open(visual_path, encoding="utf-8") as f:
        vdata = json.load(f)

    vis = vdata.get("visual", {})
    visual_type = vis.get("visualType", "")
    qs = vis.get("query", {}).get("queryState", {})
    flips = 0

    for role_name, role_body in qs.items():
        for proj in role_body.get("projections", []) or []:
            flips += _fix_projection(
                proj,
                role_name,
                visual_type,
                measure_reg,
                column_reg,
            )

    if flips:
        with open(visual_path, "w", encoding="utf-8") as f:
            json.dump(vdata, f, indent=2)

    return {
        "path": visual_path,
        "visual_type": visual_type,
        "flips": flips,
        "changed": bool(flips),
    }


def repair_report(report_path: str) -> list[dict]:
    measure_reg = _load_json(_DEFAULT_MEASURE_REG)
    column_reg = _load_json(_DEFAULT_COLUMN_REG)
    print(
        f"  [registry] {len(measure_reg)} measure(s), "
        f"{len(column_reg)} column(s) loaded"
    )
    pattern = os.path.join(report_path, "**", "visuals", "*", "visual.json")
    results: list[dict] = []
    for vp in sorted(glob.glob(pattern, recursive=True)):
        try:
            results.append(repair_visual_file(vp, measure_reg, column_reg))
        except Exception as e:
            results.append(
                {
                    "path": vp,
                    "visual_type": "?",
                    "flips": 0,
                    "changed": False,
                    "error": str(e),
                }
            )
    return results


def _print_summary(results: Iterable[dict]) -> None:
    total = changed = total_flips = 0
    for r in results:
        total += 1
        if r.get("changed"):
            changed += 1
            total_flips += r["flips"]
        name = os.path.basename(os.path.dirname(r["path"]))
        if r.get("error"):
            print(f"  [ERR ] {name:35s} {r['error']}")
        elif r["flips"]:
            print(f"  [FIX ] {name:35s} ({r['visual_type']}) " f"+{r['flips']} flip(s)")
        else:
            print(f"  [ok  ] {name:35s} ({r['visual_type']})")
    print(f"\n{changed} of {total} visuals repaired · {total_flips} ref flip(s)")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        print("\nUsage: python repair_pbir_refs.py <report.Report folder>")
        return 2
    report_path = argv[1]
    if not os.path.isdir(report_path):
        print(f"ERROR — not a directory: {report_path}")
        return 1
    results = repair_report(report_path)
    _print_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
