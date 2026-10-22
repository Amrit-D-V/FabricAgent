# Contributing to FabricAgent

Thank you for your interest in contributing to **FabricAgent**! FabricAgent is an open-source AI platform and terminal engine for generating, validating, and interacting with Microsoft Power BI and Microsoft Fabric semantic models and reports.

---

## 1. Development Setup

### Automated Setup
The fastest way to set up your development environment:
```bash
./install.sh
```

### Manual Setup
```bash
# 1. Clone the repository
git clone https://github.com/Amrit-D-V/FabricAgent.git
cd FabricAgent

# 2. Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install Python dependencies and CLI package
pip install -r requirements.txt
pip install -e . --no-deps

# 4. Run setup wizard
fabricagent setup
```

---

## 2. Running Tests

FabricAgent maintains a strict regression test suite of over 1,400 scenarios. Always verify that tests pass before opening a Pull Request:

```bash
# Run the complete test suite
pytest tests/

# Run a specific test module
pytest tests/test_cli_scenarios.py
```

---

## 3. Architecture Overview

* **CLI & TUI (`cli/`)**:
  * Rich terminal interface (`fabricagent tui`, `fabricagent chat`) providing interactive conversations with streaming tokens.
  * Setup wizard (`fabricagent setup`) for zero-friction bootstrapping.
* **Two-Brain Modeling Engine (`model/two_brain.py`)**:
  * **Brain 1 (Planner)**: Uses LLM reasoning to parse business requirements, categorize tables into Facts/Dimensions, and produce a `SchemaPlan`.
  * **Brain 2 (Compiler)**: 100% deterministic code compilation from the `SchemaPlan` into physical TMDL and PBIP definitions without LLM hallucination.
* **Deterministic Guardrails (`model/tmdl_doctor.py`, `model/bpa_engine.py`)**:
  * Auto-repairs relationship directionality, DAX syntax issues, and schema inconsistencies.
* **Multi-Provider LLM Engine (`llm/`)**:
  * Standardized routing supporting Google Gemini ($0 free tier), Ollama (local offline), OpenAI, Anthropic Claude, and Groq.

---

## 4. Submitting a Pull Request

1. Fork the repository and create a descriptive branch:
   `git checkout -b feat/your-feature-name`
2. Commit your changes with clear, concise messages.
3. Verify that all tests pass: `pytest tests/`.
4. Push your branch and submit a PR to `main`.
