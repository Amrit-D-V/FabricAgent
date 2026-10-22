# LinkedIn Launch Post

**Target Audience**: Power BI Developers, Data Engineers, Microsoft Fabric Architects, Analytics Engineers

---

### Post Text (Copy & Paste):

Power BI semantic modeling shouldn't require manual TMDL editing or wrestling with LLMs that hallucinate DAX measures.

Today, I’m releasing **FabricAgent** as an open-source project (MIT License) 🚀

FabricAgent is an autonomous AI agent and interactive terminal platform built specifically for Microsoft Power BI and Microsoft Fabric.

Instead of relying solely on general LLMs that don't understand tabular engine internals, FabricAgent uses a **Two-Brain Architecture**:
🧠 **Brain 1 (Planner)**: Analyzes business requirements and data warehouse schemas to propose optimal star-schemas and measure logic.
⚙️ **Brain 2 (Deterministic Compiler & Gate)**: Generates 100% valid TMDL and PBIP code without hallucinations, validated against 60+ Tabular Editor BPA rules and TMDL Doctor linters.

Key Capabilities:
⚡ **Interactive Terminal Assistant (`fabricagent tui`)**: Ask questions, stream DAX, and inspect models right from bash/cmd.
⚡ **TMDL & PBIR Support**: Generate models and Power BI Enhanced Reports programmatically.
⚡ **Zero-Cloud Spend**: Works with Google Gemini (Free Tier) or 100% locally and privately with Ollama.
⚡ **Azure Entra ID SSO & RBAC**: Ready for enterprise team environments.
⚡ **One-Liner Automated Install**: Zero-to-running in 60 seconds on Linux, macOS, and Windows.

Check out the code, star the repository, and try it out:
👉 GitHub: https://github.com/Amrit-D-V/FabricAgent

I'd love feedback from the Power BI and Fabric community! What's the biggest pain point in your daily modeling workflow?

#PowerBI #MicrosoftFabric #DataEngineering #DAX #OpenSource #Python #AIAgent #BusinessIntelligence #AnalyticsEngineering
