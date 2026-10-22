from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


_FIELD_PATTERNS: list[tuple[str, list[str]]] = [
    ("name", ["measurement unit", "kpi name", "measure name", "metric name", "kpi"]),
    ("category", ["category", "dms", "area", "section"]),
    ("unit", ["unit"]),
    ("logic", ["logic", "color coding", "colour coding"]),
    ("calculation", ["calculation", "cal :", "calc id"]),
    ("formula", ["formula", "expression"]),
    ("dependency", ["dependency", "depends on", "defendency"]),
    ("source", ["source", "data source"]),
    ("source_form", ["echecklist", "form"]),
    ("owner", ["owner"]),
    ("source_table_d365", ["d365"]),
    ("source_table_spc", ["spc"]),
    ("display_sds", ["sds"]),
    ("display_dds", ["dds"]),
    ("display_wds", ["wds"]),
    ("display_mds", ["mds"]),
    ("remarks", ["remarks", "notes", "comment"]),
]


@dataclass
class KPIRow:

    row_index: int
    id: str | None = None
    name: str | None = None
    category: str | None = None
    unit: str | None = None
    formula: str | None = None
    logic: str | None = None
    calculation: str | None = None
    dependency: str | None = None
    source: str | None = None
    source_form: str | None = None
    owner: str | None = None
    source_table_d365: str | None = None
    source_table_spc: str | None = None
    display_levels: list[str] = field(default_factory=list)
    remarks: str | None = None
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def has_actionable_content(self) -> bool:
        return bool((self.name or "").strip() or (self.formula or "").strip())


@dataclass
class RequirementsDoc:

    source_path: str
    kpis: list[KPIRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def actionable_count(self) -> int:
        return sum(1 for k in self.kpis if k.has_actionable_content)

    def to_prompt_block(self, max_chars: int = 18000) -> str:
        if not self.kpis:
            return ""

        lines: list[str] = []
        lines.append(
            f"## Requirements: {self.actionable_count} KPIs from `{Path(self.source_path).name}`"
        )
        lines.append("")
        lines.append(
            "Each row is a target measure the model should support. Use these to:"
        )
        lines.append(
            "1. Identify which fact tables are needed (from `source`, `source_table_d365`, `source_table_spc`)"
        )
        lines.append("2. Plan measures that match the KPI names and formulas")
        lines.append("3. Include any column referenced in a formula")
        lines.append("")
        lines.append(
            "Format: `[ID] CATEGORY · NAME (UNIT) — formula | source | levels`"
        )
        lines.append("")

        rows = sorted(
            (k for k in self.kpis if k.has_actionable_content),
            key=lambda k: (k.category or "zz", k.row_index),
        )

        for k in rows:
            id_str = f"[{k.id}] " if k.id else ""
            cat = (k.category or "?").strip()
            name = (k.name or "?").strip()
            unit = f" ({k.unit.strip()})" if k.unit else ""
            formula = (k.formula or "").strip()
            source = (k.source or "").strip()
            levels = ",".join(k.display_levels) if k.display_levels else ""
            extras = " | ".join(
                p
                for p in [
                    f"formula: {formula}" if formula else "",
                    f"src: {source}" if source else "",
                    f"levels: {levels}" if levels else "",
                ]
                if p
            )
            line = f"- {id_str}{cat} · {name}{unit}" + (
                f" — {extras}" if extras else ""
            )
            lines.append(line)

        if self.notes:
            lines.append("")
            lines.append("### Loader notes")
            for n in self.notes:
                lines.append(f"- {n}")

        text = "\n".join(lines)
        if len(text) > max_chars:
            keep = int(len(rows) * max_chars / len(text))
            text = (
                "\n".join(lines[: 5 + keep])
                + f"\n\n... ({len(rows) - keep} more KPIs truncated for prompt budget)"
            )
        return text


def _classify_column(col_name: str) -> str | None:
    if not col_name:
        return None
    low = col_name.lower().strip()
    for field_name, needles in _FIELD_PATTERNS:
        if any(n in low for n in needles):
            return field_name
    return None


def _find_header_row(rows: list[list[str]], max_search: int = 10) -> int:
    best_idx = 0
    best_score = 0
    for i, row in enumerate(rows[:max_search]):
        score = sum(1 for cell in row if _classify_column(cell))
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx


def _truthy_marker(cell: str) -> bool:
    return (cell or "").strip().lower() in {"yes", "y", "x", "true", "1", "", "✔"}


def load_requirements_csv(
    path: str | Path,
    encoding: str = "utf-8-sig",
) -> RequirementsDoc:
    if isinstance(path, str):
        path = path.strip().strip('"').strip("'")
        if not path:
            raise ValueError(
                "Path is empty. Paste the full path to your KPI CSV "
                "(e.g. C:\\projects\\...\\Data Mapping.csv)"
            )
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Requirements CSV not found: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"Path is a directory, not a CSV file: {path}")

    with open(path, "r", encoding=encoding, newline="") as f:
        all_rows = list(csv.reader(f))

    if not all_rows:
        return RequirementsDoc(source_path=str(path), notes=["CSV was empty"])

    header_idx = _find_header_row(all_rows)
    headers = all_rows[header_idx]
    data_rows = all_rows[header_idx + 1 :]

    col_field_map: dict[int, str] = {}
    unmapped: list[str] = []
    for i, h in enumerate(headers):
        field_name = _classify_column(h)
        if field_name:
            col_field_map[i] = field_name
        elif h and h.strip():
            unmapped.append(h.strip())

    doc = RequirementsDoc(source_path=str(path))

    for ri, row in enumerate(data_rows):
        if not any((c or "").strip() for c in row):
            continue

        k = KPIRow(row_index=header_idx + 2 + ri)
        raw: dict[str, str] = {}

        for ci, cell in enumerate(row):
            if ci >= len(headers):
                continue
            col_name = headers[ci] if ci < len(headers) else f"col_{ci}"
            value = (cell or "").strip()
            if not value:
                continue
            raw[col_name] = value

            field_name = col_field_map.get(ci)
            if not field_name:
                continue

            if field_name.startswith("display_"):
                level = field_name.replace("display_", "").upper()
                if _truthy_marker(value):
                    k.display_levels.append(level)
            elif field_name == "category" and not k.category:
                k.category = value
            elif not getattr(k, field_name, None):
                setattr(k, field_name, value)

        first_col_value = (row[0] if row else "").strip()
        if re.match(r"^\d+$", first_col_value):
            k.id = first_col_value

        k.raw = raw

        if not k.has_actionable_content and not k.category:
            continue
        doc.kpis.append(k)

    logger.info(
        f"Loaded {len(doc.kpis)} KPI rows from {path.name} "
        f"({doc.actionable_count} actionable, {len(unmapped)} unmapped columns)"
    )
    return doc
