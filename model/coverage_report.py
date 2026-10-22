from __future__ import annotations

import re
from difflib import SequenceMatcher

_STOP = frozenset(
    {
        "and",
        "or",
        "the",
        "of",
        "in",
        "by",
        "with",
        "vs",
        "per",
        "a",
        "an",
        "to",
        "for",
        "on",
        "last",
        "current",
        "total",
        "count",
        "number",
        "num",
        "no",
        "pill",
        "tab",
        "nav",
        "header",
        "block",
        "grid",
        "gauge",
        "donut",
        "bar",
        "line",
        "combo",
        "table",
        "matrix",
        "card",
        "chart",
        "sparkline",
        "panel",
        "column",
        "col",
        "row",
        "section",
        "view",
        "tile",
        "kpi",
        "min",
        "hrs",
        "hr",
        "vsactual",
        "actual",
        "planned",
    }
)

_KEEP_SHORT = frozenset(
    {
        "oee",
        "pph",
        "pdt",
        "updt",
        "mtbf",
        "bde",
        "pf",
        "fpy",
        "pa",
        "cil",
        "co",
        "ool",
        "sds",
        "dds",
        "wds",
        "mds",
        "rd",
        "ap",
        "cc",
        "ytd",
        "mtd",
    }
)


def extract_measure_names(tmdl_files: dict[str, str]) -> list[str]:
    names: list[str] = []
    blob = "\n".join(tmdl_files.values())
    for m in re.finditer(
        r"^\s*measure\s+(?:'([^']+)'|([^=\s]+))\s*=", blob, re.MULTILINE
    ):
        names.append((m.group(1) or m.group(2) or "").strip())
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def extract_requested_items(spec: dict | None) -> list[str]:
    if not isinstance(spec, dict):
        return []
    items: list[str] = []
    ke = spec.get("key_elements")
    if isinstance(ke, list):
        items = [str(x).strip() for x in ke if str(x).strip()]
    elif isinstance(ke, str) and ke.strip():
        items = [ln.strip("-• \t") for ln in ke.splitlines() if ln.strip()]
    return items


def _tokens(text: str) -> set[str]:
    toks = set()
    for raw in re.split(r"[^A-Za-z0-9]+", text.lower()):
        if not raw:
            continue
        if raw in _STOP:
            continue
        if len(raw) <= 2 and raw not in _KEEP_SHORT:
            continue
        toks.add(raw)
    return toks


def _score(requested: str, measure: str) -> float:
    rt, mt = _tokens(requested), _tokens(measure)
    if not rt:
        return 0.0
    overlap = len(rt & mt) / len(rt)
    seq = SequenceMatcher(None, requested.lower(), measure.lower()).ratio()
    return max(overlap, 0.6 * overlap + 0.4 * seq)


def match_coverage(
    requested: list[str],
    measures: list[str],
    covered_at: float = 0.5,
    partial_at: float = 0.25,
) -> dict:
    items = []
    used: set[str] = set()
    for req in requested:
        best, best_score = "", 0.0
        for meas in measures:
            s = _score(req, meas)
            if s > best_score:
                best, best_score = meas, s
        status = (
            "covered"
            if best_score >= covered_at
            else "partial" if best_score >= partial_at else "missing"
        )
        if status != "missing" and best:
            used.add(best)
        items.append(
            {
                "requested": req,
                "best_measure": best if status != "missing" else "",
                "score": round(best_score, 2),
                "status": status,
            }
        )

    total = len(items)
    covered = sum(1 for i in items if i["status"] == "covered")
    partial = sum(1 for i in items if i["status"] == "partial")
    missing = sum(1 for i in items if i["status"] == "missing")
    pct = round(100 * (covered + 0.5 * partial) / total) if total else 0
    orphans = [m for m in measures if m not in used]

    return {
        "items": items,
        "summary": {
            "total": total,
            "covered": covered,
            "partial": partial,
            "missing": missing,
            "coverage_pct": pct,
            "measure_count": len(measures),
            "orphan_count": len(orphans),
        },
        "orphan_measures": orphans,
    }


def coverage_markdown(result: dict, title: str = "") -> str:
    s = result["summary"]
    icon = {"covered": "", "partial": "", "missing": ""}
    out = [f"# Coverage Report — {title}".rstrip(" —"), ""]
    out.append(
        f"_{s['coverage_pct']}% coverage · {s['covered']} covered · "
        f"{s['partial']} partial · {s['missing']} missing · "
        f"{s['total']} requested items · {s['measure_count']} measures_"
    )
    out += [
        "",
        "## Requested items",
        "",
        "| Status | Requested (wireframe) | Best-match measure | Score |",
        "|---|---|---|---|",
    ]
    for it in result["items"]:
        out.append(
            f"| {icon[it['status']]} {it['status']} | {it['requested']} | "
            f"{it['best_measure'] or '—'} | {it['score']} |"
        )
    if result["orphan_measures"]:
        out += [
            "",
            f"## Measures not tied to any wireframe item ({len(result['orphan_measures'])})",
            "",
        ]
        out += [f"- {m}" for m in result["orphan_measures"]]
    return "\n".join(out)
