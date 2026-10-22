from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .plan import ColumnSpec, MeasureSpec, SchemaPlan, TableSpec

logger = logging.getLogger(__name__)


SHIFT_CONTEXT_TABLE = "_Shift Context"
S0_TABLE = "S0 - Current Shift"
S1_TABLE = "S1 - Previous Shift1"
S2_TABLE = "S2 - Previous Shift2"

_NON_CLONEABLE_MEASURES = {
    "shift_1_label",
    "shift_2_label",
    "shift_3_label",
    "_header",
    "_header_label",
    "_header_label_s2",
    "_currentshift_id",
    "_prevshift1_id",
    "_prevshift2_id",
    "currentshift_id",
    "prevshift1_id",
    "prevshift2_id",
    "prevdate_id",
    "prevdate_date",
    "_isstationselected",
    "previous-day shift",
}


_SHIFT_INTENT_SIGNALS = [
    "shift comparison",
    "current vs previous shift",
    "current shift",
    "previous shift",
    "shift over shift",
    "shift-over-shift",
    "s0/s1/s2",
    "s0_s1_s2",
    "shift trend",
    "across shifts",
    "shift level",
    "sds executive",
    "dms dashboard",
    "daily management system",
]


@dataclass
class ShiftExpansionResult:

    triggered: bool = False
    reason: str = ""
    created_tables: list[str] = field(default_factory=list)
    cloned_measures: list[tuple[str, str]] = field(default_factory=list)
    skipped_measures: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.triggered:
            return "shift-context expansion not needed"
        return (
            f"+{len(self.created_tables)} calc tables, "
            f"+{len(self.cloned_measures)} per-shift measure variants"
        )


def _should_expand(
    plan: SchemaPlan,
    requirements: str,
    kpi_requirements_block: str,
    reference_model_block: str,
) -> tuple[bool, str]:
    haystack = (
        (requirements or "")
        + " "
        + (kpi_requirements_block or "")
        + " "
        + (reference_model_block or "")
    ).lower()

    if "shift-context pattern" in haystack or S0_TABLE.lower() in haystack:
        return True, "reference model has shift-context pattern"

    for sig in _SHIFT_INTENT_SIGNALS:
        if sig in haystack:
            return True, f"detected intent signal: '{sig}'"

    plan_table_names = {t.name.lower() for t in plan.tables}
    has_shifts = any("shift" in n for n in plan_table_names)
    has_meetings_input = any(
        "meetings_input" in n or "meeting_input" in n for n in plan_table_names
    )
    has_plant_shift = any("plant_shift_mapping" in n for n in plan_table_names)
    if has_shifts and has_meetings_input and has_plant_shift:
        return (
            True,
            "schema has shifts + meetings_input + plant_shift_mapping (DMS pattern)",
        )

    return False, ""


def _has_shift_context_already(plan: SchemaPlan) -> bool:
    names = {t.name for t in plan.tables}
    return S0_TABLE in names or SHIFT_CONTEXT_TABLE in names


def _add_shift_context_tables(plan: SchemaPlan) -> list[str]:
    added: list[str] = []

    plan.tables.append(
        TableSpec(
            name=SHIFT_CONTEXT_TABLE,
            kind="calculated",
            schema=None,
            source_table=None,
            is_hidden=False,
            description="Shift navigation helpers: current and previous shift IDs",
            columns=[
                ColumnSpec(
                    name="_dummy",
                    source_column="[_dummy]",
                    data_type="int64",
                    is_hidden=True,
                    is_key=True,
                    summarize_by="none",
                    dax_expression='ROW("_dummy", 1)',
                ),
            ],
        )
    )
    added.append(SHIFT_CONTEXT_TABLE)

    for tname, desc in [
        (S0_TABLE, "Measures filtered to the current shift"),
        (S1_TABLE, "Measures filtered to the previous shift (S-1)"),
        (S2_TABLE, "Measures filtered to the shift before previous (S-2)"),
    ]:
        plan.tables.append(
            TableSpec(
                name=tname,
                kind="calculated",
                schema=None,
                source_table=None,
                is_hidden=False,
                description=desc,
                columns=[
                    ColumnSpec(
                        name="_dummy",
                        source_column="[_dummy]",
                        data_type="int64",
                        is_hidden=True,
                        is_key=True,
                        summarize_by="none",
                        dax_expression='ROW("_dummy", 1)',
                    ),
                ],
            )
        )
        added.append(tname)
    return added


def _add_shift_nav_measures(plan: SchemaPlan) -> list[tuple[str, str]]:
    existing = {m.name for m in plan.measures}
    nav_measures = [
        (
            "CurrentShift_ID",
            "The shift_id of the current shift (today's most recent shift). Returns BLANK if no input today.",
        ),
        ("PrevShift1_ID", "The shift_id immediately before CurrentShift_ID."),
        ("PrevShift2_ID", "The shift_id immediately before PrevShift1_ID."),
        ("PrevDate_ID", "The shift_id used when comparing to yesterday's last shift."),
        ("PrevDate_Date", "The date of PrevDate_ID's shift."),
    ]
    added: list[tuple[str, str]] = []
    for name, intent in nav_measures:
        if name in existing:
            continue
        plan.measures.append(
            MeasureSpec(
                name=name,
                host_table=SHIFT_CONTEXT_TABLE,
                intent=intent,
                template_id="CurrentShift_ID" if name == "CurrentShift_ID" else None,
                display_folder="Shift Navigation",
            )
        )
        existing.add(name)
        added.append(("(shift-nav)", name))
    return added


def _is_cloneable(measure: MeasureSpec) -> bool:
    low = measure.name.lower()
    if low in _NON_CLONEABLE_MEASURES:
        return False
    if re.search(r"_(s0|s1|s2|s_prev|s_prev2)$", low):
        return False
    return not any(k in low for k in ["label", "header", "switch", "isstation"])


def _clone_measure(
    base: MeasureSpec,
    new_host: str,
    suffix: str,
    intent_suffix: str,
) -> MeasureSpec:
    return MeasureSpec(
        name=f"{base.name}{suffix}",
        host_table=new_host,
        intent=f"{base.intent} {intent_suffix}".strip(),
        template_id=base.template_id,
        format_string=base.format_string,
        display_folder=base.display_folder or "Shift Variants",
    )


def expand_shift_context(
    plan: SchemaPlan,
    requirements: str = "",
    kpi_requirements_block: str = "",
    reference_model_block: str = "",
) -> tuple[SchemaPlan, ShiftExpansionResult]:
    result = ShiftExpansionResult()

    should, reason = _should_expand(
        plan, requirements, kpi_requirements_block, reference_model_block
    )
    if not should:
        return plan, result

    if _has_shift_context_already(plan):
        result.reason = "shift-context tables already present — skipping expansion"
        return plan, result

    result.triggered = True
    result.reason = reason

    base_measures = [m for m in plan.measures if _is_cloneable(m)]

    result.created_tables = _add_shift_context_tables(plan)
    logger.info(f"Shift-context: added tables {result.created_tables}")

    nav_added = _add_shift_nav_measures(plan)
    result.cloned_measures.extend(nav_added)

    for base in base_measures:
        s1_name = f"{base.name}_S1"
        s2_name = f"{base.name}_S2"
        existing_names = {m.name for m in plan.measures}
        if s1_name not in existing_names:
            plan.measures.append(
                _clone_measure(base, S1_TABLE, "_S1", "(filtered to PrevShift1_ID)")
            )
            result.cloned_measures.append((base.name, s1_name))
        if s2_name not in existing_names:
            plan.measures.append(
                _clone_measure(base, S2_TABLE, "_S2", "(filtered to PrevShift2_ID)")
            )
            result.cloned_measures.append((base.name, s2_name))

    logger.info(
        f"Shift-context expansion done: "
        f"{len(result.created_tables)} tables, "
        f"{len(result.cloned_measures)} measure additions"
    )
    return plan, result
