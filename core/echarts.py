from __future__ import annotations


from config import SEMANTIC_BAD, SEMANTIC_GOOD
from report.dashboard_polish import THEME_JSON as _CHAT_THEME

_ECHART_COLORS = list(_CHAT_THEME["dataColors"])


def _echart_rgba(hex_color: str, alpha: float) -> str:
    h = (hex_color or "").lstrip("#")
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    return f"rgba({r},{g},{b},{alpha})"


def _echart_lighten(hex_color: str, amt: float = 0.28) -> str:
    h = (hex_color or "").lstrip("#")
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    r = int(r + (255 - r) * amt)
    g = int(g + (255 - g) * amt)
    b = int(b + (255 - b) * amt)
    return f"#{r:02x}{g:02x}{b:02x}"


_SEM_RED = (
    "loss",
    "decline",
    "declin",
    "bad",
    "negative",
    "deficit",
    "shortfall",
    "drop",
    "miss",
    "below",
)
_SEM_GREEN = (
    "gain",
    "growth",
    "grow",
    "good",
    "positive",
    "surplus",
    "profit",
    "increase",
    "beat",
    "above",
)


def _semantic_color(name: str) -> str | None:
    n = (name or "").lower()
    if any(k in n for k in _SEM_RED):
        return SEMANTIC_BAD
    if any(k in n for k in _SEM_GREEN):
        return SEMANTIC_GOOD
    return None


def _echart_vgrad(top: str, bottom: str) -> dict:
    return {
        "type": "linear",
        "x": 0,
        "y": 0,
        "x2": 0,
        "y2": 1,
        "colorStops": [{"offset": 0, "color": top}, {"offset": 1, "color": bottom}],
    }


def _echart_num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return x
    if isinstance(x, str):
        s = x.strip().lstrip("$").rstrip("%").replace(",", "").strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


_ECH_AXIS_LABEL = {"color": "#5a6b80", "fontSize": 11}
_ECH_SPLITLINE = {"lineStyle": {"color": "#e2e8f1", "type": "dashed"}}
_ECH_FONT = {"fontFamily": "'IBM Plex Sans', 'Segoe UI', sans-serif"}
_AXIS3D = {
    "nameTextStyle": {"color": "#5a6b80", "fontSize": 11},
    "axisLabel": {"color": "#5a6b80", "fontSize": 10},
    "axisLine": {"lineStyle": {"color": "#94a3b5"}},
    "splitLine": {"lineStyle": {"color": "#e2e8f1"}},
}

_ECH_TOOLTIP = {
    "backgroundColor": "#0f1a28",
    "borderWidth": 0,
    "textStyle": {"color": "#eef1f5", "fontSize": 12},
    "padding": [6, 10],
}


# Axis/tooltip values are formatted (1.2M, 3.4K) by the browser: the formatter
# is a JavaScript function and would not survive JSON. Keeping it out here is
# what lets an option object be sent to the client as-is.
_SI_FMT = None
_ECH_VAL_LABEL = _ECH_AXIS_LABEL


def _to_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.replace("$", "").replace("%", "").replace(",", "").strip()
        if not s:
            return default
        mult = 1.0
        if s[-1].upper() == "K":
            mult = 1_000.0
            s = s[:-1].strip()
        elif s[-1].upper() == "M":
            mult = 1_000_000.0
            s = s[:-1].strip()
        elif s[-1].upper() == "B":
            mult = 1_000_000_000.0
            s = s[:-1].strip()
        try:
            return float(s) * mult
        except ValueError:
            return default
    return default


def _build_echarts_options(spec: dict) -> dict:
    ctype = (spec.get("chart_type") or "bar").lower()
    cats = spec.get("categories") or []
    series = spec.get("series") or []
    title = spec.get("title") or ""

    base = {
        "color": _ECHART_COLORS,
        "textStyle": _ECH_FONT,
        "title": {
            "text": title,
            "left": "center",
            "textStyle": {"fontSize": 14, "fontWeight": 600, "color": "#0f1a28"},
        },
        "toolbox": {
            "show": True,
            "right": 12,
            "top": 4,
            "feature": {
                "saveAsImage": {
                    "title": "Download",
                    "name": (title or "chart").strip().replace(" ", "_")[:80],
                    "backgroundColor": "#FFFFFF",
                }
            },
            "iconStyle": {"borderColor": "#94a3b5"},
            "emphasis": {"iconStyle": {"borderColor": "#0f1a28"}},
        },
        "animationDuration": 600,
        "animationEasing": "cubicOut",
    }

    if ctype in ("pie", "donut"):
        data = [
            {"name": (cats[i] if i < len(cats) else str(i)), "value": v}
            for i, v in enumerate((series[0].get("data") if series else []) or [])
        ]
        inner = "46%" if ctype == "donut" else "0%"
        base["tooltip"] = {
            "trigger": "item",
            "formatter": "{b}: {c} ({d}%)",
            **_ECH_TOOLTIP,
        }
        base["legend"] = {
            "orient": "vertical",
            "left": "left",
            "top": "middle",
            "type": "scroll",
            "textStyle": {"color": "#5a6b80"},
            "icon": "circle",
        }
        base["series"] = [
            {
                "type": "pie",
                "radius": [inner, "72%"],
                "center": ["62%", "52%"],
                "avoidLabelOverlap": True,
                "itemStyle": {
                    "borderColor": "#FFFFFF",
                    "borderWidth": 2,
                    "borderRadius": 6,
                },
                "label": {"color": "#475569", "fontSize": 11, "formatter": "{b}\n{d}%"},
                "labelLine": {"length": 8, "length2": 8},
                "emphasis": {
                    "scale": True,
                    "scaleSize": 6,
                    "label": {"fontWeight": 600},
                },
                "data": data,
            }
        ]
        return base

    brand = _ECHART_COLORS[0]

    if ctype == "funnel":
        vals = (series[0].get("data") if series else []) or []
        fdata = [
            {"name": (cats[i] if i < len(cats) else f"Stage {i + 1}"), "value": v}
            for i, v in enumerate(vals)
        ]
        base["tooltip"] = {"trigger": "item", "formatter": "{b}: {c}", **_ECH_TOOLTIP}
        base["legend"] = {
            "top": "bottom",
            "textStyle": {"color": "#5a6b80"},
            "icon": "circle",
        }
        base["series"] = [
            {
                "type": "funnel",
                "left": "8%",
                "right": "8%",
                "top": "20%" if title else "10%",
                "bottom": "12%",
                "minSize": "12%",
                "maxSize": "100%",
                "sort": "descending",
                "gap": 2,
                "label": {
                    "show": True,
                    "position": "inside",
                    "color": "#fff",
                    "fontWeight": 600,
                },
                "itemStyle": {"borderColor": "#fff", "borderWidth": 1},
                "emphasis": {"label": {"fontSize": 14}},
                "data": fdata,
            }
        ]
        return base

    if ctype == "treemap":
        vals = (series[0].get("data") if series else []) or []
        tdata = [
            {"name": (cats[i] if i < len(cats) else f"Item {i + 1}"), "value": v}
            for i, v in enumerate(vals)
        ]
        base["tooltip"] = {"trigger": "item", "formatter": "{b}: {c}", **_ECH_TOOLTIP}
        base["series"] = [
            {
                "type": "treemap",
                "roam": False,
                "nodeClick": False,
                "breadcrumb": {"show": False},
                "width": "94%",
                "height": "78%",
                "top": "16%" if title else "6%",
                "label": {
                    "show": True,
                    "formatter": "{b}",
                    "color": "#fff",
                    "fontWeight": 600,
                },
                "itemStyle": {"borderColor": "#fff", "borderWidth": 2, "gapWidth": 2},
                "data": tdata,
            }
        ]
        return base

    if ctype == "radar":
        allv = [
            x
            for s in series
            for x in (s.get("data") or [])
            if isinstance(x, (int, float))
        ]
        mx = (max(abs(v) for v in allv) if allv else 1) * 1.1 or 1
        base["tooltip"] = {"trigger": "item", **_ECH_TOOLTIP}
        base["legend"] = {"top": "bottom", "textStyle": {"color": "#5a6b80"}}
        base["radar"] = {
            "indicator": [{"name": c, "max": mx} for c in cats],
            "radius": "60%",
            "center": ["50%", "54%"],
            "axisName": {"color": "#5a6b80", "fontSize": 11},
            "splitLine": {"lineStyle": {"color": "#d5dde8"}},
            "splitArea": {"show": False},
            "axisLine": {"lineStyle": {"color": "#d5dde8"}},
        }
        base["series"] = [
            {
                "type": "radar",
                "data": [
                    {
                        "name": s.get("name", ""),
                        "value": s.get("data") or [],
                        "areaStyle": {"opacity": 0.12},
                        "lineStyle": {"width": 2},
                    }
                    for s in series
                ],
            }
        ]
        return base

    if ctype == "heatmap":
        ys = [(s.get("name") or f"Row {i + 1}") for i, s in enumerate(series)]
        hdata, allv = [], []
        for yi, s in enumerate(series):
            for xi, v in enumerate(s.get("data") or []):
                fv = _to_float(v)
                hdata.append([xi, yi, fv])
                allv.append(fv)

        min_val = min(allv) if allv else 0.0
        max_val = max(allv) if allv else 1.0
        if min_val == max_val:
            max_val = min_val + 1.0

        base["tooltip"] = {"position": "top", **_ECH_TOOLTIP}
        base["xAxis"] = {
            "type": "category",
            "data": cats,
            "axisLabel": _ECH_AXIS_LABEL,
            "splitArea": {"show": True},
            "axisTick": {"show": False},
        }
        base["yAxis"] = {
            "type": "category",
            "data": ys,
            "axisLabel": _ECH_AXIS_LABEL,
            "splitArea": {"show": True},
            "axisTick": {"show": False},
            "inverse": True,
        }
        base["visualMap"] = {
            "min": min_val,
            "max": max_val,
            "calculable": True,
            "orient": "horizontal",
            "left": "center",
            "bottom": "2%",
            "inRange": {"color": [_echart_lighten(brand), brand]},
            "textStyle": {"color": "#5a6b80"},
        }
        base["grid"] = {
            "left": "3%",
            "right": "5%",
            "top": "16%" if title else "8%",
            "bottom": "16%",
            "containLabel": True,
        }
        base["series"] = [
            {
                "type": "heatmap",
                "data": hdata,
                "label": {
                    "show": len(hdata) <= 100,
                    "color": "#0f1a28",
                    "fontSize": 10,
                },
                "itemStyle": {"borderColor": "#fff", "borderWidth": 1},
                "emphasis": {
                    "itemStyle": {"shadowBlur": 8, "shadowColor": "rgba(0,0,0,0.2)"}
                },
            }
        ]
        return base

    if ctype == "gantt":
        starts = (series[0].get("data") if len(series) >= 1 else []) or []
        ends = (series[1].get("data") if len(series) >= 2 else []) or []
        durations = [
            (
                (ends[i] - starts[i])
                if (
                    i < len(ends)
                    and i < len(starts)
                    and isinstance(ends[i], (int, float))
                    and isinstance(starts[i], (int, float))
                )
                else 0
            )
            for i in range(len(cats))
        ]
        base["tooltip"] = {
            "trigger": "axis",
            "axisPointer": {"type": "shadow"},
            **_ECH_TOOLTIP,
        }
        base["xAxis"] = {
            "type": "value",
            "axisLabel": _ECH_AXIS_LABEL,
            "splitLine": _ECH_SPLITLINE,
            "axisLine": {"show": False},
        }
        base["yAxis"] = {
            "type": "category",
            "data": cats,
            "inverse": True,
            "axisLabel": _ECH_AXIS_LABEL,
            "axisTick": {"show": False},
            "axisLine": {"lineStyle": {"color": "#d5dde8"}},
        }
        base["grid"] = {
            "left": "3%",
            "right": "5%",
            "top": "16%" if title else "8%",
            "bottom": "6%",
            "containLabel": True,
        }
        base["series"] = [
            {
                "type": "bar",
                "stack": "g",
                "itemStyle": {"color": "transparent"},
                "data": starts,
                "silent": True,
            },
            {
                "type": "bar",
                "stack": "g",
                "data": durations,
                "barMaxWidth": 22,
                "itemStyle": {
                    "color": _echart_vgrad(_echart_lighten(brand), brand),
                    "borderRadius": 4,
                },
            },
        ]
        return base

    if ctype == "waterfall":
        vals = (series[0].get("data") if series else []) or []
        bvals, pos, neg = [], [], []
        running = 0.0
        for v in vals:
            x = v if isinstance(v, (int, float)) else 0
            if x >= 0:
                bvals.append(running)
                pos.append(x)
                neg.append("-")
                running += x
            else:
                running += x
                bvals.append(running)
                pos.append("-")
                neg.append(-x)
        base["xAxis"] = {
            "type": "category",
            "data": cats,
            "axisLabel": _ECH_AXIS_LABEL,
            "axisLine": {"lineStyle": {"color": "#d5dde8"}},
            "axisTick": {"show": False},
        }
        base["yAxis"] = {
            "type": "value",
            "axisLabel": _ECH_VAL_LABEL,
            "splitLine": _ECH_SPLITLINE,
            "axisLine": {"show": False},
        }
        base["tooltip"] = {
            "trigger": "axis",
            "axisPointer": {"type": "shadow"},
            **_ECH_TOOLTIP,
        }
        base["legend"] = {
            "top": "bottom",
            "data": ["Increase", "Decrease"],
            "textStyle": {"color": "#5a6b80"},
            "icon": "roundRect",
        }
        base["grid"] = {
            "left": "3%",
            "right": "5%",
            "bottom": "11%",
            "top": "20%" if title else "10%",
            "containLabel": True,
        }
        base["series"] = [
            {
                "type": "bar",
                "stack": "w",
                "itemStyle": {"color": "transparent"},
                "data": bvals,
                "silent": True,
            },
            {
                "name": "Increase",
                "type": "bar",
                "stack": "w",
                "data": pos,
                "barMaxWidth": 40,
                "itemStyle": {
                    "color": _echart_vgrad(
                        _echart_lighten(SEMANTIC_GOOD), SEMANTIC_GOOD
                    ),
                    "borderRadius": [3, 3, 0, 0],
                },
            },
            {
                "name": "Decrease",
                "type": "bar",
                "stack": "w",
                "data": neg,
                "barMaxWidth": 40,
                "itemStyle": {
                    "color": _echart_vgrad(_echart_lighten(SEMANTIC_BAD), SEMANTIC_BAD),
                    "borderRadius": [3, 3, 0, 0],
                },
            },
        ]
        return base

    if ctype == "boxplot":
        bdata = (series[0].get("data") if series else []) or []
        base["xAxis"] = {
            "type": "category",
            "data": cats,
            "axisLabel": _ECH_AXIS_LABEL,
            "axisLine": {"lineStyle": {"color": "#d5dde8"}},
            "axisTick": {"show": False},
        }
        base["yAxis"] = {
            "type": "value",
            "axisLabel": _ECH_VAL_LABEL,
            "splitLine": _ECH_SPLITLINE,
            "axisLine": {"show": False},
        }
        base["tooltip"] = {"trigger": "item", **_ECH_TOOLTIP}
        base["grid"] = {
            "left": "3%",
            "right": "5%",
            "bottom": "8%",
            "top": "20%" if title else "10%",
            "containLabel": True,
        }
        base["series"] = [
            {
                "type": "boxplot",
                "data": bdata,
                "itemStyle": {
                    "color": _echart_rgba(brand, 0.2),
                    "borderColor": brand,
                    "borderWidth": 1.5,
                },
            }
        ]
        return base

    if ctype == "scatter3d":
        # 3D scatter plot (echarts-gl)
        xs = (series[0].get("data") or []) if len(series) > 0 else []
        ys = (series[1].get("data") or []) if len(series) > 1 else []
        zs = (series[2].get("data") or []) if len(series) > 2 else []
        names = [
            (series[i].get("name") or n) if len(series) > i else n
            for i, n in enumerate(("X", "Y", "Z"))
        ]
        rows = [
            [_echart_num(x), _echart_num(y), _echart_num(z), cats[i] if i < len(cats) else ""]
            for i, (x, y, z) in enumerate(zip(xs, ys, zs))
        ]
        # A point missing any axis cannot be placed, so it is dropped rather
        # than plotted at zero and read as a real reading.
        rows = [r for r in rows if None not in r[:3]]
        depths = [r[2] for r in rows]

        base.pop("toolbox", None)  # echarts-gl draws its own controls
        base["tooltip"] = {**_ECH_TOOLTIP}
        base["xAxis3D"] = {"type": "value", "name": names[0], **_AXIS3D}
        base["yAxis3D"] = {"type": "value", "name": names[1], **_AXIS3D}
        base["zAxis3D"] = {"type": "value", "name": names[2], **_AXIS3D}
        base["grid3D"] = {
            "boxWidth": 100,
            "boxDepth": 100,
            "viewControl": {"projection": "perspective", "autoRotate": False},
            "axisLine": {"lineStyle": {"color": "#94a3b5"}},
            "splitLine": {"lineStyle": {"color": "#d5dde8"}},
            "axisPointer": {"lineStyle": {"color": "#5a6b80"}},
        }
        if depths:
            base["visualMap"] = {
                "show": False,
                "min": min(depths),
                "max": max(depths),
                "inRange": {"color": [_echart_lighten(_ECHART_COLORS[0], 0.45), _ECHART_COLORS[0]]},
                "dimension": 2,
            }
        base["series"] = [
            {
                "type": "scatter3D",
                "name": names[2],
                "data": rows,
                "symbolSize": 12,
                "itemStyle": {"borderWidth": 0.5, "borderColor": "#ffffff", "opacity": 0.9},
                "emphasis": {"label": {"show": True, "formatter": "{@[3]}", "fontSize": 11}},
            }
        ]
        return base

    if ctype == "bubble":
        if len(series) >= 3:
            xs = series[0].get("data") or []
            ys = series[1].get("data") or []
            sizes = series[2].get("data") or []
        elif len(series) == 2:
            xs = None
            ys = series[0].get("data") or []
            sizes = series[1].get("data") or []
        else:
            xs = None
            ys = (series[0].get("data") if series else []) or []
            sizes = ys

        parsed_sizes = (
            [_to_float(sz) for sz in sizes] if sizes else [_to_float(y) for y in ys]
        )
        smax = max([abs(sz) for sz in parsed_sizes] or [1.0]) or 1.0

        pts = []
        n_pts = max(len(ys), len(cats))
        for i in range(n_pts):
            x_val = _to_float(xs[i]) if (xs and i < len(xs)) else i
            y_val = _to_float(ys[i]) if (ys and i < len(ys)) else 0.0
            sz_val = parsed_sizes[i] if (i < len(parsed_sizes)) else abs(y_val)
            name = cats[i] if (cats and i < len(cats)) else f"Point {i+1}"
            sym_size = max(8, min(65, 10 + 44 * (abs(sz_val) / smax)))
            pts.append(
                {
                    "name": name,
                    "value": [x_val, y_val, sz_val],
                    "symbolSize": sym_size,
                }
            )

        has_numeric_x = xs is not None and len(xs) > 0
        base["xAxis"] = {
            "type": "value" if has_numeric_x else "category",
            **({"data": cats} if not has_numeric_x else {}),
            "axisLabel": _ECH_AXIS_LABEL,
            "axisLine": {"lineStyle": {"color": "#d5dde8"}},
            "axisTick": {"show": False},
        }
        base["yAxis"] = {
            "type": "value",
            "axisLabel": _ECH_VAL_LABEL,
            "splitLine": _ECH_SPLITLINE,
            "axisLine": {"show": False},
        }
        base["tooltip"] = {
            "trigger": "item",
            "formatter": "{b}<br/>X: {c0}<br/>Y: {c1}<br/>Size: {c2}",
            **_ECH_TOOLTIP,
        }
        base["grid"] = {
            "left": "3%",
            "right": "5%",
            "bottom": "8%",
            "top": "20%" if title else "10%",
            "containLabel": True,
        }
        base["series"] = [
            {
                "type": "scatter",
                "data": pts,
                "itemStyle": {
                    "color": _echart_rgba(brand, 0.6),
                    "borderColor": brand,
                    "borderWidth": 1,
                },
            }
        ]
        return base

    horizontal = ctype == "hbar"
    cat_axis = {
        "type": "category",
        "data": cats,
        "axisLabel": _ECH_AXIS_LABEL,
        "axisLine": {"lineStyle": {"color": "#d5dde8"}},
        "axisTick": {"show": False},
    }
    val_axis = {
        "type": "value",
        "axisLabel": _ECH_VAL_LABEL,
        "splitLine": _ECH_SPLITLINE,
        "axisLine": {"show": False},
        "axisTick": {"show": False},
    }
    use_dual_yaxis = False
    if (
        not horizontal
        and len(series) == 2
        and ctype in ("line", "area", "bar", "column", "lollipop")
    ):
        s0_vals = [abs(_to_float(x)) for x in (series[0].get("data") or [])]
        s1_vals = [abs(_to_float(x)) for x in (series[1].get("data") or [])]
        m0 = max(s0_vals) if s0_vals else 0.0
        m1 = max(s1_vals) if s1_vals else 0.0
        if m0 > 0 and m1 > 0:
            ratio = max(m0, m1) / min(m0, m1)
            if ratio >= 5.0:
                use_dual_yaxis = True

    if horizontal:
        base["xAxis"] = val_axis
        base["yAxis"] = {**cat_axis, "inverse": True}
    elif use_dual_yaxis:
        base["xAxis"] = cat_axis
        val_axis_left = dict(val_axis)
        val_axis_right = {
            **val_axis,
            "splitLine": {"show": False},
        }
        base["yAxis"] = [val_axis_left, val_axis_right]
        base["grid"] = {
            "left": "3%",
            "right": "6%",
            "bottom": "11%",
            "top": "20%" if title else "10%",
            "containLabel": True,
        }
    else:
        base["xAxis"] = cat_axis
        base["yAxis"] = val_axis

    base["tooltip"] = {
        "trigger": "axis",
        "axisPointer": {"type": "shadow"},
        **_ECH_TOOLTIP,
    }
    if not use_dual_yaxis:
        base["grid"] = {
            "left": "3%",
            "right": "5%",
            "bottom": "11%",
            "top": "20%" if title else "10%",
            "containLabel": True,
        }
    base["legend"] = {
        "top": "bottom",
        "icon": "roundRect",
        "textStyle": {"color": "#5a6b80"},
    }

    single = len(series) == 1
    hide_legend = False
    out: list[dict] = []
    for idx, s in enumerate(series):
        c = _ECHART_COLORS[idx % len(_ECHART_COLORS)]
        name = s.get("name", "")
        data = s.get("data") or []
        if ctype != "scatter":
            data = [_echart_num(v) for v in data]

        if ctype in ("line", "area", "stacked_area"):
            item = {
                "name": name,
                "type": "line",
                "smooth": True,
                "data": data,
                "symbol": "circle",
                "symbolSize": 7,
                "showSymbol": len(data) <= 40,
                "lineStyle": {"width": 2.5, "color": c},
                "itemStyle": {"color": c, "borderColor": "#fff", "borderWidth": 1.5},
                "emphasis": {"focus": "series"},
            }
            if ctype in ("area", "stacked_area"):
                item["areaStyle"] = {
                    "opacity": 0.9,
                    "color": _echart_vgrad(
                        _echart_rgba(c, 0.35), _echart_rgba(c, 0.02)
                    ),
                }
            if ctype == "stacked_area":
                item["stack"] = "s"
            if use_dual_yaxis:
                item["yAxisIndex"] = idx
            out.append(item)

        elif ctype == "lollipop":
            _nums = [x for x in data if isinstance(x, (int, float))]
            _sem = _semantic_color(name)
            _diverge = single and not _sem and any(n < 0 for n in _nums)

            def _pt(v, _sem=_sem, _diverge=_diverge):
                if _sem:
                    return _sem
                if _diverge:
                    return (
                        SEMANTIC_GOOD
                        if (isinstance(v, (int, float)) and v >= 0)
                        else SEMANTIC_BAD
                    )
                return c

            if _sem or _diverge:
                hide_legend = single
                _stick = [
                    {"value": v, "itemStyle": {"color": _echart_rgba(_pt(v), 0.45)}}
                    for v in data
                ]
                _dot = [
                    {
                        "value": v,
                        "itemStyle": {
                            "color": _pt(v),
                            "shadowBlur": 6,
                            "shadowColor": _echart_rgba(_pt(v), 0.45),
                        },
                    }
                    for v in data
                ]
            else:
                _stick, _dot = data, data
            out.append(
                {
                    "name": name,
                    "type": "bar",
                    "data": _stick,
                    "barWidth": 3,
                    "itemStyle": {"color": _echart_rgba(c, 0.45), "borderRadius": 2},
                    "silent": True,
                    "z": 1,
                }
            )
            out.append(
                {
                    "name": name,
                    "type": "scatter",
                    "data": _dot,
                    "symbolSize": 15,
                    "itemStyle": {
                        "color": c,
                        "shadowBlur": 6,
                        "shadowColor": _echart_rgba(c, 0.45),
                    },
                    "emphasis": {"scale": 1.3},
                    "z": 2,
                }
            )

        elif ctype == "scatter":
            out.append(
                {
                    "name": name,
                    "type": "scatter",
                    "data": data,
                    "symbolSize": 14,
                    "itemStyle": {
                        "color": _echart_rgba(c, 0.85),
                        "borderColor": c,
                        "borderWidth": 1,
                    },
                }
            )

        else:
            radius = 0

            def _bar_grad(col: str) -> dict:
                if not horizontal:
                    return _echart_vgrad(_echart_lighten(col), col)
                return {
                    "type": "linear",
                    "x": 0,
                    "y": 0,
                    "x2": 1,
                    "y2": 0,
                    "colorStops": [
                        {"offset": 0, "color": col},
                        {"offset": 1, "color": _echart_lighten(col)},
                    ],
                }

            _nums = [x for x in data if isinstance(x, (int, float))]
            sem = _semantic_color(name)
            diverging = single and not sem and any(n < 0 for n in _nums)

            if sem:
                bar_data = data
                series_color = _bar_grad(sem)
                emphasis_color = sem
            elif diverging:
                hide_legend = True
                bar_data = [
                    {
                        "value": v,
                        "itemStyle": {
                            "borderRadius": radius,
                            "color": _bar_grad(
                                SEMANTIC_GOOD
                                if (isinstance(v, (int, float)) and v >= 0)
                                else SEMANTIC_BAD
                            ),
                        },
                    }
                    for v in data
                ]
                series_color = _bar_grad(c)
                emphasis_color = None
            else:
                bar_data = data
                series_color = _bar_grad(c)
                emphasis_color = c

            item = {
                "name": name,
                "type": "bar",
                "data": bar_data,
                "barMaxWidth": 34,
                "itemStyle": {
                    "borderRadius": radius,
                    "color": series_color,
                    "shadowBlur": 10,
                    "shadowColor": "rgba(20,40,80,0.10)",
                    "shadowOffsetY": 3,
                },
            }
            if use_dual_yaxis:
                item["yAxisIndex"] = idx
            if ctype == "stacked_bar":
                item["stack"] = "s"
            if ctype == "histogram":
                item["barCategoryGap"] = "0%"
                item["itemStyle"]["borderColor"] = "#fff"
                item["itemStyle"]["borderWidth"] = 1
            if emphasis_color:
                item["emphasis"] = {
                    "itemStyle": {"color": emphasis_color, "shadowBlur": 14}
                }
            if single and not horizontal and len(cats) <= 12:
                item["label"] = {
                    "show": True,
                    "position": "top",
                    "color": "#475569",
                    "fontSize": 10,
                    "fontWeight": 600,
                }
                if _SI_FMT:
                    item["label"]["formatter"] = _SI_FMT
            out.append(item)

    if hide_legend:
        base["legend"] = {"show": False}
    base["series"] = out
    return base


def _echart_height(spec: dict) -> str:
    ctype = (spec.get("chart_type") or "bar").lower()
    n = len(spec.get("categories") or [])
    if ctype == "hbar":
        return f"{max(320, min(1400, n * 26 + 140))}px"
    if ctype == "gantt":
        return f"{max(300, min(1200, n * 34 + 120))}px"
    if ctype in ("radar", "treemap", "funnel", "heatmap"):
        return "420px"
    if ctype == "scatter3d":
        return "460px"
    return "340px"


def spec_to_csv_bytes(spec: dict) -> bytes:
    import csv
    import io

    cats = spec.get("categories") or []
    series = spec.get("series") or []
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["category"] + [s.get("name") or f"series_{i}" for i, s in enumerate(series)]
    )
    n = max([len(cats)] + [len(s.get("data") or []) for s in series])
    for i in range(n):
        row = [cats[i] if i < len(cats) else ""]
        for s in series:
            d = s.get("data") or []
            row.append(d[i] if i < len(d) else "")
        w.writerow(row)
    return buf.getvalue().encode("utf-8")
