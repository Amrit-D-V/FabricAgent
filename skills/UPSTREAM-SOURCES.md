# Where the vendored skills came from

Recorded so a refresh can be diffed against upstream, and so the licence on each
skill is unambiguous. Previously nothing here named a revision, which meant the
vendored content could not be compared to upstream at all (HANDOVER open item).

| Skill | Upstream | Revision | Licence |
|---|---|---|---|
| `semantic-model-authoring/` | [microsoft/skills-for-fabric](https://github.com/microsoft/skills-for-fabric) | `b8d541c77ae6b8bd65da0bf9a7746750e11c32f9` (2026-08-20) — **verified identical** | MIT (`UPSTREAM-LICENSE.txt`) |
| `powerbi-report-authoring/` | [microsoft/skills-for-fabric](https://github.com/microsoft/skills-for-fabric) | `b8d541c77ae6b8bd65da0bf9a7746750e11c32f9` (2026-08-20) — **verified identical** | MIT (`UPSTREAM-LICENSE.txt`) |
| `paginated-report-authoring/` | [data-goblin/power-bi-agentic-development](https://github.com/data-goblin/power-bi-agentic-development) | `f8495e76793069b887a4d8db956ed6ac579d03e6` (2026-08-08) | **GPL-3.0** |

## The two Microsoft skills — checked 2026-08-21, no refresh needed

Diffed file-by-file against upstream `b8d541c7`: **0 of 12** reference files in
`semantic-model-authoring` and **0 of 21** in `powerbi-report-authoring` differ
in content. Our copies are current.

Diff with `--strip-trailing-cr`. Without it every file appears to differ — the
repo checks out CRLF here and upstream is LF, which reads as total drift and is
nothing at all.

Two upstream reference files are **deliberately not vendored**:
`powerbi-desktop.md` and `powerbi-report-author-cli.md`. Both instruct the agent
to drive tools FabricBI's agent is denied, so vendoring them would only produce
instructions it cannot follow. Each `SKILL.md` says so already.

Upstream now carries 26 skills, up from the 2 taken. The rest were reviewed on
2026-08-21 and rejected on the same test: the Spark skills need Spark (FabricBI has
none), and every other candidate — including the newer `powerbi-report-design`,
`powerbi-report-management`, `powerbi-report-planning`, `sqldb-cli`, `sqldw-cli`
— is CLI-driven (`powerbi-report-management` is explicitly "via `az rest` CLI").
The `-cli` suffix upstream is a reliable marker: those cannot run here.

## paginated-report-authoring — read this before redistributing

The upstream project is **GPL-3.0**, which is copyleft. `SKILL.md` here is not a
copy: the structural rules (element order, Tablix leaf-count and cell
invariants, dimension units, reference integrity) are facts about the RDL
format, and the prose, tables and workflow were written against FabricBI's own
tools because upstream targets a CLI FabricBI's agent is not permitted to run.

The same applies to the checks added to `report/rdl_builder.py:validate_rdl`,
which are attributed in a comment there. They were re-implemented server-side —
upstream's run as a script the agent is instructed to remember to run, ours run
inside `pbi_paginated_set` and refuse the write, so an invalid `.rdl` cannot
reach disk.

That distinction reduces exposure but does not remove the question. FabricBI is
open-source, so ensure the GPL-3.0 licensing terms are respected. The two Microsoft
skills are MIT and carry no such obligation.

## Refreshing

Upstream ships weekly and versions 26.26–26.38 are documented breaking
transitions. Pin a SHA, diff deliberately, and update the table above. Do not
`git pull` a vendored skill in place.
