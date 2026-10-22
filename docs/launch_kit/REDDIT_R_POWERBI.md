# Reddit Post for r/PowerBI (150k+ Members)

**Subreddit**: [r/PowerBI](https://www.reddit.com/r/PowerBI/)  
**Flair**: `Showcase` or `Discussion`  
**Title**:
> I got tired of ChatGPT hallucinating invalid DAX, so I built FabricAgent — an open-source terminal AI agent for Power BI & Fabric (Zero-cloud spend with free Gemini or local Ollama)

---

### Body (Copy & Paste):

Hey everyone,

If you've ever tried using standard ChatGPT or Claude to write DAX or design Power BI semantic models, you've probably noticed the same headache:
- It hallucinates column names that don't exist in your warehouse.
- It forgets basic safety like `DIVIDE()` vs `/`, leading to divide-by-zero errors in visual cards.
- It suggests complex bidirectional cross-filtering that breaks star schemas and tanks model performance.

Over the last few months, I built **FabricAgent** — an autonomous, 100% open-source AI platform and terminal assistant specifically built for Power BI and Microsoft Fabric:

👉 **GitHub**: https://github.com/Amrit-D-V/FabricAgent

### How It Works (The "Two-Brain" Design):
1. **Brain 1 (AI Strategy)**: Uses LLMs (Google Gemini Free Tier, Ollama local offline, OpenAI, or Claude) to reason about your business requirements or schema and build an optimal star-schema plan.
2. **Brain 2 (Deterministic Compiler & Guardrail Gate)**: 0% hallucination. It translates that plan directly into physical **TMDL** and **PBIP** files, and runs it against **60+ Tabular Editor Best Practice Analyzer (BPA) rules** and TMDL Doctor syntax linters before you ever load it into Power BI Desktop or Fabric.

### What it can do:
- **Interactive Terminal Assistant (`fabricagent tui`)**: Chat with your models, load demo datasets (`/demo`), and stream formatted DAX with zero divide-by-zero risk.
- **TMDL & PBIP Generation**: Turn requirements docs, CSVs, or SQL warehouse schemas into deployable semantic models.
- **PBIR Report Formatter**: Auto-lays out visual containers, card visual calculations, and report themes.
- **100% Free / $0 Options**: Works out of the box with Google Gemini (free API key from Google AI Studio) or completely offline and private using local Ollama (`qwen2.5-coder`).

### Quick 1-Minute Install:
**Linux & macOS**:
```bash
curl -fsSL https://raw.githubusercontent.com/Amrit-D-V/FabricAgent/main/install.sh | bash
```

**Windows (PowerShell / CMD)**:
```cmd
git clone https://github.com/Amrit-D-V/FabricAgent.git
cd FabricAgent
install.bat
```

It’s completely open-source (MIT License), has over 1,400+ unit and integration tests passing, and I'd love to hear your thoughts, feedback, and feature ideas from the Power BI community!

Star the repo if you find it useful: https://github.com/Amrit-D-V/FabricAgent
