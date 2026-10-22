from __future__ import annotations

from click.testing import CliRunner
from cli.main import cli


def test_cli_version():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "fabricagent" in result.output.lower() or "fabricbi" in result.output.lower()


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "FabricAgent" in result.output or "FabricBI" in result.output
    assert "setup" in result.output
    assert "server" in result.output
    assert "user" in result.output
    assert "validate" in result.output
    assert "tui" in result.output
    assert "chat" in result.output


def test_cli_user_invalid_email():
    runner = CliRunner()
    result = runner.invoke(cli, ["user", "create", "invalid_email_no_at"])
    assert result.exit_code != 0
    assert "valid email" in result.output.lower()


def test_cli_fabric_status():
    runner = CliRunner()
    result = runner.invoke(cli, ["fabric", "status"])
    assert result.exit_code == 0
    assert ("Fabric / Azure Status" in result.output or
            "Microsoft Fabric Connection" in result.output)


def test_cli_fabric_workspaces_unconfigured():
    runner = CliRunner()
    result = runner.invoke(cli, ["fabric", "workspaces"])
    assert result.exit_code == 0
    assert "No Azure credentials" in result.output or "Workspaces" in result.output


def test_cli_setup_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["setup", "--help"])
    assert result.exit_code == 0
    assert "--non-interactive" in result.output
    assert "--provider" in result.output


def test_install_and_start_scripts_exist():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    assert (root / "install.sh").is_file()
    assert (root / "install.bat").is_file()
    assert (root / "start.sh").is_file()
    assert (root / "start.bat").is_file()
