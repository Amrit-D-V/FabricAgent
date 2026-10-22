from __future__ import annotations

import io
import re

from report.dashboard_polish import THEME_JSON as _THEME

_COLORS = list(_THEME["dataColors"]) or ["#2369D2"]
_BRAND = _COLORS[0]
_GRID = "#D8D3C6"

_MPL_CHART_TYPES = frozenset(
    {
        "bar",
        "column",
        "hbar",
        "line",
        "area",
        "stacked_area",
        "stacked_bar",
        "pie",
        "donut",
        "scatter",
        "lollipop",
        "histogram",
    }
)


def _mpl_chart_png(spec: dict) -> bytes | None:
    ctype = (spec.get("chart_type") or "bar").lower()
    if ctype not in _MPL_CHART_TYPES:
        return None

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    cats = spec.get("categories") or []
    series = spec.get("series") or []
    title = spec.get("title") or ""
    fig, ax = plt.subplots(figsize=(6.3, 3.2), dpi=150)

    if ctype in ("pie", "donut"):
        vals = [
            v
            for v in ((series[0].get("data") if series else []) or [])
            if isinstance(v, (int, float))
        ]
        labels = (cats + [""] * len(vals))[: len(vals)]
        if vals:
            ax.pie(
                vals,
                labels=labels,
                colors=_COLORS,
                autopct="%1.0f%%",
                wedgeprops={
                    "edgecolor": "white",
                    "linewidth": 1,
                    "width": 0.45 if ctype == "donut" else 1.0,
                },
                textprops={"fontsize": 8},
            )
        ax.axis("equal")
    elif ctype == "scatter":
        xs = (series[0].get("data") if series else []) or []
        ys = (series[1].get("data") if len(series) > 1 else []) or []
        n = min(len(xs), len(ys))
        ax.scatter(xs[:n], ys[:n], color=_BRAND, alpha=0.75, edgecolor="white")
        ax.grid(True, linestyle="--", alpha=0.3)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
    else:
        x = list(range(len(cats)))
        horizontal = ctype == "hbar"
        stacked = ctype in ("stacked_bar", "stacked_area")
        bottom = [0.0] * len(cats)
        for i, s in enumerate(series):
            raw = (s.get("data") or [])[: len(cats)]
            data = [v if isinstance(v, (int, float)) else 0.0 for v in raw]
            data += [0.0] * (len(cats) - len(data))
            color = _COLORS[i % len(_COLORS)]
            name = s.get("name") or f"Series {i + 1}"
            if ctype in ("line", "area"):
                ax.plot(x, data, marker="o", markersize=4, color=color, label=name)
                if ctype == "area":
                    ax.fill_between(x, data, color=color, alpha=0.15)
            elif ctype == "stacked_area":
                top = [b + d for b, d in zip(bottom, data)]
                ax.fill_between(x, bottom, top, color=color, alpha=0.5, label=name)
                bottom = top
            elif horizontal:
                ax.barh(
                    x, data, left=bottom if stacked else None, color=color, label=name
                )
                if stacked:
                    bottom = [b + d for b, d in zip(bottom, data)]
            elif stacked:
                ax.bar(x, data, bottom=bottom, color=color, label=name)
                bottom = [b + d for b, d in zip(bottom, data)]
            else:
                width = 0.8 / max(1, len(series))
                offs = [xi + i * width - 0.4 + width / 2 for xi in x]
                ax.bar(offs, data, width=width, color=color, label=name)
        rotate = len(cats) > 8
        if horizontal:
            ax.set_yticks(x)
            ax.set_yticklabels(cats, fontsize=8)
        else:
            ax.set_xticks(x)
            ax.set_xticklabels(
                cats,
                fontsize=8,
                rotation=30 if rotate else 0,
                ha="right" if rotate else "center",
            )
        ax.grid(True, axis="x" if horizontal else "y", linestyle="--", alpha=0.3)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        if len(series) > 1:
            ax.legend(fontsize=8, frameon=False)

    if title:
        ax.set_title(title, fontsize=11, fontweight="bold")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _md_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md_inline(s: str) -> str:
    s = _md_escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"`([^`]+?)`", r'<font face="Courier" size="9">\1</font>', s)
    return s


def _is_table_row(line: str) -> bool:
    t = line.strip()
    return t.startswith("|") and t.endswith("|") and t.count("|") >= 2


def _is_table_sep(line: str) -> bool:
    t = line.strip().strip("|")
    return bool(t) and "-" in t and all(c in "-: " for c in t)


def _parse_table_row(line: str) -> list[str]:
    t = line.strip().strip("|")
    return [c.strip() for c in t.split("|")]


def _table_flowable(rows: list[list[str]], styles):
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Table, TableStyle

    n_cols = max(len(r) for r in rows)
    rows = [r + [""] * (n_cols - len(r)) for r in rows]
    width_each = 6.4 * inch / max(1, n_cols)
    wrapped = [[Paragraph(_md_inline(c), styles["Small"]) for c in row] for row in rows]
    tbl = Table(wrapped, colWidths=[width_each] * n_cols, repeatRows=1)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor(_BRAND)),
                ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor(_GRID)),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [rl_colors.white, rl_colors.HexColor("#F7F5F0")],
                ),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return tbl


def _markdown_flowables(text: str, styles) -> list:
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, Spacer

    out: list = []
    lines = (text or "").splitlines()
    i, n = 0, len(lines)
    bullet_buf: list[str] = []

    def flush_bullets():
        if not bullet_buf:
            return
        items = [Paragraph(_md_inline(b), styles["Normal"]) for b in bullet_buf]
        out.append(
            ListFlowable(
                [ListItem(p, leftIndent=6) for p in items],
                bulletType="bullet",
                start="circle",
                leftIndent=14,
            )
        )
        out.append(Spacer(1, 4))
        bullet_buf.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_bullets()
            out.append(Spacer(1, 4))
            i += 1
            continue

        if _is_table_row(line) and i + 1 < n and _is_table_sep(lines[i + 1]):
            flush_bullets()
            rows = [_parse_table_row(line)]
            j = i + 2
            while j < n and _is_table_row(lines[j]):
                rows.append(_parse_table_row(lines[j]))
                j += 1
            out.append(_table_flowable(rows, styles))
            out.append(Spacer(1, 8))
            i = j
            continue

        m = re.match(r"^(#{1,3})\s+(.+)", stripped)
        if m:
            flush_bullets()
            level = len(m.group(1))
            style = styles["Heading2"] if level <= 2 else styles["Heading3"]
            out.append(Paragraph(_md_inline(m.group(2)), style))
            i += 1
            continue

        bm = re.match(r"^[-*•]\s+(.+)", stripped)
        if bm:
            bullet_buf.append(bm.group(1))
            i += 1
            continue

        nm = re.match(r"^\d+[.)]\s+(.+)", stripped)
        if nm:
            bullet_buf.append(nm.group(1))
            i += 1
            continue

        flush_bullets()
        out.append(Paragraph(_md_inline(stripped), styles["Normal"]))
        i += 1

    flush_bullets()
    return out


def build_chat_report_pdf(
    user_message: str,
    notes: list[str],
    final_text: str,
    charts: list[dict],
    project_name: str = "",
) -> bytes:
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        HRFlowable,
        Image,
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        title="FabricBI Report",
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle("Small", parent=styles["Normal"], fontSize=8.5, leading=11)
    )
    styles.add(
        ParagraphStyle(
            "Question",
            parent=styles["Normal"],
            fontSize=10.5,
            leading=14,
            textColor=rl_colors.HexColor("#5B6675"),
            spaceAfter=10,
        )
    )

    story: list = [Paragraph("FabricBI Report", styles["Title"])]
    if project_name:
        story.append(Paragraph(_md_escape(project_name), styles["Normal"]))
    story.append(Spacer(1, 4))
    story.append(
        HRFlowable(width="100%", color=rl_colors.HexColor(_GRID), thickness=0.75)
    )
    story.append(Spacer(1, 10))

    if user_message:
        story.append(
            Paragraph(
                f"<b>You asked:</b> {_md_inline(user_message)}", styles["Question"]
            )
        )

    if notes:
        story.append(Paragraph("Working steps", styles["Heading3"]))
        story.append(
            ListFlowable(
                [
                    ListItem(Paragraph(_md_inline(nt), styles["Small"]), leftIndent=6)
                    for nt in notes
                ],
                bulletType="bullet",
                leftIndent=14,
            )
        )
        story.append(Spacer(1, 10))

    for chart in charts:
        png = _mpl_chart_png(chart)
        if png:
            story.append(
                Image(io.BytesIO(png), width=6.3 * inch, height=6.3 * inch * 3.2 / 6.3)
            )
            story.append(Spacer(1, 8))
            continue
        if chart.get("title"):
            story.append(Paragraph(_md_inline(chart["title"]), styles["Heading3"]))
        cats = chart.get("categories") or []
        series = chart.get("series") or []
        if cats and series:
            header = ["Category"] + [
                s.get("name") or f"Series {i + 1}" for i, s in enumerate(series)
            ]
            rows = [header]
            for idx, c in enumerate(cats):
                row = [str(c)]
                for s in series:
                    d = s.get("data") or []
                    row.append(str(d[idx]) if idx < len(d) else "")
                rows.append(row)
            story.append(_table_flowable(rows, styles))
        story.append(
            Paragraph(
                f"(chart type '{_md_escape(str(chart.get('chart_type', '')))}' isn't "
                "rendered as an image in PDF exports — showing its data instead)",
                styles["Small"],
            )
        )
        story.append(Spacer(1, 8))

    if final_text:
        story.extend(_markdown_flowables(final_text, styles))

    doc.build(story)
    return buf.getvalue()
