from __future__ import annotations

import os
import re
import tempfile

from model.coverage_report import (
    coverage_markdown,
    extract_measure_names,
    extract_requested_items,
    match_coverage,
)

_RX_MEASURE = re.compile(r"^\s*measure\s+", re.MULTILINE)
_ARTIFACT_CACHE: dict[int, dict] = {}


def materialize(tmdl_files: dict) -> str:
    tmp = tempfile.mkdtemp(prefix="pbi_report_")
    for rel, content in tmdl_files.items():
        full = os.path.join(tmp, "definition", rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    return tmp


def model_stats(tmdl: dict) -> dict:
    return {
        "tables": sum(1 for p in tmdl if p.startswith("tables/")),
        "measures": sum(len(_RX_MEASURE.findall(v)) for v in tmdl.values()),
        "relationships": tmdl.get("relationships.tmdl", "").count("relationship "),
    }


def coverage(requested: list[str], tmdl: dict, model_name: str) -> dict:
    result = match_coverage(requested, extract_measure_names(tmdl))
    return {
        "summary": result["summary"],
        "items": result["items"],
        "markdown": coverage_markdown(result, model_name),
    }


def requested_items(spec) -> list[str]:
    return extract_requested_items(spec)


def approved_coverage(tmdl: dict, wireframes) -> dict | None:
    approved = next((w for w in wireframes if w.status == "approved"), None)
    if not approved:
        return None
    requested = extract_requested_items(approved.spec)
    if not requested:
        return None
    return match_coverage(requested, extract_measure_names(tmdl))


def build_artifacts(model_id: int, model_name: str, tmdl: dict, coverage_result) -> dict:
    from model.model_docs import generate_docs
    from model.model_export import export_excel
    from report.model_deck import build_pdf, build_pptx

    cached = _ARTIFACT_CACHE.get(model_id)
    if cached and cached["fingerprint"] == _fingerprint(tmdl):
        return cached

    tmp_dir = materialize(tmdl)
    markdown = generate_docs(tmp_dir, model_name)
    xlsx_path = export_excel(
        tmp_dir, os.path.join(tmp_dir, f"{model_name}_dictionary.xlsx")
    )
    with open(xlsx_path, "rb") as fh:
        xlsx = fh.read()

    built = {
        "fingerprint": _fingerprint(tmdl),
        "markdown": markdown,
        "xlsx": xlsx,
        "pptx": build_pptx(tmdl, model_name, coverage=coverage_result),
        "pdf": build_pdf(tmdl, model_name, coverage=coverage_result),
    }
    _ARTIFACT_CACHE[model_id] = built
    return built


def _fingerprint(tmdl: dict) -> int:
    return hash(tuple(sorted((k, len(v)) for k, v in tmdl.items())))
