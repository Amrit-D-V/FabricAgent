from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

console = Console()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def update_env_file(env_path: Path, updates: dict[str, str]) -> None:
    """Update or append key-value pairs in the specified .env file while preserving comments."""
    if not env_path.exists():
        example_path = env_path.parent / ".env.example"
        if example_path.exists():
            env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            env_path.write_text("# FabricAgent Environment Configuration\n", encoding="utf-8")

    content = env_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    updated_keys: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _ = stripped.split("=", 1)
            key = key.strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # Append any remaining keys that were not already in the file
    remaining = [k for k in updates if k not in updated_keys]
    if remaining:
        new_lines.append("")
        new_lines.append("# Configured by FabricAgent Setup Wizard")
        for k in remaining:
            new_lines.append(f"{k}={updates[k]}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def ensure_encryption_key(env_path: Path, current_env: dict[str, str]) -> str:
    """Ensure a Fernet secret key exists in .app_secret_key or APP_ENCRYPTION_KEY."""
    secret_file = env_path.parent / ".app_secret_key"
    key = current_env.get("APP_ENCRYPTION_KEY", "").strip()

    if not key and secret_file.exists():
        key = secret_file.read_text(encoding="utf-8").strip()

    if not key:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        secret_file.write_text(key, encoding="utf-8")
        update_env_file(env_path, {"APP_ENCRYPTION_KEY": key})

    return key


def parse_env_file(env_path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip().strip("\"'")
    return result


def run_setup_wizard(
    non_interactive: bool = False,
    provider_choice: str | None = None,
    api_key: str | None = None,
) -> None:
    """Interactive wizard to configure and initialize FabricAgent."""
    env_path = PROJECT_ROOT / ".env"
    current_env = parse_env_file(env_path)

    is_interactive = not non_interactive and sys.stdin.isatty()

    console.print(
        Panel(
            "[bold white]FabricAgent — Setup & Configuration Wizard[/bold white]\n"
            "[dim]Autonomous AI Agent for Power BI & Microsoft Fabric[/dim]\n\n"
            "This wizard will configure your local storage, encryption keys, and AI provider.",
            border_style="cyan",
            expand=False,
        )
    )

    # 1. Local Storage directories
    storage_dir = Path(current_env.get("PROJECTS_DIR", "./storage"))
    if not storage_dir.is_absolute():
        storage_dir = PROJECT_ROOT / storage_dir

    for sub in [storage_dir, PROJECT_ROOT / "uploads", PROJECT_ROOT / "logs", storage_dir / "fabricbi_temp"]:
        sub.mkdir(parents=True, exist_ok=True)

    env_updates: dict[str, str] = {
        "PROJECTS_DIR": str(storage_dir),
        "FABRICBI_TEMP_DIR": str(storage_dir / "fabricbi_temp"),
        "DB_BACKEND": current_env.get("DB_BACKEND", "sqlite"),
        "FABRICBI_AGENT_HOST": current_env.get("FABRICBI_AGENT_HOST", "127.0.0.1"),
        "FABRICBI_AGENT_PORT": current_env.get("FABRICBI_AGENT_PORT", "8756"),
    }

    # 2. Secret Encryption Key
    enc_key = ensure_encryption_key(env_path, current_env)
    env_updates["APP_ENCRYPTION_KEY"] = enc_key
    console.print("[green]✓[/green] Storage directories and security encryption key verified.")

    # 3. LLM Configuration
    selected_provider = provider_choice or current_env.get("LLM_PROVIDER", "")

    if is_interactive and not provider_choice:
        console.print("\n[bold cyan]Select your LLM / AI Engine:[/bold cyan]")
        table = Table(show_header=True, header_style="bold magenta", border_style="dim")
        table.add_column("Option", style="bold yellow", width=8)
        table.add_column("Provider", style="bold white", width=22)
        table.add_column("Cost / Tier", style="green", width=16)
        table.add_column("Description")

        table.add_row(
            "[1]",
            "Google Gemini",
            "Free Tier ($0)",
            "Recommended. Free API key from Google AI Studio. Fast & highly capable.",
        )
        table.add_row(
            "[2]",
            "Ollama (Local)",
            "100% Free / Offline",
            "Run local models (e.g. qwen2.5-coder, llama3.2). Private & zero-cost.",
        )
        table.add_row(
            "[3]",
            "Offline AdventureWorks",
            "$0 (No Key Needed)",
            "Instant demo mode with built-in Retail Sales & Finance datasets.",
        )
        table.add_row(
            "[4]",
            "OpenAI",
            "Paid API",
            "GPT-4o, GPT-4o-mini via official OpenAI API key.",
        )
        table.add_row(
            "[5]",
            "Groq",
            "Free Tier / Paid",
            "Ultra-low latency Llama 3 models.",
        )
        table.add_row(
            "[6]",
            "Anthropic Claude",
            "Paid API",
            "Claude 3.5 Sonnet via Anthropic API key.",
        )
        if current_env.get("LLM_PROVIDER"):
            table.add_row(
                "[7]",
                "Keep Current",
                "Existing",
                f"Keep currently configured '{current_env.get('LLM_PROVIDER')}'.",
            )

        console.print(table)

        choice = Prompt.ask(
            "Select option",
            choices=["1", "2", "3", "4", "5", "6"] + (["7"] if current_env.get("LLM_PROVIDER") else []),
            default="7" if current_env.get("LLM_PROVIDER") else "1",
        )

        if choice == "1":
            selected_provider = "gemini"
            console.print("[dim]Get your free API key at: [link=https://aistudio.google.com/app/apikey]https://aistudio.google.com/app/apikey[/link][/dim]")
            current_key = current_env.get("GEMINI_API_KEY", "")
            key_val = Prompt.ask("Enter Google Gemini API Key", default=current_key if current_key else None)
            env_updates["LLM_PROVIDER"] = "gemini"
            env_updates["GEMINI_API_KEY"] = key_val.strip()
            env_updates["GEMINI_MODEL"] = "gemini-flash-latest"
            env_updates["GEMINI_BASE_URL"] = "https://generativelanguage.googleapis.com/v1beta/openai/"
        elif choice == "2":
            selected_provider = "ollama"
            base_url = Prompt.ask("Ollama Base URL", default=current_env.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
            model_name = Prompt.ask("Ollama Model Name", default=current_env.get("OLLAMA_MODEL", "qwen2.5-coder"))
            env_updates["LLM_PROVIDER"] = "ollama"
            env_updates["OLLAMA_BASE_URL"] = base_url
            env_updates["OLLAMA_MODEL"] = model_name
        elif choice == "3":
            selected_provider = "mock"
            env_updates["FABRIC_OFFLINE_MOCK"] = "1"
            if not current_env.get("LLM_PROVIDER"):
                env_updates["LLM_PROVIDER"] = "gemini"
            console.print("[green]Offline demo mode enabled with AdventureWorks mock dataset.[/green]")
        elif choice == "4":
            selected_provider = "openai"
            key_val = Prompt.ask("Enter OpenAI API Key", default=current_env.get("OPENAI_API_KEY", ""))
            model_val = Prompt.ask("Enter OpenAI Model", default=current_env.get("OPENAI_MODEL", "gpt-4o"))
            env_updates["LLM_PROVIDER"] = "openai"
            env_updates["OPENAI_API_KEY"] = key_val.strip()
            env_updates["OPENAI_MODEL"] = model_val.strip()
        elif choice == "5":
            selected_provider = "groq"
            key_val = Prompt.ask("Enter Groq API Key", default=current_env.get("GROQ_API_KEY", ""))
            env_updates["LLM_PROVIDER"] = "groq"
            env_updates["GROQ_API_KEY"] = key_val.strip()
        elif choice == "6":
            selected_provider = "anthropic"
            key_val = Prompt.ask("Enter Anthropic API Key", default=current_env.get("ANTHROPIC_API_KEY", ""))
            env_updates["LLM_PROVIDER"] = "anthropic"
            env_updates["ANTHROPIC_API_KEY"] = key_val.strip()
        elif choice == "7":
            pass
    elif provider_choice:
        env_updates["LLM_PROVIDER"] = provider_choice
        if api_key:
            if provider_choice == "gemini":
                env_updates["GEMINI_API_KEY"] = api_key
                env_updates["GEMINI_MODEL"] = "gemini-flash-latest"
                env_updates["GEMINI_BASE_URL"] = "https://generativelanguage.googleapis.com/v1beta/openai/"
            elif provider_choice == "openai":
                env_updates["OPENAI_API_KEY"] = api_key
            elif provider_choice == "groq":
                env_updates["GROQ_API_KEY"] = api_key
            elif provider_choice == "anthropic":
                env_updates["ANTHROPIC_API_KEY"] = api_key

    # Save updates to .env file
    update_env_file(env_path, env_updates)
    for k, v in env_updates.items():
        os.environ[k] = v

    # 4. Optional Microsoft Fabric connection check
    if is_interactive and selected_provider != "mock":
        connect_azure = Confirm.ask("\nWould you like to configure your Microsoft Azure / Fabric App Registration now?", default=False)
        if connect_azure:
            from cli.fabric_cli import connect_cmd
            import click
            ctx = click.Context(connect_cmd)
            connect_cmd.invoke(ctx)

    # 5. Initialize database and preflight checks
    console.print("\n[dim]Initializing database and verifying components...[/dim]")
    from db.database import init_db
    from api.server import preflight_check

    init_db()
    preflight_check()
    console.print("[bold green]✓ Database, storage, and background queues initialized cleanly.[/bold green]")

    # 6. Completion Summary Panel
    final_env = parse_env_file(env_path)
    summary_table = Table(title="FabricAgent Status", border_style="green", expand=False)
    summary_table.add_column("Setting", style="bold white")
    summary_table.add_column("Value", style="cyan")

    summary_table.add_row("Storage Path", str(storage_dir))
    summary_table.add_row("Database", f"{final_env.get('DB_BACKEND', 'sqlite')} (project_data.db)")
    summary_table.add_row("LLM Provider", final_env.get("LLM_PROVIDER", "None"))
    if final_env.get("LLM_PROVIDER") == "gemini":
        summary_table.add_row("Gemini Model", final_env.get("GEMINI_MODEL", "gemini-flash-latest"))
    elif final_env.get("LLM_PROVIDER") == "ollama":
        summary_table.add_row("Ollama Model", final_env.get("OLLAMA_MODEL", "qwen2.5-coder"))

    console.print("\n", summary_table)

    console.print(
        Panel(
            "[bold green]FabricAgent is configured and ready to use![/bold green]\n\n"
            "[bold]Quick Commands:[/bold]\n"
            "  • [bold cyan]fabricagent tui[/bold cyan]            Launch the interactive Terminal UI\n"
            "  • [bold cyan]fabricagent chat \"<query>\"[/bold cyan] Ask a DAX or Power BI modeling question\n"
            "  • [bold cyan]fabricagent server[/bold cyan]         Start the REST API server on port 8756\n"
            "  • [bold cyan]fabricagent fabric status[/bold cyan]  Check Microsoft Fabric cloud connection\n"
            "  • [bold cyan]fabricagent validate <dir>[/bold cyan] Run TMDL Doctor and BPA rules on models\n\n"
            "[dim]Aliases: 'fabric-agent' and 'fabricbi' work identically.[/dim]",
            border_style="green",
            title="Setup Complete",
        )
    )

    if is_interactive:
        start_now = Confirm.ask("\nWould you like to launch the Terminal UI (fabricagent tui) now?", default=True)
        if start_now:
            from cli.tui import start_tui
            start_tui()
