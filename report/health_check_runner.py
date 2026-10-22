
import os
import subprocess

from fabric.workspace_context import (
    _load_column_registry,
    _load_measure_registry,
    _load_registry_meta,
)
from llm.pbi_tool_specs import PBI_CLI
from model.json_io import _atomic_write_json
from report.scan_cache import (
    _CURRENT_SCAN,
    _get_report_scan,
    _hc_glob,
    _hc_invalidate,
    _hc_read_json,
    _hc_read_text,
)
from util.logger_setup import get_logger

logger = get_logger(__name__)


def _hc_check_pbir_validate(report_path: str) -> list[dict]:
    findings: list[dict] = []
    try:
        proc = subprocess.run(
            [*PBI_CLI, "--json", "report", "--path", report_path, "validate"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
            check=False,
        )
        from report.report_health_checks import interpret_pbir_validator_output

        findings.extend(
            interpret_pbir_validator_output(
                proc.returncode, proc.stdout or "", proc.stderr or ""
            )
        )
    except Exception as e:
        findings.append(
            {
                "check": "pbir_validate",
                "severity": "warning",
                "message": f"validator launch failed: {e}",
            }
        )
    return findings


def _hc_check_pages_coherent(report_path: str) -> tuple[list[dict], int]:
    findings: list[dict] = []
    repairs = 0
    pages_json_path = _hc_glob(f"{report_path}/**/pages/pages.json", recursive=True)
    if not pages_json_path:
        findings.append(
            {
                "check": "pages_coherent",
                "severity": "error",
                "message": "pages/pages.json not found",
            }
        )
        return findings, repairs
    pages_json_path = pages_json_path[0]
    pages_meta = _hc_read_json(pages_json_path)
    page_dir = os.path.dirname(pages_json_path)
    on_disk = sorted(
        {
            os.path.basename(d)
            for d in _hc_glob(f"{page_dir}/*")
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "page.json"))
        }
    )
    declared = list(pages_meta.get("pageOrder", []))
    missing = [p for p in on_disk if p not in declared]
    extra = [p for p in declared if p not in on_disk]
    if missing:
        pages_meta["pageOrder"] = declared + missing
        _atomic_write_json(pages_json_path, pages_meta, indent=2)
        _hc_invalidate(pages_json_path)
        repairs += 1
        findings.append(
            {
                "check": "pages_coherent",
                "severity": "warning",
                "message": f"AUTO-REPAIRED: appended {missing} to pageOrder "
                f"(were on disk but not declared)",
            }
        )
    if extra:
        findings.append(
            {
                "check": "pages_coherent",
                "severity": "error",
                "message": f"pageOrder lists pages not on disk: {extra}. "
                f"Remove or create the missing folder.",
            }
        )
    active = pages_meta.get("activePageName")
    if active and active not in on_disk:
        findings.append(
            {
                "check": "pages_coherent",
                "severity": "error",
                "message": f"activePageName={active!r} but no such page on disk.",
            }
        )
    return findings, repairs


def _hc_check_filter_config(report_path: str) -> tuple[list[dict], int]:
    findings: list[dict] = []
    repairs = 0
    import secrets as _secrets

    for vp in _hc_glob(f"{report_path}/**/visuals/*/visual.json", recursive=True):
        v = _hc_read_json(vp)
        qs = v.get("visual", {}).get("query", {}).get("queryState", {})
        required: list[tuple[str, str, str, dict]] = []
        seen = set()
        for role_name, role_body in qs.items():
            if role_name == "Tooltips":
                continue
            for proj in role_body.get("projections", []) or []:
                field = proj.get("field")
                if not isinstance(field, dict):
                    continue
                kind = next(iter(field), None)
                if kind not in ("Measure", "Column"):
                    continue
                ent = (
                    field[kind]
                    .get("Expression", {})
                    .get("SourceRef", {})
                    .get("Entity", "")
                )
                prop = field[kind].get("Property", "")
                sig = (kind, ent, prop)
                if sig in seen:
                    continue
                seen.add(sig)
                required.append((kind, ent, prop, field))
        if not required:
            continue
        existing = v.get("filterConfig", {}).get("filters", []) or []
        existing_sigs = set()
        for ff in existing:
            fld = ff.get("field", {})
            k = next(iter(fld), None)
            if k:
                e = fld[k].get("Expression", {}).get("SourceRef", {}).get("Entity", "")
                p = fld[k].get("Property", "")
                existing_sigs.add((k, e, p))
        missing = [
            (k, e, p, f) for (k, e, p, f) in required if (k, e, p) not in existing_sigs
        ]
        if missing:
            new_filters = list(existing)
            for kind, ent, prop, field in missing:
                new_filters.append(
                    {
                        "name": _secrets.token_hex(10),
                        "field": {kind: field[kind]},
                        "type": "Advanced" if kind == "Measure" else "Categorical",
                    }
                )
            v["filterConfig"] = {"filters": new_filters}
            _atomic_write_json(vp, v, indent=2)
            _hc_invalidate(vp)
            repairs += 1
            name = os.path.basename(os.path.dirname(vp))
            findings.append(
                {
                    "check": "filter_config",
                    "severity": "warning",
                    "message": f"AUTO-REPAIRED: {name} added {len(missing)} missing filterConfig entry(ies)",
                }
            )
    return findings, repairs


def _hc_check_schema_versions(report_path: str) -> tuple[list[dict], int]:
    findings: list[dict] = []
    repairs = 0
    for vp in _hc_glob(f"{report_path}/**/visuals/*/visual.json", recursive=True):
        try:
            v = _hc_read_json(vp)
            sch = v.get("$schema", "")
            if "/visualContainer/2.7.0/" in sch:
                vtype = v.get("visual", {}).get("visualType", "")
                if vtype not in ("textbox",):
                    v["$schema"] = sch.replace("/2.7.0/", "/2.8.0/")
                    _atomic_write_json(vp, v, indent=2)
                    _hc_invalidate(vp)
                    repairs += 1
                    findings.append(
                        {
                            "check": "schema_versions",
                            "severity": "warning",
                            "message": f"AUTO-REPAIRED: {os.path.basename(os.path.dirname(vp))} bumped visualContainer 2.7.0 → 2.8.0",
                        }
                    )
        except Exception as e:
            findings.append(
                {
                    "check": "schema_versions",
                    "severity": "warning",
                    "message": f"read failed {vp}: {e}",
                }
            )
    rj = _hc_glob(f"{report_path}/**/definition/report.json", recursive=True)
    if rj:
        try:
            rpt = _hc_read_json(rj[0])
            sch = rpt.get("$schema", "")
            if "/report/1.2.0/" in sch:
                findings.append(
                    {
                        "check": "schema_versions",
                        "severity": "error",
                        "message": "report.json uses legacy schema 1.2.0 — Desktop expects 3.2.0+. Re-run pbi_report_create or call dashboard_polish.inject_theme.",
                    }
                )
        except Exception:
            pass
    for pbip in _hc_glob(f"{report_path}/../*.pbip"):
        try:
            pj = _hc_read_json(pbip)
            if "$schema" not in pj:
                findings.append(
                    {
                        "check": "schema_versions",
                        "severity": "error",
                        "message": ".pbip is missing top-level $schema — Desktop may refuse to recognise the artifact.",
                    }
                )
        except Exception:
            pass
    return findings, repairs


def _hc_check_no_legacy_fields(report_path: str) -> list[dict]:
    findings: list[dict] = []
    legacy_markers = (
        "pbiServiceModelId",
        "pbiModelDatabaseName",
        "pbiModelVirtualServerName",
        "pbiServiceXmlaStyleLive",
    )
    pbir = _hc_glob(f"{report_path}/*.pbir") + _hc_glob(
        f"{report_path}/**/definition.pbir", recursive=True
    )
    for p in pbir:
        try:
            txt = _hc_read_text(p)
            hits = [m for m in legacy_markers if m in txt]
            if hits:
                findings.append(
                    {
                        "check": "no_legacy_fields",
                        "severity": "error",
                        "message": f"{os.path.relpath(p, report_path)} still contains legacy fields: {hits}. "
                        f"Use modern semanticmodelid form.",
                    }
                )
        except Exception:
            pass
    return findings


def _hc_check_visual_name_eq_folder(report_path: str) -> list[dict]:
    findings: list[dict] = []
    for vp in _hc_glob(f"{report_path}/**/visuals/*/visual.json", recursive=True):
        try:
            v = _hc_read_json(vp)
            folder = os.path.basename(os.path.dirname(vp))
            name = v.get("name", "")
            if name != folder:
                findings.append(
                    {
                        "check": "visual_name_eq_folder",
                        "severity": "error",
                        "message": f"folder={folder!r} but visual.json name={name!r}. Rename one to match.",
                    }
                )
        except Exception:
            pass
    return findings


def _hc_check_no_apostrophes_in_entity(report_path: str) -> list[dict]:
    findings: list[dict] = []
    for vp in _hc_glob(f"{report_path}/**/visuals/*/visual.json", recursive=True):
        try:
            v = _hc_read_json(vp)
        except Exception as e:
            logger.debug("skipping unreadable %s: %s", vp, e)
            continue

        def walk(node):
            if isinstance(node, dict):
                src = node.get("SourceRef")
                if isinstance(src, dict):
                    ent = src.get("Entity", "")
                    if isinstance(ent, str) and "'" in ent:
                        findings.append(
                            {
                                "check": "no_apostrophes_in_entity",
                                "severity": "error",
                                "message": f"{os.path.basename(os.path.dirname(vp))}: Entity={ent!r} contains apostrophe — strip it.",
                            }
                        )
                for v2 in node.values():
                    walk(v2)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(v)
    return findings


def _hc_check_tooltip_pages(report_path: str) -> list[dict]:
    findings: list[dict] = []
    page_displayopt: dict[str, str] = {}
    for pj in _hc_glob(f"{report_path}/**/pages/*/page.json", recursive=True):
        try:
            page = _hc_read_json(pj)
            page_displayopt[page.get("name", "")] = page.get("displayOption", "")
        except Exception:
            pass
    for vp in _hc_glob(f"{report_path}/**/visuals/*/visual.json", recursive=True):
        try:
            v = _hc_read_json(vp)
            tt = v.get("visual", {}).get("objects", {}).get("tooltip", [])
            if not tt:
                continue
            for entry in tt:
                props = entry.get("properties", {})
                ref = (
                    props.get("canvasTooltipPageName", {})
                    .get("expr", {})
                    .get("Literal", {})
                    .get("Value", "")
                )
                page_name = ref.strip("'") if isinstance(ref, str) else ""
                if not page_name:
                    continue
                if page_name not in page_displayopt:
                    findings.append(
                        {
                            "check": "tooltip_pages",
                            "severity": "error",
                            "message": f"{os.path.basename(os.path.dirname(vp))} references tooltip page {page_name!r} that doesn't exist.",
                        }
                    )
                elif page_displayopt[page_name] != "Tooltip":
                    findings.append(
                        {
                            "check": "tooltip_pages",
                            "severity": "error",
                            "message": f"tooltip page {page_name!r} exists but its displayOption is {page_displayopt[page_name]!r}, must be 'Tooltip'.",
                        }
                    )
        except Exception:
            pass
    return findings


def _hc_check_fill_rules(report_path: str) -> list[dict]:
    findings: list[dict] = []

    def _find_fillrules(node):
        out = []
        if isinstance(node, dict):
            if "Input" in node and "FillRule" in node:
                out.append(node)
            for v in node.values():
                out.extend(_find_fillrules(v))
        elif isinstance(node, list):
            for v in node:
                out.extend(_find_fillrules(v))
        return out

    for vp in _hc_glob(f"{report_path}/**/visuals/*/visual.json", recursive=True):
        vis = os.path.basename(os.path.dirname(vp))
        try:
            v = _hc_read_json(vp)
        except Exception as e:
            logger.debug("skipping unreadable %s: %s", vp, e)
            continue
        valid_refs: set[str] = set()
        qs = ((v.get("visual", {}) or {}).get("query", {}) or {}).get(
            "queryState", {}
        ) or {}
        for body in qs.values():
            for proj in body.get("projections") or []:
                if proj.get("nativeQueryRef"):
                    valid_refs.add(proj["nativeQueryRef"])
                elif proj.get("queryRef"):
                    valid_refs.add(proj["queryRef"])
        objects = (v.get("visual", {}) or {}).get("objects", {}) or {}
        for obj_name, entries in objects.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                fills = _find_fillrules(entry.get("properties", {}))
                if not fills:
                    continue
                meta = (entry.get("selector") or {}).get("metadata")
                if not meta:
                    findings.append(
                        {
                            "check": "fill_rules",
                            "severity": "error",
                            "message": (
                                f"{vis}: conditional format on '{obj_name}' has no "
                                f"selector.metadata — Desktop can't bind it and the "
                                f"report won't open. Re-run pbi_visual_conditional_color."
                            ),
                        }
                    )
                elif valid_refs and meta not in valid_refs:
                    findings.append(
                        {
                            "check": "fill_rules",
                            "severity": "error",
                            "message": (
                                f"{vis}: conditional-format selector.metadata {meta!r} "
                                f"matches no field in the visual (valid: {sorted(valid_refs)}). "
                                f"Use the bare measure name, not Entity.Property. "
                                f"Re-run pbi_visual_conditional_color."
                            ),
                        }
                    )
                for fr in fills:
                    grad = fr.get("FillRule", {}) or {}
                    for gname, stops in grad.items():
                        if not isinstance(stops, dict):
                            continue
                        for stop_name, stop in stops.items():
                            if isinstance(stop, dict) and "solid" in stop:
                                findings.append(
                                    {
                                        "check": "fill_rules",
                                        "severity": "error",
                                        "message": (
                                            f"{vis}: {gname}.{stop_name} stop is wrapped "
                                            f"in 'solid' — FillRule stops must be a bare "
                                            f"{{'color': ...}}. Re-run "
                                            f"pbi_visual_conditional_color."
                                        ),
                                    }
                                )
    return findings


def _hc_check_field_references(report_path: str) -> list[dict]:
    cols = _load_column_registry()
    meas = _load_measure_registry()
    if not cols and not meas:
        return [
            {
                "check": "field_references",
                "severity": "warning",
                "message": "registry empty — run prefetch_metadata to enable field-resolution checking.",
            }
        ]
    meas_props = {k.rsplit(".", 1)[-1] for k in meas}

    refs: list[tuple[str, str, str, str]] = []  # (visual, kind, entity, property)
    parse_findings: list[dict] = []
    for vp in _hc_glob(f"{report_path}/**/visuals/*/visual.json", recursive=True):
        vis = os.path.basename(os.path.dirname(vp))
        try:
            v = _hc_read_json(vp)
        except Exception as _pe:
            parse_findings.append(
                {
                    "check": "field_references",
                    "severity": "error",
                    "message": (
                        f"visual.json for '{vis}' is not valid JSON ({_pe}) - "
                        f"fields cannot be checked and Desktop will fail to "
                        f"load the report."
                    ),
                }
            )
            continue
        qs = ((v.get("visual", {}) or {}).get("query", {}) or {}).get(
            "queryState", {}
        ) or {}
        for role, body in qs.items():
            for proj in body.get("projections") or []:
                field = proj.get("field")
                if not isinstance(field, dict):
                    continue
                kind = next(iter(field), None)
                if kind not in ("Column", "Measure"):
                    continue
                ref = field[kind]
                prop = ref.get("Property", "")
                ent = ((ref.get("Expression") or {}).get("SourceRef") or {}).get(
                    "Entity"
                ) or ""
                if prop and ent:
                    refs.append((f"{vis} (role {role})", kind, ent, prop))

    from report.report_health_checks import registry_matches_report

    registry_entities = {k.split(".")[0] for k in list(cols) + list(meas) if "." in k}
    report_entities = {ent for _v, _k, ent, _p in refs}
    if not registry_matches_report(report_entities, registry_entities):
        loaded = _load_registry_meta().get("model_names") or ["unknown"]
        return parse_findings + [
            {
                "check": "field_references",
                "severity": "warning",
                "message": (
                    f"skipped — the loaded registry is for {', '.join(loaded)}, but this "
                    f"report binds {', '.join(sorted(report_entities)[:4])}: no table in "
                    f"common, so field resolution would be meaningless. Run "
                    f"pbi_refresh_registry with this report's model to check fields."
                ),
            }
        ]

    findings: list[dict] = list(parse_findings)
    for where, kind, ent, prop in refs:
        if f"{ent}.{prop}" in cols or f"{ent}.{prop}" in meas:
            continue
        if kind == "Measure" and prop in meas_props:
            continue
        findings.append(
            {
                "check": "field_references",
                "severity": "error",
                "message": (
                    f"{where}: {kind} {ent}[{prop}] is not in the connected model — "
                    f"Desktop fails with reading 'Column'. Re-bind to a real field "
                    f"or run prefetch_metadata."
                ),
            }
        )
    return findings


def _hc_check_registered_resources(report_path: str) -> list[dict]:
    findings: list[dict] = []
    for rj in _hc_glob(f"{report_path}/**/definition/report.json", recursive=True):
        base = os.path.dirname(os.path.dirname(rj))
        try:
            rpt = _hc_read_json(rj)
        except Exception as e:
            logger.debug("skipping unreadable %s: %s", rj, e)
            continue
        from report.report_health_checks import package_needs_local_files

        for pkg in rpt.get("resourcePackages", []) or []:
            if not package_needs_local_files(pkg):
                continue
            for item in pkg.get("items", []) or []:
                path = (item.get("path", "") or "").replace("\\", "/")
                if not path:
                    continue
                stem = os.path.basename(path)
                hits = _hc_glob(
                    f"{base}/StaticResources/**/{stem}", recursive=True
                ) or _hc_glob(f"{base}/StaticResources/**/{stem}.*", recursive=True)
                if not hits:
                    findings.append(
                        {
                            "check": "registered_resources",
                            "severity": "error",
                            "message": (
                                f"resourcePackages item {item.get('name')!r} path "
                                f"{path!r} has no matching file under StaticResources/ "
                                f"— Desktop will fail to render. Re-run inject_theme."
                            ),
                        }
                    )
    return findings


def _hc_check_filter_name_unique(report_path: str) -> list[dict]:
    # Filter names must be unique across the WHOLE report definition, not just
    # one file. Ported from microsoft/skills-for-fabric (powerbi-report-author
    # validate: PBIR_FILTER_NAME_DUPLICATE_*) — Desktop resolves filters by
    # name, and a duplicate silently shadows its twin.
    findings: list[dict] = []
    seen: dict[str, str] = {}
    sources = _hc_glob(f"{report_path}/**/definition/report.json", recursive=True)
    sources += _hc_glob(f"{report_path}/**/pages/*/page.json", recursive=True)
    sources += _hc_glob(f"{report_path}/**/visuals/*/visual.json", recursive=True)
    for path in sources:
        doc = _hc_read_json(path)
        if not isinstance(doc, dict):
            continue
        for flt in (doc.get("filterConfig") or {}).get("filters", []) or []:
            name = flt.get("name")
            if not name:
                continue
            where = os.path.relpath(path, report_path)
            if name in seen:
                findings.append(
                    {
                        "check": "filter_name_unique",
                        "severity": "error",
                        "message": (
                            f"duplicate filter name {name!r} in {where} "
                            f"(also in {seen[name]}) — filter names are report-wide "
                            "IDs; regenerate one with a fresh hex name"
                        ),
                    }
                )
            else:
                seen[name] = where
    return findings


def _hc_check_format_versions(report_path: str) -> list[dict]:
    # File-content format versions are constants for the current PBIR format
    # (microsoft/skills-for-fabric): version.json -> "2.0.0",
    # definition.pbir -> "4.0". These are NOT the $schema URL versions.
    findings: list[dict] = []
    for vp in _hc_glob(f"{report_path}/**/definition/version.json", recursive=True):
        doc = _hc_read_json(vp)
        ver = doc.get("version") if isinstance(doc, dict) else None
        if ver != "2.0.0":
            findings.append(
                {
                    "check": "format_versions",
                    "severity": "warning",
                    "message": (
                        f"version.json declares version {ver!r} — the current "
                        'PBIR format constant is "2.0.0"'
                    ),
                }
            )
    for dp in _hc_glob(f"{report_path}/**/definition.pbir", recursive=True):
        doc = _hc_read_json(dp)
        ver = doc.get("version") if isinstance(doc, dict) else None
        if ver != "4.0":
            findings.append(
                {
                    "check": "format_versions",
                    "severity": "warning",
                    "message": (
                        f"definition.pbir declares version {ver!r} — the current "
                        'PBIR format constant is "4.0"'
                    ),
                }
            )
    return findings


def _health_check_report(report_path: str) -> dict:
    if not os.path.isdir(report_path):
        return {
            "report_path": report_path,
            "ok": False,
            "errors": [
                {
                    "check": "input",
                    "severity": "error",
                    "message": f"report_path does not exist: {report_path}",
                }
            ],
            "warnings": [],
            "auto_repairs": 0,
            "checks_run": [],
        }
    errors: list[dict] = []
    warnings: list[dict] = []
    repairs_total = 0
    checks_run: list[str] = []

    def _split(findings: list[dict]) -> None:
        for f in findings:
            (errors if f.get("severity") == "error" else warnings).append(f)

    from report.report_health_checks import (
        check_native_visual_types,
        check_report_format_consistent,
        check_report_not_empty,
    )

    scan = _get_report_scan(report_path)
    _scan_tok = _CURRENT_SCAN.set(scan)
    try:
        for name, fn in [
            ("pages_coherent", _hc_check_pages_coherent),
            ("filter_config", _hc_check_filter_config),
            ("schema_versions", _hc_check_schema_versions),
        ]:
            result = fn(report_path)
            if isinstance(result, tuple):
                findings, n = result
                repairs_total += n
            else:
                findings = result
            _split(findings)
            checks_run.append(name)

        for name, fn in [
            ("no_legacy_fields", _hc_check_no_legacy_fields),
            ("visual_name_eq_folder", _hc_check_visual_name_eq_folder),
            ("no_apostrophes_in_entity", _hc_check_no_apostrophes_in_entity),
            ("tooltip_pages", _hc_check_tooltip_pages),
            ("fill_rules", _hc_check_fill_rules),
            ("field_references", _hc_check_field_references),
            ("registered_resources", _hc_check_registered_resources),
            ("pbir_validate", _hc_check_pbir_validate),
            ("report_not_empty", check_report_not_empty),
            ("report_format_consistent", check_report_format_consistent),
            ("filter_name_unique", _hc_check_filter_name_unique),
            ("format_versions", _hc_check_format_versions),
        ]:
            _split(fn(report_path))
            checks_run.append(name)

        _split(check_native_visual_types(report_path, scan))
        checks_run.append("native_visual_types")

        from report.pbir_schema_check import check_pbir_schemas

        _split(check_pbir_schemas(report_path, scan))
        checks_run.append("pbir_schema")
    finally:
        _CURRENT_SCAN.reset(_scan_tok)

    return {
        "report_path": report_path,
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "auto_repairs": repairs_total,
        "checks_run": checks_run,
    }
