from __future__ import annotations

import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.theme import Theme

from cli import __version__
from fabric.mock_provider import DEMO_REQUIREMENTS, DEMO_SCHEMA_TABLES
from llm.llm_client import get_model_name, get_provider, stream_text

custom_theme = Theme(
    {
        "info": "cyan",
        "warning": "yellow",
        "error": "bold red",
        "success": "bold green",
        "accent": "bold magenta",
        "prompt": "bold blue",
    }
)

console = Console(theme=custom_theme)


def print_banner(mode: str):
    provider = get_provider("assistant")
    model = get_model_name("assistant")
    banner_content = (
        f"[bold white]FabricAgent Terminal Assistant[/bold white] [dim]v{__version__}[/dim]\n"
        f"Engine: [cyan]{provider}[/cyan] [dim]•[/dim] Model: [green]{model}[/green] [dim]•[/dim] Mode: [magenta]{mode}[/magenta]\n\n"
        "[dim]Ask about DAX, Power BI models, star schemas, or type [bold]/help[/bold] for commands.[/dim]"
    )
    console.print(Panel(banner_content, border_style="cyan", expand=False))


def print_help():
    help_md = """
### Available TUI Commands:
* **`/demo`** — Load the built-in Retail Analytics dataset (`DimCustomer`, `DimProduct`, `FactInternetSales`) into memory.
* **`/azure`** or **`/connect`** — Check Azure App Registration & Power BI tenant connection.
* **`/mode [dashboard|general]`** — Switch between specialized Power BI modeling and general analytics.
* **`/clear`** — Clear active conversation history.
* **`/status`** — Display current LLM provider, active model, and loaded context.
* **`/help`** — Show this command reference.
* **`/exit`** or **`/quit`** — Exit FabricAgent TUI.
"""
    console.print(Markdown(help_md))


def build_system_prompt(mode: str, demo_loaded: bool) -> str:
    base = (
        "You are FabricAgent, an expert AI assistant for Microsoft Power BI, DAX, and Microsoft Fabric.\n"
        "You help data engineers, analysts, and business users design star schemas, write clean DAX measures,\n"
        "and optimize semantic models according to Microsoft and Tabular Editor BPA best practices.\n"
        "Always use DIVIDE() instead of '/' for safety, format measures clearly, and explain business reasoning."
    )
    if demo_loaded:
        tables_str = ""
        for tbl, cols in DEMO_SCHEMA_TABLES.items():
            cols_fmt = ", ".join(f"{c[0]} ({c[1]})" for c in cols)
            tables_str += f"\n- **{tbl}**: {cols_fmt}"
        base += (
            f"\n\n### ACTIVE DATASET (Retail Analytics):\n{tables_str}\n\n"
            f"### BUSINESS REQUIREMENTS:\n{DEMO_REQUIREMENTS}"
        )
    return base


def run_single_query(prompt: str, mode: str = "dashboard"):
    """Execute a single query from the command line."""
    system_prompt = build_system_prompt(mode, demo_loaded=True)
    console.print(Rule(style="dim"))
    try:
        chunks = []
        with console.status("[bold cyan]FabricAgent Thinking...[/bold cyan]", spinner="dots"):
            for chunk in stream_text(system_prompt, prompt, agent="assistant"):
                if isinstance(chunk, str):
                    chunks.append(chunk)

        content = "".join(chunks).strip()
        console.print(Markdown(content))
        console.print(Rule(style="dim"))
    except Exception as exc:
        console.print(f"[error]Error running prompt:[/error] {exc}")


def start_tui(initial_mode: str = "dashboard"):
    """Start the interactive terminal chat loop."""
    mode = initial_mode
    demo_loaded = False
    history: list[dict[str, str]] = []

    print_banner(mode)

    while True:
        try:
            user_input = Prompt.ask("\n[prompt]fabricagent[/prompt]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Exiting FabricAgent. Goodbye![/dim]")
            sys.exit(0)

        if not user_input:
            continue

        # Handle slash commands
        cmd = user_input.lower()
        if cmd in ("/exit", "/quit", "exit", "quit"):
            console.print("[dim]Exiting FabricAgent. Goodbye![/dim]")
            break
        elif cmd in ("/help", "help"):
            print_help()
            continue
        elif cmd in ("/clear", "clear"):
            history.clear()
            console.print("[dim]Conversation history cleared.[/dim]")
            continue
        elif cmd == "/demo":
            demo_loaded = True
            console.print("[success]✓ Loaded Retail Analytics dataset (DimCustomer, DimProduct, FactInternetSales).[/success]")
            continue
        elif cmd == "/status":
            provider = get_provider("assistant")
            model = get_model_name("assistant")
            console.print(
                f"[info]Provider:[/info] {provider} | [info]Model:[/info] {model} | "
                f"[info]Mode:[/info] {mode} | [info]Demo Dataset:[/info] {'Loaded' if demo_loaded else 'None'}"
            )
            continue
        elif cmd in ("/azure", "/connect", "/fabric"):
            from cli.fabric_cli import get_current_connection_info

            info = get_current_connection_info()
            if info["is_placeholder"] or not info["has_secret"]:
                console.print(
                    "[yellow]Azure Service Principal is not configured. Running in local mock mode (AdventureWorks demo).[/yellow]\n"
                    "[dim]To connect your Azure App Registration, run: [bold cyan]fabricagent fabric connect[/bold cyan][/dim]"
                )
            else:
                console.print(
                    f"[green]✓ Connected to Azure Tenant:[/green] {info['tenant_id']} | "
                    f"[green]Workspace:[/green] {info['workspace'] or '(default)'}"
                )
            continue
        elif cmd.startswith("/mode"):
            parts = cmd.split(maxsplit=1)
            if len(parts) > 1 and parts[1] in ("dashboard", "general"):
                mode = parts[1]
                console.print(f"[success]Switched mode to [bold]{mode}[/bold].[/success]")
            else:
                console.print("[warning]Usage: /mode [dashboard|general][/warning]")
            continue

        # Construct conversation prompt
        system = build_system_prompt(mode, demo_loaded)
        conversation = ""
        for h in history[-4:]:
            conversation += f"\nUser: {h['user']}\nAssistant: {h['assistant']}\n"
        full_user_turn = f"{conversation}\nUser: {user_input}".strip()

        console.print(Rule(style="dim"))
        try:
            chunks: list[str] = []
            with console.status("[bold cyan]Thinking & generating...[/bold cyan]", spinner="dots"):
                for chunk in stream_text(system, full_user_turn, agent="assistant"):
                    if isinstance(chunk, str):
                        chunks.append(chunk)

            reply = "".join(chunks).strip()
            console.print(Markdown(reply))
            console.print(Rule(style="dim"))
            history.append({"user": user_input, "assistant": reply})
        except Exception as exc:
            console.print(f"[error]Error:[/error] {exc}")
            console.print(Rule(style="dim"))
