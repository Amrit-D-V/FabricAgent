import glob
import json
import os
import re
from itertools import pairwise

from config import (
    ALT_TEXT_MAX_CHARS,
    CVD_AUDIT,
    DESIGN_BUDGETS,
    REPORT_DENSITY,
    REPORT_LAYOUT,
    SLICER_MAX_Y_FRAC,
    TOP_LEFT_REGION,
    WCAG_TEXT_CONTRAST_MIN,
)
from fabric.workspace_context import (
    _load_column_registry,
    _load_measure_registry,
    _load_registry_meta,
)
from model.build_trace import _build_trace
from report.pbir_roles import known_roles, unsupported_roles
from model.tool_guards import (
    _DATE_LIKE_RE,
    _SORT_BY_MEASURE_TYPES,
    _registered_theme,
)
from util.logger_setup import get_logger

logger = get_logger(__name__)


def _allowed_field_keys(report_path: str) -> set[str] | None:
    try:
        m_reg = _load_measure_registry()
        c_reg = _load_column_registry()
    except Exception:
        return None
    keys = {k for k in list(m_reg) + list(c_reg) if not k.startswith("_")}
    if not keys:
        return None
    meta_models: set[str] = set(_load_registry_meta().get("model_names") or [])

    catalog = ""
    for pbir in glob.glob(f"{report_path}/**/definition.pbir", recursive=True):
        try:
            with open(pbir, encoding="utf-8") as f:
                conn = (
                    json.load(f)
                    .get("datasetReference", {})
                    .get("byConnection", {})
                    .get("connectionString", "")
                )
            m = re.search(r'initial catalog="([^"]+)"', conn, re.IGNORECASE)
            if m:
                catalog = m.group(1)
                break
        except Exception as e:
            logger.debug("skipping unreadable %s: %s", pbir, e)
            continue
    if not catalog or catalog not in meta_models:
        return None

    # Both spellings: PBIR writes "reportExtension.json" (singular), older
    # reports use the plural. Globbing only the plural meant SVG/extension
    # measures never entered the allowed-field catalog, so every visual bound
    # to one was reported as broken_binding — a false error on a correct report.
    _ext_files = glob.glob(f"{report_path}/**/reportExtension.json", recursive=True)
    _ext_files += glob.glob(f"{report_path}/**/reportExtensions.json", recursive=True)
    for ext in _ext_files:
        try:
            with open(ext, encoding="utf-8") as f:
                for ent in json.load(f).get("entities", []) or []:
                    for ms in ent.get("measures", []) or []:
                        if ent.get("name") and ms.get("name"):
                            keys.add(f"{ent['name']}.{ms['name']}")
        except Exception as e:
            logger.debug("skipping unreadable %s: %s", ext, e)
            continue
    return keys


def _hex_rgb(h: str) -> tuple[int, int, int] | None:
    h = (h or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return None


def _rel_luminance(h: str) -> float | None:
    rgb = _hex_rgb(h)
    if rgb is None:
        return None

    def _lin(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (_lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(h1: str, h2: str) -> float | None:
    l1, l2 = _rel_luminance(h1), _rel_luminance(h2)
    if l1 is None or l2 is None:
        return None
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _hue_deg(h: str) -> float | None:
    rgb = _hex_rgb(h)
    if rgb is None:
        return None
    import colorsys

    return colorsys.rgb_to_hls(*(c / 255.0 for c in rgb))[0] * 360.0


def _audit_theme_colors(report_path: str) -> list[dict]:
    theme = _registered_theme(report_path)
    if not theme:
        return []
    warnings: list[dict] = []

    card_bg = "#FFFFFF"
    try:
        card_bg = theme["visualStyles"]["*"]["*"]["background"][0]["color"]["solid"][
            "color"
        ]
    except (KeyError, IndexError, TypeError):
        pass
    for cls, spec in (theme.get("textClasses") or {}).items():
        color = (spec or {}).get("color")
        ratio = _contrast_ratio(color, card_bg) if color else None
        if ratio is not None and ratio < WCAG_TEXT_CONTRAST_MIN:
            warnings.append(
                {
                    "check": "text_contrast",
                    "text_class": cls,
                    "color": color,
                    "against": card_bg,
                    "ratio": round(ratio, 2),
                    "min": WCAG_TEXT_CONTRAST_MIN,
                    "fix": f"darken this text color or lighten the background — WCAG 2.1 requires {WCAG_TEXT_CONTRAST_MIN}:1 for report text",
                }
            )

    data_colors = [c for c in (theme.get("dataColors") or []) if isinstance(c, str)]
    for a, b in pairwise(data_colors):
        la, lb = _rel_luminance(a), _rel_luminance(b)
        ha, hb = _hue_deg(a), _hue_deg(b)
        if la is None or lb is None or ha is None or hb is None:
            continue
        hue_gap = min(abs(ha - hb), 360 - abs(ha - hb))
        if (
            abs(la - lb) < CVD_AUDIT["lightness_delta_min"]
            and hue_gap < CVD_AUDIT["hue_gap_min_deg"]
        ):
            warnings.append(
                {
                    "check": "palette_lightness",
                    "colors": [a, b],
                    "fix": "consecutive series colors must differ in lightness or hue — this pair merges in grayscale (color-blind unsafe)",
                }
            )

    reds: list[str] = []
    greens: list[str] = []
    for c in data_colors:
        h = _hue_deg(c)
        if h is None:
            continue
        if h < CVD_AUDIT["red_hue_max_deg"] or h > 360 - CVD_AUDIT["red_hue_max_deg"]:
            reds.append(c)
        elif CVD_AUDIT["green_hue_min_deg"] <= h <= CVD_AUDIT["green_hue_max_deg"]:
            greens.append(c)
    for r_c in reds:
        for g_c in greens:
            lr, lg = _rel_luminance(r_c), _rel_luminance(g_c)
            if (
                lr is not None
                and lg is not None
                and abs(lr - lg) < CVD_AUDIT["red_green_lum_delta_min"]
            ):
                warnings.append(
                    {
                        "check": "red_green_palette",
                        "colors": [r_c, g_c],
                        "fix": "remove red/green from the series rotation (reserve them for good/bad variance semantics) or separate them strongly in lightness",
                    }
                )

    return warnings


def _value_axis_start(v: dict) -> float | None:
    try:
        for obj in v.get("visual", {}).get("objects", {}).get("valueAxis", []):
            expr = obj.get("properties", {}).get("start", {}).get("expr", {})
            raw = expr.get("Literal", {}).get("Value")
            if raw is not None:
                return float(str(raw).rstrip("DLdl"))
    except (TypeError, ValueError, AttributeError):
        return None
    return None


def _audit_dashboard(report_path: str) -> dict:
    from report.layout_engine import (
        canvas_coverage as _layout_coverage,
    )
    from report.layout_engine import (
        is_under_filled as _layout_under_filled,
    )
    from report.visual_sizing import (
        SLICER_VISUAL_TYPES as _SLICER_VISUAL_TYPES,
    )
    from report.visual_sizing import (
        classify_visual as _classify_visual,
    )
    from report.visual_sizing import (
        estimate_min_size as _estimate_min_size,
    )
    from report.visual_sizing import (
        sizing_spec_from_pbir_visual as _sizing_spec_from_pbir_visual,
    )

    result: dict = {"pages": {}, "summary": {"errors": 0, "warnings": 0}}
    pages_dir = os.path.join(report_path, "definition", "pages")
    if not os.path.isdir(pages_dir):
        for candidate in glob.glob(f"{report_path}/**/pages", recursive=True):
            if os.path.isdir(candidate):
                pages_dir = candidate
                break
    if not os.path.isdir(pages_dir):
        return {"error": f"pages directory not found under {report_path}"}

    allowed_fields = _allowed_field_keys(report_path)

    # _allowed_field_keys returns None when the registry belongs to a different
    # model than the report — which switches the broken_binding check off
    # entirely. Silently: the audit reported "0 errors" while simply not
    # looking. Say it out loud, so a clean result means "checked and clean"
    # rather than "did not check".
    from report.model_identity import registry_mismatch

    _reg_mismatch = registry_mismatch(report_path)
    if _reg_mismatch:
        result["registry"] = {
            "check": "registry_model_mismatch",
            "detail": _reg_mismatch,
            "consequence": (
                "field-existence checking (broken_binding) is DISABLED for this "
                "audit — a clean result here does not mean the bindings are valid"
            ),
        }

    for page_name in sorted(os.listdir(pages_dir)):
        page_folder = os.path.join(pages_dir, page_name)
        page_json_path = os.path.join(page_folder, "page.json")
        if not os.path.isfile(page_json_path):
            continue
        try:
            with open(page_json_path, encoding="utf-8") as f:
                page = json.load(f)
        except Exception as e:
            result["pages"][page_name] = {
                "errors": [{"check": "page_json_parse", "detail": str(e)}],
                "warnings": [],
            }
            result["summary"]["errors"] += 1
            continue
        page_w = float(page.get("width", 1280))
        page_h = float(page.get("height", 720))

        visuals = []
        _bad_visuals: list[tuple[str, str]] = []
        visuals_dir = os.path.join(page_folder, "visuals")
        if os.path.isdir(visuals_dir):
            for v_name in sorted(os.listdir(visuals_dir)):
                v_path = os.path.join(visuals_dir, v_name, "visual.json")
                if not os.path.isfile(v_path):
                    continue
                try:
                    with open(v_path, encoding="utf-8") as f:
                        visuals.append(json.load(f))
                except Exception as e:
                    _bad_visuals.append((v_name, str(e)))
                    continue

        errors: list = []
        warnings: list = []

        for _bv_name, _bv_err in _bad_visuals:
            errors.append(
                {
                    "check": "visual_json_parse",
                    "visual": _bv_name,
                    "detail": _bv_err,
                }
            )

        if str(page.get("displayName", "")).strip().lower().startswith("page "):
            warnings.append(
                {
                    "check": "page_default_name",
                    "display_name": page.get("displayName"),
                    "fix": "rename the page to its metric/question (no numbers - static titles go stale) — default 'Page N' names read as unfinished",
                }
            )

        banner = next((v for v in visuals if v.get("name") == "header_banner"), None)
        if banner:
            bw = float(banner.get("position", {}).get("width", 0))
            if abs(bw - page_w) > 1:
                errors.append(
                    {
                        "check": "banner_width_mismatch",
                        "visual": "header_banner",
                        "expected_width": page_w,
                        "actual_width": bw,
                        "fix": f"call pbi_visual_update visual_name=header_banner page={page_name} width={int(page_w)}",
                    }
                )

        for v in visuals:
            pos = v.get("position", {})
            name = v.get("name", "?")
            x, y = float(pos.get("x", 0)), float(pos.get("y", 0))
            w, h = float(pos.get("width", 0)), float(pos.get("height", 0))
            if w <= 0 or h <= 0:
                errors.append(
                    {
                        "check": "invalid_dimensions",
                        "visual": name,
                        "width": w,
                        "height": h,
                        "fix": f"{name} has non-positive dimensions ({w}x{h}) — Desktop will fail to render it; set a positive width/height",
                    }
                )
                continue
            if x + w > page_w + 1:
                errors.append(
                    {
                        "check": "off_page_right",
                        "visual": name,
                        "right_edge": round(x + w, 2),
                        "page_width": page_w,
                        "fix": f"reduce {name}.width or shift x left",
                    }
                )
            if y + h > page_h + 1:
                errors.append(
                    {
                        "check": "off_page_bottom",
                        "visual": name,
                        "bottom_edge": round(y + h, 2),
                        "page_height": page_h,
                        "fix": f"reduce {name}.height or shift y up",
                    }
                )

        non_banner = [v for v in visuals if v.get("name") != "header_banner"]
        for i, a in enumerate(non_banner):
            pa = a.get("position", {})
            ax1, ay1 = float(pa.get("x", 0)), float(pa.get("y", 0))
            ax2, ay2 = ax1 + float(pa.get("width", 0)), ay1 + float(pa.get("height", 0))
            for b in non_banner[i + 1 :]:
                pb = b.get("position", {})
                bx1, by1 = float(pb.get("x", 0)), float(pb.get("y", 0))
                bx2, by2 = (
                    bx1 + float(pb.get("width", 0)),
                    by1 + float(pb.get("height", 0)),
                )
                ix = min(ax2, bx2) - max(ax1, bx1)
                iy = min(ay2, by2) - max(ay1, by1)
                if ix > 1 and iy > 1:
                    errors.append(
                        {
                            "check": "overlap",
                            "visuals": [a.get("name"), b.get("name")],
                            "overlap_px": {"x": round(ix, 2), "y": round(iy, 2)},
                            "fix": f"reposition {b.get('name')} so its rectangle doesn't intersect {a.get('name')}",
                        }
                    )

        slicers = [
            v
            for v in visuals
            if v.get("visual", {}).get("visualType") in _SLICER_VISUAL_TYPES
        ]
        rows: list[list[dict]] = []
        for s in slicers:
            sy = float(s.get("position", {}).get("y", 0))
            for row in rows:
                if abs(float(row[0].get("position", {}).get("y", 0)) - sy) < 5:
                    row.append(s)
                    break
            else:
                rows.append([s])
        for s in slicers:
            _sy = float(s.get("position", {}).get("y", 0))
            if _sy > page_h * SLICER_MAX_Y_FRAC:
                warnings.append(
                    {
                        "check": "slicer_below_content",
                        "visual": s.get("name"),
                        "y": round(_sy, 2),
                        "fix": f"move {s.get('name')} into the top filter strip or a side rail — a filter in the bottom half of the canvas goes unnoticed",
                    }
                )
            _sh = float(s.get("position", {}).get("height", 0))
            if 0 < _sh < REPORT_LAYOUT["slicer_min_px"]:
                errors.append(
                    {
                        "check": "slicer_too_short",
                        "visual": s.get("name"),
                        "height": _sh,
                        "min_px": REPORT_LAYOUT["slicer_min_px"],
                        "fix": f"grow {s.get('name')} to ≥{REPORT_LAYOUT['slicer_min_px']}px — Desktop renders thinner slicers as a blank strip",
                    }
                )

        _tl_types = {
            "slicer",
            "listSlicer",
            "textSlicer",
            "advancedSlicerVisual",
            "actionButton",
            "image",
        }
        for v in visuals:
            if v.get("visual", {}).get("visualType", "") not in _tl_types:
                continue
            _px = float(v.get("position", {}).get("x", 0))
            _py = float(v.get("position", {}).get("y", 0))
            if (
                _px < page_w * TOP_LEFT_REGION["x_frac"]
                and _py < page_h * TOP_LEFT_REGION["y_frac"]
            ):
                warnings.append(
                    {
                        "check": "top_left_misuse",
                        "visual": v.get("name"),
                        "visual_type": v.get("visual", {}).get("visualType", ""),
                        "fix": f"move {v.get('name')} out of the top-left — that region belongs to the most important KPI/insight, not chrome or filters",
                    }
                )

        for row in rows:
            if len(row) < 2:
                continue
            heights = {
                s.get("name"): float(s.get("position", {}).get("height", 0))
                for s in row
            }
            if len(set(heights.values())) > 1:
                target = max(heights.values())
                warnings.append(
                    {
                        "check": "slicer_row_height_mismatch",
                        "row_y": round(
                            float(row[0].get("position", {}).get("y", 0)), 2
                        ),
                        "heights": {k: round(v, 2) for k, v in heights.items()},
                        "fix": f"set every slicer in this row to height={target}",
                    }
                )

        for v in visuals:
            vis = v.get("visual", {})
            vtype = vis.get("visualType", "")
            if vtype in (
                "textbox",
                "image",
                "shape",
                "actionButton",
                "pageNavigator",
                "",
            ):
                continue
            query_state = vis.get("query", {}).get("queryState", {})
            total = sum(
                len(role.get("projections", []))
                for role in query_state.values()
                if isinstance(role, dict)
            )
            if total == 0:
                errors.append(
                    {
                        "check": "empty_binding",
                        "visual": v.get("name"),
                        "visual_type": vtype,
                        "fix": f"call pbi_visual_bind to add fields to {v.get('name')}, or pbi_visual_delete to remove it",
                    }
                )

            # A visual can be fully bound and still render empty: Power BI
            # discards roles it does not recognise instead of erroring, so a
            # measure in a misspelled or invented role counts toward `total`
            # above while contributing nothing on screen. That is invisible to
            # every other check here, and was the defect behind every blank
            # combo chart, ungrouped treemap and target-less gauge this repo
            # has shipped.
            for bad_role in unsupported_roles(vtype, query_state):
                valid = known_roles(vtype) or set()
                errors.append(
                    {
                        "check": "unsupported_role",
                        "visual": v.get("name"),
                        "visual_type": vtype,
                        "role": bad_role,
                        "detail": (
                            f"'{bad_role}' is not a data role of {vtype}; Power BI "
                            f"drops it and renders the visual empty"
                        ),
                        "fix": (
                            f"re-bind {v.get('name')} using one of {sorted(valid)}"
                        ),
                    }
                )

        def _vtype(v: dict) -> str:
            return v.get("visual", {}).get("visualType", "")

        decorative = {"textbox", "image", "shape", "actionButton", "pageNavigator", ""}
        data_visuals = [v for v in non_banner if _vtype(v) not in decorative]

        card_names = [
            v.get("name")
            for v in data_visuals
            if _vtype(v) in ("card", "cardVisual", "kpi")
        ]
        if len(card_names) > DESIGN_BUDGETS["max_cards"]:
            warnings.append(
                {
                    "check": "kpi_carpet_bombing",
                    "card_count": len(card_names),
                    "max": DESIGN_BUDGETS["max_cards"],
                    "visuals": card_names,
                    "fix": f"keep the {DESIGN_BUDGETS['max_cards']} decision-driving KPIs; delete or merge the rest",
                }
            )

        card_visuals = [
            v for v in data_visuals if _vtype(v) in ("card", "cardVisual", "kpi")
        ]
        card_rows: list[list[dict]] = []
        for c in card_visuals:
            cy = float(c.get("position", {}).get("y", 0))
            for row in card_rows:
                if abs(float(row[0].get("position", {}).get("y", 0)) - cy) < 5:
                    row.append(c)
                    break
            else:
                card_rows.append([c])
        for row in card_rows:
            if len(row) < 2:
                continue
            dims = {
                c.get("name"): (
                    float(c.get("position", {}).get("width", 0)),
                    float(c.get("position", {}).get("height", 0)),
                )
                for c in row
            }
            ws = {d[0] for d in dims.values()}
            hs = {d[1] for d in dims.values()}
            if max(ws) - min(ws) > REPORT_LAYOUT["grid_px"] or max(hs) - min(hs) > 1:
                warnings.append(
                    {
                        "check": "kpi_row_size_mismatch",
                        "cards": {
                            k: {"width": d[0], "height": d[1]} for k, d in dims.items()
                        },
                        "fix": "make every KPI card in this row the same width and height (use pbi_layout_compute with equal weights)",
                    }
                )

        if len(slicers) > DESIGN_BUDGETS["max_slicers"]:
            warnings.append(
                {
                    "check": "slicer_wall",
                    "slicer_count": len(slicers),
                    "max": DESIGN_BUDGETS["max_slicers"],
                    "fix": "keep the key slicers; delete the rest (users still have the filter pane)",
                }
            )

        if len(data_visuals) > DESIGN_BUDGETS["max_visuals"]:
            warnings.append(
                {
                    "check": "visual_overload",
                    "visual_count": len(data_visuals),
                    "max": DESIGN_BUDGETS["max_visuals"],
                    "fix": "move detail visuals to a drill/detail page",
                }
            )

        pie_names = [
            v.get("name")
            for v in data_visuals
            if _vtype(v) in ("pieChart", "donutChart")
        ]
        if len(pie_names) > 1:
            warnings.append(
                {
                    "check": "pie_repetition",
                    "visuals": pie_names,
                    "fix": "keep ONE donut/pie per page; convert the others to a sorted bar or treemap",
                }
            )

        combo_types = ("lineStackedColumnComboChart", "lineClusteredColumnComboChart")
        bar_types = (
            "barChart",
            "clusteredBarChart",
            "columnChart",
            "clusteredColumnChart",
            "stackedBarChart",
            "stackedColumnChart",
            "hundredPercentStackedBarChart",
            "hundredPercentStackedColumnChart",
        )
        for v in data_visuals:
            vtype = _vtype(v)
            name = v.get("name", "?")
            if vtype == "gauge":
                warnings.append(
                    {
                        "check": "gauge_visual",
                        "visual": name,
                        "fix": f"replace {name} with a cardVisual plus a small line chart — a gauge spends a large area on one number with no trend",
                    }
                )
            if vtype not in combo_types and '"secondaryYAxis"' in json.dumps(
                v.get("visual", {}).get("objects", {})
            ):
                warnings.append(
                    {
                        "check": "dual_y_axis",
                        "visual": name,
                        "fix": f"split {name} into two charts stacked vertically sharing the x-axis",
                    }
                )
            if vtype in bar_types:
                start = _value_axis_start(v)
                if start is not None and start != 0.0:
                    warnings.append(
                        {
                            "check": "truncated_bar_baseline",
                            "visual": name,
                            "axis_start": start,
                            "fix": f"set {name}'s value-axis start to 0 — a non-zero baseline exaggerates differences",
                        }
                    )
            _vh = float(v.get("position", {}).get("height", 0))
            if (
                vtype not in ("card", "cardVisual", "kpi", "multiRowCard", "slicer")
                and _vh < REPORT_LAYOUT["chart_min_px"]
            ):
                warnings.append(
                    {
                        "check": "chart_too_short",
                        "visual": name,
                        "height": _vh,
                        "min_px": REPORT_LAYOUT["chart_min_px"],
                        "fix": f"grow {name} to ≥{REPORT_LAYOUT['chart_min_px']}px tall or move it out of the KPI band — a squashed chart is unreadable",
                    }
                )

            # Width minimum check based on visual sizing estimation
            _vw_check = float(v.get("position", {}).get("width", 0))
            _vclass = _classify_visual({"type": vtype})
            _min_w, _ = _estimate_min_size(_sizing_spec_from_pbir_visual(v), _vclass)
            if vtype not in (
                "card",
                "cardVisual",
                "kpi",
                "multiRowCard",
                "slicer",
            ) and (0 < _vw_check < _min_w):
                _extra = ""
                if _vclass in ("table", "matrix"):
                    _cols = _sizing_spec_from_pbir_visual(v)["column_count"]
                    _extra = f" ({_cols} columns)"
                warnings.append(
                    {
                        "check": "visual_too_narrow",
                        "visual": name,
                        "visual_type": vtype,
                        "width": _vw_check,
                        "min_px": _min_w,
                        "fix": f"grow {name}{_extra} to ≥{_min_w}px wide — content clips or is unreadable below this width",
                    }
                )
            _q = v.get("visual", {}).get("query", {})
            if (
                vtype in _SORT_BY_MEASURE_TYPES
                and isinstance(_q, dict)
                and not _q.get("sortDefinition")
            ):
                _cats = [
                    (p.get("field") or {}).get("Column", {}).get("Property", "")
                    for p in (
                        (_q.get("queryState", {}).get("Category") or {}).get(
                            "projections"
                        )
                        or []
                    )
                ]
                if not any(_DATE_LIKE_RE.search(c) for c in _cats if c):
                    warnings.append(
                        {
                            "check": "unsorted_chart",
                            "visual": name,
                            "fix": f"{name} has no sortDefinition — ranked charts sort by their value descending (pbi_visual_add_bound writes this automatically)",
                        }
                    )
            _title_entries = (
                v.get("visual", {}).get("visualContainerObjects", {}).get("title") or []
            )
            _has_title = any(
                str(
                    (e.get("properties") or {})
                    .get("text", {})
                    .get("expr", {})
                    .get("Literal", {})
                    .get("Value", "")
                )
                .strip("'")
                .strip()
                for e in _title_entries
            )
            if not _has_title:
                warnings.append(
                    {
                        "check": "missing_title",
                        "visual": name,
                        "fix": f"call pbi_visual_set_container visual_name={name} page={page_name} title=<the insight/metric>",
                    }
                )
            _qs = v.get("visual", {}).get("query", {}).get("queryState", {})
            _proj_fields: list[str] = []
            for _role in _qs.values():
                for _p in (_role or {}).get("projections", []) or []:
                    _f = _p.get("field")
                    if not isinstance(_f, dict):
                        continue
                    _kind = next(iter(_f), None)
                    if _kind not in ("Measure", "Column"):
                        continue
                    _ent = (
                        _f[_kind]
                        .get("Expression", {})
                        .get("SourceRef", {})
                        .get("Entity", "")
                    )
                    _prop = _f[_kind].get("Property", "")
                    if _ent and _prop:
                        _proj_fields.append(f"{_ent}.{_prop}")
            if len(_proj_fields) > DESIGN_BUDGETS["max_fields_per_visual"]:
                warnings.append(
                    {
                        "check": "too_many_fields",
                        "visual": name,
                        "fields": len(_proj_fields),
                        "max": DESIGN_BUDGETS["max_fields_per_visual"],
                        "fix": f"{name} carries {len(_proj_fields)} fields — split it or move detail to a drill page",
                    }
                )
            if vtype in ("card", "cardVisual") and len(_proj_fields) == 1:
                warnings.append(
                    {
                        "check": "kpi_no_context",
                        "visual": name,
                        "fix": f"a bare number answers nothing — bind a reference value (target/PY) as a second field on {name}, or use the kpi visual (indicator+trend+goal)",
                    }
                )
            if allowed_fields:
                for _fk in _proj_fields:
                    _prop_only = _fk.split(".", 1)[1]
                    if (
                        _fk not in allowed_fields
                        and f"*.{_prop_only}" not in allowed_fields
                    ):
                        errors.append(
                            {
                                "check": "broken_binding",
                                "visual": name,
                                "field": _fk,
                                "fix": f"{_fk} does not exist in the model — rebind {name} to a real field (verify via metadata/INFO functions) or delete the visual",
                            }
                        )
            _vw = float(v.get("position", {}).get("width", 0))
            if (
                vtype in bar_types + ("lineChart", "areaChart") + combo_types
                and _vh > _vw
            ):
                warnings.append(
                    {
                        "check": "chart_taller_than_wide",
                        "visual": name,
                        "width": _vw,
                        "height": _vh,
                        "fix": f"cartesian charts read left-to-right — make {name} wider than tall",
                    }
                )
            if vtype in ("lineChart", "areaChart", "stackedAreaChart"):
                _line_cats = [
                    (p.get("field") or {}).get("Column", {}).get("Property", "")
                    for p in ((_qs.get("Category") or {}).get("projections") or [])
                ]
                _line_cats = [c for c in _line_cats if c]
                if _line_cats and not any(_DATE_LIKE_RE.search(c) for c in _line_cats):
                    warnings.append(
                        {
                            "check": "line_chart_categorical_x",
                            "visual": name,
                            "category": _line_cats,
                            "fix": f"a line through categories implies a relationship that doesn't exist — make {name} a column/bar chart, or put a real time field on the axis",
                        }
                    )
            _alt_raw = str(
                (
                    (
                        v.get("visual", {})
                        .get("visualContainerObjects", {})
                        .get("general")
                        or [{}]
                    )[0]
                    .get("properties", {})
                    .get("altText", {})
                    or {}
                )
                .get("expr", {})
                .get("Literal", {})
                .get("Value", "")
            ).strip("'")
            if not _alt_raw.strip():
                warnings.append(
                    {
                        "check": "missing_alt_text",
                        "visual": name,
                        "fix": f"{name} has no screen-reader alt text — rebuild with pbi_visual_add_bound (sets it from the title) or write visualContainerObjects.general altText",
                    }
                )
            elif len(_alt_raw) > ALT_TEXT_MAX_CHARS:
                warnings.append(
                    {
                        "check": "alt_text_too_long",
                        "visual": name,
                        "length": len(_alt_raw),
                        "fix": f"alt text is capped at {ALT_TEXT_MAX_CHARS} characters — shorten it",
                    }
                )
            if v.get("visual", {}).get("objects", {}).get("tooltip"):
                errors.append(
                    {
                        "check": "hand_rolled_tooltip",
                        "visual": name,
                        "fix": f"{name} puts tooltip wiring under the WRONG path objects.tooltip — this broke Desktop loads on 2026-06-11. Move it to visual.visualContainerObjects.visualTooltip (show/type='ReportPage'/section=<tooltip page name>) per the CUSTOM TOOLTIP PAGES template.",
                    }
                )

            # Scrollbar risk check on bar charts
            if vtype in bar_types:
                _filters = v.get("filterConfig", {}).get("filters", []) or []
                _has_top_n = any(
                    f.get("type") == "TopN" for f in _filters if isinstance(f, dict)
                )
                if not _has_top_n and _vh < 320:
                    warnings.append(
                        {
                            "check": "scrollbar_risk",
                            "visual": name,
                            "fix": f"bar chart {name} risks vertical scrollbars when categories exceed 6 — apply Top-N filter (e.g. Top 7)",
                        }
                    )

            # Donut label overlap check
            if vtype in ("donutChart", "pieChart"):
                _lbl_obj = (
                    v.get("visual", {}).get("objects", {}).get("labels") or [{}]
                )[0]
                _pos = (
                    ((_lbl_obj.get("properties") or {}).get("position") or {})
                    .get("expr", {})
                    .get("Literal", {})
                    .get("Value", "")
                )
                if "inside" in str(_pos).lower():
                    warnings.append(
                        {
                            "check": "donut_inside_label",
                            "visual": name,
                            "fix": f"donut chart {name} uses inside labels which collide on small slices — set position to outside",
                        }
                    )

        grid = REPORT_LAYOUT["grid_px"]
        off_grid = []
        off_grid_suggest = {}
        for v in data_visuals:
            pos = v.get("position", {})
            vals = [float(pos.get(k, 0)) for k in ("x", "y", "width", "height")]
            if any(min(val % grid, grid - (val % grid)) > 1 for val in vals):
                off_grid.append(v.get("name"))
                snapped = {
                    k: round(float(pos.get(k, 0)) / grid) * grid
                    for k in ("x", "y", "width", "height")
                }
                snapped["x"] = max(0, min(snapped["x"], int(page_w) - snapped["width"]))
                snapped["y"] = max(
                    0, min(snapped["y"], int(page_h) - snapped["height"])
                )
                off_grid_suggest[v.get("name")] = snapped
        if off_grid:
            warnings.append(
                {
                    "check": "off_grid_positions",
                    "grid_px": grid,
                    "visuals": off_grid,
                    "suggested": off_grid_suggest,
                    "fix": f"apply each suggested rect: pbi_visual_update visual_name=<v> page={page_name} x=<x> y=<y> width=<width> height=<height>",
                }
            )

        if non_banner:
            max_right = max(
                float(v.get("position", {}).get("x", 0))
                + float(v.get("position", {}).get("width", 0))
                for v in non_banner
            )
            max_bottom = max(
                float(v.get("position", {}).get("y", 0))
                + float(v.get("position", {}).get("height", 0))
                for v in non_banner
            )
            right_gap = page_w - max_right
            bottom_gap = page_h - max_bottom
            _dead_frac = REPORT_DENSITY["dead_margin_frac"]
            if right_gap > page_w * _dead_frac:
                warnings.append(
                    {
                        "check": "dead_right_margin",
                        "gap_px": round(right_gap, 2),
                        "page_width": page_w,
                        "fix": f"widen the rightmost column of visuals so content extends near x={int(page_w)}",
                    }
                )
            if bottom_gap > page_h * _dead_frac:
                warnings.append(
                    {
                        "check": "dead_bottom_margin",
                        "gap_px": round(bottom_gap, 2),
                        "page_height": page_h,
                        "fix": f"add a row of visuals or stretch the bottom row down to y≈{int(page_h - 15)}",
                    }
                )

        _boxes = [
            (
                float(v.get("position", {}).get("width", 0)),
                float(v.get("position", {}).get("height", 0)),
            )
            for v in (data_visuals + ([banner] if banner else []))
        ]
        if _layout_under_filled(
            _boxes, page_w, page_h, len(data_visuals), REPORT_DENSITY
        ):
            _cov = _layout_coverage(_boxes, page_w, page_h)
            warnings.append(
                {
                    "check": "low_canvas_coverage",
                    "coverage_pct": round(_cov * 100, 1),
                    "target_pct": round(REPORT_DENSITY["min_coverage_frac"] * 100),
                    "fix": (
                        f"the page is only {round(_cov * 100)}% filled — too much "
                        f"empty space. Stretch visuals to span full rows (widths summing "
                        f"to ~{int(page_w)}px per row) and the bottom row down to "
                        f"y≈{int(page_h - 15)}, or add another row of visuals, until "
                        f"coverage reaches ~{round(REPORT_DENSITY['min_coverage_frac'] * 100)}%."
                    ),
                }
            )

        v_rows: list[list[dict]] = []
        for v in data_visuals:
            vy = float(v.get("position", {}).get("y", 0))
            for row in v_rows:
                if abs(float(row[0].get("position", {}).get("y", 0)) - vy) < 5:
                    row.append(v)
                    break
            else:
                v_rows.append([v])
        for row in v_rows:
            row.sort(key=lambda v: float(v.get("position", {}).get("x", 0)))
            for a, b in pairwise(row):
                _gap = float(b.get("position", {}).get("x", 0)) - (
                    float(a.get("position", {}).get("x", 0))
                    + float(a.get("position", {}).get("width", 0))
                )
                if _gap > REPORT_LAYOUT["row_gap_warn_px"]:
                    warnings.append(
                        {
                            "check": "row_gap",
                            "between": [a.get("name"), b.get("name")],
                            "gap_px": round(_gap, 2),
                            "fix": f"widen {a.get('name')} / shift {b.get('name')} left, or fill the band with another visual — a {int(_gap)}px hole inside a row reads as unfinished",
                        }
                    )

        _v_rows_by_y = sorted(
            v_rows,
            key=lambda row: min(
                float(vv.get("position", {}).get("y", 0)) for vv in row
            ),
        )
        _row_gaps = []
        for row_a, row_b in pairwise(_v_rows_by_y):
            a_bottom = max(
                float(vv.get("position", {}).get("y", 0))
                + float(vv.get("position", {}).get("height", 0))
                for vv in row_a
            )
            b_top = min(float(vv.get("position", {}).get("y", 0)) for vv in row_b)
            _row_gaps.append(round(b_top - a_bottom, 2))
        if (
            len(_row_gaps) >= 2
            and (max(_row_gaps) - min(_row_gaps)) > 2 * REPORT_LAYOUT["grid_px"]
        ):
            warnings.append(
                {
                    "check": "row_vertical_spacing_inconsistent",
                    "gaps_px": _row_gaps,
                    "fix": (
                        f"row-to-row vertical gaps vary ({min(_row_gaps)}-{max(_row_gaps)}px) — "
                        f"align them to a consistent spacing (a multiple of gutter_px="
                        f"{REPORT_LAYOUT['gutter_px']}) so the page reads as one grid"
                    ),
                }
            )

        _binding_type = str((page.get("pageBinding") or {}).get("type", ""))
        _is_aux_page = _binding_type in ("Tooltip", "Drillthrough") or page.get(
            "visibility"
        ) in (1, "HiddenInViewMode")
        _title_like = {"textbox", "pageNavigator"}
        if (
            visuals
            and not _is_aux_page
            and banner is None
            and not any(_vtype(v) in _title_like for v in visuals)
        ):
            warnings.append(
                {
                    "check": "page_missing_title",
                    "fix": "nothing on the canvas names this page — add a slim textbox with the page title (or a page navigator) in the top margin",
                }
            )

        result["pages"][page_name] = {
            "page_dimensions": {"width": page_w, "height": page_h},
            "visual_count": len(visuals),
            "errors": errors,
            "warnings": warnings,
        }
        result["summary"]["errors"] += len(errors)
        result["summary"]["warnings"] += len(warnings)

    theme_warnings = _audit_theme_colors(report_path)
    if theme_warnings:
        result["theme"] = {"errors": [], "warnings": theme_warnings}
        result["summary"]["warnings"] += len(theme_warnings)

    try:
        from report.wireframe_render import render_wireframes

        wf_paths = render_wireframes(report_path, result)
        if wf_paths:
            result["wireframes"] = [p.replace("\\", "/") for p in wf_paths]
    except Exception as e:
        _build_trace("wireframe_render_error", error=str(e))

    report_root = os.path.dirname(os.path.dirname(pages_dir))
    pbi_marker = os.path.join(report_root, ".pbi", "localSettings.json")
    newest_def = 0.0
    for sub in ("definition", "StaticResources"):
        for dirpath, _dirs, files in os.walk(os.path.join(report_root, sub)):
            for fn in files:
                try:
                    newest_def = max(
                        newest_def, os.path.getmtime(os.path.join(dirpath, fn))
                    )
                except OSError:
                    pass
    opened = os.path.isfile(pbi_marker)
    stale = (not opened) or (os.path.getmtime(pbi_marker) < newest_def)
    result["desktop_view"] = {
        "ever_opened_in_desktop": opened,
        "stale": stale,
        "hint": (
            (
                "Power BI Desktop has not loaded the current definition files — "
                "close Desktop fully and reopen the .pbip to see theme/layout changes. "
                "Data refresh alone does NOT re-read definition files."
            )
            if stale
            else "Desktop view is current."
        ),
    }

    try:
        from report.bpa_lint import inspector_available, lint_report

        if inspector_available():
            lint = lint_report(report_path)
            if lint.get("errors") or lint.get("warnings"):
                result["lint"] = {
                    "engine": lint.get("engine"),
                    "errors": lint.get("errors", []),
                    "warnings": lint.get("warnings", []),
                    "note": (
                        "Deterministic best-practice rules (PBI-Inspector). "
                        "Fix errors; use judgment on warnings — intentional "
                        "design choices (e.g. per-category brand colors) may "
                        "trip generic rules."
                    ),
                }
                result["summary"]["errors"] += len(lint.get("errors", []))
                result["summary"]["warnings"] += len(lint.get("warnings", []))
    except Exception:
        pass

    return result


# The subset of _audit_dashboard's checks that are structural/functional —
# things that break the report (broken bindings, off-canvas, overlap, an
# unreadable slicer, a tooltip wiring known to crash Desktop loads) — rather
# than subjective design/style judgment (density, chart-type opinions,
# alignment preference). Curated deliberately as an explicit allowlist rather
# than derived from each check's errors/warnings severity: that field isn't a
# reliable proxy for "safe to gate an automated retry loop on" even after the
# two label corrections above (low_canvas_coverage is an `error` but is a
# density preference, not a break).
BLOCKING_CHECKS = frozenset(
    {
        "page_json_parse",
        "visual_json_parse",
        "invalid_dimensions",
        "off_page_right",
        "off_page_bottom",
        "overlap",
        "empty_binding",
        "broken_binding",
        # A bound-but-invisible visual is as broken as an unbound one, and the
        # agent cannot see the difference without being told.
        "unsupported_role",
        "banner_width_mismatch",
        "slicer_too_short",
        "hand_rolled_tooltip",
    }
)


def blocking_errors(result: dict) -> list[dict]:
    out: list[dict] = []
    for page_name, page in (result.get("pages") or {}).items():
        for e in page.get("errors") or []:
            if e.get("check") in BLOCKING_CHECKS:
                out.append({**e, "page": page_name})
    return out
