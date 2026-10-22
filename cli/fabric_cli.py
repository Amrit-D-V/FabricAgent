from __future__ import annotations

from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

_PLACEHOLDERS = {
    "your_azure_client_id_here",
    "your_azure_client_secret_here",
    "your-client-id",
    "your-client-secret",
    "",
}


def _is_placeholder(val: str) -> bool:
    return not val or val.strip().lower() in _PLACEHOLDERS


def get_current_connection_info() -> dict[str, Any]:
    from fabric.fabric_connection import get_fabric_connection

    fc = get_fabric_connection()
    return {
        "tenant_id": fc.tenant_id,
        "client_id": fc.client_id,
        "has_secret": bool(fc.client_secret and not _is_placeholder(fc.client_secret)),
        "workspace": fc.workspace,
        "workspace_id": fc.workspace_id,
        "models": list(fc.models),
        "source": fc.source,
        "is_placeholder": _is_placeholder(fc.client_id) or _is_placeholder(fc.tenant_id),
    }


def test_azure_credentials(tenant_id: str, client_id: str, client_secret: str) -> tuple[bool, str, list]:
    """Test service principal credentials against Microsoft Entra ID and Power BI."""
    from fabric.powerbi_rest import list_workspaces, sp_powerbi_token

    try:
        token = sp_powerbi_token(tenant_id.strip(), client_id.strip(), client_secret.strip())
        workspaces = list_workspaces(token)
        return True, "Authentication successful", workspaces
    except Exception as exc:
        return False, str(exc), []


@click.group("fabric")
def fabric_group():
    """Manage Microsoft Fabric & Power BI Azure connection."""
    pass


@fabric_group.command("status")
def status_cmd():
    """Display current Azure Service Principal & Fabric connection status."""
    info = get_current_connection_info()

    if info["is_placeholder"] or not info["has_secret"]:
        console.print(
            Panel(
                "[bold yellow]Azure Service Principal is NOT configured.[/bold yellow]\n\n"
                "FabricAgent is currently operating in [bold green]Offline Mock Mode[/bold green] (AdventureWorks demo).\n\n"
                "[dim]To connect your Microsoft Azure App Registration and Power BI / Fabric tenant:[/dim]\n"
                "  Run: [bold cyan]fabricagent fabric connect[/bold cyan]",
                title="Fabric / Azure Status",
                border_style="yellow",
            )
        )
        return

    table = Table(title="Active Microsoft Fabric Connection", border_style="cyan")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Tenant ID", info["tenant_id"])
    table.add_row("Client ID", info["client_id"])
    table.add_row("Client Secret", "[green]Configured[/green]" if info["has_secret"] else "[red]Missing[/red]")
    table.add_row("Workspace", info["workspace"] or "[dim](none specified)[/dim]")
    table.add_row("Source", info["source"])

    console.print(table)

    # Test live reachability
    with console.status("[cyan]Verifying authentication with Microsoft Entra ID...[/cyan]"):
        from fabric.fabric_connection import get_fabric_connection

        fc = get_fabric_connection()
        success, msg, workspaces = test_azure_credentials(fc.tenant_id, fc.client_id, fc.client_secret)

    if success:
        console.print(f"[bold green]✓ Azure Authentication Verified![/bold green] Found {len(workspaces)} accessible workspace(s).")
        if workspaces:
            ws_table = Table(title="Accessible Workspaces", border_style="dim")
            ws_table.add_column("Workspace Name", style="bold cyan")
            ws_table.add_column("Workspace ID", style="dim")
            for name, wid in workspaces[:10]:
                ws_table.add_row(name, wid)
            console.print(ws_table)
    else:
        console.print(f"[bold red]✗ Azure Connection Error:[/bold red] {msg}")


@fabric_group.command("connect")
@click.option("--tenant-id", default=None, help="Directory (tenant) ID")
@click.option("--client-id", default=None, help="Application (client) ID")
@click.option("--client-secret", default=None, help="Client secret value")
@click.option("--workspace", default=None, help="Target Fabric / Power BI workspace name")
def connect_cmd(tenant_id: str | None, client_id: str | None, client_secret: str | None, workspace: str | None):
    """Interactively configure and test your Azure App Registration."""
    console.print(
        Panel(
            "[bold white]Microsoft Fabric & Power BI Connection Setup[/bold white]\n"
            "[dim]You will need an App Registration (Service Principal) in Microsoft Entra ID\n"
            "with access granted in the Power BI Admin Portal and your target workspace.[/dim]",
            border_style="cyan",
        )
    )

    t_id = tenant_id or click.prompt("Directory (tenant) ID", type=str)
    c_id = client_id or click.prompt("Application (client) ID", type=str)
    c_sec = client_secret or click.prompt("Client Secret", hide_input=True, type=str)

    console.print("\n[dim]Testing credentials against Microsoft Entra...[/dim]")
    success, msg, workspaces = test_azure_credentials(t_id, c_id, c_sec)

    if not success:
        console.print(f"\n[bold red]Authentication failed:[/bold red] {msg}")
        console.print("[yellow]Please check your Tenant ID, Client ID, and Client Secret in Azure Portal.[/yellow]")
        return

    console.print(f"[bold green]✓ Authentication successful![/bold green] Found {len(workspaces)} workspace(s).\n")

    ws_name = workspace
    if not ws_name and workspaces:
        ws_options = [w[0] for w in workspaces if w[0]]
        console.print("[bold]Available Workspaces:[/bold]")
        for i, w_name in enumerate(ws_options[:15], 1):
            console.print(f"  [{i}] {w_name}")
        choice = click.prompt("Select workspace number or type name", default="1")
        if choice.isdigit() and 1 <= int(choice) <= len(ws_options):
            ws_name = ws_options[int(choice) - 1]
        else:
            ws_name = choice

    # Save to database library
    try:
        from db.database import init_db, save_fabric_connection_entry

        init_db()
        conn_id = save_fabric_connection_entry(
            name=f"Azure-{ws_name or 'Default'}",
            tenant_id=t_id,
            client_id=c_id,
            client_secret=c_sec,
            workspace_name=ws_name or "",
            workspace_id="",
            models=[],
            catalogs={},
            make_default=True,
            updated_by="cli",
        )
        console.print(f"[bold green]✓ Saved active Fabric connection (ID: {conn_id}) to database![/bold green]")
    except Exception as exc:
        console.print(f"[yellow]Notice when saving to database: {exc}[/yellow]")

    console.print("\n[bold green]Ready![/bold green] You can now query your Fabric models directly in [cyan]fabricagent tui[/cyan].")


@fabric_group.command("workspaces")
def workspaces_cmd():
    """List all Power BI / Fabric workspaces accessible to the Service Principal."""
    from fabric.fabric_connection import get_fabric_connection

    fc = get_fabric_connection()
    if _is_placeholder(fc.client_id) or not fc.client_secret:
        console.print("[yellow]No Azure credentials configured. Run 'fabricagent fabric connect' first.[/yellow]")
        return

    success, msg, workspaces = test_azure_credentials(fc.tenant_id, fc.client_id, fc.client_secret)
    if not success:
        console.print(f"[bold red]Error listing workspaces:[/bold red] {msg}")
        return

    table = Table(title=f"Workspaces ({len(workspaces)})", border_style="cyan")
    table.add_column("Workspace Name", style="bold cyan")
    table.add_column("Workspace ID", style="dim")
    for name, wid in workspaces:
        table.add_row(name, wid)
    console.print(table)
