# Hacker News — Show HN Submission

**Site**: [news.ycombinator.com/submit](https://news.ycombinator.com/submit)

**Title**:
> Show HN: FabricAgent – Open-source AI terminal agent for Power BI and Microsoft Fabric

**URL**:
> https://github.com/Amrit-D-V/FabricAgent

---

### First Comment (Post immediately after submitting):

Hi HN,

I built FabricAgent to solve a persistent issue in enterprise analytics: LLMs are great at conversational queries, but notoriously unreliable at data modeling in Microsoft Power BI and Microsoft Fabric.

If you ask ChatGPT or Claude to write DAX or generate a tabular model, it frequently:
1. Hallucinates functions that don't exist in the Tabular Object Model (TOM).
2. Generates division without `DIVIDE()`, which throws runtime errors on visual cards when denominator evaluation is 0.
3. Suggests invalid relationships or bidirectional filters that cause ambiguous path errors.

To address this, FabricAgent separates the problem into a "Two-Brain" architecture:
- **Planner (LLM)**: Analyzes unstructured requirements docs, CSVs, or warehouse schemas and outputs a structured schema plan (identifying dimensions, facts, and business metrics).
- **Compiler & Gate (Deterministic Python)**: Compiles the plan into physical TMDL and PBIP files. It evaluates the model against 60+ Tabular Editor Best Practice Analyzer (BPA) rules and repairs syntax errors (via our TMDL Doctor engine) before anything touches disk.

It comes with an interactive terminal assistant (`fabricagent tui`), automated one-liner installers (`./install.sh` / `install.bat`), and supports $0 free tiers: Google Gemini free tier, local offline Ollama (e.g. `qwen2.5-coder`), or a built-in AdventureWorks mock dataset that requires zero cloud connections.

The project is 100% open-source under the MIT license with 1,400+ unit and integration tests: https://github.com/Amrit-D-V/FabricAgent

I would appreciate any feedback on the architecture, compiler pipeline, or terminal interface!
