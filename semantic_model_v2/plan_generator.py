from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

from llm.llm_client import UsageInfo, get_model_name, get_provider
from util.json_utils import strip_json_fences as _strip_json_fences

from ._llm import log_usage, stream_collect
from .plan import SchemaPlan, dict_to_plan
from .validate import format_errors, validate_plan

logger = logging.getLogger(__name__)


PLAN_SYSTEM_PROMPT = """You are a Power BI semantic model architect. Given a database schema, a data profile, and dashboard requirements, design the model AS STRUCTURED JSON. The deterministic skeleton generator will turn your JSON into TMDL — you do NOT write TMDL.

## Output

Output ONLY valid JSON matching this exact shape — nothing before, nothing after, no markdown fences:

{
  "model_name": "string — usually the project name",
  "server_fqdn": "string — pass through from input",
  "db_name": "string — pass through from input",
  "tables": [
    {
      "name": "string — the FRIENDLY model table name that appears in the model (e.g. 'Sales', 'Product', 'FactReturns'). Rename raw source tables to clean names; do NOT put the schema prefix here.",
      "kind": "fact" | "dim" | "calculated",
      "schema": "string — schema of the SOURCE table (e.g. 'DEP', 'dbo') — null for calculated",
      "source_table": "string — the EXACT physical source table name; with `schema` it MUST resolve to a real table in the source schema (e.g. schema='dbo', source_table='adventureworks sales data 2020'). null for calculated",
      "is_hidden": false,
      "description": "string or null — one short sentence about the table's role",
      "columns": [
        {
          "name": "string — display name (usually = source_column)",
          "source_column": "string — MUST exist in the source schema (for import tables); for calculated-table columns referencing the table's own DAX result, use bracket form like '[Date]'",
          "data_type": "int64" | "double" | "string" | "dateTime" | "boolean",
          "is_hidden": false,
          "is_key": false,
          "summarize_by": "none" | "sum" | "count" | null,
          "display_folder": "string or null",
          "description": "string or null",
          "dax_expression": "string or null — REQUIRED for non-key columns on calculated tables. Holds the DAX that produces the column's values. Example: 'YEAR([Date])', 'FORMAT([Date], \"MMMM\")', 'MONTH([Date])'. For the calculated table's KEY column, set dax_expression to the table's base generator like 'CALENDARAUTO()' or 'CALENDAR(DATE(2024,1,1), DATE(2027,12,31))' — that becomes the table's source. Leave null for columns on regular import (fact/dim) tables."
        }
      ]
    }
  ],
  "relationships": [
    {
      "from_table": "string — the fact side (many)",
      "from_column": "string — column on from_table; MUST exist there",
      "to_table": "string — the dim side (one)",
      "to_column": "string — column on to_table; MUST exist and be UNIQUE in data",
      "is_active": true,
      "cross_filter": "singleDirection",
      "rationale": "string — one sentence: why this relationship, why this column owns the join"
    }
  ],
  "measures": [
    {
      "name": "string — unique across the model; never the same as any column in host_table",
      "host_table": "string — must be one of tables[].name",
      "intent": "string — what the measure means in business terms (drives DAX generation in Phase 6)",
      "template_id": "string or null — match one of the names from the MEASURE TEMPLATE LIBRARY if applicable",
      "format_string": "string or null — e.g. '#,##0' or '0.0%'",
      "display_folder": "string or null"
    }
  ],
  "excluded_tables": [
    ["table_name", "reason — why we omitted it"]
  ],
  "excluded_relationships": [
    ["from.col -> to.col", "reason — why we omitted it"]
  ],
  "notes": [
    "short design rationale strings the user might want to read"
  ]
}

## Design rules — non-negotiable

### RULE 0 — ENUMERATE EVERY SCHEMA OBJECT (do this FIRST, before anything else)

Before applying any other design rule, walk the schema text line by line and
build an internal list of every object declared with `CREATE TABLE` or
`CREATE VIEW`. Every object MUST end up in EXACTLY ONE of:
  - `tables[]` (included in the model)
  - `excluded_tables[]` (intentionally omitted, with a one-line reason)

NO schema object may be silently dropped. A plan whose `tables[]` +
`excluded_tables[]` count is less than the schema's CREATE-statement count
is a BUG — the validator will flag it. To prevent this:

  (i) Count the `CREATE TABLE` lines and the `CREATE VIEW` lines separately.
 (ii) Reserve a slot for each one in either `tables[]` or `excluded_tables[]`.
(iii) At the end of your plan, the `notes[]` field MUST contain a single
      line of the form:
        "Coverage: <N>/<total> schema objects placed in tables[] · <M> excluded — see excluded_tables[]."

### RULE 0a — VIEWS ARE FIRST-CLASS FACTS (highest priority after Rule 0)

Objects declared with `CREATE VIEW` (not `CREATE TABLE`) are pre-joined /
denormalized queries the warehouse owners explicitly built for analytics.
In DMS / manufacturing / operational / Power Apps warehouses, views like
`vw_*_combined`, `vw_*_input`, `vw_*_mapping`, `vw_defects_*`,
`vw_downtime_*`, `vw_param_*`, `vw_input_*` are typically the PRIMARY
KPI source — they fold line-level + station-level rows together with a
`level` discriminator column, which base tables can't do without UNIONs.

**Hard rules for views:**
  (a) Include EVERY `CREATE VIEW` object in `tables[]`. Default kind=`fact`
      unless the view has < 5 columns and is mostly text (then `dim`).
  (b) `source_table` = the view name (last segment). The deterministic
      skeleton renderer will produce the correct M expression — you do
      not need to write `[Query="..."]` yourself.
  (c) Treat the view's `*_id` / `*_key` columns as foreign keys; create
      relationships to matching base-table primary keys.
  (d) Excluding a view requires HIGHER justification than excluding a
      base-table fact. Allowed reasons ONLY:
        - All-null in profile (view exists but has zero rows AND zero
          meaningful columns for analytics).
        - Pure system telemetry (clearly named `vw_audit_*`, `vw_sys_*`).
      "Looks similar to a base table" / "I don't see a KPI for this view"
      / "the base tables already cover this domain" are NOT valid reasons.

### RULE 0b — DETECT THE DOMAIN BEFORE APPLYING ARCHITECTURE

Before assuming hub-and-spoke, classify the schema. Look at the
distribution of column-name patterns:
  - **Insurance hub-and-spoke**: a single dim (e.g. `golddimpolicy`,
    `dimcustomer`) carries 10+ FK columns to lookups; every fact has the
    same natural key (e.g. `policyNumber`).
  - **Manufacturing / DMS / operational**: multiple roots
    (`plant`, `line`, `station`, `parameter`, `dms`). No single dim
    dominates; facts and views share several FKs without a universal one.
    For these schemas, follow Rule 1's hub-and-spoke ONLY IF a clear hub
    exists; otherwise, design as a **snowflake with multiple dim roots**
    and skip the "every fact joins to the hub" mandate.
  - **Star schema with one fact**: a single fact and a ring of dims —
    simplest case; treat the fact as the only "centerpiece".

Document the chosen domain shape in `notes[]` as the FIRST note:
  `"Domain shape: <hub-and-spoke|multi-root-snowflake|star>. <one-sentence why>."`

### RULE 0c — AUTHOR ONE MEASURE PER SPEC KPI (the single most-skipped rule)

If a "Client KPI requirements" block is present in the user message (look for
lines starting with `- [<id>] <category> · <name>`), you MUST author one
`measures[]` entry per actionable KPI. This is NOT optional. Phase 3.85
will deterministically add boilerplate row counts on top of your measures;
your job is to add the BUSINESS measures from the spec.

**Authoring rules:**

  (a) For each KPI line `- [<id>] <category> · <name> (<unit>) — formula: <f> | src: <s>`,
      emit a measure with:
        - `name`: `<category> <name>`  (e.g. "Centerline Health Check",
          "BDE # of breakdowns", "Defects Handling Defects Found").
          Avoid colliding with any column name on the host table.
        - `host_table`: the fact table whose columns the formula needs.
          For most Power Apps-SQL KPIs that's `dbo.vw_input_combined` (the
          values fact) joined via `vw_param_mapping_combined` to dms /
          sub_dms / parameters. For defect counts, `dbo.vw_defects_input_combined`.
          For downtime, `dbo.vw_downtime_input_combined`.
        - `intent`: paste the spec's formula text verbatim. Phase 6 reads
          this to generate DAX. e.g. `"IF HC = Safety HC THEN Weighted Score"`
          or `"# of occurance = IF sub category = BDE"`.
        - `template_id`: `null` (Phase 6 picks the template).
        - `format_string`: `"#,##0"` for Count, `"0.0%"` for %, else null.
        - `display_folder`: `"KPIs/<category>"` (e.g. `"KPIs/Centerline"`).

  (b) For KPIs whose `src` includes ONLY external systems your model
      doesn't have (D365, SPC) — i.e. no Power Apps-SQL source — do NOT
      emit a measure. Instead add the KPI id to `notes[]` as
      `"BLOCKED: [<id>] <name> — needs <src>"`. The pipeline's Phase 6.6
      coverage gate counts these as BLOCKED, not MISSING.

  (c) For each KPI you DO author, do NOT generate a separate boilerplate
      measure with the same intent (Phase 3.85 handles those). E.g. if
      the spec has KPI `[19] BDE · # of breakdowns`, emit ONE measure
      called `BDE # of breakdowns` — don't also emit `Total Breakdowns`
      / `Count of Breakdowns` etc.

  (d) End-of-plan coverage check: in `notes[]`, add a line:
      `"KPI authoring: <built>/<total> spec KPIs emitted as measures · <blocked> marked BLOCKED."`
      Coverage target is >= 60% of (total - blocked). Below that you've
      under-authored — review your skipped lines and the dms/sub_dms/parameter
      table to see whether a column-filtered measure could satisfy the
      KPI. The defaults are too conservative.

**Pitfall: do not skip a KPI just because the formula looks complex.**
Phase 6 (deterministic DAX) is good at translating formulas; your job is
just to declare the measure with the right host_table + intent text. Phase
3.85 only adds generic Total/Active/Inactive counts — IT DOES NOT EMIT
SPEC KPIS. If you skip them, the model ships at 0% spec coverage. That's
been the #1 failure mode in past builds.

### RULE 1 — HUB-AND-SPOKE (only applies when Rule 0b classified the domain as hub-and-spoke)

If Rule 0b detected a hub-and-spoke domain, apply this rule. Otherwise
ignore it and use snowflake/star semantics.

1. SCHEMA STYLE: HUB-AND-SPOKE AROUND THE CENTRAL BUSINESS ENTITY.

   The model has ONE designated **hub dim** — the central business entity that every fact references. In insurance, the hub is `dbo.golddimpolicy` (one row per policy, natural key = `policyNumber`). In other domains it might be `dimcustomer`, `dimorder`, `dimemployee`. Identify the hub from the schema + requirements. There is exactly one hub; multiple competing hubs are not allowed.

   **Mandatory architecture:**

   - **Every fact** in `tables[]` MUST have an active relationship to the hub via the hub's natural key (e.g. every fact joins `golddimpolicy` via `policyNumber`). A fact that cannot reach the hub does not belong in the model and goes to `excluded_tables`.
   - **Multiple facts at different grains are EXPECTED.** Premium + Claims + Billing + Commission all coexist because each joins to the hub independently. Include every business fact that has a non-null hub-key column.
   - **Lookup dims** (state, lob, agent, agency, company, adjuster, etc.) attach to the hub via direct FK on the hub (`golddimpolicy.stateCode → golddimstate.stateCode`, `golddimpolicy.agentCode → golddimagent.agentCode`, ...). These are snowflake spokes radiating from the hub.
   - **Direct fact → lookup-dim relationships** are permitted ONLY when the fact column carries semantic meaning DISTINCT from the hub's same-name column. Example: `factclaim.lossState` (where the loss happened) is distinct from `golddimpolicy.stateCode` (where the policy was written) — direct FK allowed and documented. But `factpolicytransaction.stateCodeDep` is just a copy of policy state — DON'T duplicate the relationship; route it through the hub.
   - **DimDate** (calculated) is the only table that joins to facts directly. Each fact wires its primary business date as an active relationship to DimDate; secondary date columns wire inactive relationships for USERELATIONSHIP usage.

   **Decision procedure (apply in order):**

   STEP A — Identify the hub. The hub is the dim that:
     (a) Carries the broadest set of FK columns to lookup dims (state, lob, company, agent, ...), AND
     (b) Has a natural key column referenced by every (or nearly every) candidate fact.
     For insurance warehouses with `dbo.golddimpolicy`, this is unambiguous.

   STEP B — For each candidate fact, verify it has a non-null FK to the hub's natural key. Profile check: if hub-key is 100% null on the fact, exclude it with reason "no hub join".

   STEP C — Emit `fact → hub` relationships for every surviving fact (one per fact, on the hub's natural key).

   STEP D — Emit `hub → lookup_dim` snowflake spokes for every lookup dim with an FK column on the hub. The hub joins TO lookups on the lookup's natural key (e.g. `golddimpolicy.stateCode → golddimstate.stateCode`).

   STEP E — Emit `fact → DimDate` relationships for each fact's business date(s). Primary date active, others inactive.

   STEP F — Emit direct `fact → lookup_dim` relationships ONLY when the fact column is semantically distinct from the hub's equivalent (document the rationale, e.g. "lossState ≠ policyState"). Default is route through hub.

   STEP G — Document the hub choice in `notes`: `"hub: dbo.golddimpolicy (policyNumber) — N facts join here"`.

   **Anti-patterns that remain hard errors:**
   - A fact in `tables[]` with no relationship to the hub.
   - More than one hub (every dim with FK columns claims to be hub) — pick one.
   - dim → dim chains beyond the hub-spoke pattern (no spoke → spoke).
   - Ambiguous double paths (fact reaches the same dim via hub AND directly for the SAME semantic column — pick one).
   - Bidirectional cross-filter on hub → spoke edges; stays singleDirection.
   - Pure attribute columns as relationship targets (`golddimpolicy.policyTier` is a column, never a relationship target).

   Optimize for: every fact reachable from every in-plan dim via the hub; no ambiguous paths; DimDate shared across facts via inactive relationships when multiple date columns matter.
2. ONE-SIDE OWNERSHIP: a dim is the valid "one" side ONLY if the dim's name contains the column's base name (e.g. golddimpolicy owns policyNumber; dimrisk does NOT). If a fact column matches multiple dims, pick the owning dim, list the others under excluded_relationships.
3. UNIQUE TO-SIDE: never set a relationship's to_column to a column with duplicates in the data profile. Check the profile's "X distinct / Y rows" — they must match. Likewise, set `is_key: true` ONLY on a column that is UNIQUE and NON-NULL in the data (a dimension's primary/surrogate key) — NEVER on a fact-table id column (e.g. review_id), which repeats and/or is blank; a TMDL key must be unique+non-null or refresh fails.
4. EACH FACT COLUMN appears at most ONCE in relationships as from_column. No second active relationship from the same fact column.
5. NEVER INVENT COLUMNS: every source_column must appear in the source-schema for that table. If you need agentKey but the schema only has agentCode, use agentCode.
6. AUDIT COLUMNS hidden: loadTimestamp, sourceSystem, pipelineRunId, attribute1..N, MSSQL_System_Uniquifier_*, recordSource, recordVersion → set is_hidden: true.
7. MEASURE NAME ≠ COLUMN NAME on the same table — CASE-INSENSITIVE. Power BI rejects `measure Taxes` when `column taxes` exists; the engine treats the names as identical. Prefix every measure name with an aggregation verb ("Total ", "Average ", "Count of ", "Sum of ") so it can never collide with a raw source column. Examples: a column named `taxes` → measure should be "Total Taxes" or "Tax Amount", not "Taxes". A column `claimCount` → measure "Total Claim Count", not "Claim Count". This rule is enforced by the validator; violations block the build.
8. SCOPE — INCLUDE-BY-DEFAULT, HUB-JOIN CONSTRAINT.

   (Note: views are already covered by Rule 0a above — this section is for base tables only.)

   **FACTS:** Include every business fact that joins to the hub. A fact BELONGS in `tables[]` if ALL of:
   (a) It has an FK column matching the hub's natural key (e.g. `policyNumber`).
   (b) That FK column is not 100% null in the data profile.
   (c) It is a business fact (not system telemetry).

   Past plans were too conservative — they only picked the 5-6 facts named directly by KPIs and skipped 10+ facts that the warehouse clearly intends to be analytical (billing, refunds, claim financials, commission checks, etc.). That is wrong. The default for any business fact that joins to the hub is INCLUDE.

   A fact goes to `excluded_tables` ONLY when one of:
   - No column references the hub's natural key (cannot join).
   - The hub-key column is 100% null in the profile (no rows can join).
   - Non-business / system telemetry (`queryinsights.*`, `dm_*`, MSSQL system views).
   - Duplicate of another included fact (same data, different name).

   "I don't see a KPI for this fact" is NOT a valid fact-exclusion reason. Include it; the user adds measures later via refinement chat.

   **DIMS:** Include by default. A dim BELONGS in `tables[]` if ANY of:
   (a) A measure in the plan references one of its columns.
   (b) Any column on the dim is named anywhere in the requirements text (KPI name, filter list, business-rule formula, glossary). Match loosely.
   (c) The hub dim has an FK column to it (it's a snowflake spoke target).
   (d) Any slicer/filter in the wireframe needs it (e.g. State slicer → `golddimstate`).
   (e) It is the hub itself (e.g. `dbo.golddimpolicy`). Always include the hub.
   (f) Any time-intelligence measure uses dates → include a Date calculated table with kind=calculated, an is_key dateTime column, and a column-level dax_expression.

   A dim goes to `excluded_tables` ONLY when explicitly:
   - Non-business / system telemetry.
   - Fails uniqueness in the profile (distinct < rows). Document the distinct/rows numbers.
   - No in-plan fact and no hub-spoke path reaches it.
   - Duplicate of another included dim.

   "I don't see a KPI for this dim" is NOT a valid dim-exclusion reason. Include it.
9. VALUES MATCH REAL DATA: when an intent involves a filter, the intent text must reference values that actually appear in the data profile samples (e.g. policyStatus = "Active", not invented "Inforce").
10. Output ONLY the JSON document. No prose. No markdown.
11. KEEP IT COMPACT: descriptions/rationale must be ≤ 60 chars each. Skip them entirely when obvious. Verbose descriptions blow up the output and risk truncation.
12. Skip the `description` field on columns unless it adds real information. Most columns don't need one.

13. **SYNONYMS — populate for Q&A and Copilot readiness**. For columns and measures that an analyst would query by name, add 2-4 business-friendly aliases to the `synonyms` field. Aliases should be lowercased natural-language tokens. Examples:
    - Column `writtenPremium` → synonyms ["written premium", "gross premium", "premium written", "GWP"]
    - Column `agencyBrokerCode` → synonyms ["agency code", "broker code", "agency", "broker"]
    - Measure "Gross Written Premium" → synonyms ["GWP", "premium volume", "sales"]
    - Measure "Total Inforce Policies" → synonyms ["active policies", "policy count", "policies in force"]
    Skip synonyms for technical / hidden columns (lineageTag, audit cols, surrogate keys). Skip when no obvious alias exists.

14. **EXPLICIT MEASURES — hide raw numeric columns that back measures**. When a column is the source of a `SUM(...)` / `AVERAGE(...)` / etc. measure, set `is_hidden: true` and `summarize_by: "none"` on that column. The pipeline auto-applies this post-validation as a safety net, but emit it explicitly when you can — it's a clearer plan. Examples:
    - `writtenPremium` (used by `Gross Written Premium` measure) → `is_hidden: true, summarize_by: "none"`
    - `earnedPremium` (used by `Earned Premium` measure) → `is_hidden: true, summarize_by: "none"`
    The end-user dragging "writtenPremium" into a visual would otherwise get an implicit SUM and bypass any calculated business logic the named measure encapsulates.

15. **NO ORPHAN DIMENSIONS — connect every dimension**. Every dimension table in `tables[]` MUST participate in at least one active relationship connecting it to a fact table or to the central hub dim. Disconnected dimensions cannot filter measures, causing visuals (e.g. breakdown charts) to display flat, identical numbers across all categories.

16. **PERCENTAGES & RATIOS — consistent 0.0 to 1.0 scale and 0.0% formatString**. For all percentage, rate, ratio, share, and margin measures, ALWAYS assign `format_string: "0.0%"`. The underlying DAX must evaluate to a 0.0–1.0 fraction (never multiply by 100 in DAX, never SUM percentages across rows; use AVERAGE or DIVIDE). Sibling metrics across categories must all share the same percentage format.
"""


def _strip_json_comments(text: str) -> str:
    out: list[str] = []
    i = 0
    in_string = False
    string_char = ""
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            out.append(ch)
            if ch == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if ch == string_char:
                in_string = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_string = True
            string_char = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            end = text.find("\n", i)
            if end < 0:
                break
            i = end
            continue
        if ch == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            if end < 0:
                break
            i = end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    import re as _re

    prev = None
    while prev != text:
        prev = text
        text = _re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def _save_raw_for_debug(raw: str, attempt: int, reason: str) -> str:
    import os
    import tempfile
    import time

    debug_dir = os.path.join(tempfile.gettempdir(), "pbi_plan_debug")
    os.makedirs(debug_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(debug_dir, f"plan_raw_{ts}_attempt{attempt}_{reason}.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
    except Exception:
        pass
    return path


def _try_parse_robust(raw: str) -> tuple[dict | None, str]:
    import json as _json

    last_err = ""
    try:
        return _json.loads(raw), ""
    except Exception as e:
        last_err = f"plain: {e}"
    try:
        return _json.loads(_strip_json_comments(raw)), ""
    except Exception as e:
        last_err = f"comments: {e}"
    try:
        return _json.loads(_strip_trailing_commas(_strip_json_comments(raw))), ""
    except Exception as e:
        last_err = f"comments+commas: {e}"
    try:
        first = raw.find("{")
        if first < 0:
            return None, f"no `{{` found in output (len={len(raw)})"
        depth = 0
        in_string = False
        string_char = ""
        end = -1
        i = first
        while i < len(raw):
            ch = raw[i]
            if in_string:
                if ch == "\\":
                    i += 2
                    continue
                if ch == string_char:
                    in_string = False
                i += 1
                continue
            if ch in ('"', "'"):
                in_string = True
                string_char = ch
                i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
            i += 1
        if end > first:
            sliced = raw[first : end + 1]
            sliced = _strip_trailing_commas(_strip_json_comments(sliced))
            return _json.loads(sliced), ""
    except Exception as e:
        last_err = f"brace-scan: {e}"
    return None, last_err


def _call_llm(
    system_prompt: str,
    user_msg: str,
    max_tokens: int = 16000,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[str, UsageInfo | None]:
    return stream_collect(
        system_prompt,
        user_msg,
        agent="big_task",
        max_tokens=max_tokens,
        progress_callback=progress_callback,
        progress_label="plan JSON",
    )


def generate_plan(
    schema_text: str,
    profile_context: str,
    requirements: str,
    model_name: str,
    server_fqdn: str,
    db_name: str,
    schema_tables: dict[str, list[tuple[str, str]]],
    profile: dict | None = None,
    rules_context: str = "",
    template_library: str = "",
    project_id: int | None = None,
    max_retries: int = 3,
    progress_callback: Callable[[str], None] | None = None,
    kpi_requirements_block: str = "",
    reference_model_block: str = "",
) -> tuple[SchemaPlan, list, str]:
    is_local_small_ctx = get_provider("semantic_model") == "ollama"
    eff_profile = profile_context
    eff_templates = template_library
    if is_local_small_ctx:
        if len(eff_profile) > 6000:
            eff_profile = (
                eff_profile[:6000]
                + "\n(... profile truncated for local-model context budget ...)"
            )
        if eff_templates:
            template_names = re.findall(r"^###\s+(.+?)$", eff_templates, re.MULTILINE)
            if template_names:
                eff_templates = (
                    "## Available measure templates (set template_id to one of these names)\n"
                    + "\n".join(f"- {n}" for n in template_names)
                )

    kpi_block = (
        f"\n## Explicit KPI requirements (treat each as a target measure)\n{kpi_requirements_block}\n"
        if kpi_requirements_block
        else ""
    )
    ref_block = (
        f"\n## Reference model to imitate\n{reference_model_block}\n"
        if reference_model_block
        else ""
    )

    cached_system = f"""{PLAN_SYSTEM_PROMPT}

## Database schema (source of truth — every source_column MUST exist here)
{schema_text}

{eff_profile}

{rules_context}

{eff_templates}
{kpi_block}{ref_block}"""

    extra_directives = []
    if kpi_requirements_block:
        extra_directives.append(
            "**KPI requirements are provided.** Each row in `## Explicit KPI "
            "requirements` is a target measure the model MUST support. For each "
            "KPI: (a) include the source tables it references in `tables[]`, "
            "(b) emit a `MeasureSpec` whose `name` matches the KPI name and "
            "whose `intent` paraphrases the formula. Set `template_id` when a "
            "template matches; leave null otherwise. Do not silently drop KPIs — "
            "if you can't host one (e.g. its source table doesn't exist in the "
            "schema), add it to `notes[]` as `BLOCKED: [<id>] <name> — needs <src>` "
            "(matches Rule 0c · parsed by Phase 6.6 coverage gate)."
        )
    if reference_model_block:
        extra_directives.append(
            "**A reference model is provided.** Imitate its structure where it "
            "fits this schema: same table classifications (fact vs dim vs "
            "calculated), same relationship topology, same measure names where "
            "the requirement maps to one of its measures. Replicate any "
            "`detected_patterns` the reference flags (shift-context tables, "
            "bridge tables, role-playing dates) when the new schema supports "
            "them. Do NOT copy tables that aren't in this build's source schema."
        )
    directives_block = (
        "\n## Build directives\n" + "\n\n".join(extra_directives) + "\n"
        if extra_directives
        else ""
    )

    base_msg = f"""## Model name
{model_name}

## Data source
server_fqdn: {server_fqdn}
db_name: {db_name}

## Confirmed dashboard requirements
{requirements or "(no specific requirements — design a clean star schema covering the visible business domains)"}
{directives_block}
Produce the JSON plan now. ONLY JSON. No prose, no markdown fences."""

    last_raw = ""
    last_errors: list = []
    plan: SchemaPlan | None = None
    error_feedback = ""

    BUDGET = 32000

    for attempt in range(max_retries):
        prompt = base_msg
        if error_feedback:
            prompt += f"\n\n## Previous attempt had these validation errors — FIX them:\n{error_feedback}\n\nProduce a corrected JSON plan now."

        t0 = time.time()
        if progress_callback:
            progress_callback(
                f"Phase 3: LLM call (attempt {attempt + 1}/{max_retries}) — "
                f"streaming plan JSON from {get_model_name('semantic_model')}..."
            )
        raw, usage = _call_llm(
            cached_system,
            prompt,
            max_tokens=BUDGET,
            progress_callback=progress_callback,
        )
        latency_ms = int((time.time() - t0) * 1000)
        if progress_callback:
            progress_callback(
                f"Phase 3: LLM finished in {latency_ms / 1000:.1f}s — parsing {len(raw):,} chars of JSON..."
            )
        last_raw = raw
        logger.info(
            f"Plan generator attempt {attempt + 1}/{max_retries}: {latency_ms}ms"
        )

        def _save_usage(status: str) -> None:
            log_usage(project_id, "plan_generator", usage, latency_ms, status)

        cleaned = _strip_json_fences(raw)
        data, parse_err = _try_parse_robust(cleaned)
        if data is None:
            truncated = not cleaned.rstrip().endswith("}")
            kind = "truncated" if truncated else "parse-fail"
            debug_path = _save_raw_for_debug(raw, attempt + 1, kind)
            if truncated:
                error_feedback = (
                    f"Output truncated at max_tokens={BUDGET} (ends with "
                    f"...{cleaned[-120:]!r}). Return a COMPLETE but more COMPACT "
                    f"JSON plan that fits the budget — shorter descriptions, no "
                    f"prose. Final character must be `}}`."
                )
            else:
                error_feedback = (
                    f"JSON parse failed across all strategies: {parse_err}\n"
                    f"Output length was {len(raw)} chars. Return ONE complete JSON "
                    f"object with no markdown fences, no prose, no comments. "
                    f"Final character must be `}}`."
                )
            logger.warning(
                f"Plan attempt {attempt + 1}: {kind} — saved to {debug_path}"
            )
            _save_usage("error")
            continue

        try:
            plan = dict_to_plan(data)
            plan.model_name = plan.model_name or model_name
            plan.server_fqdn = plan.server_fqdn or server_fqdn
            plan.db_name = plan.db_name or db_name
        except Exception as e:
            debug_path = _save_raw_for_debug(raw, attempt + 1, "shape-error")
            error_feedback = (
                f"Plan shape error: {e}\n"
                f"The JSON parsed but didn't match the expected schema. "
                f"Raw saved to: {debug_path}"
            )
            logger.warning(f"Plan attempt {attempt + 1}: shape error — {e}")
            _save_usage("error")
            continue

        try:
            from .repair import auto_repair_plan as _auto_repair

            plan, _repair_actions = _auto_repair(plan, schema_tables)
            if _repair_actions:
                logger.info(
                    f"Plan attempt {attempt + 1}: pre-validate auto-repair "
                    f"applied {len(_repair_actions)} action(s)"
                )
        except Exception as _e:
            logger.warning(f"Plan attempt {attempt + 1}: auto-repair failed: {_e}")

        errors = validate_plan(plan, schema_tables, profile=profile)
        blocking = [e for e in errors if e.severity == "error"]
        if not blocking:
            _save_usage("success")
            return plan, errors, raw

        last_errors = errors
        error_feedback = format_errors(blocking)
        _save_usage("error")
        debug_path = _save_raw_for_debug(raw, attempt + 1, "validate-fail")
        try:
            errs_path = debug_path.replace(".txt", "_errors.txt")
            with open(errs_path, "w", encoding="utf-8") as _f:
                _f.write(f"Validation errors for attempt {attempt + 1}\n")
                _f.write(
                    f"({len(blocking)} blocking error(s), "
                    f"{len(errors) - len(blocking)} warning(s))\n\n"
                )
                _f.write(error_feedback)
        except Exception:
            pass
        logger.warning(
            f"Plan attempt {attempt + 1}: {len(blocking)} validation error(s) — "
            f"saved to {debug_path}"
        )

    if plan is None:
        raise RuntimeError(
            f"Plan generator exhausted {max_retries} attempts without "
            f"producing parseable JSON. Raw outputs saved under "
            f"%TEMP%\\pbi_plan_debug\\ — inspect to see what the LLM "
            f"produced. Last raw output (first 500 chars): {last_raw[:500]}"
        )
    return plan, last_errors, last_raw
