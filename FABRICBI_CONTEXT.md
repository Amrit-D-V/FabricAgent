# FabricBI — Architecture & Context Reference

*Verified against the codebase for the FabricBI open-source release.*

---

## 1. What FabricBI Is

An open-source AI platform and terminal engine for **Microsoft Power BI / Microsoft Fabric**. Users attach requirement
documents or warehouse schemas to a project, and FabricBI compiles TMDL semantic models from
them, checks coverage of the resulting models, and answers analytical questions with live DAX and chart specs.

Everything is **project-scoped**: a project header `x-fabricbi-project` scopes API requests, and the interactive TUI
allows conversational data modeling directly in the terminal.

---

## 2. Stack

| Layer | What |
|---|---|
| CLI / TUI | Rich Terminal Interface (`fabricbi tui`, `fabricbi chat`), `cli/` |
| API | **Starlette** (ASGI), single app in `api/server.py` |
| Auth | Microsoft Entra SSO → encrypted session cookie (`fabricbi_session`). Roles: `admin` / `editor` / `viewer` |
| DB | SQLite **or** Fabric SQL, switched by `DB_BACKEND` env (`db/database.py`) |
| LLM | Google Gemini, Anthropic Claude, Ollama (local), OpenAI, Groq; MCP tool servers |
| Fabric | XMLA via ADOMD.NET (pythonnet/.NET 8), SQL via pyodbc/ODBC 18, REST via `requests` |

HTTP routes registered across route modules (`api/server.py`); `api/registry.py`'s `@route` decorator handles auth, role gating, threadpool
hops, and JSON serialisation for every endpoint.

---

## 3. Interfaces & Access

- **FabricBI TUI (`fabricbi tui`)**: Interactive terminal console with streaming tokens, slash commands (`/demo`, `/mode`, `/status`), and syntax highlighting.
- **FabricBI CLI (`fabricbi`)**: Headless command line interface for model validation, single queries, user provisioning, and server startup.
- **REST / SSE API**: Headless endpoints for semantic model compilation, background task dispatch, report scans, and data exploration.

---

## 4. Key Architectural Facts

- **Two-Brain Architecture**: Strict decoupling between Brain 1 (LLM Semantic Planner) and Brain 2 (Deterministic TMDL/PBIP Compiler).
- **Offline Mock Engine (`fabric/mock_provider.py`)**: Pre-seeded AdventureWorks datasets allow zero-cloud development and testing without Azure credentials.
- **Lazy Imports**: Heavy native dependencies (ADOMD, pyodbc, pandas, playwright) are lazily imported.
- **Config**: Requires `PROJECTS_DIR` to be set before booting.
