from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from cli import __version__


@click.group()
@click.version_option(version=__version__, prog_name="fabricagent (fabricbi)")
def cli():
    """FabricAgent (FabricBI) — Autonomous AI platform for Microsoft Power BI & Fabric."""
    pass


@cli.command("setup")
@click.option("--non-interactive", "-y", is_flag=True, help="Run setup non-interactively with defaults.")
@click.option("--provider", type=click.Choice(["gemini", "ollama", "openai", "groq", "anthropic", "mock"]), help="Preset AI provider")
@click.option("--api-key", help="API key for selected provider")
def setup_cmd(non_interactive: bool, provider: str | None, api_key: str | None):
    """Run interactive configuration & setup wizard."""
    from cli.setup_wizard import run_setup_wizard

    run_setup_wizard(non_interactive=non_interactive, provider_choice=provider, api_key=api_key)


from cli.fabric_cli import fabric_group  # noqa: E402

cli.add_command(fabric_group)


@cli.command("server")
@click.option("--host", default=None, help="Host to bind (default: FABRICBI_AGENT_HOST or 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Port to listen on (default: FABRICBI_AGENT_PORT or 8756)")
@click.option("--reload", is_flag=True, help="Enable auto-reload for development.")
def run_server(host: str | None, port: int | None, reload: bool):
    """Start the FabricBI API server."""
    import uvicorn
    from api.server import app, _require_auth_when_shared, preflight_check
    from db.database import init_db

    bind_host = host or os.getenv("FABRICBI_AGENT_HOST", "127.0.0.1").strip()
    bind_port = port or int(os.getenv("FABRICBI_API_PORT", os.getenv("FABRICBI_AGENT_PORT", "8756")))

    if reload:
        os.environ["FABRICBI_DEV"] = "1"

    _require_auth_when_shared()
    init_db()
    preflight_check()

    click.echo(f"Starting FabricBI server on http://{bind_host}:{bind_port} (reload={reload})...")
    if reload:
        uvicorn.run("api.server:app", host=bind_host, port=bind_port, log_level="info", reload=True)
    else:
        uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")


@cli.group("user")
def user_group():
    """Manage FabricBI users and authorizations."""
    pass


@user_group.command("create")
@click.argument("email")
@click.option("--role", default="editor", type=click.Choice(["viewer", "editor", "admin"]), help="User role (default: editor)")
def create_user_cmd(email: str, role: str):
    """Authorise an email address to sign in via Microsoft Entra SSO."""
    from db.database import audit, create_app_user, init_db

    normalized_email = email.lower().strip()
    if "@" not in normalized_email:
        click.echo("Error: not a valid email address.", err=True)
        sys.exit(1)

    init_db()
    try:
        create_app_user(normalized_email, "", role=role)
        audit("user.create", target=normalized_email, details=f"role={role} (sso)", user_email="cli")
        click.echo(f"Authorised user: {normalized_email} (role: {role})")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command("validate")
@click.argument("path", type=click.Path(exists=True))
def validate_model_cmd(path: str):
    """Run TMDL doctor and Best Practice Analyzer (BPA) checks on a folder or file."""
    target = Path(path)
    click.echo(f"Validating model definitions in: {target}...")

    from model.tmdl_doctor import diagnose_and_fix

    if target.is_file():
        text = target.read_text(encoding="utf-8", errors="ignore")
        res = diagnose_and_fix(text, target.name)
        if res.get("issues"):
            click.echo(f"Found {len(res['issues'])} issues:")
            for issue in res["issues"]:
                click.echo(f"  - [{issue.get('severity', 'WARN')}] {issue.get('message')}")
        else:
            click.echo("✓ TMDL validation passed: No issues found.")
    else:
        # Folder scan
        tmdl_files = list(target.rglob("*.tmdl"))
        click.echo(f"Scanned {len(tmdl_files)} TMDL file(s).")
        total_issues = 0
        for f in tmdl_files:
            text = f.read_text(encoding="utf-8", errors="ignore")
            res = diagnose_and_fix(text, f.name)
            issues = res.get("issues") or []
            if issues:
                total_issues += len(issues)
                click.echo(f"File {f.name}: {len(issues)} issue(s)")
                for iss in issues:
                    click.echo(f"  - {iss.get('message')}")
        if total_issues == 0:
            click.echo("✓ All TMDL files verified cleanly.")


@cli.command("tui")
@click.option("--mode", default="dashboard", type=click.Choice(["dashboard", "general"]), help="Initial assistant mode")
def tui_cmd(mode: str):
    """Launch the interactive FabricBI Terminal User Interface (TUI)."""
    from cli.tui import start_tui

    start_tui(initial_mode=mode)


@cli.command("chat")
@click.argument("prompt", required=False)
@click.option("--mode", default="dashboard", type=click.Choice(["dashboard", "general"]), help="Assistant mode")
def chat_cmd(prompt: str | None, mode: str):
    """Ask a question or enter interactive terminal chat."""
    from cli.tui import run_single_query, start_tui

    if prompt:
        run_single_query(prompt, mode=mode)
    else:
        start_tui(initial_mode=mode)


if __name__ == "__main__":
    cli()
