---
name: powerbi-report-authoring
description: Microsoft's official guidance for authoring Power BI reports in PBIR/PBIP format — pages, visuals, filters, slicers, theming, conditional formatting, and the validate → reload → review loop. Load BEFORE creating or editing report pages or visuals.
---

# Power BI Report Authoring (Microsoft guidance, adapted for FabricBI)

Vendored from [microsoft/skills-for-fabric](https://github.com/microsoft/skills-for-fabric)
(MIT — see `skills/UPSTREAM-LICENSE.txt`), with the tool workflow re-mapped to
FabricBI's toolset. The reference files under `references/` are upstream originals.

## When to use

Any request that creates or edits report definition files: pages, visuals,
field bindings, filters, slicers, themes, formatting, drillthrough/tooltips —
anything under a `*.Report/definition/` tree.

## Tool mapping (FabricBI)

Upstream assumes Microsoft's `powerbi-report-author` / `powerbi-desktop` CLIs.
In FabricBI, the same loop is:

| Upstream step | FabricBI tool |
|---|---|
| Scaffold report project | `pbi_report_create` (wires the live connection) |
| Add pages / visuals | `pbi_report_add_page`, `pbi_visual_add`, `pbi_visual_add_bound` |
| Bind fields / format | `pbi_visual_bind`, `pbi_visual_format`, `pbi_visual_update` |
| Validate (offline preflight) | `pbi_report_validate`, `pbi_report_health_check` |
| Design/layout audit | `pbi_dashboard_audit`, `pbi_design_judge`, `pbi_layout_compute` |
| Desktop status / reload | `pbi_desktop_status`, `pbi_desktop_sync`, `pbi_report_reload` |
| Legacy (non-PBIR) reports | `pbi_report_inspect` — PBIR tools cannot read PBIR-Legacy |

All `pbi_*` tools are **local-file operations** on the PBIP project on disk.

**The loop (keep it):** edit → `pbi_report_validate` → fix errors →
`pbi_desktop_sync` → review (see `screenshot-review`). Validate after every
logical batch of changes; do not report completion until validation is clean.
`pbi_desktop_sync` (not `pbi_report_reload`) is the step that actually shows
the user the result — it reloads Desktop AND captures a screenshot of every
page. Use `pbi_report_reload` only for a bare reload with no visual changes
worth screenshotting (e.g. after a pure data/model change).

## Critical rules (upstream, keep verbatim)

- **Preserve existing `$schema` versions** — never bump schema versions
  independently of the content changes that require them.
- **Always use modern visual types**: `cardVisual`, `tableEx`, `pivotTable`,
  `azureMap` — never their legacy equivalents.
- **Generate unique IDs**; never reuse names across visuals or filters.
- **Visual Quality & Executive Design Standards**:
  - **Bar Charts**: Always apply Top-N filter (e.g. Top 7) to avoid inner scrollbars when categories exceed 6.
  - **Time Series / Area Charts**: Always exclude `(Blank)` dates via non-blank filter; use semi-transparent gradient area fills.
  - **Donut / Pie Charts**: Always use outside data labels with leader lines and bottom legend; never place inside labels on narrow slices.
  - **KPI Cards**: Ensure numbers are formatted ($40M, 85%, 1,240) with proper contrast and Card 2.0 side/top accent styling. To set/fix the big number's color on a `cardVisual`, use `pbi_visual_format`'s `data_label_color` — it writes the callout/value text object directly; there is no separate value-color param, and this is not a PBIR limitation.
  - **Page Anatomy**: Structure pages with a top Header Masthead -> Filter Strip -> KPI Row -> Analytics Matrix.
- Do not guess visual roles, formatting object names, or enum values from
  memory — check the reference for the visual type first; wrong names fail
  silently.
- For multi-visual color consistency, maintain an explicit color mapping and
  apply it per-visual — theme `dataColors` alone drift by index.
- Dark themes need the full checklist: style presets, selector-based fills,
  background/text hierarchy, contrast audit (see `theming` / `re-theming`).

## References (load on demand)

Load with `load_skill("powerbi-report-authoring/<name>")`:

- `authoring` — core PBIR mechanics and anti-patterns table
- `card`, `cartesian`, `table`, `map`, `image`, `shape`, `textbox` — per-visual-type roles and properties
- `slicers` — slicer types, modes, sync groups
- `filters`, `filter-pane` — filter configuration at visual/page/report level
- `conditional-formatting` — rules, gradients, field-value formatting
- `expressions` — literal/dynamic expression syntax in PBIR JSON
- `formatting`, `formatting-overview`, `page-formatting` — objects/properties/selectors
- `theming`, `re-theming`, `color-strategy` — theme JSON, palette strategy
- `screenshot-review` — how to judge rendered output before declaring done
- `version-control` — PBIP-in-git practices

(Upstream's `powerbi-report-author-cli` and `powerbi-desktop` CLI docs are
intentionally not vendored — FabricBI ships its own equivalents, mapped above.)
