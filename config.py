from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


PROJECT_ROOT: Path = Path(__file__).resolve().parent
UPLOADS_DIR: Path = PROJECT_ROOT / "uploads"
LOGS_DIR: Path = PROJECT_ROOT / "logs"

# Storage directory configuration
_PROJECTS_DIR_ENV: str = os.getenv("PROJECTS_DIR", "").strip()
if not _PROJECTS_DIR_ENV:
    raise RuntimeError(
        "PROJECTS_DIR is not set. It is where FabricBI keeps every project, so "
        "there is no safe default to fall back on.\n"
        "Set it in .env, or in the deployment's environment:\n"
        "  Linux / macOS:  PROJECTS_DIR=/data/projects\n"
        "  Windows:        PROJECTS_DIR=C:\\FabricBI-storage"
    )

PROJECTS_DIR: Path = Path(_PROJECTS_DIR_ENV)

# Temporary scratch directory for Fabric artifacts
FABRICBI_TEMP_DIR: Path = Path(
    os.getenv(
        "FABRICBI_TEMP_DIR",
        str(PROJECTS_DIR / "fabricbi_temp"),
    )
)

PBI_PAGINATED_CATALOGS: dict[str, str] = {
    "Sales & Profitability": "sobe_wowvirtualserver-01b7aca3-871d-4158-a4fe-41276bda7585",
}

PAGINATED_DESIGN: dict[str, str] = {
    "header_bg": os.getenv("PAGINATED_HEADER_BG", "#1F3864"),
    "header_text": os.getenv("PAGINATED_HEADER_TEXT", "White"),
    "zebra_bg": os.getenv("PAGINATED_ZEBRA_BG", "#F2F2F2"),
    "title_color": os.getenv("PAGINATED_TITLE_COLOR", "#1F3864"),
    "subtitle_color": os.getenv("PAGINATED_SUBTITLE_COLOR", "#595959"),
    "border_color": os.getenv("PAGINATED_BORDER_COLOR", "#BFBFBF"),
    "total_bg": os.getenv("PAGINATED_TOTAL_BG", "#D9E1F2"),
    "group_bg": os.getenv("PAGINATED_GROUP_BG", "#DDE3F0"),
    "pos_color": os.getenv("PAGINATED_POS_COLOR", "#006100"),
    "neg_color": os.getenv("PAGINATED_NEG_COLOR", "#C00000"),
}

FABRICBI_MEMORY_MAX_CHARS: int = int(os.getenv("FABRICBI_MEMORY_MAX_CHARS", "8000"))

NEO4J_URI: str = os.getenv("NEO4J_URI", "")
NEO4J_USERNAME: str = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD: str = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE: str = os.getenv("NEO4J_DATABASE", "neo4j")
GRAPH_MEMORY_ENABLED: bool = bool(NEO4J_URI) and os.getenv("GRAPH_MEMORY", "1") == "1"
GRAPH_MEMORY_TIMEOUT_S: float = float(os.getenv("GRAPH_MEMORY_TIMEOUT_S", "5"))
GRAPH_MEMORY_RETRY_AFTER_S: int = int(os.getenv("GRAPH_MEMORY_RETRY_AFTER_S", "300"))
GRAPH_MEMORY_RECALL_LIMIT: int = int(os.getenv("GRAPH_MEMORY_RECALL_LIMIT", "15"))
GRAPH_MEMORY_NOTES_PER_PROJECT: int = int(
    os.getenv("GRAPH_MEMORY_NOTES_PER_PROJECT", "100")
)
GRAPH_MEMORY_SOURCE_TABLES: int = int(os.getenv("GRAPH_MEMORY_SOURCE_TABLES", "200"))
GRAPH_MEMORY_SOURCE_COLUMNS: int = int(os.getenv("GRAPH_MEMORY_SOURCE_COLUMNS", "60"))

CONSOLE_DASHBOARD: bool = os.getenv("CONSOLE_DASHBOARD", "1") == "1"
CONSOLE_COLOR: bool = os.getenv("CONSOLE_COLOR", "1") == "1" and not os.getenv(
    "NO_COLOR"
)
CONSOLE_WIDTH: int = int(os.getenv("CONSOLE_WIDTH", "100"))
CONSOLE_PROMPT_CHARS: int = int(os.getenv("CONSOLE_PROMPT_CHARS", "2000"))
LOG_FULL_PROMPT: bool = os.getenv("LOG_FULL_PROMPT", "0") == "1"

SQLITE_BUSY_TIMEOUT_S: float = float(os.getenv("SQLITE_BUSY_TIMEOUT_S", "15"))

SNAPSHOT_REPORT_CONTENT: bool = os.getenv("SNAPSHOT_REPORT_CONTENT", "0") == "1"


FABRIC_WORKSPACE: str = os.getenv("FABRIC_WORKSPACE", "")
FABRIC_WORKSPACE_ID: str = os.getenv("FABRIC_WORKSPACE_ID", "")
FABRIC_MODEL: str = os.getenv("FABRIC_MODEL", "")
FABRIC_MODELS: list[str] = [
    m.strip() for m in os.getenv("FABRIC_MODELS", "").split(",") if m.strip()
]
FABRIC_SQL_DB_ID: str = os.getenv("FABRIC_SQL_DB_ID", "")
FABRIC_APP_DB_SERVER: str = os.getenv("FABRIC_APP_DB_SERVER", "")
FABRIC_APP_DB_NAME: str = os.getenv("FABRIC_APP_DB_NAME", "")


# Tenant lineage (Scanner API) cache TTL and scan settings
LINEAGE_CACHE_TTL_S: int = int(os.getenv("LINEAGE_CACHE_TTL_S", "3600"))
LINEAGE_SCAN_TIMEOUT_S: int = int(os.getenv("LINEAGE_SCAN_TIMEOUT_S", "600"))
LINEAGE_EXCLUDE_PERSONAL: bool = os.getenv("LINEAGE_EXCLUDE_PERSONAL", "1") == "1"
# Max downstream reports to fetch definitions for visual-level lineage
LINEAGE_MAX_REPORT_FETCH: int = int(os.getenv("LINEAGE_MAX_REPORT_FETCH", "40"))


AZURE_TENANT_ID: str = os.getenv("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID: str = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET: str = os.getenv("AZURE_CLIENT_SECRET", "")

# Entra authentication redirect URI
ENTRA_REDIRECT_URI: str = os.getenv(
    "ENTRA_REDIRECT_URI", "http://localhost:8756/login/callback"
)

# Sign-in app registration (falls back to AZURE_* when ENTRA_SSO_* unset)
ENTRA_SSO_TENANT_ID: str = os.getenv("ENTRA_SSO_TENANT_ID", "") or AZURE_TENANT_ID
ENTRA_SSO_CLIENT_ID: str = os.getenv("ENTRA_SSO_CLIENT_ID", "") or AZURE_CLIENT_ID
ENTRA_SSO_CLIENT_SECRET: str = (
    os.getenv("ENTRA_SSO_CLIENT_SECRET", "") or AZURE_CLIENT_SECRET
)

# Delegated Power BI scope used to read the signed-in user's workspace list.
PBI_WORKSPACE_SCOPE: str = "https://analysis.windows.net/powerbi/api/Workspace.Read.All"

# Delegated scopes for read-only Power BI admin APIs
PBI_TENANT_SCOPES: tuple[str, ...] = (
    "https://analysis.windows.net/powerbi/api/Tenant.Read.All",
    "https://analysis.windows.net/powerbi/api/Tenant.ReadWrite.All",
)

# Delegated Fabric scope for workspace item listing
FABRIC_ITEMS_SCOPE: str = "https://api.fabric.microsoft.com/Item.Read.All"

# Gate project access on Fabric workspace membership
FABRIC_ACCESS_GATE: bool = os.getenv("FABRIC_ACCESS_GATE", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Cache TTL for user workspace access
FABRIC_ACCESS_TTL_S: int = int(os.getenv("FABRIC_ACCESS_TTL_S", "900"))

# Break-glass fallback: allow serving stale workspace list during upstream outages
FABRIC_ACCESS_ALLOW_STALE: bool = os.getenv(
    "FABRIC_ACCESS_ALLOW_STALE", ""
).strip().lower() in ("1", "true", "yes")


def _cookie_secure_default() -> bool:
    """Mark session cookies Secure whenever the app is reached over HTTPS.

    Defaulting off would silently ship a non-Secure cookie to production;
    defaulting on would break local http://localhost development. The redirect
    URI already records which one this deployment is, so key on it. An explicit
    SESSION_COOKIE_SECURE always wins.
    """
    explicit = os.getenv("SESSION_COOKIE_SECURE", "").strip().lower()
    if explicit in ("1", "true", "yes"):
        return True
    if explicit in ("0", "false", "no"):
        return False
    return ENTRA_REDIRECT_URI.strip().lower().startswith("https://")


SESSION_COOKIE_SECURE: bool = _cookie_secure_default()


MCP_SERVER_PATH: str = os.getenv("MCP_SERVER_PATH", "")
PBI_DESKTOP_CLI: str = os.getenv("PBI_DESKTOP_CLI", "powerbi-desktop")
PBI_DESKTOP_EXE: str = os.getenv(
    "PBI_DESKTOP_EXE",
    r"C:\Program Files\WindowsApps\Microsoft.MicrosoftPowerBIDesktop_Store\bin\PBIDesktop.exe",
)


def validate_config() -> list[str]:
    problems: list[str] = []
    sp = [AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]
    if any(sp) and not all(sp):
        problems.append(
            "Incomplete service principal — set all of AZURE_TENANT_ID, "
            "AZURE_CLIENT_ID, AZURE_CLIENT_SECRET (or none)."
        )
    if MCP_SERVER_PATH and not Path(MCP_SERVER_PATH).is_file():
        problems.append(f"MCP_SERVER_PATH points to a missing file: {MCP_SERVER_PATH}")
    return problems


LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "anthropic").lower()
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


SEMANTIC_MODEL_PROVIDER: str = os.getenv(
    "SEMANTIC_MODEL_PROVIDER", LLM_PROVIDER
).lower()
DASHBOARD_PROVIDER: str = os.getenv("DASHBOARD_PROVIDER", LLM_PROVIDER).lower()
WIREFRAME_PROVIDER: str = os.getenv("WIREFRAME_PROVIDER", LLM_PROVIDER).lower()
ASSISTANT_PROVIDER: str = os.getenv("ASSISTANT_PROVIDER", LLM_PROVIDER).lower()

SMALL_TASK_PROVIDER: str = os.getenv("SMALL_TASK_PROVIDER", LLM_PROVIDER).lower()
BIG_TASK_PROVIDER: str = os.getenv("BIG_TASK_PROVIDER", LLM_PROVIDER).lower()

# Tools disallowed for CLI inference runtimes
CLAUDE_CLI_DISALLOWED_TOOLS: list[str] = [
    # Shell / process
    "Bash",
    "PowerShell",
    "KillShell",
    "BashOutput",
    # Filesystem
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "Glob",
    "Grep",
    # Network
    "WebFetch",
    "WebSearch",
    # Delegation
    "Task",
    "Agent",
    "Workflow",
    "Skill",
    "SlashCommand",
    # Side effects outside the conversation
    "ScheduleWakeup",
    "CronCreate",
    "CronDelete",
    "CronList",
    "Artifact",
    "Monitor",
    "EndConversation",
]


OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")


OPENAI_COMPATIBLE_PROVIDERS: dict[str, dict] = {
    "openai": {
        "base_url": os.getenv("OPENAI_BASE_URL", "") or None,
        "api_key_env": "OPENAI_API_KEY",
        "default_model": os.getenv("OPENAI_MODEL", "gpt-4o"),
    },
    "gemini": {
        "base_url": os.getenv(
            "GEMINI_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
        ),
        "api_key_env": "GEMINI_API_KEY",
        "default_model": os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
    },
    "groq": {
        "base_url": os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        "api_key_env": "GROQ_API_KEY",
        "default_model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
    },
    "openrouter": {
        "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "api_key_env": "OPENROUTER_API_KEY",
        "default_model": os.getenv("OPENROUTER_MODEL", "openai/gpt-4o"),
    },
}

LLM_PICKER_CATALOG: dict[str, dict] = {
    "Claude (Anthropic API)": {
        "provider": "anthropic",
        "models": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    },
    "Codex (Codex CLI)": {
        "provider": "codex_cli",
        "models": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
    },
    "OpenAI": {
        "provider": "openai",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o3", "o4-mini"],
    },
    "Google Gemini": {
        "provider": "gemini",
        "models": [
            "gemini-flash-latest",
            "gemini-3.8-flash",
            "gemini-2.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-2.5-pro",
        ],
    },
    "Ollama (local)": {
        "provider": "ollama",
        "models": [OLLAMA_MODEL],
    },
    "Groq": {
        "provider": "groq",
        "models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "gemma2-9b-it",
        ],
    },
    "OpenRouter": {
        "provider": "openrouter",
        "models": [
            "openai/gpt-4o",
            "anthropic/claude-sonnet-4-6",
            "google/gemini-3.6-flash",
            "meta-llama/llama-3.3-70b-instruct",
        ],
    },
}

PIPELINE_LLM_AGENTS: dict[str, list[str]] = {
    "semantic_model": ["semantic_model", "big_task", "small_task"],
}

PROMPT_BLOCK_CAPS: dict[str, dict[str, int]] = {
    "default": {"db_schema": 30000, "kpi": 8000, "wf_layout": 4000, "wf_text": 3000},
    "api": {"db_schema": 8000, "kpi": 3000, "wf_layout": 1500, "wf_text": 1500},
}


POWERBI_API_BASE: str = os.getenv(
    "POWERBI_API_BASE", "https://api.powerbi.com/v1.0/myorg"
)
XMLA_ENDPOINT_BASE: str = os.getenv(
    "XMLA_ENDPOINT_BASE", "powerbi://api.powerbi.com/v1.0/myorg"
)


LANGFUSE_HOST: str = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
LANGFUSE_PUBLIC_KEY: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY: str = os.getenv("LANGFUSE_SECRET_KEY", "")


PBI_SCOPE: str = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_SCOPE: str = "https://api.fabric.microsoft.com/.default"
SQL_SCOPE: str = "https://database.windows.net/.default"


REPORT_PAGE_BG: str = os.getenv("REPORT_PAGE_BG", "#F5F7FA")

SEMANTIC_GOOD: str = os.getenv("SEMANTIC_GOOD", "#059669")
SEMANTIC_BAD: str = os.getenv("SEMANTIC_BAD", "#DC2626")
SEMANTIC_MID: str = os.getenv("SEMANTIC_MID", "#FACC15")

# Design tokens — the single source of truth for the generated-report look.
# Every colour, font and radius in the report theme derives from these; no hex
# is repeated inline in report code. Each token is deployable per client via
# env without touching code.
DESIGN_TOKENS: dict[str, str] = {
    "accent": os.getenv("REPORT_ACCENT", "#2369D2"),  # brand / emphasis
    "ink": os.getenv("REPORT_INK", "#0F172A"),  # primary text & data labels
    "muted": os.getenv("REPORT_MUTED", "#64748B"),  # secondary text, axis labels
    "faint": os.getenv("REPORT_FAINT", "#94A3B8"),  # neutral series / disabled
    "card_bg": os.getenv("REPORT_CARD_BG", "#FFFFFF"),  # visual surface
    "hairline": os.getenv("REPORT_HAIRLINE", "#EDF2F8"),  # card borders
    "grid": os.getenv("REPORT_GRIDLINE", "#F1F5F9"),  # chart gridlines, table rules
    "header_bg": os.getenv("REPORT_HEADER_BG", "#1E293B"),  # chrome: nav / header band
    "header_fg": os.getenv("REPORT_HEADER_FG", "#FFFFFF"),
    "font": os.getenv("REPORT_FONT", "Segoe UI"),
    "font_semibold": os.getenv("REPORT_FONT_SEMIBOLD", "Segoe UI Semibold"),
}

# Categorical palette with contrast and color-blind safety ordering
REPORT_DATA_COLORS: list[str] = [
    c.strip()
    for c in os.getenv(
        "REPORT_DATA_COLORS",
        "#2369D2,#10B981,#F59E0B,#6366F1,#06B6D4,#8B5CF6,#EC4899,#64748B",
    ).split(",")
    if c.strip()
]

# Design-policy defaults: reusable minimums and safety limits
REPORT_LAYOUT: dict[str, int] = {
    "kpi_min_px": 112,
    "kpi_clip_px": 80,
    "kpi_callout_w_px": 260,  # width budget per KPI callout on a multi-value card
    "callout_px": 32,
    "gutter_px": 16,
    "nav_px": 40,
    "button_w": 120,
    "button_h": 32,
    "grid_px": 8,
    "margin_px": 24,
    "chart_min_px": 200,
    "chart_min_w_px": 280,
    "row_gap_warn_px": 64,
    # Slicer height: 60px chrome + 20px padding
    "slicer_min_px": 80,
    "visual_padding_px": 10,
    "radius_px": 14,
    "header_h": 48,
    "slicer_w": 260,
    "slicer_gap_px": 8,
    "table_min_px": 200,
    "table_min_w_px": 280,
    "table_col_w_px": 120,
    "matrix_min_px": 200,
    "matrix_min_w_px": 320,
    "matrix_col_w_px": 140,
    # Canvas growth bounds
    "canvas_min_w": 960,
    "canvas_preferred_w": 1280,
    "canvas_max_w": 1920,
    "canvas_min_h": 540,
    "canvas_preferred_h": 720,
    "canvas_max_h": 1440,
}

REPORT_DENSITY: dict[str, float] = {
    "min_coverage_frac": float(os.getenv("REPORT_MIN_COVERAGE_FRAC", "0.55")),
    "dead_margin_frac": float(os.getenv("REPORT_DEAD_MARGIN_FRAC", "0.12")),
    "coverage_min_visuals": int(os.getenv("REPORT_COVERAGE_MIN_VISUALS", "2")),
}

SLICER_MAX_Y_FRAC: float = 0.5

TOP_LEFT_REGION: dict[str, float] = {"x_frac": 0.3, "y_frac": 0.25}

WCAG_TEXT_CONTRAST_MIN: float = 4.5
ALT_TEXT_MAX_CHARS: int = 250
CVD_AUDIT: dict[str, float] = {
    "lightness_delta_min": 0.05,
    "hue_gap_min_deg": 60.0,
    "red_hue_max_deg": 20.0,
    "green_hue_min_deg": 80.0,
    "green_hue_max_deg": 170.0,
    "red_green_lum_delta_min": 0.15,
}

TINT_FACTORS: dict[str, float] = {
    "gradient_min": 0.85,
    "gradient_center": 0.45,
    "nav_hover_fill": 0.88,
}

WIREFRAME_MAX_W: int = int(os.getenv("WIREFRAME_MAX_W", "960"))

WATERMARK_SVG_PATH: str = os.getenv("WATERMARK_SVG_PATH", "")
WATERMARK_OPACITY: float = float(os.getenv("WATERMARK_OPACITY", "0.08"))

JUDGE_PASS_SCORE: int = int(os.getenv("JUDGE_PASS_SCORE", "7"))

JUDGE_MAX_ROUNDS: int = int(os.getenv("JUDGE_MAX_ROUNDS", "3"))

DESIGN_BUDGETS: dict[str, int] = {
    "max_cards": 5,
    "max_slicers": 4,
    "max_visuals": 12,
    "max_pie_slices": 5,
    "max_fields_per_visual": 5,
}


DAX_SPILL_DIR: Path = Path(os.getenv("DAX_SPILL_DIR", str(LOGS_DIR / "dax_spills")))
DAX_SPILL_ROW_THRESHOLD: int = int(os.getenv("DAX_SPILL_ROW_THRESHOLD", "40"))
DAX_SPILL_PREVIEW_ROWS: int = int(os.getenv("DAX_SPILL_PREVIEW_ROWS", "10"))
DAX_SPILL_PAGE_MAX_ROWS: int = int(os.getenv("DAX_SPILL_PAGE_MAX_ROWS", "200"))


TE2_PATH: str = os.getenv(
    "TE2_PATH", "C:/Program Files (x86)/Tabular Editor/TabularEditor.exe"
)
XMLA_CONN_TTL_S: int = int(os.getenv("XMLA_CONN_TTL_S", "300"))
DAX_LIVE_VALIDATE: bool = os.getenv("DAX_LIVE_VALIDATE", "1") == "1"
MODEL_ASSERTIONS_ON_APPLY: bool = os.getenv("MODEL_ASSERTIONS_ON_APPLY", "1") == "1"
BPA_RULES_PATH: Path = Path(
    os.getenv("BPA_RULES_PATH", str(PROJECT_ROOT / "tools" / "bpa" / "BPARules.json"))
)
PBI_INSPECTOR_PATH: Path = Path(
    os.getenv(
        "PBI_INSPECTOR_PATH",
        str(
            PROJECT_ROOT
            / "tools"
            / "pbi-inspector"
            / "win-x64"
            / "CLI"
            / "PBIRInspectorCLI.exe"
        ),
    )
)
PBI_INSPECTOR_RULES_PATH: Path = Path(
    os.getenv(
        "PBI_INSPECTOR_RULES_PATH",
        str(PROJECT_ROOT / "tools" / "pbi-inspector" / "Base-rules.json"),
    )
)
LINT_TIMEOUT_S: int = int(os.getenv("LINT_TIMEOUT_S", "120"))


SMB_TREND_MAX_BASES: int = int(os.getenv("SMB_TREND_MAX_BASES", "3"))
SMB_TREND_FOLDER: str = os.getenv("SMB_TREND_FOLDER", "Trends")
SMB_TREND_PCT_FORMAT: str = os.getenv("SMB_TREND_PCT_FORMAT", "0.0%")
SMB_PROFILE_SAMPLE_CAP: int = int(os.getenv("SMB_PROFILE_SAMPLE_CAP", "10"))

SMB_POSTAL_NAME_RX: str = os.getenv("SMB_POSTAL_NAME_RX", r"zip|postal|cep")
SMB_DATE_NAME_RX: str = os.getenv(
    "SMB_DATE_NAME_RX", r"(_date|_at|_time|_timestamp|_dt)$"
)
SMB_INT_NAME_RX: str = os.getenv("SMB_INT_NAME_RX", r"score|rating|(_qty|_count|_num)$")

SMB_FMT_WHOLE: str = os.getenv("SMB_FMT_WHOLE", "#,0")
SMB_FMT_DECIMAL: str = os.getenv("SMB_FMT_DECIMAL", "#,0.00")
SMB_FMT_PERCENT: str = os.getenv("SMB_FMT_PERCENT", SMB_TREND_PCT_FORMAT)
SMB_PCT_NAME_RX: str = os.getenv(
    "SMB_PCT_NAME_RX", r"%|percent|\bpct\b|\brate\b|ratio|margin|share|penetration"
)
SMB_COUNT_FN_RX: str = os.getenv(
    "SMB_COUNT_FN_RX", r"\b(COUNTROWS|COUNTA?|DISTINCTCOUNT|COUNTAX|COUNTX)\s*\("
)

SMB_TREND_BASE_GROUPS: list[str] = [
    r"revenue|sales|gmv",
    r"order count|\borders\b",
    r"payment value|order value",
]

SMB_DATE_PARSE_FORMATS: list[str] = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y-%m-%dT%H:%M:%S",
]

SMB_TABLE_STRIP_SUFFIXES: list[str] = ["_datasets", "_dataset"]

SMB_FOLDER_COUNTS: str = os.getenv("SMB_FOLDER_COUNTS", "1. Counts")
SMB_FOLDER_DERIVED: str = os.getenv("SMB_FOLDER_DERIVED", "4. Derived")
SMB_SKIP_TABLE_PREFIXES: list[str] = ["_", "S0 ", "S1 ", "S2 "]
SMB_SKIP_TABLE_NAMES: list[str] = ["DimDate", "DateTableTemplate"]

SMB_PROFILE_MAX_TABLES: int = int(os.getenv("SMB_PROFILE_MAX_TABLES", "60"))
SMB_PROFILE_SKIP_SCHEMAS: list[str] = ["queryinsights", "sys"]


POWERBI_NATIVE_VISUALS: dict[str, str] = {
    "Bar chart": "barChart",
    "Clustered bar chart": "clusteredBarChart",
    "Column chart": "columnChart",
    "Clustered column chart": "clusteredColumnChart",
    "Line chart": "lineChart",
    "Area chart": "areaChart",
    "Ribbon chart": "ribbonChart",
    "Donut chart": "donutChart",
    "Treemap": "treemap",
    "Funnel chart": "funnel",
    "Scatter / bubble chart": "scatterChart",
    "Waterfall chart": "waterfallChart",
    "Gauge": "gauge",
    "Card": "card",
    "Callout card": "cardVisual",
    "KPI": "kpi",
    "Table": "tableEx",
    "Matrix": "pivotTable",
    "Slicer": "slicer",
    "Map": "map",
}

POWERBI_VISUAL_ALIASES: dict[str, str] = {
    "bar": "barChart",
    "clustered bar": "clusteredBarChart",
    "stacked bar": "barChart",
    "column": "columnChart",
    "clustered column": "clusteredColumnChart",
    "stacked column": "columnChart",
    "line": "lineChart",
    "area": "areaChart",
    "stacked area": "areaChart",
    "ribbon": "ribbonChart",
    "donut": "donutChart",
    "pie": "donutChart",
    "treemap": "treemap",
    "funnel": "funnel",
    "scatter": "scatterChart",
    "bubble": "scatterChart",
    "waterfall": "waterfallChart",
    "gauge": "gauge",
    "card": "card",
    "callout": "cardVisual",
    "callout card": "cardVisual",
    "kpi": "kpi",
    "multi-row card": "card",
    "multirow card": "card",
    "table": "tableEx",
    "matrix": "pivotTable",
    "slicer": "slicer",
    "map": "map",
    "filled map": "map",
    "filled (choropleth) map": "map",
    "choropleth": "map",
    "bubble map": "map",
    "combo": "lineChart",
    "line-and-column combo chart": "lineChart",
    "line and column combo": "lineChart",
    "decomposition tree": "pivotTable",
    "key influencers": "clusteredBarChart",
    "q&a": "card",
    "qna": "card",
}

POWERBI_FORBIDDEN_VISUALS: list[str] = [
    "streamgraph",
    "marimekko/mosaic",
    "sankey",
    "chord diagram",
    "calendar/GitHub-style heatmap",
    "emoji heatmap",
    "word cloud",
    "sunburst",
    "radar/spider",
    "bullet chart",
    "gantt",
    "network graph",
    "candlestick",
]

POWERBI_DEFAULT_VISUAL: str = "columnChart"


def normalize_visual_type(raw: str) -> str:
    if not raw:
        return POWERBI_DEFAULT_VISUAL
    native_ids = set(POWERBI_NATIVE_VISUALS.values())
    key = raw.strip()
    if key in native_ids:
        return key
    low = key.lower()
    for nid in native_ids:
        if low == nid.lower():
            return nid
    for label, nid in POWERBI_NATIVE_VISUALS.items():
        if low == label.lower():
            return nid
    return POWERBI_VISUAL_ALIASES.get(low, POWERBI_DEFAULT_VISUAL)


POWERBI_ALL_NATIVE_VISUAL_IDS: frozenset = frozenset(
    {
        "barChart",
        "clusteredBarChart",
        "stackedBarChart",
        "hundredPercentStackedBarChart",
        "columnChart",
        "clusteredColumnChart",
        "stackedColumnChart",
        "hundredPercentStackedColumnChart",
        "lineChart",
        "areaChart",
        "stackedAreaChart",
        "hundredPercentStackedAreaChart",
        "lineClusteredColumnComboChart",
        "lineStackedColumnComboChart",
        "ribbonChart",
        "waterfallChart",
        "scatterChart",
        "funnel",
        "treemap",
        "pieChart",
        "donutChart",
        "card",
        "cardVisual",
        "multiRowCard",
        "kpi",
        "kpiNew",
        "gauge",
        "tableEx",
        "pivotTable",
        "slicer",
        "listSlicer",
        "textSlicer",
        "advancedSlicerVisual",
        "map",
        "filledMap",
        "shapeMap",
        "azureMap",
        "decompositionTreeVisual",
        "keyInfluencersVisual",
        "keyDriversVisual",
        "keyDriversDrillVisual",
        "qnaVisual",
        "aiNarratives",
        "scorecard",
        "pythonVisual",
        "scriptVisual",
        "rdlVisual",
        "textbox",
        "image",
        "shape",
        "actionButton",
        "pageNavigator",
        "bookmarkNavigator",
        "personalizeVisual",
    }
)
