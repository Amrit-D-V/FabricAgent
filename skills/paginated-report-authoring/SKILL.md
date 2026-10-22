---
name: paginated-report-authoring
description: How to author and edit Power BI paginated reports (.rdl) without breaking them — element order, Tablix count invariants, data providers, and the errors Report Builder reports as "index out of bounds". Load BEFORE creating or editing any .rdl with pbi_paginated_create/get/set.
---

# Paginated Report (RDL) Authoring — adapted for FabricBI

Structural rules derived from
[data-goblin/power-bi-agentic-development](https://github.com/data-goblin/power-bi-agentic-development)
(GPL-3.0, Kurt Buhler / Data Goblins), commit `f8495e76`, re-mapped to FabricBI's
tools. Upstream targets a CLI workflow the agent is told to run; FabricBI enforces
the same invariants server-side, so this skill is about **authoring correctly**,
not about remembering to validate.

## When to use

Any request mentioning a paginated report, RDL, `.rdl`, Report Builder, SSRS, a
print-perfect or multi-page document, an invoice, a statement, or a regulatory
filing. For interactive on-screen dashboards, use PBIR (`pbi_report_create`)
instead — paginated is for documents that print.

## FabricBI's tools, and what each guarantees

| Step | Tool | Note |
|---|---|---|
| Scaffold | `pbi_paginated_create` | Generates a valid RDL. **Start here** — never hand-type an RDL from scratch. |
| Read | `pbi_paginated_get` | Returns the whole XML. |
| Write | `pbi_paginated_set` | **Validates first and refuses to write if invalid.** |
| Check | `pbi_paginated_validate` | Same checks, without writing. |
| Locate | `pbi_paginated_list` | Returns absolute paths — pass one back verbatim. |

`pbi_paginated_set` will not save a structurally broken file. A refusal is the
tool doing its job; read the message and fix the named problem. Do **not**
respond to a refusal by recreating the report from scratch.

## The one rule that causes most breakage

**Element order is load-bearing.** The report processor — not the XSD — enforces
it, so a schema validator will not catch a reorder, and Report Builder fails to
load with no useful error. The structural sequence is:

```
DataSources -> DataSets -> ReportSections -> ReportParameters -> ReportParametersLayout
```

`ReportParameters` comes **after** `ReportSections`, which is the opposite of
what most people assume. Within a dataset, `QueryParameters` comes before
`CommandText`.

Metadata position is *not* load-bearing: `rd:ReportID`, `df:DefaultFontFamily`,
`Description`, `Variables`, `Code`, `EmbeddedImages`, `CustomProperties` vary in
real Report Builder output. Match what the file already does; do not reorder
metadata you find.

## Tablix count invariants — the "index out of bounds" crash

Violating any of these makes Report Builder throw **"index out of bounds"** with
no line number and no tablix name. FabricBI refuses the write instead, but you save
a round trip by getting them right:

1. Row-hierarchy **leaf** members == number of `<TablixRow>` in `TablixBody`
2. Column-hierarchy **leaf** members == number of `<TablixColumn>`
3. One `<TablixCell>` per column, in every row

"Leaf" matters: a `<TablixMember>` containing nested `<TablixMembers>` is a
*group*, and its leaves are the rows. A grouped table with a header, a detail
row and a total is three leaves, not one member.

Rule 3 applies only to a **flat** tablix. A matrix (a `Group` in the column
hierarchy) generates columns at render time, and a `RowSpan` greater than 1
consumes cells from following rows — both legitimately diverge, and FabricBI's
validator skips the cell check for them. Rules 1 and 2 always apply.

`ColSpan` is a render-merge hint. It does **not** change the cell count — still
one cell per column.

## Other rules FabricBI enforces on save

- **Units are mandatory** on layout dimensions: `0.5in`, `2.5cm`, `8pt`, not
  `0.5`. **`px` is not valid RDL.** The exception is the internal coordinate
  space of charts, gauges and maps, where dimensions are unitless.
- **References must resolve**: every `DataSet` names a real `DataSource`, every
  `Tablix` names a real `DataSet`, every `Embedded` image `Value` names a real
  `<EmbeddedImage>` (unless it is an `=` expression chosen at runtime).
- **`Name` uniqueness is scoped**, not global: the namespace is the innermost
  containing element that itself has a `Name`. Two tablixes may each contain a
  group called `Detail`.
- **Escape `<`, `>` and `&` inside `<CommandText>`** or the document is corrupt.
- `<DataSource Name>` and field names must be NCNames — start with a letter or
  underscore, no spaces.

## Connecting to data

| Source | `DataProvider` | Query | Watch out for |
|---|---|---|---|
| Power BI semantic model | `PBIDATASET` | DAX `EVALUATE` | Column field is `Table[Col]`, measure is `[Measure]`. **Measures return `System.String`** — wrap in `CDbl()` before arithmetic. |
| Azure SQL / SQL Server | `SQLAZURE` / `SQL` | T-SQL | On-prem needs a gateway. |
| Analysis Services | `OLEDB-MD` | MDX | Prefer a tabular DAX path. |
| Inline test data | `ENTERDATA` | embedded `<XmlData>` | All fields are `System.String`; cast for maths. |

A paginated report embeds its own data sources and datasets — the service does
not support shared `.rds`/`.rsd` files.

## Working method

1. **Scaffold with `pbi_paginated_create`.** Charts especially are too verbose to
   hand-write correctly.
2. **Iterate layout against `ENTERDATA`** before wiring a live source.
   Re-querying a model on every layout tweak is the biggest time sink. Keep the
   inline field names identical to the real query's so swapping the source
   touches only `<DataSource>` and `<DataSet><Query>`, never the layout.
3. **Swap in the real query**, keeping field names.
4. **Save with `pbi_paginated_set`** — validation runs automatically.

## What validation does NOT prove

Structure only. It never checks expressions, `DataField` names, DAX
correctness, or parameter wiring. A report with a mistyped measure or a field
that does not exist in the model passes validation and then renders blank or
errors at open time.

FabricBI currently has **no render-to-verify tool** — no publish and no export.
Until it does, confirm the data contract before calling a report done: run the
exact `EVALUATE` with `pbi_dax_query` and check the returned column names and
types match the `<Field>` definitions in the RDL. A green validator plus an
unverified query is not "done".

## If a path is refused

`ERROR — refused ... is outside the allowed project directory` is a **path**
problem, not a missing file. The report is almost certainly there. Call
`pbi_paginated_list` and pass one of the absolute paths back verbatim. Never
recreate a report in response to a path error — that has destroyed correct work.
