from __future__ import annotations

import importlib.util
import sys

from config import PROJECTS_DIR

SKILL_TOOLS_SPEC: tuple[tuple[str, str, dict], ...] = (
    (
        "list_skills",
        "List available Power BI skills (slug + when to use each). Call first, then load_skill.",
        {"type": "object", "properties": {}},
    ),
    (
        "load_skill",
        (
            "Load the full expert playbook for one skill by slug (e.g. 'power-bi-dax'); read and follow it before related work. "
            "A skill's reference files load with 'slug/reference-name' (e.g. 'semantic-model-authoring/dax-guidelines')."
        ),
        {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "skill slug, or slug/reference-name for a reference file",
                }
            },
            "required": ["skill"],
        },
    ),
)


def _pbi_cli() -> list[str]:
    # Prefer running pbi_cli module directly with active interpreter
    if importlib.util.find_spec("pbi_cli"):
        return [sys.executable, "-m", "pbi_cli"]
    return ["pbi"]


PBI_CLI = _pbi_cli()

# Shared item schema for pbi_dashboard_layout's section arrays (slicers/kpis/
# charts/tables/supporting) — see report/visual_sizing.py for how these hints
# drive minimum-size estimation.
_DASHBOARD_LAYOUT_VISUAL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "unique visual identifier"},
        "type": {
            "type": "string",
            "description": "PBIR visualType, e.g. 'clusteredColumnChart', 'slicer', 'tableEx'",
        },
        "title": {"type": "string"},
        "field_count": {
            "type": "integer",
            "description": "measures/columns bound — drives width for charts/cards",
        },
        "label_max_len": {
            "type": "integer",
            "description": "longest expected label/category text",
        },
        "slicer_mode": {
            "type": "string",
            "description": "slicer only: Dropdown|Between|Basic|Single|Relative... (default Dropdown)",
        },
        "slicer_item_count": {
            "type": "integer",
            "description": "slicer only, Basic/Single/listSlicer — visible item count",
        },
        "column_count": {
            "type": "integer",
            "description": "table/matrix only — column count (default 3)",
        },
        "has_top_n_filter": {
            "type": "boolean",
            "description": "chart only — suppresses the un-filtered-bar-chart scrollbar-risk height bump",
        },
        "row_break": {
            "type": "boolean",
            "description": "force a new row to start before this visual, even if it would otherwise fit",
        },
    },
    "required": ["name"],
}

PBI_TOOLS = [
    {
        "name": "pbi_report_create",
        "description": "Scaffold a new PBIR report project on disk. Automatically wires the Fabric live connection AND stamps a default color theme onto the report (inject_theme runs unconditionally — the report is never created 'theme-less'). Call this first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": f"Directory to create the report in, e.g. {PROJECTS_DIR / 'ShazeDashboard' / 'report'}",
                },
                "name": {"type": "string", "description": "Report display name"},
                "model_name": {
                    "type": "string",
                    "description": "Fabric semantic model name to connect to, e.g. 'Shaze Dashboard-Fabcon 4'",
                },
                "dataset_id": {
                    "type": "string",
                    "description": "Fabric dataset GUID for the model",
                },
            },
            "required": ["path", "name", "model_name", "dataset_id"],
        },
    },
    {
        "name": "pbi_report_add_page",
        "description": "Add a new page to a PBIR report. Also auto-applies a page background/polish pass — the page is not created blank.",
        "input_schema": {
            "type": "object",
            "properties": {
                "display_name": {
                    "type": "string",
                    "description": "Page display name shown in the report tab",
                },
                "name": {
                    "type": "string",
                    "description": "Page identifier — lowercase, no spaces, e.g. overview",
                },
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": ["display_name", "name", "report_path"],
        },
    },
    {
        "name": "pbi_visual_add",
        "description": (
            "Add a positioned visual to a report page. Always specify x, y, width, height for proper layout. "
            "Types: card, cardVisual, bar, column, clustered_column, clustered_bar, stacked_bar, "
            "line, area, pie, donut, scatter, combo, "
            "waterfall, funnel, ribbon, treemap, "
            "map, "
            "table, matrix, kpi, gauge, slicer, list_slicer, advanced_slicer, "
            "multi_row_card, textbox, shape, page_navigator. "
            "Side effects beyond creating the container: x/y/width/height are snapped to the "
            "layout grid; a slicer type triggers an automatic PBIR schema rewrite; a "
            "cardVisual/card gets KPI accent styling auto-applied — none of these are opt-out."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Page identifier"},
                "type": {"type": "string", "description": "Visual type alias"},
                "name": {
                    "type": "string",
                    "description": "Visual identifier — lowercase, no spaces",
                },
                "x": {
                    "type": "number",
                    "description": "X position in pixels from left edge",
                },
                "y": {
                    "type": "number",
                    "description": "Y position in pixels from top edge",
                },
                "width": {"type": "number", "description": "Width in pixels"},
                "height": {"type": "number", "description": "Height in pixels"},
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": [
                "page",
                "type",
                "name",
                "x",
                "y",
                "width",
                "height",
                "report_path",
            ],
        },
    },
    {
        "name": "pbi_visual_bind",
        "description": (
            "Bind data fields to a visual. MUST supply at least one binding field "
            "appropriate to the visual type: "
            "card/slicer→field. "
            "bar/column/clustered_column/clustered_bar/stacked_bar/line/area/pie/donut→category+value. "
            "scatter→category+value (x=category, y=value). "
            "combo→category+value+line (line=second measure). "
            "waterfall→category+value (optional: breakdown for variance segments). "
            "funnel→category+value. "
            "ribbon→category+value (optional: legend for series). "
            "treemap→category+value. "
            "map→category+value (category=location field e.g. City/Country, value=size measure). "
            "matrix→row+value (optional: column for column grouping). "
            "kpi→indicator+trend. "
            "gauge→value+goal (goal=target/max measure for the gauge's needle range). "
            "multi_row_card→field. "
            "Empty-binding calls are rejected by the schema. "
            "NOTE: binding a list_slicer/advanced_slicer collapses it to a plain dropdown "
            "slicer (visualType is forcibly normalized to 'slicer' the moment a field is "
            "bound) — those two types cannot be preserved through a bind call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "visual_name": {"type": "string", "description": "Visual identifier"},
                "page": {"type": "string", "description": "Page identifier"},
                "field": {
                    "type": "string",
                    "description": "Single field for card/slicer, e.g. Sales[Total Revenue]",
                },
                "category": {
                    "type": "string",
                    "description": "Category/axis field for charts",
                },
                "value": {
                    "type": "string",
                    "description": "Value/measure field for charts",
                },
                "breakdown": {
                    "type": "string",
                    "description": "Breakdown/variance segment field for waterfall chart",
                },
                "row": {"type": "string", "description": "Row field for matrix"},
                "column": {
                    "type": "string",
                    "description": "Column field for matrix or combo",
                },
                "line": {
                    "type": "string",
                    "description": "Line series for combo chart",
                },
                "legend": {
                    "type": "string",
                    "description": "Legend/series grouping field",
                },
                "indicator": {"type": "string", "description": "KPI indicator measure"},
                "trend": {"type": "string", "description": "KPI trend axis field"},
                "goal": {
                    "type": "string",
                    "description": "Target/max measure for a gauge's needle range",
                },
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": ["visual_name", "page", "report_path"],
        },
    },
    {
        "name": "pbi_visual_set_container",
        "description": "Set title and border/background visibility on a visual container. Note: only title text and show/hide toggles are supported — no color values.",
        "input_schema": {
            "type": "object",
            "properties": {
                "visual_name": {"type": "string", "description": "Visual identifier"},
                "page": {"type": "string", "description": "Page identifier"},
                "title": {"type": "string", "description": "Visual title text"},
                "border_show": {
                    "type": "boolean",
                    "description": "Show (true) or hide (false) the border",
                },
                "background_show": {
                    "type": "boolean",
                    "description": "Show (true) or hide (false) the background",
                },
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": ["visual_name", "page", "report_path"],
        },
    },
    {
        "name": "pbi_visual_add_bound",
        "description": "FAST PATH — create a COMPLETE visual in ONE call: positioned container + data bindings + title. Equivalent to pbi_visual_add → pbi_visual_bind ×N → pbi_visual_set_container; ALWAYS prefer this over the 3-call sequence. `bindings` is an array of pbi_visual_bind argument objects (category/value/field/legend/row/line/goal/trend/breakdown etc. — same semantics and arity rules; one array element per bind call you would have made). If a binding fails, the output reports it — repair with an individual pbi_visual_bind.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "string"},
                "report_path": {"type": "string"},
                "type": {"type": "string", "description": "Visual type alias"},
                "name": {
                    "type": "string",
                    "description": "Visual identifier — lowercase, no spaces",
                },
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number"},
                "height": {"type": "number"},
                "title": {
                    "type": "string",
                    "description": "Visual title — states the insight/metric",
                },
                "bindings": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Array of pbi_visual_bind argument objects (without visual_name/page/report_path)",
                },
            },
            "required": [
                "page",
                "report_path",
                "type",
                "name",
                "x",
                "y",
                "width",
                "height",
                "title",
                "bindings",
            ],
        },
    },
    {
        "name": "pbi_layout_compute",
        "description": (
            "Deterministic layout engine — describe the page as ROWS of named visuals "
            "with weights; returns grid-snapped, non-overlapping x/y/width/height rects "
            "honoring page margins and gutters. ALWAYS use this instead of computing pixel "
            "positions yourself, then pass each returned rect VERBATIM to pbi_visual_add_bound. "
            "Row height: px number, 'NN%' of page height, or 'fill' (shares leftover height by "
            "row weight). Item width: splits the row by item weight (default 1)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page_width": {
                    "type": "number",
                    "description": "Page width from pbi_report_info",
                },
                "page_height": {
                    "type": "number",
                    "description": "Page height from pbi_report_info",
                },
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "height": {
                                "description": "px number, 'NN%', or 'fill' (default fill)"
                            },
                            "weight": {
                                "type": "number",
                                "description": "share of leftover height for fill rows",
                            },
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {
                                            "type": "string",
                                            "description": "visual identifier",
                                        },
                                        "weight": {"type": "number"},
                                    },
                                    "required": ["name"],
                                },
                            },
                        },
                        "required": ["items"],
                    },
                },
            },
            "required": ["page_width", "page_height", "rows"],
        },
    },
    {
        "name": "pbi_dashboard_layout",
        "description": (
            "Smart layout engine for standard page anatomy (Header -> Slicers -> KPIs -> "
            "Charts/Tables). Describe visuals by SECTION, not by pixel position — pass type/"
            "field_count/title hints per visual and this tool classifies each one, estimates its "
            "minimum readable size, wraps slicers/charts/tables into extra rows when they don't "
            "fit the available width, GROWS the canvas when needed (never shrinks a visual below "
            "its minimum) — EXCEPT when you pass explicit page_width+page_height together: growth "
            "is then suppressed to respect your explicit size, and a visual CAN render below its "
            "minimum if that size is too small (a diagnostics string will say so — check it), and returns grid-snapped non-overlapping rects VERBATIM to pass to "
            "pbi_visual_add_bound, plus plain-English diagnostics of what it decided and why. "
            "Prefer this over pbi_layout_compute for standard dashboards; use pbi_layout_compute "
            "only when you need fully manual row/weight control over a bespoke layout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page_width": {
                    "type": "number",
                    "description": "Optional explicit width — if given together with page_height, the canvas will NOT be grown",
                },
                "page_height": {"type": "number"},
                "aspect_ratio": {
                    "type": "number",
                    "description": "Optional width/height ratio used only when page dimensions aren't given (default 16:9)",
                },
                "margin_px": {"type": "number"},
                "gutter_px": {"type": "number"},
                "max_charts_per_row": {"type": "integer"},
                "canvas": {
                    "type": "object",
                    "description": "Optional override of canvas growth bounds (each defaults from policy)",
                    "properties": {
                        "min_width": {"type": "number"},
                        "preferred_width": {"type": "number"},
                        "max_width": {"type": "number"},
                        "min_height": {"type": "number"},
                        "preferred_height": {"type": "number"},
                        "max_height": {"type": "number"},
                    },
                },
                "sections": {
                    "type": "object",
                    "description": "At least one section must contain a visual",
                    "properties": {
                        "header": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "title": {"type": "string"},
                            },
                            "required": ["name"],
                        },
                        "slicers": {
                            "type": "array",
                            "items": _DASHBOARD_LAYOUT_VISUAL_SCHEMA,
                        },
                        "kpis": {
                            "type": "array",
                            "items": _DASHBOARD_LAYOUT_VISUAL_SCHEMA,
                        },
                        "charts": {
                            "type": "array",
                            "items": _DASHBOARD_LAYOUT_VISUAL_SCHEMA,
                        },
                        "tables": {
                            "type": "array",
                            "items": _DASHBOARD_LAYOUT_VISUAL_SCHEMA,
                        },
                        "supporting": {
                            "type": "array",
                            "items": _DASHBOARD_LAYOUT_VISUAL_SCHEMA,
                        },
                    },
                },
            },
            "required": ["sections"],
        },
    },
    {
        "name": "pbi_report_resize_page",
        "description": (
            "Resize an EXISTING page's canvas (page.json width/height) — for when a page "
            "already has visuals placed and needs a different canvas size. GROWING is always "
            "safe. SHRINKING trims any visual that would now overflow the new bounds down to "
            "its own minimum readable size, in place (positions never move) — if a visual "
            "can't be accommodated without going below that minimum, the WHOLE resize is "
            "refused with an ERROR naming which visual blocked it; nothing is written. Move or "
            "resize the named visual first (pbi_visual_update / pbi_visual_delete), or choose a "
            "larger target, then retry. Width/height must be within the report's standard "
            "canvas range (960-1920 / 540-1440px). Use pbi_dashboard_layout instead when "
            "planning a NEW page from scratch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
                "page": {"type": "string", "description": "Page identifier"},
                "width": {"type": "number", "description": "Target page width in px"},
                "height": {"type": "number", "description": "Target page height in px"},
            },
            "required": ["report_path", "page", "width", "height"],
        },
    },
    {
        "name": "pbi_add_svg_measure",
        "description": (
            "Create a REPORT-LEVEL (extension) DAX measure that renders inline SVG — "
            "sparklines, progress bars, variance bullets — without touching the semantic "
            "model. The expression must return a 'data:image/svg+xml;utf8,...' string; "
            "escape hex colors as %23RRGGBB (a raw # blanks the image) and use YOUR "
            "palette colors. dataCategory is set to ImageUrl automatically. Bind the "
            "result into a KPI card with pbi_card_set_image, or into a table/matrix "
            "column via pbi_visual_bind value=Entity[Name].\n"
            "SPARKLINE TEMPLATE — do NOT derive from scratch; substitute <DateCol>, "
            "<MonthCol>, [M], and replace <HEX> with a color from YOUR report palette "
            "(%23-escaped), then use as-is:\n"
            "VAR _pts = ADDCOLUMNS(CALCULATETABLE(VALUES('T'[<MonthCol>]), ALLSELECTED('T')), \"@y\", CALCULATE([M]))\n"
            "VAR _n = COUNTROWS(_pts) VAR _mx = MAXX(_pts, [@y])\n"
            "VAR _line = CONCATENATEX(_pts, VAR _x = (RANKX(_pts, [<MonthCol>],, ASC) - 1) * DIVIDE(100, _n - 1) "
            'VAR _y = 28 - DIVIDE(CALCULATE([M]), _mx) * 24 RETURN _x & "," & _y, " ", [<MonthCol>], ASC)\n'
            "RETURN IF(_n > 1, \"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 30' "
            "preserveAspectRatio='none'><polyline fill='none' stroke='%23<HEX>' stroke-width='2' points='\" & _line & \"'/></svg>\")\n"
            "Verify the FIRST measure once with dax_query_operations (EVALUATE ROW), then CLONE it for "
            "the other KPIs changing only [M] and the stroke color — do not re-verify each clone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {"type": "string"},
                "entity": {
                    "type": "string",
                    "description": "EXISTING model table to host the measure, e.g. Sales",
                },
                "name": {
                    "type": "string",
                    "description": "Measure name, e.g. 'Revenue Sparkline SVG'",
                },
                "expression": {
                    "type": "string",
                    "description": "DAX returning the SVG data-URI string",
                },
            },
            "required": ["report_path", "entity", "name", "expression"],
        },
    },
    {
        "name": "pbi_card_set_image",
        "description": (
            "Bind an ImageUrl measure (SVG sparkline / variance bar / status arrow) into a "
            "cardVisual's image slot — this is how a KPI card gets in-card trend context. "
            "position: Left | Right | Top | Bottom (Bottom = classic value-over-sparkline; "
            "any other value is now rejected). Only applies to cardVisual — called on any "
            "other visual type it returns a SKIP, not a silent success."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {"type": "string"},
                "page": {"type": "string"},
                "visual_name": {"type": "string"},
                "entity": {
                    "type": "string",
                    "description": "Table hosting the image measure",
                },
                "measure": {
                    "type": "string",
                    "description": "The ImageUrl measure name",
                },
                "position": {
                    "type": "string",
                    "description": "Left | Right | Top | Bottom",
                },
                "from_extension": {
                    "type": "boolean",
                    "description": "true (default) when the measure was created with pbi_add_svg_measure; false for a model measure",
                },
            },
            "required": ["report_path", "page", "visual_name", "entity", "measure"],
        },
    },
    {
        "name": "pbi_design_judge",
        "description": (
            "OPT-IN soft design review — an LLM scores one page 1-10 on six dimensions "
            "(layout, chart fit, color/accessibility, KPI hierarchy, titles, density) and "
            "returns concrete `repairs` plus a `next_step` telling you whether to loop. "
            "Costs an LLM call. LOOP: call with round=1; if next_step.action is "
            "'apply_repairs', apply the `repairs` with the normal fix tools and call again "
            "with round incremented and prev_overall set to this call's `overall`; stop "
            "when next_step.action is 'done'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {"type": "string"},
                "page": {"type": "string", "description": "Page identifier to judge"},
                "round": {
                    "type": "integer",
                    "description": "Which judging round this is (start at 1). Drives the loop stop condition.",
                },
                "prev_overall": {
                    "type": "number",
                    "description": "The `overall` score from the previous round (omit on round 1). Lets the loop stop if a repair round didn't improve the score.",
                },
            },
            "required": ["report_path", "page"],
        },
    },
    {
        "name": "pbi_refresh_registry",
        "description": "Rebuild the bind-validator's measure/column registry for the given semantic model(s). Call this YOURSELF if pbi_visual_bind reports 'registry is empty' or 'registry is STALE', then retry the bind — do NOT ask the user to click anything.",
        "input_schema": {
            "type": "object",
            "properties": {
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": 'Semantic model name(s) you are connected to, e.g. ["Sales & Profitability"]',
                },
            },
            "required": ["models"],
        },
    },
    {
        "name": "pbi_page_clear_visuals",
        "description": "Delete ALL visuals from a page so it can be rebuilt cleanly. Use this before adding visuals to an existing page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Page identifier"},
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": ["page", "report_path"],
        },
    },
    {
        "name": "pbi_visual_update",
        "description": (
            "Move or resize an existing visual. Pass ONLY the field(s) you want to "
            "change (e.g. just height) — omitted fields are left as-is, this is a "
            "targeted partial update, not a full replace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "visual_name": {"type": "string", "description": "Visual identifier"},
                "page": {"type": "string", "description": "Page identifier"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number"},
                "height": {"type": "number"},
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": ["visual_name", "page", "report_path"],
        },
    },
    {
        "name": "pbi_visual_delete",
        "description": "Delete a single visual from a page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "visual_name": {"type": "string", "description": "Visual identifier"},
                "page": {"type": "string", "description": "Page identifier"},
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": ["visual_name", "page", "report_path"],
        },
    },
    {
        "name": "pbi_visual_format",
        "description": "Apply rich formatting to a visual that pbi_visual_set_container can't reach: data label color/size, axis show/color, legend position, actual background color (not just toggle), title color/size/alignment, and dataPoint default color. Pass only the params you want to set — None means leave-as-is. Use this AFTER pbi_visual_bind to polish a visual. For matrix/table cell heatmaps use pbi_visual_conditional_color instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "visual_name": {"type": "string"},
                "page": {"type": "string"},
                "report_path": {"type": "string"},
                "data_label_show": {"type": "boolean"},
                "data_label_color": {
                    "type": "string",
                    "description": (
                        "Hex e.g. #0F172A. For cardVisual (KPI cards), this IS the "
                        "big number's color (writes the callout/value text object) — "
                        "there is no separate 'value_color' param. Use this, not "
                        "background_color/title_color, to fix an invisible/wrong-color "
                        "KPI number."
                    ),
                },
                "data_label_size": {"type": "number", "description": "Font size in pt"},
                "axis_show": {"type": "boolean"},
                "axis_color": {"type": "string"},
                "legend_show": {"type": "boolean"},
                "legend_position": {
                    "type": "string",
                    "description": "Top | Bottom | Left | Right | None",
                },
                "background_color": {
                    "type": "string",
                    "description": "Hex — sets actual color, not just visibility",
                },
                "title_color": {"type": "string"},
                "title_size": {"type": "number"},
                "title_align": {
                    "type": "string",
                    "description": "left | center | right",
                },
                "data_colors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Hex colors overriding theme palette for THIS visual",
                },
            },
            "required": ["visual_name", "page", "report_path"],
        },
    },
    {
        "name": "pbi_visual_conditional_color",
        "description": "Apply a 2- or 3-stop color gradient to the value cells of a matrix or table based on a measure. Produces the heatmap look (state × LOB grid, league-table loss-ratio column). For 2-stop gradient pass min_color + max_color. For diverging (good/neutral/bad) pass mid_color too. Only applies to tableEx/pivotTable/table/matrix visuals — called on any other visual type it returns a SKIP, not a silent success.",
        "input_schema": {
            "type": "object",
            "properties": {
                "visual_name": {"type": "string"},
                "page": {"type": "string"},
                "report_path": {"type": "string"},
                "target_table": {
                    "type": "string",
                    "description": "Table name (no quotes), e.g. Sales",
                },
                "target_measure": {
                    "type": "string",
                    "description": "Measure name driving the gradient, e.g. Loss Ratio",
                },
                "min_color": {
                    "type": "string",
                    "description": "Hex at low end, e.g. #059669",
                },
                "max_color": {
                    "type": "string",
                    "description": "Hex at high end, e.g. #DC2626",
                },
                "mid_color": {
                    "type": "string",
                    "description": "Optional hex at midpoint for diverging scale",
                },
            },
            "required": [
                "visual_name",
                "page",
                "report_path",
                "target_table",
                "target_measure",
                "min_color",
                "max_color",
            ],
        },
    },
    {
        "name": "pbi_visual_color_by_measure",
        "description": "Color a BAR/COLUMN chart's bars by a measure (gradient: min_color at the lowest value → max_color at the highest; pass mid_color for a diverging 3-stop). Use this for 'shade the bars by Profit Margin / growth / etc.' This is the CHART version — for matrix/table CELL heatmaps use pbi_visual_conditional_color instead. CRITICAL: do NOT hand-write dataPoint.fill yourself — a `metadata` selector makes every bar the SAME color (the gradient silently does nothing); this tool uses the correct per-data-point dataViewWildcard selector. Bounds are data-driven so even a narrow value range spreads full min→max across the bars.",
        "input_schema": {
            "type": "object",
            "properties": {
                "visual_name": {"type": "string"},
                "page": {"type": "string"},
                "report_path": {"type": "string"},
                "target_table": {
                    "type": "string",
                    "description": "Table name (no quotes), e.g. Measures_Table",
                },
                "target_measure": {
                    "type": "string",
                    "description": "Measure driving the gradient, e.g. Profit Margin %",
                },
                "min_color": {
                    "type": "string",
                    "description": "Hex for the LOWEST value (e.g. light #DCEAF7)",
                },
                "max_color": {
                    "type": "string",
                    "description": "Hex for the HIGHEST value (e.g. dark #0A3D62)",
                },
                "mid_color": {
                    "type": "string",
                    "description": "Optional hex at midpoint for a diverging scale",
                },
            },
            "required": [
                "visual_name",
                "page",
                "report_path",
                "target_table",
                "target_measure",
                "min_color",
                "max_color",
            ],
        },
    },
    {
        "name": "pbi_set_report_style",
        "description": "Set the report's visual identity — YOUR design choice per report. Pass 4-8 data colors (first = primary accent unless accent is given), optional page background, optional good/bad semantic colors. Re-injects the structural theme (fonts/cards/shadows stay) with YOUR palette and recolors existing page backgrounds. Call ONCE right after pbi_report_create, BEFORE adding visuals — pick colors that fit the domain/brand (finance=restrained blues, retail=warm, healthcare=teal/green...). KPI card accents and theme colors all follow from this.",
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {"type": "string"},
                "data_colors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "4-8 hex colors, most-important first",
                },
                "accent": {
                    "type": "string",
                    "description": "Primary accent hex (defaults to data_colors[0])",
                },
                "page_background": {
                    "type": "string",
                    "description": "Page canvas hex — keep light/neutral so white visual cards float",
                },
                "good": {"type": "string", "description": "Positive/favorable hex"},
                "bad": {"type": "string", "description": "Negative/unfavorable hex"},
            },
            "required": ["report_path", "data_colors"],
        },
    },
    {
        "name": "pbi_visual_color_by_category",
        "description": "Give each category member its own palette color on a single-series bar/column chart (multi-colored bars, one color per segment/channel/region). Pass category_colors as {member: hex} — query the category's distinct values first (dax_query_operations) and cycle the report palette. Keep the SAME member→color mapping as any donut/legend on the page showing the same dimension. Do NOT use on time/ordered axes (years, months) — ordered data stays single-hue. Only applies to bar/column chart types — called on any other visual type it returns a SKIP, not a silent success.",
        "input_schema": {
            "type": "object",
            "properties": {
                "visual_name": {"type": "string"},
                "page": {"type": "string"},
                "report_path": {"type": "string"},
                "table": {
                    "type": "string",
                    "description": "Table of the category column, e.g. Geography",
                },
                "column": {
                    "type": "string",
                    "description": "Category column name, e.g. Country",
                },
                "category_colors": {
                    "type": "object",
                    "description": 'Member value -> hex color, e.g. {"USA": "#D97706", "Australia": "#2369D2"}',
                },
                "is_numeric": {
                    "type": "boolean",
                    "description": "True when the category column is numeric (emits 2015L literals instead of '2015')",
                },
            },
            "required": [
                "visual_name",
                "page",
                "report_path",
                "table",
                "column",
                "category_colors",
            ],
        },
    },
    {
        "name": "pbi_visual_add_button",
        "description": "Add a styled action button (rectangle with text + optional onClick). Use for view toggles (Stacked / 100% / Side-by-side), chrome chips (Share / Export / Reset), and tab-style nav. Never put a static date/timestamp or number on a chip - it is invented or goes stale; bind freshness text to a measure instead. Set action_type+action_target TOGETHER to wire navigation; leave BOTH None for a non-interactive chip — passing only one of the two is now a rejected call (previously it silently created a button with no navigation wired).",
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "string"},
                "name": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number"},
                "height": {"type": "number"},
                "report_path": {"type": "string"},
                "label": {"type": "string", "description": "Button text"},
                "shape": {
                    "type": "string",
                    "description": "Blank | Back | Reset | Information | Help | Bookmark | PageNavigation",
                },
                "action_type": {
                    "type": "string",
                    "description": "Bookmark | PageNavigation | WebUrl",
                },
                "action_target": {
                    "type": "string",
                    "description": "Bookmark id, page id, or URL",
                },
                "fill_color": {
                    "type": "string",
                    "description": "Hex background, default #1E40AF",
                },
                "text_color": {
                    "type": "string",
                    "description": "Hex text, default #FFFFFF",
                },
            },
            "required": ["page", "name", "x", "y", "width", "height", "report_path"],
        },
    },
    {
        "name": "pbi_visual_add_page_nav",
        "description": "Insert a built-in pageNavigator visual (top-tab navigation across all report pages). PBI auto-populates the tab list at render. Use as a top strip on every page for multi-page dashboards (Underwriting / Claims / Broker Performance style).",
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "string"},
                "name": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "width": {"type": "number"},
                "height": {"type": "number"},
                "report_path": {"type": "string"},
                "orientation": {
                    "type": "string",
                    "description": "horizontal | vertical",
                },
                "selected_color": {
                    "type": "string",
                    "description": "Hex for the active tab text",
                },
                "unselected_color": {
                    "type": "string",
                    "description": "Hex for inactive tabs",
                },
            },
            "required": ["page", "name", "x", "y", "width", "height", "report_path"],
        },
    },
    {
        "name": "pbi_report_reload",
        "description": "Reload the report in Power BI Desktop. Call this ONCE after all visuals are added/updated — do not call after every single visual.",
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                }
            },
            "required": ["report_path"],
        },
    },
    {
        "name": "pbi_dashboard_audit",
        "description": "Post-build audit — scans every page for layout + binding issues and returns a structured JSON report. Call ONCE after every visual is placed and bound. Checks (partial list — 30+ checks actually run, including BPA lint, WCAG contrast, and alt-text): banner width matches page width, no visual extends past page edges, no two visuals overlap, slicers in the same row have consistent height, no visual has an empty binding, no large dead margin on the right or bottom, canvas coverage (an under-filled page with too much white space is an ERROR — low_canvas_coverage), broken_binding (a bound field no longer exists in the model), and hand_rolled_tooltip (a specific hand-written-tooltip wiring mistake that has broken Desktop loads before). broken_binding and hand_rolled_tooltip are BLOCKING — do not report the build done with either present, even if the description above didn't mention them by name. Fix every ERROR with pbi_visual_update / pbi_visual_delete / pbi_visual_add, then re-run audit. Stop when errors=0 or after 3 audit loops.",
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                }
            },
            "required": ["report_path"],
        },
    },
    {
        "name": "pbi_dax_page",
        "description": "Page through a spilled DAX result. When dax_query_operations returns more rows than fit in context, the full result is auto-saved to a CSV and you get a preview + csv_path. Use this to read further slices. Prefer re-querying with DAX aggregation (SUMMARIZECOLUMNS/TOPN) over paging raw rows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "csv_path": {
                    "type": "string",
                    "description": "csv_path from the spilled result summary",
                },
                "offset": {
                    "type": "integer",
                    "description": "Row offset to start from (0-based, default 0)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Rows to return (default 50)",
                },
            },
            "required": ["csv_path"],
        },
    },
    {
        "name": "pbi_model_bpa",
        "description": "Deterministic Best Practice Analyzer for the SEMANTIC MODEL — runs Tabular Editor 2 with Microsoft's official BPA rules against the live model (Fabric workspace or local Desktop) WHEN TE2 is installed. TE2 is Windows-only .NET and is NOT available on this project's actual deployment target (VM + ext4, no containers), so in practice this silently falls back to a much smaller (~12-rule) homegrown Python rule set instead of Microsoft's 40+ official rules — check the result's `note`/`engine` field to see which one actually ran; do not assume 'same findings every run' without checking it. Returns rule violations grouped by severity (errors/warnings/info) with the exact objects flagged. Use for model audits, before deployments, and after bulk model edits.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Semantic model (database) name, e.g. 'DIAD Final Report'",
                },
                "workspace": {
                    "type": "string",
                    "description": "Fabric workspace name (defaults to the configured workspace)",
                },
                "server": {
                    "type": "string",
                    "description": "Explicit XMLA server, e.g. localhost:51542 for a local Desktop instance — overrides workspace",
                },
            },
            "required": ["model"],
        },
    },
    {
        "name": "pbi_model_test",
        "description": "Run a YAML regression-test spec against the live model: each test declares a measure (or raw DAX), an optional CALCULATE filter context, an expected value with tolerance, and optional RLS impersonation (role / effective_user). Returns pass/fail per test with actual vs expected. Use after model edits to prove measure values did not silently change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "spec_path": {
                    "type": "string",
                    "description": "Path to the YAML test spec file",
                },
            },
            "required": ["spec_path"],
        },
    },
    {
        "name": "pbi_report_health_check",
        "description": "MANDATORY after every batch of visual edits. Runs the full structural health check that catches the defect classes Power BI Desktop rejects on load — orphan pages, missing filterConfig, legacy schemas, legacy connection fields, visual-name/folder mismatches, broken tooltip-page wiring, apostrophes in Entity refs, plus the pbi-cli JSON validator. Auto-repairs safe deterministic issues (missing filterConfig entries get rebuilt; orphan page folders get appended to pageOrder; visualContainer 2.7.0 schemas get bumped to 2.8.0) and reports the rest as errors. Returns {ok, errors, warnings, auto_repairs, checks_run}. LOOP until ok=true before claiming the build is done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                }
            },
            "required": ["report_path"],
        },
    },
    {
        "name": "pbi_report_info",
        "description": "Report summary — for each page returns name, display_name, width, height, display_option, visual_count, plus theme and dataset reference. Call this once per page right after pbi_report_add_page to discover the page's actual width and height before placing visuals. Do not hardcode dimensions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                }
            },
            "required": ["report_path"],
        },
    },
    {
        "name": "pbi_visual_list",
        "description": "List all visuals on a report page with their positions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Page identifier"},
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": ["page", "report_path"],
        },
    },
    {
        "name": "pbi_visual_bindings",
        "description": (
            "DIAGNOSTIC — dump the ACTUAL field bindings of every visual: for each "
            "visual, the exact `Table[Field] (Measure|Column)` references it uses "
            "(from projections AND filters). This is the tool to find a broken/"
            "unresolved reference: list the bindings, then diff them against the "
            "live model's INFO.VIEW.MEASURES()/COLUMNS(). One call replaces grepping "
            "files or re-running structural checks. Omit `page` to scan the whole report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
                "page": {
                    "type": "string",
                    "description": "Optional page identifier. Omit to list every page.",
                },
            },
            "required": ["report_path"],
        },
    },
    {
        "name": "pbi_reports_list",
        "description": (
            "DISCOVERY — list every interactive report that exists on disk, with its "
            "project, page names, visual counts, and which semantic model it is bound "
            "to. WORKFLOW: call this FIRST whenever you need a report path, or when "
            "the user asks what reports/dashboards exist. Do NOT guess paths and probe "
            "them with pbi_report_info one at a time — this returns the whole index in "
            "one call, and it is the ONLY tool that can find a report you don't already "
            "know the path of. Also flags reports with no pages/visuals, which the "
            "Power BI Service rejects on publish."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": (
                        "Optional folder to scan. Omit to scan the whole projects "
                        "store (the normal case)."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "pbi_memory_search",
        "description": (
            "ORG MEMORY — search every project this organisation has built (not just "
            "the current one) for measures and past decisions. Results are capped at "
            "15 rows and each matched DAX body is truncated to 800 characters — a "
            "broader-matching term may have more hits than you see, with no signal "
            "that truncation occurred; narrow the query if you suspect you're missing "
            "matches. WORKFLOW: before you "
            "write DAX for a common KPI (churn, YoY, attrition, on-time %, margin), "
            "search for it here — if a sibling project already defined it, reuse that "
            "name and adapt that expression so KPIs stay consistent across clients. "
            "Also useful for 'how did we handle X last time'. Read-only; matches "
            "measure names, measure DAX, and session notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "term": {
                    "type": "string",
                    "description": (
                        "Keyword to look for — a KPI or measure name ('churn'), a DAX "
                        "function ('DATEADD'), or a topic from a past session."
                    ),
                },
            },
            "required": ["term"],
        },
    },
    {
        "name": "pbi_page_set_background",
        "description": "Set the background color of a report page. Only use when the user specifies a color.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Page identifier"},
                "color": {"type": "string", "description": "Hex color e.g. #F8F9FA"},
                "transparency": {
                    "type": "number",
                    "description": "0 (opaque) to 100 (invisible). Default 0.",
                },
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": ["page", "color", "report_path"],
        },
    },
    {
        "name": "pbi_page_set_background_image",
        "description": (
            "Set an IMAGE as a page background or wallpaper (e.g. a brand watermark behind the visuals). "
            "Image/wallpaper backgrounds ARE supported — the image is registered as a report resource "
            "(same mechanism as image visuals) and referenced from the page. Provide an absolute path to a "
            "PNG/JPG already on disk (e.g. an uploaded asset). layer='canvas' puts it on the page background "
            "where visuals sit; layer='wallpaper' puts it on the outspace behind/around the canvas. "
            "Reload Desktop afterwards to confirm it renders."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Page identifier"},
                "image_path": {
                    "type": "string",
                    "description": "Absolute path to a PNG/JPG file already on disk.",
                },
                "scaling": {
                    "type": "string",
                    "enum": ["Fit", "Fill", "Tile", "Normal"],
                    "description": "Image scaling mode. Default Fit.",
                },
                "layer": {
                    "type": "string",
                    "enum": ["canvas", "wallpaper"],
                    "description": "canvas = page background (under visuals); wallpaper = outspace. Default canvas.",
                },
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": ["page", "image_path", "report_path"],
        },
    },
    {
        "name": "pbi_visual_add_image",
        "description": (
            "Place an IMAGE VISUAL (logo, icon, brand mark) on a page. Registers the file as a "
            "report resource and creates a standalone image visual at the given position. Use for "
            "branding — e.g. a company logo top-RIGHT (~120x40); NEVER top-left (that corner is for "
            "the #1 KPI). Provide an absolute path to a PNG/JPG/SVG already on disk — when the user "
            "attaches an image in chat, its saved path appears in the message."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Page identifier"},
                "image_path": {
                    "type": "string",
                    "description": "Absolute path to a PNG/JPG/SVG file already on disk.",
                },
                "visual_name": {
                    "type": "string",
                    "description": "Visual identifier — lowercase, no spaces, e.g. logo_brand",
                },
                "x": {"type": "integer", "description": "Left position in px"},
                "y": {"type": "integer", "description": "Top position in px"},
                "width": {"type": "integer", "description": "Width in px"},
                "height": {"type": "integer", "description": "Height in px"},
                "scaling": {
                    "type": "string",
                    "enum": ["Fit", "Fill", "Normal"],
                    "description": "Image scaling mode. Default Fit.",
                },
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": [
                "page",
                "image_path",
                "visual_name",
                "x",
                "y",
                "width",
                "height",
                "report_path",
            ],
        },
    },
    {
        "name": "pbi_wire_tooltip_page",
        "description": (
            "Turn an existing page into a report-page tooltip and wire a source visual to show it on hover. "
            "WORKFLOW: first pbi_report_add_page for the tooltip page, then add ONE compact visual to it with "
            "pbi_visual_add_bound (size it to fill the tooltip canvas, e.g. a small lineChart trend), THEN call "
            "this tool. It (1) marks the page as a tooltip — type=Tooltip, ActualSize, small canvas, hidden from "
            "tabs — and (2) opts the source visual in via visualContainerObjects.visualTooltip. The tooltip "
            "auto-filters to the hovered data point, so the tooltip visual needs NO manual filter for the hovered "
            "field. This is the ONLY supported way to wire tooltips — never hand-edit visual.json for this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tooltip_page": {
                    "type": "string",
                    "description": "Page identifier of the tooltip page (the one shown on hover)",
                },
                "source_page": {
                    "type": "string",
                    "description": "Page identifier holding the visual the user hovers over",
                },
                "source_visual": {
                    "type": "string",
                    "description": "Visual identifier (name) that triggers the tooltip on hover",
                },
                "width": {
                    "type": "number",
                    "description": "Tooltip canvas width in px. Default 360.",
                },
                "height": {
                    "type": "number",
                    "description": "Tooltip canvas height in px. Default 260.",
                },
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": ["tooltip_page", "source_page", "source_visual", "report_path"],
        },
    },
    {
        "name": "pbi_wire_drillthrough_page",
        "description": (
            "Make an EXISTING page a drillthrough target bound to one column, so the user can "
            "right-click a data point anywhere in the report and jump to that page filtered to "
            "the clicked value. WORKFLOW: first pbi_report_add_page for the detail page, then "
            "add its visuals with pbi_visual_add_bound — the page MUST end up with at least one "
            "visual or pbi_report_health_check fails (report_not_empty) — THEN call this tool. "
            "It writes (1) pageBinding type=Drillthrough carrying the bound field expression, "
            "(2) the matching Categorical / howCreated=Drillthrough filter, and (3) hides the "
            "page from the tab strip. It does NOT touch the source page or source visual — "
            "Desktop offers 'Drill through' automatically on every visual whose query uses the "
            "bound column. Do NOT add a filter for the drilled column to the detail page's "
            "visuals; Desktop injects the clicked value at runtime. Re-running REPLACES the "
            "binding (safe to re-run, use it to re-point the page at a different column). This "
            "is the ONLY supported way to author drillthrough — there is no 'Drillthrough' "
            "filter type, and hand-written JSON made Desktop refuse to load the entire report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_page": {
                    "type": "string",
                    "description": "Page identifier of the detail page users drill INTO. Must already exist and have >=1 visual.",
                },
                "field": {
                    "type": "string",
                    "description": "Column the drillthrough filters on, as Table[Column] — e.g. Sales_Fact[Region]. Must be a COLUMN (not a measure) that exists in the model. NOTE: this is only checked for Table[Column] STRING SYNTAX — the tool does not query the live model schema, so a nonexistent column or a measure name in this shape is written through without error; verify the reference is real yourself (e.g. via pbi_model_inspect) before calling this.",
                },
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": ["target_page", "field", "report_path"],
        },
    },
    {
        "name": "pbi_report_set_theme",
        "description": "Apply a theme JSON file to the report. Create the theme file first then pass its path. Only use when the user specifies colors/branding.",
        "input_schema": {
            "type": "object",
            "properties": {
                "theme_file": {
                    "type": "string",
                    "description": "Path to the theme JSON file",
                },
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                },
            },
            "required": ["theme_file", "report_path"],
        },
    },
    {
        "name": "pbi_report_validate",
        "description": "Validate the report structure and JSON files for errors.",
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder.",
                }
            },
            "required": ["report_path"],
        },
    },
    {
        "name": "pbi_desktop_status",
        "description": (
            "Check the Power BI Desktop Bridge connection. Returns the running Desktop "
            "instances (pid, open file, pages) as JSON, or an explanation if the bridge "
            "is unavailable. Use before pbi_desktop_sync, or when the user asks whether "
            "Desktop is connected."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pbi_desktop_sync",
        "description": (
            "Push the report you just built/edited into a RUNNING Power BI Desktop: "
            "opens the .pbip (launching Desktop if needed), reloads the definition from "
            "disk, and captures a PNG screenshot of every page. Call this ONCE at the "
            "END of a report build/edit so the user sees the live result. Returns the "
            "screenshot file paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pbip_path": {
                    "type": "string",
                    "description": (
                        "Path to the .pbip file — sits NEXT TO the .Report folder "
                        "(same name, .pbip extension). A .Report path is auto-converted."
                    ),
                },
            },
            "required": ["pbip_path"],
        },
    },
    {
        "name": "render_chart",
        "description": (
            "Draw an interactive chart INLINE IN THE CHAT to illustrate numbers you are "
            "explaining (a breakdown, trend, ranking, or comparison from a DAX query or "
            "the model). This does NOT touch the report on disk — it's a visual aid for "
            "the user in this conversation. Call it right after stating figures so the "
            "user can see them, not just read them. Provide simple data (categories + "
            "series); the app builds the chart. Use 'bar' for rankings/breakdowns, "
            "'line' for trends over time, 'pie' for share-of-total."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Chart title shown above it",
                },
                "chart_type": {
                    "type": "string",
                    "enum": [
                        "bar",
                        "column",
                        "hbar",
                        "stacked_bar",
                        "histogram",
                        "line",
                        "area",
                        "stacked_area",
                        "pie",
                        "donut",
                        "lollipop",
                        "scatter",
                        "bubble",
                        "scatter3d",
                        "funnel",
                        "waterfall",
                        "treemap",
                        "radar",
                        "heatmap",
                        "boxplot",
                        "gantt",
                    ],
                    "description": (
                        "Pick the type that fits the question. STANDARD (categories + one "
                        "or more numeric series aligned to categories): bar/column=vertical "
                        "ranking/breakdown; hbar=horizontal (long names / many items); "
                        "stacked_bar=composition across categories (multiple series); "
                        "histogram=distribution (one series, categories are bins); "
                        "line=trend; area=filled trend; stacked_area=composition over time "
                        "(multiple series); pie/donut=share-of-total; lollipop=clean ranking; "
                        "scatter=correlation; funnel=sequential stages (one series); "
                        "waterfall=bridge of how signed deltas ACCUMULATE to an end "
                        "total — bars float at the running total. ONLY for "
                        "contribution-to-total stories; plain per-period +/- "
                        "differences belong in bar (negatives auto-plot below zero "
                        "in red); "
                        "treemap=hierarchical magnitude (one series); radar=compare entities "
                        "across axes (categories are the AXES, each series is one entity). "
                        "SPECIAL DATA SHAPES: bubble→series[0]=Y, series[1]=size (optional); "
                        "scatter3d→an interactive, rotatable 3-D scatter: series[0]=X, "
                        "series[1]=Y, series[2]=Z (three numeric measures), categories=point "
                        "labels. Use ONLY when the user explicitly asks for 3D AND the three "
                        "measures genuinely vary; tell them it's for visual impact, not precise "
                        "reading. NOT a default — prefer 2D. "
                        "heatmap→categories are the X columns, each series is a Y row; "
                        "boxplot→series[0].data is a list of [min,q1,median,q3,max] arrays, "
                        "one per category; gantt→series[0]=start, series[1]=end per task "
                        "(categories are task names)."
                    ),
                },
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "X-axis / slice labels, e.g. ['2021','2022','2023'] or ['North','South']. Required for bar/line/area; for pie these are the slice names.",
                },
                "series": {
                    "type": "array",
                    "description": "One object per data series. data aligns 1:1 with categories.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Series label, e.g. 'Revenue'",
                            },
                            "data": {
                                "type": "array",
                                "items": {"type": ["number", "array"]},
                                "description": (
                                    "Numeric values, same length/order as categories. "
                                    "ALWAYS pass RAW values in original units — write "
                                    "6200000, never pre-scale to 6.2 'millions'; the "
                                    "chart auto-formats axis labels to K/M/B. "
                                    "For boxplot, each item is a 5-number array "
                                    "[min, q1, median, q3, max] instead of a single number."
                                ),
                            },
                        },
                        "required": ["data"],
                    },
                },
            },
            "required": ["chart_type", "series"],
        },
    },
    {
        "name": "render_map",
        "description": (
            "Draw an interactive geospatial map INLINE IN THE CHAT using point or arc "
            "data from a DAX query (e.g. sales by city, store locations, delivery routes). "
            "Use 'scatter' for single locations, 'heatmap' for density, 'arc' for "
            "origin→destination flows. This does NOT touch the report on disk."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Map title shown above it",
                },
                "layer_type": {
                    "type": "string",
                    "enum": ["scatter", "heatmap", "arc"],
                    "description": (
                        "scatter = one point per location (size/colour driven by value); "
                        "heatmap = density surface from lat/lon density; "
                        "arc = curved lines from source_lat/source_lon to target_lat/target_lon."
                    ),
                },
                "points": {
                    "type": "array",
                    "description": (
                        "For scatter/heatmap: array of {lat, lon, label?, value?}. "
                        "For arc: array of {source_lat, source_lon, target_lat, target_lon, label?, value?}."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "lat": {"type": "number"},
                            "lon": {"type": "number"},
                            "source_lat": {"type": "number"},
                            "source_lon": {"type": "number"},
                            "target_lat": {"type": "number"},
                            "target_lon": {"type": "number"},
                            "label": {"type": "string"},
                            "value": {"type": "number"},
                        },
                    },
                },
            },
            "required": ["layer_type", "points"],
        },
    },
    {
        "name": "propose_model_edit",
        "description": (
            "PROPOSE a model change — either CREATE a new measure, calculated column, "
            "relationship, or calculation group, or UPDATE an existing measure, column, "
            'or relationship (operation="update"). This QUEUES the change for the user '
            "to approve — it is NOT applied until they click Approve in the 'Pending "
            "model edits' panel. Direct mutating calls to the modeling tools are BLOCKED; "
            "this queue is the ONLY way to change the model. ALWAYS give a clear `reason`. "
            "A CREATE is temporary and removed at session end; an approved UPDATE to an "
            "existing object is a permanent change (there is no auto-revert), so only "
            "propose one when the user clearly wants to change that object."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "update"],
                    "description": '"create" (default) adds a NEW object; "update" changes an EXISTING measure/column/relationship. calculation_group supports create only.',
                },
                "object_type": {
                    "type": "string",
                    "enum": ["measure", "column", "relationship", "calculation_group"],
                    "description": "What to create or update.",
                },
                "name": {
                    "type": "string",
                    "description": "Object name (measure/column name, a relationship name, or the calculation group / table name). For update, this names the EXISTING object to change.",
                },
                "table": {
                    "type": "string",
                    "description": "Host table for a measure or calculated column. Omit for a calculation group (its `name` IS its table).",
                },
                "definition": {
                    "description": (
                        "For measure/column: the DAX expression. For relationship: an "
                        "object {fromTable, fromColumn, toTable, toColumn, "
                        "fromCardinality, toCardinality, crossFilteringBehavior, isActive}. "
                        "For calculation_group: an object {precedence?, isHidden?, "
                        "calculationItems: [{name, expression, ordinal?, "
                        "formatStringExpression?}, ...]} — each item's `expression` is DAX "
                        "using SELECTEDMEASURE()."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "REQUIRED — why this edit is needed (shown to the user for approval).",
                },
                "format_string": {
                    "type": "string",
                    "description": "Optional measure format string.",
                },
                "display_folder": {
                    "type": "string",
                    "description": "Optional display folder.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Semantic model this edit targets, e.g. 'MavenMarket_Report'. "
                        "REQUIRED when more than one model is connected — the DAX is "
                        "validated against this model, so the wrong one fails for the "
                        "wrong reason. Omit only when a single model is connected."
                    ),
                },
            },
            "required": ["object_type", "name", "definition", "reason"],
        },
    },
    {
        "name": "pbi_model_quality",
        "description": (
            "Score the semantic model's QUALITY (0-100 + letter grade) and return "
            "an actionable issue list: measure correctness, relationship correctness, "
            "and best-practice checks (missing format strings, bare '/' instead of "
            "DIVIDE(), empty/BLANK() placeholder measures, no marked date table). "
            "Reads the local .SemanticModel TMDL. Run this after building/changing the "
            "model and report the score to the user; fix any ERROR issues and re-score."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the .SemanticModel folder, the sibling .Report, the .pbip, or the project folder — it's resolved automatically.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "pbi_tmdl_doctor",
        "description": (
            "Triggered by '/tmdl-doctor' or a request to diagnose/fix broken TMDL. "
            "Runs the deterministic TMDL fixer pipeline against a .SemanticModel's "
            "definition/ folder: repairs flat relationships, orphan relationship "
            "references, missing column dataTypes, malformed formatStrings, "
            "placeholder DAX_EXPRESSION stubs, duplicate columns, embedded file "
            "markers, and stray *_partitions files. It ALSO DELETES any tables/*.tmdl "
            "file that has no table/model/relationship declaration anywhere in it "
            "(garbage-file cleanup) — review the dry-run report's removed-file list "
            "carefully before setting apply=true, this is destructive. Missing "
            "dataTypes are inferred from the COLUMN NAME (a heuristic guess, not "
            "from real data) and flagged as such in the report — verify before "
            "trusting them. On a genuine duplicate column, the fixer keeps whichever "
            "definition is more complete, not just whichever came first. Always "
            "reports what it found and what it would fix; pass apply=true to "
            "actually write the fixes to disk (default is a dry-run report only)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the .SemanticModel folder, the sibling .Report, the .pbip, or the project folder — it's resolved automatically.",
                },
                "apply": {
                    "type": "boolean",
                    "description": "If true, write the fixes to disk. Default false (dry-run report only).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "pbi_report_review",
        "description": (
            "Triggered by '/report-review' or a request to review the whole "
            "dashboard. Analyzes every page and visual of a PBIR report (types, "
            "titles, field bindings) in one pass. Pass `change_request` to also "
            "get a proposed change-set (summary + itemized changes) for that "
            "request — proposals are suggestions only; use the normal pbi_visual_* "
            "tools to actually apply any change you and the user agree on."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {
                    "type": "string",
                    "description": "Path to the .Report folder or .pbip.",
                },
                "change_request": {
                    "type": "string",
                    "description": "Optional — describe a change to propose against the analyzed dashboard.",
                },
            },
            "required": ["report_path"],
        },
    },
    {
        "name": "pbi_generate_docs",
        "description": (
            "Generate PROJECT DOCUMENTATION from the semantic model's TMDL. "
            "doc_type picks the document: data_dictionary (tables/columns/measures/"
            "relationships), tech_design (architecture, fact-vs-dimension split, "
            "relationship map, open items), measure_spec (per-measure definition + "
            "resolved dependencies), business_rules (every literal filter and ratio "
            "a measure encodes — the assumptions a business owner signs off), "
            "testing_checklist (one checkable item per measure/relationship), "
            "deployment_notes (pre/deploy/post/rollback runbook), release_notes "
            "(draft seeded from current contents). Pass save=true to write the file "
            "next to the model. Everything is derived from the real model — no "
            "invented content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the .SemanticModel / .Report / .pbip / project folder — resolved automatically.",
                },
                "doc_type": {
                    "type": "string",
                    "enum": [
                        "data_dictionary",
                        "tech_design",
                        "measure_spec",
                        "business_rules",
                        "testing_checklist",
                        "deployment_notes",
                        "release_notes",
                    ],
                    "description": "Which document to generate. Default data_dictionary.",
                },
                "save": {
                    "type": "boolean",
                    "description": "If true, also write the document next to the model.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "pbi_dax_analyze",
        "description": (
            "EXPLAIN, OPTIMIZE or DEBUG a DAX expression — deterministic analysis, "
            "not guesswork. mode='explain': resolves every table/column/measure the "
            "expression touches against the live model registry (flagging any that "
            "don't exist), names the functions used and the evaluation shape "
            "(iterator / filter-context change / time intelligence). mode='optimize': "
            "scans for real anti-patterns (bare division, COUNTROWS(FILTER()), "
            "FILTER(ALL()), DISTINCTCOUNT inside an iterator, repeated sub-expressions "
            "that should be a VAR, …) and names the rewrite. mode='debug': runs the "
            "reference gate then probes the expression against the LIVE engine and "
            "returns the engine's actual error — OR 'INCONCLUSIVE' if the probe itself "
            "hit an infra error (timeout/auth/connection), which is NOT a correctness "
            "signal either way, only 'passed' or 'FAILED' are. Use this instead of "
            "eyeballing DAX."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "The DAX expression (a measure/column definition, not a full EVALUATE query).",
                },
                "mode": {
                    "type": "string",
                    "enum": ["explain", "optimize", "debug"],
                    "description": "explain | optimize | debug. Default explain.",
                },
                "table": {
                    "type": "string",
                    "description": "Home table — required for mode=debug so the engine probe can DEFINE it.",
                },
                "object_type": {
                    "type": "string",
                    "enum": ["measure", "column"],
                    "description": "What the expression defines. Default measure.",
                },
            },
            "required": ["expression"],
        },
    },
    {
        "name": "pbi_export_model",
        "description": (
            "Export the semantic model to a shareable Excel data dictionary "
            "(Columns / Measures / Relationships sheets). Reads the local "
            ".SemanticModel TMDL. Use when the user wants the model in Excel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the .SemanticModel / .Report / .pbip / project folder — resolved automatically.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "pbi_paginated_create",
        "description": (
            "Generate a guaranteed-valid, professionally-designed PAGINATED report "
            "(.rdl) bound to a Fabric semantic model via a DAX query. layout='table' "
            "uses columns; layout='matrix' uses rows_field/cols_field/value_field. "
            "For a polished report (not a bare grid): add summary_cards (a KPI band "
            "across the top), group_field (banded group headers + per-group "
            "subtotals), and per-column conditional='posneg' (red/green by sign). "
            "Writes the .rdl, then edit it with pbi_paginated_get/set. NOT for "
            "interactive dashboards — those are PBIR (pbi_report_create)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Report file name, or '<project>/paginated/<Report>.rdl'. "
                        "Always resolved inside the project store — every "
                        "paginated report is written to "
                        "<store>/<project>/paginated/<Report>.rdl, so an "
                        "absolute path or one containing '..' is re-rooted "
                        "there rather than followed. Omit the project to use "
                        "the one selected in the sidebar."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Report title shown in the branded header band.",
                },
                "subtitle": {
                    "type": "string",
                    "description": "Optional subtitle under the title (e.g. what the report shows).",
                },
                "dataset": {
                    "type": "string",
                    "description": "Semantic model (dataset) name to connect to.",
                },
                "dax": {
                    "type": "string",
                    "description": "DAX EVALUATE query that returns the report rows.",
                },
                "layout": {
                    "type": "string",
                    "enum": ["table", "matrix"],
                    "description": "Default 'table'.",
                },
                "columns": {
                    "type": "array",
                    "description": "Table layout: one entry per column. Output is professionally styled by default (branded header band, navy header row, zebra rows, page footer).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {
                                "type": "string",
                                "description": "EXACT DAX result column, FULLY BRACKETED — Table[Column] (e.g. Sales_Fact[Country]) or [Measure] (e.g. [Total Sales]). Bare names render BLANK.",
                            },
                            "header": {
                                "type": "string",
                                "description": "Column header text.",
                            },
                            "format": {
                                "type": "string",
                                "description": "RDL number format, e.g. $#,##0 (currency), 0.0% (percent), #,##0 (thousands). Numeric columns (with a format) are right-aligned.",
                            },
                            "total": {
                                "type": "string",
                                "enum": ["sum"],
                                "description": "Set to 'sum' to include this column in the grand-total / per-group subtotal rows (use for amounts/counts, NOT ratios like margin %).",
                            },
                            "conditional": {
                                "type": "string",
                                "enum": ["posneg"],
                                "description": "Set 'posneg' to colour the value red when negative and green when positive — use on variance/profit/change columns.",
                            },
                        },
                        "required": ["field"],
                    },
                },
                "total_row": {
                    "type": "boolean",
                    "description": "Add a bold grand-total row at the bottom (sums the columns marked total='sum'). Default false.",
                },
                "group_field": {
                    "type": "string",
                    "description": "Table layout: FULLY-BRACKETED DAX column to group rows by (e.g. Sales_Fact[Region]). Adds a banded group header per value and a per-group subtotal row. If it matches one of `columns`, that column is shown only in the band (not repeated per row).",
                },
                "page_break_per_group": {
                    "type": "boolean",
                    "description": "With group_field: start each group on a new page. Set true for LONG grouped reports — it renders the first page while the rest stream and bounds memory (MS perf guidance). Default false.",
                },
                "summary_cards": {
                    "type": "array",
                    "description": "Up to 5 KPI tiles in a band across the top (label + big value). Great for an executive summary above the table.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "Caption under the value, e.g. 'Total Sales'.",
                            },
                            "field": {
                                "type": "string",
                                "description": "FULLY-BRACKETED DAX result column/measure to aggregate (must be present in the DAX result), e.g. [Total Sales].",
                            },
                            "agg": {
                                "type": "string",
                                "enum": [
                                    "sum",
                                    "avg",
                                    "count",
                                    "countdistinct",
                                    "min",
                                    "max",
                                ],
                                "description": "How to aggregate `field` across the result. Default 'sum'.",
                            },
                            "value": {
                                "type": "string",
                                "description": 'Alternative to field/agg: a raw RDL expression, e.g. =CountRows("Data"). Use for row counts or custom math.',
                            },
                            "format": {
                                "type": "string",
                                "description": "RDL number format for the value, e.g. $#,##0.",
                            },
                        },
                        "required": ["label"],
                    },
                },
                "rows_field": {
                    "type": "string",
                    "description": "Matrix layout: DAX column for row groups.",
                },
                "cols_field": {
                    "type": "string",
                    "description": "Matrix layout: DAX column for column groups.",
                },
                "value_field": {
                    "type": "string",
                    "description": "Matrix layout: DAX column/measure for the cell value.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Fabric workspace name. Defaults to the configured workspace.",
                },
                "landscape": {
                    "type": "boolean",
                    "description": "Landscape page orientation for wide tables. Default false.",
                },
            },
            "required": ["path", "title", "dataset", "dax"],
        },
    },
    {
        "name": "pbi_paginated_get",
        "description": "Read the raw RDL XML of a paginated report so you can edit it. Returns the full .rdl text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the .rdl file."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "pbi_paginated_set",
        "description": (
            "Write edited RDL XML back to a .rdl file. STRUCTURALLY VALIDATES first "
            "and REFUSES to save broken RDL (returns the issues instead) — so you "
            "can't corrupt the report. Use after pbi_paginated_get + your edits. "
            "Rules SSRS enforces at load that you must follow when writing XML by "
            "hand: (1) every report item Name (Textbox, Tablix, Chart, Image) is "
            "unique across the WHOLE report, not per container; (2) every "
            "Fields!X must be declared as a <Field> in its dataset — a KPI card "
            "over a measure needs that measure in <Fields> too; (3) with two or "
            "more datasets, a Textbox outside a Tablix/Chart must scope its field "
            "with an aggregate, e.g. =First(Fields!X.Value, \"dsKPI\"), never a "
            "bare Fields!X; (4) a ReportParameter may be MultiValue or Nullable, "
            "never both, and MultiValue cannot be Boolean; (5) a Chart needs "
            "<DataSetName> and <ChartData>; (6) a Nullable parameter is not the "
            "way to mean 'no filter' — give a MultiValue parameter a default that "
            "selects everything instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the .rdl file."},
                "xml": {
                    "type": "string",
                    "description": "The full edited RDL document.",
                },
            },
            "required": ["path", "xml"],
        },
    },
    {
        "name": "pbi_paginated_validate",
        "description": "Structurally validate a paginated report (.rdl): well-formed XML, correct namespace, DataSource/DataSet/Tablix present and wired, page size set. Returns 'ok' or the list of issues.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the .rdl file."}
            },
            "required": ["path"],
        },
    },
    {
        "name": "pbi_paginated_delete",
        "description": "Delete a paginated report (.rdl) file. Only deletes .rdl files inside the project store. Use when the user wants to remove a paginated report.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the .rdl file to delete.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "pbi_paginated_list",
        "description": "List the user's paginated reports (.rdl) by name across their projects — no path needed. Use to answer 'do I have a paginated report?' / 'what reports do I have?' / to explore what exists.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pbi_report_inspect",
        "description": (
            "INSPECT ANY FORMAT — summarise a report on disk: pages, visual "
            "counts, visual types, and every field binding. Unlike "
            "pbi_report_info / pbi_visual_list, this also reads PBIR-Legacy "
            "reports (the single-report.json format that Fabric will not "
            "convert on export). WORKFLOW: after pbi_report_fetch, call this to "
            "describe the report. If pbi_report_info or pbi_visual_list came "
            "back empty, call this before concluding anything — an empty PBIR "
            "result usually means the report is legacy, not that it has no "
            "visuals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_path": {
                    "type": "string",
                    "description": (
                        "Path to the .Report folder, as returned by "
                        "pbi_report_fetch."
                    ),
                },
                "bindings": {
                    "type": "boolean",
                    "description": (
                        "Return the per-visual binding list instead of the "
                        "summary. Default false."
                    ),
                },
            },
            "required": ["report_path"],
        },
    },
    {
        "name": "pbi_report_fetch",
        "description": (
            "BRIDGE — download a report that lives in the Fabric SERVICE onto "
            "local disk as a PBIP project, so the on-disk analysis tools can "
            "read it. WORKFLOW: every other report tool "
            "(pbi_report_info, pbi_visual_list, pbi_visual_bindings, "
            "pbi_dashboard_audit, pbi_report_health_check) needs a local "
            "`report_path` and CANNOT see reports in the service. So when the "
            "user asks you to analyse, audit, review or explain a report that "
            "is in the workspace rather than already on disk, call this FIRST, "
            "then pass the `report:` path it returns to those tools. Use "
            "pbi_workspace_items or pbi_reports_list first if you need the "
            "exact report name. "
            "CRITICAL: if this returns FAILED, nothing was downloaded — do not "
            "describe the report's contents. If it warns the report is "
            "PBIR-Legacy, the analysis tools will return EMPTY on it; report "
            "that the format blocks inspection rather than saying the report "
            "has no visuals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report": {
                    "type": "string",
                    "description": (
                        "Report name exactly as it appears in the workspace."
                    ),
                },
                "include_pbix": {
                    "type": "boolean",
                    "description": (
                        "Also download the binary .pbix. Only when the user "
                        "wants the file itself; not needed for analysis."
                    ),
                },
            },
            "required": ["report"],
        },
    },
    {
        "name": "pbi_workspace_items",
        "description": (
            "DISCOVERY — list what exists in a Fabric workspace: semantic models, "
            "warehouses, lakehouses, SQL/KQL databases, or the workspaces "
            "themselves. WORKFLOW: call this whenever the user asks what EXISTS "
            "rather than what is inside the model they are connected to — 'list "
            "all the semantic models', 'what models are in this workspace', "
            "'which workspaces can you see', 'find the model called X', 'what "
            "databases are there'. Pass `name_filter` to search by name instead "
            "of listing everything. "
            "CRITICAL — do NOT confuse this with the modeling tools: "
            "`table_operations`/`column_operations`/`measure_operations` list what "
            "is INSIDE the one connected model. This tool lists the models "
            "themselves. 'List the semantic models' means THIS tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "semantic_models",
                        "data_sources",
                        "reports",
                        "workspaces",
                        "all",
                    ],
                    "description": (
                        "What to list. 'semantic_models' (default) for datasets, "
                        "'data_sources' for warehouses/lakehouses/SQL/KQL "
                        "databases, 'reports' for the reports PUBLISHED in the "
                        "Fabric service (use this for 'what reports are in the "
                        "workspace?' — pbi_reports_list only sees LOCAL .pbip "
                        "projects on disk), 'workspaces' for the workspace list "
                        "itself, 'all' for models plus data sources plus reports."
                    ),
                },
                "workspace": {
                    "type": "string",
                    "description": (
                        "Workspace name. Omit to use the currently connected "
                        "workspace. Ignored when kind='workspaces'."
                    ),
                },
                "name_filter": {
                    "type": "string",
                    "description": (
                        "Case-insensitive substring to match names against — use "
                        "this to search rather than listing everything."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "pbi_impact_analysis",
        "description": (
            "SAFETY NET — before dropping or renaming anything in a published "
            "semantic model, call this to find out what breaks. Reads the tenant "
            "lineage graph and names the exact reports, visuals, dashboards and "
            "chained datasets that reference the fields you are about to remove. "
            "WORKFLOW: call this BEFORE proposing a destructive model edit, not "
            "after — the whole point is to catch the breakage while it is still "
            "cheap. Report its findings to the user verbatim; do NOT summarise a "
            "list of broken reports down to 'some reports may be affected'. "
            "CRITICAL: the output separates confirmed breakage from items it "
            "could not check. Never describe an unchecked item as safe, and never "
            "claim 'nothing will break' if the tool returned an ERROR — an empty "
            "result from a failed scan is not a clean bill of health."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": (
                        "Semantic model (dataset) name being changed, as it "
                        "appears in the Fabric workspace."
                    ),
                },
                "removed_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Fields the change will drop or rename, each written "
                        "Table[Field] — e.g. ['Sales[Total Revenue]', "
                        "'Sales[Region]']. A bare table name means the whole "
                        "table is going. Renames count: pass the OLD name."
                    ),
                },
                "workspace": {
                    "type": "string",
                    "description": (
                        "Workspace name. Only needed when two workspaces hold a "
                        "model with the same name."
                    ),
                },
                "refresh": {
                    "type": "boolean",
                    "description": (
                        "Force a fresh tenant scan instead of the cached graph. "
                        "Slow (minutes) and rate-limited — use only when the "
                        "model or report was created since the last scan."
                    ),
                },
            },
            "required": ["model", "removed_fields"],
        },
    },
    {
        "name": "pbi_user_access",
        "description": (
            "DISCOVERY — list every Fabric/Power BI item one named person can "
            "access across the whole tenant, with the access right on each. Use "
            "for 'what can <user> see?', 'does <user> have access to <report>?', "
            "or an access review before changing someone's permissions. Takes a "
            "UPN (someone@company.com) or an Entra object ID; both work. "
            "Read-only — it reports access and cannot grant or revoke it. Prefers "
            "the tenant Admin API, which works for anyone whether or not they are "
            "signed in; if that is not enabled it falls back to the signed-in "
            "user's own delegated view, and ONLY when the userId asked about is "
            "that same person. Check the `source` field: 'admin-api' is "
            "authoritative per-item, 'delegated' inherits rights from the "
            "workspace and covers the caller alone. Rate-limited to 200 "
            "calls/hour tenant-wide, so do not loop it over a list of users."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "userId": {
                    "type": "string",
                    "description": (
                        "UPN (someone@company.com) or Entra object ID (GUID). "
                        "Guests use the encoded form, "
                        "e.g. user_other.com#EXT#@yourtenant.com"
                    ),
                },
                "artifactTypes": {
                    "type": "string",
                    "description": (
                        "Optional comma-separated filter, e.g. 'Report,Dataset'. "
                        "One or more of: Report, PaginatedReport, Dashboard, "
                        "Dataset, Dataflow, Workspace, Group, PersonalGroup, App, "
                        "Capacity. Narrow with this rather than paging widely."
                    ),
                },
            },
            "required": ["userId"],
        },
    },
    {
        "name": "pbi_lineage_map",
        "description": (
            "DISCOVERY — show what consumes a semantic model across the whole "
            "tenant: reports, dashboards, and datasets chained off it, with how "
            "many hops away each sits. Use for 'what depends on this model?', "
            "'who uses this dataset?', or 'is this model safe to retire?'. Omit "
            "`model` to get a summary of the whole lineage graph instead. "
            "Read-only. For 'what breaks if I change X', use pbi_impact_analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": (
                        "Semantic model name. Omit for a tenant-wide summary."
                    ),
                },
                "workspace": {
                    "type": "string",
                    "description": "Workspace name, to disambiguate duplicates.",
                },
                "refresh": {
                    "type": "boolean",
                    "description": (
                        "Force a fresh tenant scan. Slow and rate-limited."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "pbi_report_open",
        "description": (
            "OPEN A PUBLISHED REPORT BY NAME and show what is inside it — pages, "
            "visuals and field bindings. This is the tool for 'show me <report>', "
            "'what's in <report>', 'open <report>', 'analyse <report>'. "
            "It does the whole chain itself: resolves the name against the Fabric "
            "workspace, downloads it as a local PBIP if it isn't already there, "
            "detects PBIR vs PBIR-Legacy, and reads it with the right reader. "
            "Do NOT call pbi_reports_list first — that only sees LOCAL .pbip "
            "projects and will look empty. To discover which reports exist, use "
            'pbi_workspace_items kind="reports". '
            "The returned ACTIVE_REPORT_PATH is the local folder for the on-disk "
            "editing tools."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report": {
                    "type": "string",
                    "description": (
                        "Published report name, e.g. 'DIAD Final Report'. Must "
                        "match a report in the connected workspace."
                    ),
                },
                "bindings": {
                    "type": "boolean",
                    "description": (
                        "Also list every visual's field bindings (legacy reports). "
                        "Verbose — use when asked which fields a report uses."
                    ),
                },
                "refresh": {
                    "type": "boolean",
                    "description": (
                        "Re-download even if a local copy exists. Use when the "
                        "published report has changed since it was fetched."
                    ),
                },
            },
            "required": ["report"],
        },
    },
    {
        "name": "pbi_model_inspect",
        "description": (
            "READ THE LIVE SEMANTIC MODEL over XMLA — tables, columns, measures, "
            "relationships and row counts, straight from the deployed model. "
            "Hidden/private tables and their columns are filtered out of `tables`/"
            "`columns_by_table` (auto-generated Date/calc-group noise tables included) "
            "— measures defined on such a table are filtered out of `measures_by_table` "
            "too, for consistency; a '0 visuals'-style surprise here means the table "
            "really is hidden/private, not that nothing exists on it. "
            "USE THIS INSTEAD OF `table_operations` / `column_operations` / "
            "`measure_operations`: those MCP tools require a `connection_operations` "
            "ConnectFabric that cannot authenticate as this tenant's service "
            "principal, so they fail. This tool connects with the service principal "
            "directly and works. "
            "Defaults to the connected workspace/model, so usually call it with no "
            "arguments. Use `pbi_workspace_items` to list the models themselves; "
            "this shows what is INSIDE one model."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": (
                        "Semantic model name. Omit to use the connected model."
                    ),
                },
                "workspace": {
                    "type": "string",
                    "description": (
                        "Workspace name. Omit to use the connected workspace."
                    ),
                },
                "section": {
                    "type": "string",
                    "enum": ["all", "tables", "measures", "columns", "relationships"],
                    "description": (
                        "Which part of the model to return. Default 'all'."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "pbi_dax_query",
        "description": (
            "RUN A DAX QUERY against the live semantic model over XMLA and return "
            "the rows (capped at 200). Use this for any real number, aggregate or "
            "breakdown the user asks for — never estimate or recall figures from "
            "chat history. "
            "USE THIS INSTEAD OF `dax_query_operations`: that MCP tool needs a "
            "ConnectFabric that fails for this tenant's service principal. "
            "The query must start with EVALUATE (or DEFINE ... EVALUATE). "
            "Defaults to the connected workspace/model."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dax": {
                    "type": "string",
                    "description": (
                        "The DAX query, e.g. EVALUATE TOPN(10, 'Sales'). Must "
                        "start with EVALUATE or DEFINE."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Semantic model name. Omit to use the connected model."
                    ),
                },
                "workspace": {
                    "type": "string",
                    "description": (
                        "Workspace name. Omit to use the connected workspace."
                    ),
                },
            },
            "required": ["dax"],
        },
    },
]
