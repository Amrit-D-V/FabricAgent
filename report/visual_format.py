from __future__ import annotations

import glob
import json
import os

from config import DESIGN_TOKENS, REPORT_LAYOUT, TINT_FACTORS
from report.dashboard_polish import (
    HEADER_BG as _HEADER_BG,
)
from report.dashboard_polish import (
    HEADER_FG as _HEADER_FG,
)
from report.dashboard_polish import (
    NAV_SELECTED as _NAV_SELECTED,
)
from report.dashboard_polish import (
    NAV_UNSELECTED as _NAV_UNSELECTED,
)
from report.dashboard_polish import (
    THEME_JSON as _THEME_DEFAULTS,
)
from report.dashboard_polish import (
    _tint,
)


def _str(value: str) -> dict:
    return {"expr": {"Literal": {"Value": f"'{value}'"}}}


def _num(value: float) -> dict:
    return {"expr": {"Literal": {"Value": f"{value}D"}}}


def _bool(value: bool) -> dict:
    return {"expr": {"Literal": {"Value": "true" if value else "false"}}}


def _color(hex_color: str) -> dict:
    return {"solid": {"color": _str(hex_color)}}


def _gradient_stop(hex_color: str) -> dict:
    return {"color": {"Literal": {"Value": f"'{hex_color}'"}}}


def _find_visual_json(
    report_path: str, visual_name: str, page: str | None = None
) -> str | None:
    if page:
        matches = glob.glob(
            f"{report_path}/**/pages/{page}/visuals/{visual_name}/visual.json",
            recursive=True,
        )
        if matches:
            return matches[0]
    matches = glob.glob(
        f"{report_path}/**/visuals/{visual_name}/visual.json",
        recursive=True,
    )
    return matches[0] if matches else None


def _load_visual(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_visual(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


_LABEL_OBJECT = "labels"


def apply_format(
    report_path: str,
    visual_name: str,
    *,
    data_label_show: bool | None = None,
    data_label_color: str | None = None,
    data_label_size: float | None = None,
    axis_show: bool | None = None,
    axis_color: str | None = None,
    legend_show: bool | None = None,
    legend_position: str | None = None,
    background_color: str | None = None,
    title_color: str | None = None,
    title_size: float | None = None,
    title_align: str | None = None,
    data_colors: list[str] | None = None,
    page: str | None = None,
) -> str:
    path = _find_visual_json(report_path, visual_name, page)
    if not path:
        return f"[format] ERROR — visual '{visual_name}' not found under {report_path}"

    vdata = _load_visual(path)
    vis = vdata.setdefault("visual", {})
    objects = vis.setdefault("objects", {})
    container = vis.setdefault("visualContainerObjects", {})
    changes: list[str] = []
    is_card_visual = vis.get("visualType") == "cardVisual"

    if data_label_show is not None or data_label_color or data_label_size is not None:
        props: dict = {}
        if data_label_show is not None:
            props["show"] = _bool(data_label_show)
        if data_label_color:
            props["color"] = _color(data_label_color)
            if is_card_visual:
                props["fontColor"] = _color(data_label_color)
        if data_label_size is not None:
            props["fontSize"] = _num(data_label_size)
        if is_card_visual:
            entries = objects.setdefault("value", [])
            entry = next(
                (e for e in entries if e.get("selector") == {"id": "default"}), None
            )
            if entry is None:
                entry = {"properties": {}, "selector": {"id": "default"}}
                entries.append(entry)
            entry.setdefault("properties", {}).update(props)
            changes.append("value (cardVisual)")
        else:
            _merge_object(objects, _LABEL_OBJECT, props)
            changes.append("labels")

    if axis_show is not None or axis_color:
        props = {}
        if axis_show is not None:
            props["show"] = _bool(axis_show)
        if axis_color:
            props["color"] = _color(axis_color)
        _merge_object(objects, "valueAxis", props)
        changes.append("axis")

    if legend_show is not None or legend_position:
        props = {}
        if legend_show is not None:
            props["show"] = _bool(legend_show)
        if legend_position:
            props["position"] = _str(legend_position)
        _merge_object(objects, "legend", props)
        changes.append("legend")

    if background_color:
        _merge_container(
            container,
            "background",
            {
                "show": _bool(True),
                "color": _color(background_color),
                "transparency": _num(0),
            },
        )
        changes.append(f"bg={background_color}")

    if title_color or title_size is not None or title_align:
        props = {}
        if title_color:
            props["fontColor"] = _color(title_color)
        if title_size is not None:
            props["fontSize"] = _num(title_size)
        if title_align:
            props["alignment"] = _str(title_align)
        _merge_container(container, "title", props)
        changes.append("title")

    if data_colors:
        _merge_object(
            objects,
            "dataPoint",
            {
                "defaultColor": _color(data_colors[0]),
            },
        )
        if len(data_colors) > 1:
            changes.append(
                f"defaultColor={data_colors[0]} (+{len(data_colors)-1} ignored — multi-series needs per-category selectors)"
            )
        else:
            changes.append(f"color={data_colors[0]}")

    if not changes:
        return "[format] no-op — pass at least one formatting parameter"

    _save_visual(path, vdata)
    return f"[format] {visual_name}: {', '.join(changes)}"


def _merge_object(objects: dict, key: str, props: dict) -> None:
    entries = objects.setdefault(key, [{"properties": {}}])
    if not entries:
        entries.append({"properties": {}})
    entries[0].setdefault("properties", {}).update(props)


def _merge_container(container: dict, key: str, props: dict) -> None:
    entries = container.setdefault(key, [{"properties": {}}])
    if not entries:
        entries.append({"properties": {}})
    entries[0].setdefault("properties", {}).update(props)


_TABLE_LIKE_TYPES = {"tableEx", "pivotTable", "table", "matrix"}


def apply_conditional_color(
    report_path: str,
    visual_name: str,
    *,
    target_table: str,
    target_measure: str,
    min_color: str,
    max_color: str,
    mid_color: str | None = None,
    page: str | None = None,
) -> str:
    path = _find_visual_json(report_path, visual_name, page)
    if not path:
        return f"[cond-color] ERROR — visual '{visual_name}' not found"

    vdata = _load_visual(path)
    vis = vdata.setdefault("visual", {})
    vtype = vis.get("visualType")
    if vtype not in _TABLE_LIKE_TYPES:
        return (
            f"[cond-color] SKIP — '{visual_name}' is {vtype!r}; conditional "
            f"cell coloring only applies to {sorted(_TABLE_LIKE_TYPES)}"
        )
    objects = vis.setdefault("objects", {})

    if mid_color:
        fill_rule = {
            "linearGradient3": {
                "min": _gradient_stop(min_color),
                "mid": _gradient_stop(mid_color),
                "max": _gradient_stop(max_color),
            }
        }
    else:
        fill_rule = {
            "linearGradient2": {
                "min": _gradient_stop(min_color),
                "max": _gradient_stop(max_color),
            }
        }

    back_color = {
        "solid": {
            "color": {
                "expr": {
                    "FillRule": {
                        "Input": {
                            "Measure": {
                                "Expression": {"SourceRef": {"Entity": target_table}},
                                "Property": target_measure,
                            }
                        },
                        "FillRule": fill_rule,
                    }
                }
            }
        }
    }

    metadata_ref = target_measure
    entries = objects.setdefault("values", [])
    entries[:] = [e for e in entries if "backColor" not in (e.get("properties") or {})]
    entries.append(
        {
            "properties": {"backColor": back_color},
            "selector": {"metadata": metadata_ref},
        }
    )

    _save_visual(path, vdata)
    span = (
        f"{min_color}→{mid_color}→{max_color}"
        if mid_color
        else f"{min_color}→{max_color}"
    )
    return f"[cond-color] {visual_name}: backColor on {target_table}[{target_measure}] {span}"


_BARLIKE_TYPES = {
    "clusteredBarChart",
    "clusteredColumnChart",
    "barChart",
    "columnChart",
    "stackedBarChart",
    "stackedColumnChart",
    "hundredPercentStackedBarChart",
    "hundredPercentStackedColumnChart",
}


def color_bars_by_measure(
    report_path: str,
    visual_name: str,
    *,
    target_table: str,
    target_measure: str,
    min_color: str,
    max_color: str,
    mid_color: str | None = None,
    page: str | None = None,
) -> str:
    path = _find_visual_json(report_path, visual_name, page)
    if not path:
        return f"[bar-gradient] ERROR — visual '{visual_name}' not found"

    vdata = _load_visual(path)
    vis = vdata.setdefault("visual", {})
    vtype = vis.get("visualType", "")
    if vtype not in _BARLIKE_TYPES:
        return (
            f"[bar-gradient] ERROR — visual '{visual_name}' is '{vtype}'. This "
            f"colors bar/column charts only. Matrix/table → pbi_visual_conditional_color."
        )

    if mid_color:
        grad = {
            "linearGradient3": {
                "min": _gradient_stop(min_color),
                "mid": _gradient_stop(mid_color),
                "max": _gradient_stop(max_color),
                "nullColoringStrategy": {
                    "strategy": {"Literal": {"Value": "'asZero'"}}
                },
            }
        }
    else:
        grad = {
            "linearGradient2": {
                "min": _gradient_stop(min_color),
                "max": _gradient_stop(max_color),
                "nullColoringStrategy": {
                    "strategy": {"Literal": {"Value": "'asZero'"}}
                },
            }
        }
    fill = {
        "solid": {
            "color": {
                "expr": {
                    "FillRule": {
                        "Input": {
                            "Measure": {
                                "Expression": {"SourceRef": {"Entity": target_table}},
                                "Property": target_measure,
                            }
                        },
                        "FillRule": grad,
                    }
                }
            }
        }
    }

    objects = vis.setdefault("objects", {})
    entries = objects.setdefault("dataPoint", [])
    entries[:] = [e for e in entries if "fill" not in (e.get("properties") or {})]
    entries.append(
        {
            "properties": {"fill": fill},
            "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}]},
        }
    )

    _save_visual(path, vdata)
    span = (
        f"{min_color}→{mid_color}→{max_color}"
        if mid_color
        else f"{min_color}→{max_color}"
    )
    return (
        f"[bar-gradient] {visual_name}: bars colored by {target_table}[{target_measure}] "
        f"{span} (low→high)"
    )


def style_kpi_card(
    report_path: str,
    visual_name: str,
    *,
    accent_color: str,
    value_font_size: int | None = None,
    page: str | None = None,
) -> str:
    if value_font_size is None:
        value_font_size = REPORT_LAYOUT["callout_px"]
    path = _find_visual_json(report_path, visual_name, page)
    if not path:
        return f"[kpi-card] ERROR — visual '{visual_name}' not found"

    vdata = _load_visual(path)
    vis = vdata.setdefault("visual", {})
    if vis.get("visualType") not in ("cardVisual", None):
        return (
            f"[kpi-card] SKIP — '{visual_name}' is {vis.get('visualType')!r}, "
            f"styling only applies to cardVisual"
        )
    objects = vis.setdefault("objects", {})
    _default = {"id": "default"}

    # The number wears ink; the accent lives on the side bar. Accent-coloured
    # values on every card made the accent meaningless and read as template
    # output rather than a designed KPI.
    _ink = DESIGN_TOKENS["ink"]
    objects["value"] = [
        {"properties": {"show": _bool(True)}},
        {
            "properties": {
                "fontColor": _color(_ink),
                "color": _color(_ink),
                "fontSize": {"expr": {"Literal": {"Value": f"{value_font_size}D"}}},
                "horizontalAlignment": _str("left"),
            },
            "selector": _default,
        },
    ]
    objects["label"] = [{"properties": {"show": _bool(False)}, "selector": _default}]
    objects["accentBar"] = [
        {
            "properties": {
                "show": _bool(True),
                "width": {"expr": {"Literal": {"Value": "4D"}}},
                "position": _str("Left"),
                "color": _color(accent_color),
            },
            "selector": _default,
        },
    ]
    objects["padding"] = [
        {
            "properties": {
                "paddingSelection": _str("Custom"),
                "leftMargin": {"expr": {"Literal": {"Value": "16L"}}},
                "topMargin": {"expr": {"Literal": {"Value": "8L"}}},
                "bottomMargin": {"expr": {"Literal": {"Value": "8L"}}},
                "rightMargin": {"expr": {"Literal": {"Value": "8L"}}},
            },
            "selector": _default,
        },
    ]

    _save_visual(path, vdata)
    return (
        f"[kpi-card] {visual_name}: ink value {value_font_size}pt, "
        f"accent bar {accent_color}, label hidden"
    )


_EXT_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/reportExtension/1.0.0/schema.json"
)


def add_extension_measures(report_path: str, entity: str, measures: list[dict]) -> str:
    if not report_path or not os.path.exists(report_path):
        return f"[ext-measure] ERROR — report path does not exist: {report_path}"

    if not entity or not measures:
        return "[ext-measure] ERROR — entity name and at least one measure dict are required"

    # 1. Robust definition folder resolution
    norm_path = os.path.normpath(report_path)
    if os.path.basename(norm_path).lower() == "definition" and os.path.isdir(norm_path):
        def_dir = norm_path
    elif os.path.isdir(os.path.join(norm_path, "definition")):
        def_dir = os.path.join(norm_path, "definition")
    else:
        matches = glob.glob(f"{glob.escape(norm_path)}/**/definition", recursive=True)
        def_dir = next((m for m in matches if os.path.isdir(m)), None)

    if not def_dir:
        return f"[ext-measure] ERROR — definition folder not found under {report_path}"

    # 2. Check for singular 'reportExtension.json' (PBIR standard) or legacy 'reportExtensions.json'
    ext_path_singular = os.path.join(def_dir, "reportExtension.json")
    ext_path_plural = os.path.join(def_dir, "reportExtensions.json")

    if os.path.isfile(ext_path_singular):
        ext_path = ext_path_singular
    elif os.path.isfile(ext_path_plural):
        ext_path = ext_path_plural
    else:
        ext_path = ext_path_singular

    # 3. Read existing or initialize new
    if os.path.isfile(ext_path):
        try:
            with open(ext_path, encoding="utf-8-sig") as f:
                ext = json.load(f)
            if not isinstance(ext, dict):
                ext = {}
        except Exception as e:
            return f"[ext-measure] ERROR — unreadable extension file {ext_path}: {e}"
    else:
        ext = {}

    ext.setdefault("$schema", _EXT_SCHEMA)
    ext.setdefault("name", "extension")
    if not isinstance(ext.get("entities"), list):
        ext["entities"] = []

    # 4. Find or create target entity
    ent = next(
        (e for e in ext["entities"] if isinstance(e, dict) and e.get("name") == entity),
        None,
    )
    if ent is None:
        ent = {"name": entity, "measures": []}
        ext["entities"].append(ent)
    elif not isinstance(ent.get("measures"), list):
        ent["measures"] = []

    # 5. Upsert measures safely
    valid_measures = [m for m in measures if isinstance(m, dict) and m.get("name")]
    if not valid_measures:
        return "[ext-measure] ERROR — no valid measures with 'name' property provided"

    new_names = {m["name"] for m in valid_measures}
    ent["measures"] = [
        m
        for m in ent["measures"]
        if isinstance(m, dict) and m.get("name") not in new_names
    ]
    for m in valid_measures:
        entry = {"dataType": "Text"}
        entry.update(m)
        ent["measures"].append(entry)

    # 6. Save back to disk
    with open(ext_path, "w", encoding="utf-8") as f:
        json.dump(ext, f, indent=2)

    return f"[ext-measure] {len(valid_measures)} measure(s) upserted on entity '{entity}' ({os.path.basename(ext_path)})"


_CARD_IMAGE_POSITIONS = {"Left", "Right", "Top", "Bottom"}


def set_card_image(
    report_path: str,
    visual_name: str,
    *,
    entity: str,
    measure: str,
    position: str = "Bottom",
    from_extension: bool = True,
    image_type: str = "imageUrl",
    page: str | None = None,
) -> str:
    path = _find_visual_json(report_path, visual_name, page)
    if not path:
        return f"[card-image] ERROR — visual '{visual_name}' not found"
    if position not in _CARD_IMAGE_POSITIONS:
        return (
            f"[card-image] ERROR — position must be one of "
            f"{sorted(_CARD_IMAGE_POSITIONS)}, got {position!r}"
        )

    vdata = _load_visual(path)
    vtype = vdata.get("visual", {}).get("visualType")
    if vtype != "cardVisual":
        return (
            f"[card-image] SKIP — '{visual_name}' is {vtype!r}; the image "
            f"slot this sets only exists on cardVisual"
        )

    source_ref: dict = {"Entity": entity}
    if from_extension:
        source_ref = {"Schema": "extension", "Entity": entity}

    objects = vdata.setdefault("visual", {}).setdefault("objects", {})
    objects["image"] = [
        {"properties": {"show": _bool(True)}},
        {
            "properties": {
                "padding": {"expr": {"Literal": {"Value": "0L"}}},
                "position": _str(position),
                "imageType": _str(image_type),
                ("imageData" if image_type == "imageData" else "imageUrl"): {
                    "expr": {
                        "Measure": {
                            "Expression": {"SourceRef": source_ref},
                            "Property": measure,
                        }
                    }
                },
            },
            "selector": {"id": "default"},
        },
    ]
    _save_visual(path, vdata)
    return f"[card-image] {visual_name}: image ← {entity}[{measure}] ({position})"


def color_by_category(
    report_path: str,
    visual_name: str,
    *,
    table: str,
    column: str,
    category_colors: dict[str, str],
    is_numeric: bool = False,
    page: str | None = None,
) -> str:
    path = _find_visual_json(report_path, visual_name, page)
    if not path:
        return f"[color-by-cat] ERROR — visual '{visual_name}' not found"

    vdata = _load_visual(path)
    vis = vdata.setdefault("visual", {})
    vtype = vis.get("visualType")
    if vtype not in _BARLIKE_TYPES:
        return (
            f"[color-by-cat] SKIP — '{visual_name}' is {vtype!r}; per-category "
            f"dataPoint fills only apply to single-series bar/column charts "
            f"{sorted(_BARLIKE_TYPES)}"
        )
    objects = vis.setdefault("objects", {})

    entries = []
    for value, hex_color in category_colors.items():
        literal = f"{value}L" if is_numeric else f"'{value}'"
        entries.append(
            {
                "properties": {
                    "fill": {
                        "solid": {
                            "color": {"expr": {"Literal": {"Value": f"'{hex_color}'"}}}
                        }
                    },
                },
                "selector": {
                    "data": [
                        {
                            "scopeId": {
                                "Comparison": {
                                    "ComparisonKind": 0,
                                    "Left": {
                                        "Column": {
                                            "Expression": {
                                                "SourceRef": {"Entity": table}
                                            },
                                            "Property": column,
                                        }
                                    },
                                    "Right": {"Literal": {"Value": literal}},
                                }
                            }
                        }
                    ]
                },
            }
        )
    objects["dataPoint"] = entries

    _save_visual(path, vdata)
    return (
        f"[color-by-cat] {visual_name}: {len(entries)} category fills on "
        f"{table}[{column}]"
    )


def _stateful(props: dict) -> list:
    return [
        {"properties": props},
        {"properties": dict(props), "selector": {"id": "default"}},
    ]


_BUTTON_SHAPES = {
    "Blank",
    "Back",
    "Reset",
    "Information",
    "Help",
    "Bookmark",
    "PageNavigation",
}


def create_action_button(
    report_path: str,
    page: str,
    name: str,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    label: str = "",
    shape: str = "Blank",
    action_type: str | None = None,
    action_target: str | None = None,
    fill_color: str | None = None,
    text_color: str | None = None,
    theme: dict | None = None,
) -> str:
    if shape not in _BUTTON_SHAPES:
        return f"[button] ERROR — shape must be one of {_BUTTON_SHAPES}, got {shape!r}"
    if bool(action_type) != bool(action_target):
        return (
            "[button] ERROR — action_type and action_target must be given "
            "together; got only "
            f"{'action_type=' + repr(action_type) if action_type else 'action_target=' + repr(action_target)}"
            " — the button would be created with no navigation wired"
        )
    theme = theme or {}
    # Chrome, not accent -- see HEADER_BG in dashboard_polish for why.
    fill_color = fill_color or theme.get("headerBackground") or _HEADER_BG
    text_color = text_color or _HEADER_FG

    page_dirs = glob.glob(f"{report_path}/**/pages/{page}", recursive=True)
    if not page_dirs:
        return f"[button] ERROR — page '{page}' not found under {report_path}"
    visuals_dir = os.path.join(page_dirs[0], "visuals", name)
    if os.path.isdir(visuals_dir):
        return f"[button] ERROR — visual '{name}' already exists on page '{page}'"
    os.makedirs(visuals_dir, exist_ok=True)

    visual: dict = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
        "name": name,
        "position": {
            "x": x,
            "y": y,
            "z": 100,
            "height": height,
            "width": width,
            "tabOrder": 100,
        },
        "visual": {
            "visualType": "actionButton",
            "drillFilterOtherVisuals": True,
            "objects": {
                "shape": [
                    {
                        "properties": {
                            "shape": _str(shape),
                            "tileShape": _str("rectangle"),
                        }
                    }
                ],
                "fill": _stateful(
                    {
                        "show": _bool(True),
                        "fillColor": _color(fill_color),
                        "transparency": _num(0),
                    }
                ),
            },
        },
    }
    if label:
        visual["visual"]["objects"]["text"] = _stateful(
            {
                "show": _bool(True),
                "text": _str(label),
                "fontColor": _color(text_color),
                "fontSize": _num(11),
                "fontFamily": _str("Segoe UI Semibold"),
                "horizontalAlignment": _str("center"),
                "verticalAlignment": _str("middle"),
            }
        )

    if action_type and action_target:
        action: dict = {"type": _str(action_type)}
        if action_type == "Bookmark":
            action["bookmark"] = _str(action_target)
        elif action_type == "PageNavigation":
            action["navigationSection"] = _str(action_target)
        elif action_type == "WebUrl":
            action["webUrl"] = _str(action_target)
        else:
            return f"[button] ERROR — unknown action_type {action_type!r}"
        visual["visual"]["objects"]["visualLink"] = [{"properties": action}]

    _save_visual(os.path.join(visuals_dir, "visual.json"), visual)
    extra = f" → {action_type}:{action_target}" if action_type else ""
    return f"[button] {name} ({shape}, {label!r}) at ({x},{y}) {width}x{height}{extra}"


def create_page_navigator(
    report_path: str,
    page: str,
    name: str,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    orientation: str = "horizontal",
    selected_color: str | None = None,
    unselected_color: str | None = None,
    theme: dict | None = None,
) -> str:
    if orientation not in ("horizontal", "vertical"):
        return "[page-nav] ERROR — orientation must be 'horizontal' or 'vertical'"
    theme = theme or {}
    selected_color = selected_color or theme.get("headerBackground") or _NAV_SELECTED
    unselected_color = unselected_color or theme.get("neutral") or _NAV_UNSELECTED

    page_dirs = glob.glob(f"{report_path}/**/pages/{page}", recursive=True)
    if not page_dirs:
        return f"[page-nav] ERROR — page '{page}' not found"
    visuals_dir = os.path.join(page_dirs[0], "visuals", name)
    if os.path.isdir(visuals_dir):
        return f"[page-nav] ERROR — visual '{name}' already exists"
    os.makedirs(visuals_dir, exist_ok=True)

    visual = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
        "name": name,
        "position": {
            "x": x,
            "y": y,
            "z": 50,
            "height": height,
            "width": width,
            "tabOrder": 50,
        },
        "visual": {
            "visualType": "pageNavigator",
            "drillFilterOtherVisuals": True,
            "objects": {
                "shape": [
                    {
                        "properties": {
                            "tileShape": _str("rectangle"),
                        }
                    }
                ],
                "fill": _stateful(
                    {
                        "show": _bool(True),
                        "fillColor": _color(_THEME_DEFAULTS["background"]),
                    }
                ),
                "text": _stateful(
                    {
                        "fontColor": _color(unselected_color),
                        "fontSize": _num(11),
                        "fontFamily": _str("Segoe UI Semibold"),
                    }
                ),
                "selectedState": [
                    {
                        "properties": {
                            "fontColor": _color(selected_color),
                            "fillColor": _color(
                                _tint(selected_color, TINT_FACTORS["nav_hover_fill"])
                            ),
                        }
                    }
                ],
                "grid": [
                    {
                        "properties": {
                            "orientation": _str(orientation),
                            "padding": _num(8),
                        }
                    }
                ],
            },
            "visualContainerObjects": {
                "border": [{"properties": {"show": _bool(False)}}],
                "title": [{"properties": {"show": _bool(False)}}],
            },
        },
    }
    _save_visual(os.path.join(visuals_dir, "visual.json"), visual)
    return f"[page-nav] {name} ({orientation}) at ({x},{y}) {width}x{height}"


def apply_top_n_filter(
    report_path: str,
    visual_name: str,
    *,
    count: int = 7,
    target_table: str,
    target_column: str,
    value_measure: str,
    value_table: str | None = None,
    page: str | None = None,
) -> str:
    path = _find_visual_json(report_path, visual_name, page)
    if not path:
        return f"[top-n] ERROR — visual '{visual_name}' not found"

    vdata = _load_visual(path)
    fc = vdata.setdefault("filterConfig", {})
    filters = fc.setdefault("filters", [])

    v_table = value_table or target_table
    top_n_filter = {
        "name": f"TopN_{target_column}",
        "type": "TopN",
        "field": {
            "Column": {
                "Expression": {"SourceRef": {"Entity": target_table}},
                "Property": target_column,
            }
        },
        "itemCount": count,
        "operator": "Top",
        "value": {
            "Measure": {
                "Expression": {"SourceRef": {"Entity": v_table}},
                "Property": value_measure,
            }
        },
        "howCreated": "User",
    }

    kept = [f for f in filters if f.get("name") != f"TopN_{target_column}"]
    kept.append(top_n_filter)
    fc["filters"] = kept
    _save_visual(path, vdata)
    return (
        f"[top-n] {visual_name}: filtered to Top {count} by {v_table}[{value_measure}]"
    )


def apply_non_blank_filter(
    report_path: str,
    visual_name: str,
    *,
    target_table: str,
    target_column: str,
    page: str | None = None,
) -> str:
    path = _find_visual_json(report_path, visual_name, page)
    if not path:
        return f"[non-blank] ERROR — visual '{visual_name}' not found"

    vdata = _load_visual(path)
    fc = vdata.setdefault("filterConfig", {})
    filters = fc.setdefault("filters", [])

    nb_filter = {
        "name": f"NonBlank_{target_column}",
        "type": "Advanced",
        "field": {
            "Column": {
                "Expression": {"SourceRef": {"Entity": target_table}},
                "Property": target_column,
            }
        },
        "conditions": [{"operator": "IsNotBlank"}],
        "howCreated": "User",
    }
    kept = [f for f in filters if f.get("name") != f"NonBlank_{target_column}"]
    kept.append(nb_filter)
    fc["filters"] = kept
    _save_visual(path, vdata)
    return (
        f"[non-blank] {visual_name}: excluded blanks on {target_table}[{target_column}]"
    )
