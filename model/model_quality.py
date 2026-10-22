from __future__ import annotations

import glob
import os
import re

_TEXT_CACHE: dict[str, tuple[float, str]] = {}


def read_text_cached(path: str) -> str:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    hit = _TEXT_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = f.read()
    except OSError:
        return ""
    _TEXT_CACHE[path] = (mtime, data)
    return data


def _load_tmdl_files(model_dir: str) -> dict[str, str]:
    files: dict[str, str] = {}
    defn = os.path.join(model_dir, "definition")
    base = defn if os.path.isdir(defn) else model_dir
    for p in glob.glob(os.path.join(base, "**", "*.tmdl"), recursive=True):
        files[os.path.relpath(p, base).replace(os.sep, "/")] = read_text_cached(p)
    return files


def find_model_dir(path: str) -> str | None:
    path = path.rstrip("/\\")
    if glob.glob(os.path.join(path, "definition", "*.tmdl")):
        return path
    hits = glob.glob(os.path.join(path, "**", "*.SemanticModel"), recursive=True)
    if hits:
        return hits[0]
    parent = os.path.dirname(path)
    sib = glob.glob(os.path.join(parent, "*.SemanticModel"))
    return sib[0] if sib else None


_MEASURE_BLOCK = re.compile(
    r"^\s+measure\s+(?:'([^']+)'|([^=\s]+))\s*=\s*(.*?)"
    r"(?=\n\s+(?:measure|column|hierarchy|partition)\b|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _measure_blocks(text: str) -> list[tuple[str, str, str]]:
    out = []
    for m in _MEASURE_BLOCK.finditer(text):
        name = (m.group(1) or m.group(2) or "").strip()
        raw = m.group(3) or ""
        cut = re.search(r"\n\s+(?:\w+:|///|annotation\b)", raw)
        expr = (raw[: cut.start()] if cut else raw).strip()
        out.append((name, expr, m.group(0)))
    return out


def score_model(model_dir: str) -> dict:
    from evals.measure_correctness import run_measure_evals
    from evals.relationship_correctness import run_relationship_evals

    tmdl = _load_tmdl_files(model_dir)
    if not tmdl:
        return {"ok": False, "error": f"no TMDL files found under {model_dir}"}

    _, m_score = run_measure_evals(tmdl)
    _, r_score = run_relationship_evals(tmdl)

    all_text = "\n".join(tmdl.values())
    measures = [b for content in tmdl.values() for b in _measure_blocks(content)]
    n_measures = len(measures)

    issues: list[dict] = []

    def _flag(sev, msg):
        issues.append({"severity": sev, "message": msg})

    no_format = [n for n, e, blk in measures if "formatString" not in blk]
    bad_div = [
        n
        for n, e, blk in measures
        if re.search(r"[^/]/[^/]", e) and "DIVIDE" not in e.upper()
    ]
    blank_only = [
        n for n, e, blk in measures if e.replace(" ", "").upper() in ("BLANK()", "")
    ]
    no_folder = [n for n, e, blk in measures if "displayFolder" not in blk]

    has_date_table = bool(
        re.search(r"dataCategory:\s*Time|isDateTable", all_text, re.IGNORECASE)
    )
    has_dates = bool(re.search(r"dataType:\s*dateTime", all_text, re.IGNORECASE))

    if no_format:
        _flag(
            "warning",
            f"{len(no_format)} measure(s) missing formatString: {no_format[:8]}",
        )
    if bad_div:
        _flag(
            "error",
            f"{len(bad_div)} measure(s) use bare '/' instead of DIVIDE(): {bad_div[:8]}",
        )
    if blank_only:
        _flag(
            "error",
            f"{len(blank_only)} measure(s) are empty/BLANK() placeholders: {blank_only[:8]}",
        )
    if no_folder:
        _flag(
            "info",
            f"{len(no_folder)} measure(s) not in a displayFolder: {no_folder[:8]}",
        )
    if has_dates and not has_date_table:
        _flag(
            "warning",
            "date columns exist but no table is marked as a date table (dataCategory: Time)",
        )

    if n_measures:
        viol = len(bad_div) + len(blank_only) + 0.5 * len(no_format)
        denom = n_measures * 2.5
        structural = max(0.0, 1.0 - viol / denom)
    else:
        structural = 1.0

    overall = round(100 * (0.40 * m_score + 0.35 * r_score + 0.25 * structural))
    grade = (
        "A"
        if overall >= 90
        else (
            "B"
            if overall >= 80
            else "C" if overall >= 70 else "D" if overall >= 60 else "F"
        )
    )

    return {
        "ok": True,
        "score": overall,
        "grade": grade,
        "measure_score": round(m_score, 3),
        "relationship_score": round(r_score, 3),
        "structural_score": round(structural, 3),
        "counts": {
            "measures": n_measures,
            "missing_format": len(no_format),
            "bare_division": len(bad_div),
            "blank_placeholders": len(blank_only),
            "marked_date_table": has_date_table,
        },
        "issues": issues,
    }
