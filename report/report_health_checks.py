from __future__ import annotations

import difflib
import json
import os

from config import POWERBI_ALL_NATIVE_VISUAL_IDS


def package_needs_local_files(pkg: dict) -> bool:
    return (pkg.get("type") or pkg.get("name")) != "SharedResources"


def _pages_dir(report_path: str) -> str:
    return os.path.join(report_path, "definition", "pages")


def _page_names(report_path: str) -> list[str]:
    pages = _pages_dir(report_path)
    if not os.path.isdir(pages):
        return []
    return [
        d
        for d in os.listdir(pages)
        if os.path.isfile(os.path.join(pages, d, "page.json"))
    ]


def _visual_count(report_path: str, page_name: str) -> int:
    vis_dir = os.path.join(_pages_dir(report_path), page_name, "visuals")
    if not os.path.isdir(vis_dir):
        return 0
    return sum(
        1
        for v in os.listdir(vis_dir)
        if os.path.isfile(os.path.join(vis_dir, v, "visual.json"))
    )


def _iter_visual_jsons(report_path: str, scan=None, errors: list | None = None):
    pages = _pages_dir(report_path)
    if not os.path.isdir(pages):
        return
    for page_name in os.listdir(pages):
        vis_dir = os.path.join(pages, page_name, "visuals")
        if not os.path.isdir(vis_dir):
            continue
        for vname in os.listdir(vis_dir):
            vpath = os.path.join(vis_dir, vname, "visual.json")
            if not os.path.isfile(vpath):
                continue
            try:
                if scan is not None:
                    yield page_name, vname, scan.read_json(vpath)
                else:
                    with open(vpath, encoding="utf-8") as f:
                        yield page_name, vname, json.load(f)
            except Exception as e:
                if errors is not None:
                    errors.append(
                        {
                            "page": page_name,
                            "visual": vname,
                            "path": vpath,
                            "error": f"{type(e).__name__}: {e}",
                        }
                    )
                continue


def check_report_not_empty(report_path: str) -> list[dict]:
    findings: list[dict] = []
    page_names = _page_names(report_path)

    if not page_names:
        legacy = os.path.join(report_path, "report.json")
        if os.path.isfile(legacy):
            try:
                with open(legacy, encoding="utf-8") as f:
                    if not json.load(f).get("sections", []):
                        findings.append(
                            {
                                "check": "report_not_empty",
                                "severity": "error",
                                "message": "report.json has no sections — the report is empty (no pages/visuals).",
                            }
                        )
                return findings
            except Exception:
                pass
        findings.append(
            {
                "check": "report_not_empty",
                "severity": "error",
                "message": "Report has no pages — nothing to display.",
            }
        )
        return findings

    for page_name in page_names:
        if _visual_count(report_path, page_name) == 0:
            findings.append(
                {
                    "check": "report_not_empty",
                    "severity": "error",
                    "page": page_name,
                    "message": f"Page '{page_name}' has no visuals.",
                }
            )
    return findings


def registry_matches_report(report_tables: set[str], registry_tables: set[str]) -> bool:
    if not report_tables:
        return True  # nothing to check; let the caller proceed
    return bool(report_tables & registry_tables)


def check_report_format_consistent(report_path: str) -> list[dict]:
    pbir = os.path.join(report_path, "definition.pbir")
    if not os.path.isfile(pbir):
        return []
    try:
        with open(pbir, encoding="utf-8-sig") as f:
            version = str(json.load(f).get("version", ""))
    except Exception:
        return []

    is_pbir = version.split(".")[0].isdigit() and int(version.split(".")[0]) >= 4
    has_definition = os.path.isdir(os.path.join(report_path, "definition"))
    has_legacy = os.path.isfile(os.path.join(report_path, "report.json"))

    if is_pbir and not has_definition:
        return [
            {
                "check": "report_format_consistent",
                "severity": "error",
                "message": (
                    f"definition.pbir declares PBIR (version {version}) but there is no "
                    f"definition/ folder"
                    + (
                        " — the body is a legacy root report.json. Enable "
                        "'Store reports using enhanced metadata format (PBIR)' in "
                        "Power BI Desktop preview features and re-save."
                        if has_legacy
                        else " and no report body at all."
                    )
                ),
            }
        ]
    return []


def check_native_visual_types(report_path: str, scan=None) -> list[dict]:
    findings: list[dict] = []
    parse_errors: list[dict] = []
    for page_name, vname, vjson in _iter_visual_jsons(
        report_path, scan, errors=parse_errors
    ):
        vtype = (vjson.get("visual", {}) or {}).get("visualType", "")
        if vtype and vtype not in POWERBI_ALL_NATIVE_VISUAL_IDS:
            findings.append(
                {
                    "check": "native_visual_types",
                    "severity": "error",
                    "page": page_name,
                    "visual": vname,
                    "visual_type": vtype,
                    "message": (
                        f"visualType '{vtype}' on '{vname}' is not a native Power BI visual — "
                        f"Desktop will fail to load (CustomVisualNotFound)."
                    ),
                }
            )
    for pe in parse_errors:
        findings.append(
            {
                "check": "visual_json_parse",
                "severity": "error",
                "page": pe["page"],
                "visual": pe["visual"],
                "message": (
                    f"visual.json for '{pe['visual']}' on page '{pe['page']}' is not "
                    f"valid JSON ({pe['error']}) - Desktop will fail to load the report."
                ),
            }
        )
    return findings


def interpret_pbir_validator_output(
    returncode: int, stdout: str, stderr: str = ""
) -> list[dict]:
    findings: list[dict] = []
    out = (stdout or "").strip()
    if returncode != 0 and not out:
        findings.append(
            {
                "check": "pbir_validate",
                "severity": "error",
                "message": f"pbi report validate exited {returncode}: "
                f"{(stderr or '').strip()[:200]}",
            }
        )
        return findings
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        if returncode != 0:
            findings.append(
                {
                    "check": "pbir_validate",
                    "severity": "error",
                    "message": (
                        f"validator output unusable - exited {returncode} with "
                        f"non-JSON stdout: {out[:200]}"
                    ),
                }
            )
        else:
            findings.append(
                {
                    "check": "pbir_validate",
                    "severity": "warning",
                    "message": (
                        f"validator returned non-JSON output - result unknown: "
                        f"{out[:200]}"
                    ),
                }
            )
        return findings
    if not isinstance(data, dict) or "valid" not in data:
        findings.append(
            {
                "check": "pbir_validate",
                "severity": "warning",
                "message": "validator output missing valid flag - result unknown",
            }
        )
        return findings
    if not data.get("valid", True):
        errors = data.get("errors", []) or []
        for err in errors:
            findings.append(
                {
                    "check": "pbir_validate",
                    "severity": "error",
                    "message": str(err)[:400],
                }
            )
        if not errors:
            findings.append(
                {
                    "check": "pbir_validate",
                    "severity": "error",
                    "message": "validator reported valid=false with no error details",
                }
            )
    return findings


def validate_registry_field(
    measure_reg: dict, column_reg: dict, table: str, measure: str | None = None
) -> tuple[str | None, str]:
    if not measure_reg and not column_reg:
        return None, " (field not verified - model metadata not loaded)"

    known_tables = {k.split(".", 1)[0] for k in column_reg if "." in k} | {
        k.split(".", 1)[0] for k in measure_reg if "." in k and not k.startswith("*.")
    }
    if measure is None:
        if table in known_tables:
            return None, ""
        hints = difflib.get_close_matches(table, sorted(known_tables), n=3, cutoff=0.5)
        hint = f" Close matches: {', '.join(hints)}." if hints else ""
        return (
            (
                f"ERROR - refused to write: table '{table}' does not exist in the "
                f"connected model.{hint} Use a table name that appears VERBATIM in "
                f"the model metadata ({len(known_tables)} table(s) in the registry)."
            ),
            "",
        )

    key = f"{table}.{measure}"
    bare_measure_names = {k.split(".", 1)[1] for k in measure_reg if k.startswith("*.")}
    if (
        key in measure_reg
        or key in column_reg
        or measure in bare_measure_names
        or any(k.endswith(f".{measure}") for k in measure_reg)
    ):
        return None, ""
    all_measures = sorted({k.split(".", 1)[1] for k in measure_reg if "." in k})
    hints = difflib.get_close_matches(measure, all_measures, n=3, cutoff=0.5)
    hint = f" Close matches: {', '.join(hints)}." if hints else ""
    return (
        (
            f"ERROR - refused to write: measure '{table}'[{measure}] does not exist "
            f"in the connected model.{hint} Call dax_query_operations on "
            f"INFO.MEASURES() to see the actual schema, then retry with VERBATIM "
            f"names. The registry has {len(column_reg)} column(s) and "
            f"{len(measure_reg)} measure(s) loaded."
        ),
        "",
    )


def validate_extension_measure(
    ext: dict | None, entity: str, measure: str
) -> str | None:
    entities = (ext or {}).get("entities") or []
    if not entities:
        return (
            f"ERROR - refused to write: '{entity}'[{measure}] is marked "
            "from_extension=true but this report has no extension measures. "
            "Create it with pbi_add_svg_measure first, or pass "
            "from_extension=false for a model measure."
        )
    for ent in entities:
        if ent.get("name") == entity and any(
            m.get("name") == measure for m in ent.get("measures") or []
        ):
            return None
    available = [
        f"{ent.get('name', '?')}[{m.get('name', '')}]"
        for ent in entities
        for m in ent.get("measures") or []
        if m.get("name")
    ]
    hints = difflib.get_close_matches(
        f"{entity}[{measure}]", available, n=3, cutoff=0.5
    )
    hint = f" Close matches: {', '.join(hints)}." if hints else ""
    return (
        f"ERROR - refused to write: extension measure '{entity}'[{measure}] is not "
        f"in reportExtensions.json.{hint} Available: {', '.join(available[:12])}. "
        "Create it with pbi_add_svg_measure or use an existing name VERBATIM."
    )
