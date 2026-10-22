---
name: semantic-model-authoring
description: Microsoft's official guidance for authoring Power BI semantic models — star schema design, TMDL, DAX authoring and performance, Direct Lake, PBIP layout, deployment and refresh. Load BEFORE creating or editing any semantic model, measure, or relationship.
---

# Semantic Model Authoring (Microsoft guidance, adapted for FabricBI)

Vendored from [microsoft/skills-for-fabric](https://github.com/microsoft/skills-for-fabric)
(MIT — see `skills/UPSTREAM-LICENSE.txt`), with the tool workflow re-mapped to
FabricBI's toolset. The reference files under `references/` are upstream originals.

## When to use

Any request involving: creating a semantic model, adding/editing tables,
columns, relationships or measures, DAX development or optimization,
Direct Lake, model deployment/refresh, best-practice review, or preparing a
model for AI/Copilot consumption.

## Tool mapping (FabricBI)

Upstream assumes Microsoft's CLIs (`az rest`, `powerbi-modeling-mcp`). In
FabricBI, use:

| Task | FabricBI tool |
|---|---|
| Inspect / edit the **live connected model** (preferred, Tier 1) | `powerbi_modeling` MCP: `database_operations`, `table_operations`, `column_operations`, `measure_operations`, `relationship_operations`, `model_operations`, `dax_query_operations` |
| List all semantic models in the workspace | `database_operations` `operation=List` — **a semantic model is a "database" to the MCP server**; `table_operations` lists tables *inside* the connected model only |
| Run/validate DAX | `dax_query_operations`, `pbi_dax_analyze` |
| Best-practice audit | `pbi_model_bpa`, `pbi_model_quality`, `pbi_model_test` |
| TMDL on-disk work (PBIP projects) | `pbi_tmdl_doctor`, `pbi_export_model` |
| Fetch a service model down to local PBIP | `pbi_report_fetch` |
| Docs / lineage | `pbi_generate_docs`, `pbi_lineage_map`, `pbi_impact_analysis` |

`pbi_*` tools operate on **local files** under the projects directory — they do
not edit the Fabric service. Live-model changes go through the
`powerbi_modeling` MCP tools.

## Critical rules (upstream, keep verbatim)

- **Prefer star schema** over snowflake or single flat tables.
- **Prefer TMDL** (text) over TMSL when authoring on disk.
- Build objects in dependency order: partitions → columns → relationships →
  measures.
- **Deploy exactly once — deployment is NOT idempotent.** A retried deploy
  creates duplicates.
- When the MCP connection is live, inspect via MCP operations — do not read
  TMDL files in parallel (they can be stale).
- On refresh credential errors: stop immediately and have the user configure
  credentials in the Service portal; do not retry.
- Calculated columns vs. measures, naming, and cardinality rules: see
  `modeling-guidelines` and `naming-conventions` before building.

## References (load on demand)

Load with `load_skill("semantic-model-authoring/<name>")`:

- `modeling-guidelines` — star schema, cardinality, calculated columns vs measures
- `naming-conventions` — object naming standards
- `tmdl-guidelines` — TMDL syntax and structure
- `pbip` — PBIP folder scaffolding and layout
- `dax-guidelines` — DAX syntax and best practices
- `dax-perf-decision-guide` — optimization decision tree
- `dax-perf-patterns` — optimization pattern catalog
- `direct-lake-guidelines` — Direct Lake constraints (OneLake sources only)
- `metadata-discovery` — DAX INFO function patterns (read-only inspection)
- `semantic-model-ai-readiness` — Copilot/AI-readiness checklist
- `semantic-model-rest-api` — Fabric REST endpoints (background; FabricBI calls these through its own tools)
- `connection-binding` — bind/unbind models to Fabric connections (background)
