from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_AUTO_DATE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")
_PROPERTY_PREFIXES = (
    "lineageTag:",
    "formatString:",
    "displayFolder:",
    "isHidden",
    "annotation ",
    "kpi ",
    "kpi:",
    "description:",
)


@dataclass
class ReferenceMeasure:
    name: str
    host_table: str
    dax: str
    format_string: str | None = None
    display_folder: str | None = None


@dataclass
class ReferenceColumn:
    name: str
    data_type: str | None = None
    is_hidden: bool = False
    is_key: bool = False
    has_dax: bool = False


@dataclass
class ReferenceTable:
    name: str
    kind: str
    is_hidden: bool = False
    columns: list[ReferenceColumn] = field(default_factory=list)
    measure_count: int = 0
    table_dax: str | None = None


@dataclass
class ReferenceRelationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    is_active: bool = True
    cross_filter: str = "singleDirection"


@dataclass
class ReferenceModel:
    source_path: str
    model_name: str = ""
    tables: list[ReferenceTable] = field(default_factory=list)
    measures: list[ReferenceMeasure] = field(default_factory=list)
    relationships: list[ReferenceRelationship] = field(default_factory=list)
    detected_patterns: list[str] = field(default_factory=list)

    @property
    def business_tables(self) -> list[ReferenceTable]:
        return [t for t in self.tables if not t.name.startswith(_AUTO_DATE_PREFIXES)]

    @property
    def measure_count(self) -> int:
        return len(self.measures)

    @property
    def relationship_count(self) -> int:
        return len(self.relationships)

    def measures_on(self, table_name: str) -> list[ReferenceMeasure]:
        return [m for m in self.measures if m.host_table == table_name]

    def to_prompt_block(self, max_chars: int = 12000, sample_measures: int = 30) -> str:
        lines: list[str] = []
        biz_tables = self.business_tables
        model_label = Path(self.source_path).name if self.source_path else self.model_name or "model"
        lines.append(f"## Reference model: `{model_label}`\n")
        lines.append(
            f"An existing PBIP model the new build should imitate. "
            f"{len(biz_tables)} business tables, {self.measure_count} measures, "
            f"{self.relationship_count} relationships.\n"
        )

        if self.detected_patterns:
            lines.append("### Detected patterns to replicate")
            for p in self.detected_patterns:
                lines.append(f"- {p}")
            lines.append("")

        lines.append("### Tables (kind · #cols · #measures)")
        for t in biz_tables:
            n_meas = len(self.measures_on(t.name))
            lines.append(f"- `{t.name}` ({t.kind} · {len(t.columns)} cols · {n_meas} measures)")
        lines.append("")

        non_trivial = sorted(
            self.measures,
            key=lambda m: (-(len(m.dax) if m.dax else 0), m.name),
        )[:sample_measures]

        if non_trivial:
            lines.append(f"### Sample measures (top {len(non_trivial)} by complexity)")
            for m in non_trivial:
                dax_compact = re.sub(r"\s+", " ", m.dax or "").strip()
                if len(dax_compact) > 220:
                    dax_compact = dax_compact[:220] + " …"
                folder = f" [{m.display_folder}]" if m.display_folder else ""
                lines.append(f"- **{m.name}** on `{m.host_table}`{folder}: `{dax_compact}`")
            lines.append("")

        if self.relationships:
            lines.append("### Relationships")
            for r in self.relationships:
                active = "" if r.is_active else " (inactive)"
                lines.append(f"- `{r.from_table}.{r.from_column}` → `{r.to_table}.{r.to_column}`{active}")
            lines.append("")

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n... (reference truncated for prompt budget)"
        return text


_RX_TABLE_HEADER = re.compile(r"^\s*table\s+'?([^'\n]+?)'?\s*$", re.MULTILINE)
_RX_COLUMN_BLOCK = re.compile(
    r"^\s*column\s+'?([^'\n]+?)'?\s*$(.*?)(?=^\s*(?:column|measure|partition|table|hierarchy)\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_RX_MEASURE_BLOCK = re.compile(
    r"^\s*measure\s+'?([^'=\n]+?)'?\s*=\s*(.*?)(?=^\s*(?:column|measure|partition|table|hierarchy)\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_RX_RELATIONSHIP_BLOCK = re.compile(
    r"^\s*relationship\s+\S+\s*$(.*?)(?=^\s*relationship\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _clean_dax(body: str) -> str:
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in _PROPERTY_PREFIXES):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _parse_table_file(path: Path) -> tuple[ReferenceTable | None, list[ReferenceMeasure]]:
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to read {path}: {e}")
        return None, []

    header_m = _RX_TABLE_HEADER.search(content)
    if not header_m:
        return None, []
    table_name = header_m.group(1).strip()

    kind = "unknown"
    table_dax = None
    calc_table_m = re.search(
        r"^\s*table\s+'?[^'\n]+?'?\s*=\s*(.+?)(?=^\s*(?:column|measure|partition|table)\s|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    if calc_table_m:
        kind = "calculated"
        table_dax = _clean_dax(calc_table_m.group(1))
    elif re.search(r"^\s*partition\s+", content, re.MULTILINE):
        low = table_name.lower()
        if "fact" in low or low.endswith(("_input", "input")):
            kind = "fact"
        else:
            kind = "dim"

    table = ReferenceTable(
        name=table_name,
        kind=kind,
        is_hidden=bool(re.search(r"^\s*isHidden\b", content, re.MULTILINE)),
        table_dax=table_dax,
    )

    for cm in _RX_COLUMN_BLOCK.finditer(content):
        col_name = cm.group(1).strip()
        body = cm.group(2)
        dtype_m = re.search(r"^\s*dataType:\s*(\S+)", body, re.MULTILINE)
        table.columns.append(
            ReferenceColumn(
                name=col_name,
                data_type=dtype_m.group(1) if dtype_m else None,
                is_hidden=bool(re.search(r"^\s*isHidden\b", body, re.MULTILINE)),
                is_key=bool(re.search(r"^\s*isKey\b", body, re.MULTILINE)),
                has_dax=bool(re.search(r"^\s*expression:", body, re.MULTILINE)),
            )
        )

    measures: list[ReferenceMeasure] = []
    for mm in _RX_MEASURE_BLOCK.finditer(content):
        name = mm.group(1).strip()
        body = mm.group(2)
        dax = _clean_dax(body)
        fmt_m = re.search(r"^\s*formatString:\s*(.+)$", body, re.MULTILINE)
        folder_m = re.search(r"^\s*displayFolder:\s*(.+)$", body, re.MULTILINE)
        measures.append(
            ReferenceMeasure(
                name=name,
                host_table=table_name,
                dax=dax,
                format_string=fmt_m.group(1).strip() if fmt_m else None,
                display_folder=folder_m.group(1).strip() if folder_m else None,
            )
        )

    table.measure_count = len(measures)
    return table, measures


def _parse_ref(line_value: str) -> tuple[str, str] | None:
    s = line_value.strip()
    if not s:
        return None
    # 'Table'[Column] or Table[Column]
    m = re.match(r"^'?([^'\[\]]+)'?\[([^\[\]]+)\]$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # 'Table'.'Column' or Table.Column or 'Table'.Column or Table.'Column'
    m = re.match(r"^'?([^'\.]+)'?\.'?([^'\.]+)'?$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None


def _parse_relationships_file(path: Path) -> list[ReferenceRelationship]:
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to read {path}: {e}")
        return []

    rels: list[ReferenceRelationship] = []
    for rm in _RX_RELATIONSHIP_BLOCK.finditer(content):
        body = rm.group(1)
        from_line = re.search(r"^\s*fromColumn:\s*(.+?)\s*$", body, re.MULTILINE)
        to_line = re.search(r"^\s*toColumn:\s*(.+?)\s*$", body, re.MULTILINE)
        if not (from_line and to_line):
            continue

        from_ref = _parse_ref(from_line.group(1))
        to_ref = _parse_ref(to_line.group(1))
        if not (from_ref and to_ref):
            continue

        cf_m = re.search(r"crossFilteringBehavior:\s*(\S+)", body)
        cross = cf_m.group(1).strip() if cf_m else "singleDirection"

        rels.append(
            ReferenceRelationship(
                from_table=from_ref[0],
                from_column=from_ref[1],
                to_table=to_ref[0],
                to_column=to_ref[1],
                is_active=not bool(re.search(r"isActive:\s*false", body)),
                cross_filter=cross,
            )
        )
    return rels


def _detect_patterns(model: ReferenceModel) -> list[str]:
    patterns: list[str] = []
    table_names = {t.name for t in model.business_tables}

    shift_indicators = {
        "S0 - Current Shift",
        "S1 - Previous Shift1",
        "S2 - Previous Shift2",
    }
    if shift_indicators & table_names:
        patterns.append(
            "**Shift-context pattern**: measures are organized into three calculated "
            "tables (`S0 - Current Shift`, `S1 - Previous Shift1`, `S2 - Previous "
            "Shift2`) that hold the same KPI computed for different shift windows. "
            "Each business KPI exists in 3 variants (e.g. `Defects_Found_S0` / `_S1` "
            "/ `_S2`). If the new build covers shift-comparison dashboards, replicate "
            "this structure with calculated tables and per-shift measures."
        )

    underscore_calcs = [
        t.name
        for t in model.business_tables
        if t.name.startswith("_") and t.kind == "calculated"
    ]
    if underscore_calcs:
        sample = ", ".join(underscore_calcs[:5]) + ("..." if len(underscore_calcs) > 5 else "")
        patterns.append(
            f"**Helper-calc-tables convention**: tables prefixed with `_` ({sample}) "
            f"hold measure-only logic; they have no real data, just measures and small "
            f"DAX expressions for shift navigation, dynamic switches, debug, etc."
        )

    date_rel_counts: dict[str, int] = {}
    for r in model.relationships:
        if r.to_table.lower() in {"dimdate", "date", "datetable"}:
            date_rel_counts[r.from_table] = date_rel_counts.get(r.from_table, 0) + 1
    multi_date = [t for t, n in date_rel_counts.items() if n > 1]
    if multi_date:
        patterns.append(
            f"**Role-playing dates**: tables {', '.join(multi_date)} each have multiple "
            f"relationships to DimDate (active + inactive). Use `USERELATIONSHIP` "
            f"in measures to switch between date roles."
        )

    bridge_tables = [
        t.name
        for t in model.business_tables
        if "bridge" in t.name.lower() or t.name.lower().startswith("brigde")
    ]
    if bridge_tables:
        patterns.append(
            f"**Bridge tables**: {', '.join(bridge_tables)}. Used to resolve many-to-many "
            f"relationships between facts and dims. Replicate when source facts "
            f"have m:m joins on the same key."
        )

    d365_tables = [
        t.name
        for t in model.business_tables
        if t.name.startswith("dw eAPS_")
        or "d365" in t.name.lower()
        or "dataverse" in t.name.lower()
    ]
    if d365_tables:
        sample = ", ".join(d365_tables[:3]) + ("..." if len(d365_tables) > 3 else "")
        patterns.append(
            f"**External-system views**: {len(d365_tables)} D365-sourced tables ({sample}). "
            f"These come from a separate warehouse; load them as views or import "
            f"tables alongside the primary database."
        )

    return patterns


def _resolve_model_paths(pbip_path: str | Path) -> tuple[Path, Path, str, str]:
    if isinstance(pbip_path, str):
        pbip_path = pbip_path.strip().strip('"').strip("'")
        if not pbip_path:
            raise ValueError(
                "Path is empty. Paste the full path to a .pbip file, .SemanticModel folder, or definition folder."
            )
    p = Path(pbip_path).resolve()

    if p.is_dir():
        if (p / "tables").is_dir():
            definition = p
            model_name = p.parent.name.replace(".SemanticModel", "")
            return definition, p / "tables", model_name, str(p.parent)
        if p.name == "tables" and p.parent.name == "definition":
            definition = p.parent
            model_name = definition.parent.name.replace(".SemanticModel", "")
            return definition, p, model_name, str(definition.parent)

    if p.is_file() and p.suffix == ".pbip":
        sem_folder = p.parent / (p.stem + ".SemanticModel")
        if (sem_folder / "definition" / "tables").is_dir():
            return sem_folder / "definition", sem_folder / "definition" / "tables", p.stem, str(p)

    if p.is_dir():
        sem_folder = p if p.name.endswith(".SemanticModel") else None
        if not sem_folder:
            for child in p.iterdir():
                if child.is_dir() and child.name.endswith(".SemanticModel"):
                    sem_folder = child
                    break
        if sem_folder and (sem_folder / "definition" / "tables").is_dir():
            name = sem_folder.name.replace(".SemanticModel", "")
            source = str(sem_folder.parent / f"{name}.pbip")
            return sem_folder / "definition", sem_folder / "definition" / "tables", name, source

    raise FileNotFoundError(
        f"Couldn't find a .SemanticModel definition/tables folder for: {pbip_path}"
    )


def load_pbip_reference(pbip_path: str | Path) -> ReferenceModel:
    definition, tables_dir, model_name, source_path = _resolve_model_paths(pbip_path)

    model = ReferenceModel(source_path=source_path, model_name=model_name)

    for tmdl in sorted(tables_dir.glob("*.tmdl")):
        if tmdl.stem.startswith(_AUTO_DATE_PREFIXES):
            continue
        table, measures = _parse_table_file(tmdl)
        if table is not None:
            model.tables.append(table)
            model.measures.extend(measures)

    model.relationships = _parse_relationships_file(definition / "relationships.tmdl")
    model.detected_patterns = _detect_patterns(model)

    logger.info(
        f"Loaded reference {model.model_name}: "
        f"{len(model.business_tables)} business tables, "
        f"{model.measure_count} measures, "
        f"{model.relationship_count} relationships, "
        f"{len(model.detected_patterns)} detected patterns"
    )
    return model
