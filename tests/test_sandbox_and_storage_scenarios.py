"""Scenario matrix for the two invariants that have already been violated once.

1. The sandbox deny list. Blocking shell and filesystem tools is not enough on
   its own: any tool that can spawn an agent hands that agent a fresh
   unrestricted toolset. Workflow was missing once, and the assistant found it
   -- blocked from Read/Edit/Bash three times, it fell back to Workflow to
   patch a report.json on disk. The API server and Streamlit had also drifted
   apart, with one blocking Workflow and the other not.

2. The SQLite journal mode. WAL coordinates readers through a shared-memory
   -shm file that SMB and NFS do not implement, so /data on Container Apps
   cannot run WAL -- while /data on the VM is ext4 and should. The guard has to
   key on the FILESYSTEM, not the path, or it wrongly refuses to boot on the VM.

Both are config, and config has no type checker.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from config import CLAUDE_CLI_DISALLOWED_TOOLS

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / "venv" / "Scripts" / "python.exe"
PYTHON = str(VENV_PY) if VENV_PY.exists() else sys.executable


# --- The deny list ----------------------------------------------------------


def test_the_deny_list_has_no_duplicates():
    """A duplicate is a merge artefact and hides how many tools are really
    covered."""
    assert len(CLAUDE_CLI_DISALLOWED_TOOLS) == len(set(CLAUDE_CLI_DISALLOWED_TOOLS))


@pytest.mark.parametrize("tool", ["Task", "Agent", "Workflow", "Skill", "SlashCommand"])
def test_every_delegation_tool_is_denied(tool):
    """These are the escape hatches: each one can start a sub-agent that gets a
    fresh, unrestricted toolset. Denying the shell while leaving one of these
    open denies nothing."""
    assert tool in CLAUDE_CLI_DISALLOWED_TOOLS


@pytest.mark.parametrize("tool", ["Bash", "PowerShell", "KillShell", "BashOutput"])
def test_every_shell_tool_is_denied(tool):
    """PowerShell is listed separately from Bash on purpose -- omitting it once
    let the agent Remove-Item a report."""
    assert tool in CLAUDE_CLI_DISALLOWED_TOOLS


@pytest.mark.parametrize(
    "tool", ["Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep"]
)
def test_every_filesystem_tool_is_denied(tool):
    assert tool in CLAUDE_CLI_DISALLOWED_TOOLS


@pytest.mark.parametrize("tool", ["WebFetch", "WebSearch"])
def test_network_tools_are_denied(tool):
    assert tool in CLAUDE_CLI_DISALLOWED_TOOLS


@pytest.mark.parametrize(
    "tool", ["ScheduleWakeup", "CronCreate", "CronDelete", "CronList"]
)
def test_scheduling_tools_are_denied(tool):
    """A scheduled run is a delegated run that outlives the turn."""
    assert tool in CLAUDE_CLI_DISALLOWED_TOOLS


def test_the_api_server_derives_its_list_from_the_same_constant():
    """The two had already drifted once. Deriving, not re-listing, is the
    fix -- so assert the derivation still holds rather than that the strings
    happen to match today."""
    from api import chat as chat_api_server

    derived = set(chat_api_server.DISALLOWED_TOOLS.split(","))
    assert set(CLAUDE_CLI_DISALLOWED_TOOLS) <= derived


def test_the_api_server_adds_todowrite_without_duplicating():
    from api import chat as chat_api_server

    parts = chat_api_server.DISALLOWED_TOOLS.split(",")
    assert "TodoWrite" in parts
    assert len(parts) == len(set(parts))


def test_the_api_server_does_not_maintain_its_own_literal_list():
    """A second hard-coded list is how the drift happened. The module must
    import the constant, not restate it."""
    src = (ROOT / "api" / "chat.py").read_text(encoding="utf-8")
    assert "from config import CLAUDE_CLI_DISALLOWED_TOOLS" in src


# --- Journal mode selection -------------------------------------------------


def _journal_mode_with(env_value):
    """Import db.database in a fresh interpreter -- the mode is resolved at
    import time, so it cannot be re-read in-process."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    if env_value is None:
        env.pop("SQLITE_JOURNAL_MODE", None)
    else:
        env["SQLITE_JOURNAL_MODE"] = env_value
    return subprocess.run(
        [PYTHON, "-c", "import db.database as d; print(d.SQLITE_JOURNAL_MODE)"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_wal_is_the_default_when_nothing_is_set():
    """Local disk is the common case and WAL is right there. The override
    exists only for the network-share deployment."""
    r = _journal_mode_with(None)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "WAL"


@pytest.mark.parametrize(
    "value", ["WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"]
)
def test_every_supported_mode_is_accepted(value):
    r = _journal_mode_with(value)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == value


@pytest.mark.parametrize(
    "value,expected",
    [("wal", "WAL"), ("  delete  ", "DELETE"), ("Truncate", "TRUNCATE")],
    ids=["lowercase", "padded", "mixed case"],
)
def test_case_and_whitespace_are_normalised(value, expected):
    """An env var set from a YAML file often arrives with stray whitespace;
    that must not be a boot failure."""
    r = _journal_mode_with(value)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expected


@pytest.mark.parametrize("value", ["FAST", "wal2", "", "DELETE;DROP", "NONE"])
def test_an_unsupported_mode_fails_loudly_at_import(value):
    """The alternative is PRAGMA journal_mode silently ignoring the value and
    the deployment running WAL on a share, which corrupts on the first
    concurrent read."""
    r = _journal_mode_with(value)
    assert r.returncode != 0
    assert "SQLITE_JOURNAL_MODE" in r.stderr


# --- The container entrypoint guard -----------------------------------------

HAS_DEPLOY = (ROOT / "deploy").is_dir()
HAS_DOCKER = (ROOT / "Dockerfile").is_file()
HAS_DOCKERIGNORE = (ROOT / ".dockerignore").is_file()

ENTRYPOINTS = ["deploy/aca/entrypoint.sh", "deploy/vm/entrypoint.sh"]


@pytest.mark.skipif(not HAS_DEPLOY, reason="deploy/ directory not present")
@pytest.mark.parametrize("rel", ENTRYPOINTS)
def test_the_guard_tests_the_filesystem_not_the_path(rel):
    """/data is SMB on Container Apps and ext4 on the VM. A path check would
    refuse to boot the VM, where WAL is correct and wanted."""
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "stat -f -c %T" in src
    assert re.search(r"smb\*\|cifs\|nfs\*", src), "network filesystems not matched"


@pytest.mark.skipif(not HAS_DEPLOY, reason="deploy/ directory not present")
@pytest.mark.parametrize("rel", ENTRYPOINTS)
def test_the_guard_refuses_to_start_rather_than_warning(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    block = src.split("stat -f -c %T", 1)[1]
    assert "exit 1" in block.split("esac", 1)[0]


@pytest.mark.skipif(not HAS_DEPLOY, reason="deploy/ directory not present")
def test_the_two_entrypoint_guards_have_not_drifted():
    """Same failure mode as the deny list: two copies of one rule, edited
    once."""
    blocks = []
    for rel in ENTRYPOINTS:
        src = (ROOT / rel).read_text(encoding="utf-8")
        start = src.index("DATA_FS=")
        end = src.index("esac", src.index("esac", start) + 4) + 4
        blocks.append(re.sub(r"\s+", " ", src[start:end]))
    assert blocks[0] == blocks[1]


SH = shutil.which("sh")


def _run_guard(fs_type, db_path, journal_mode=None):
    """Execute the real guard block from deploy/vm/entrypoint.sh with `stat`
    stubbed to report the filesystem we want to simulate."""
    src = (ROOT / "deploy/vm/entrypoint.sh").read_text(encoding="utf-8")
    start = src.index("DATA_FS=")
    end = src.index("esac", src.index("esac", start) + 4) + 4
    block = src[start:end]
    script = f'stat() {{ echo "{fs_type}"; }}\nDATA_DIR=/data\n' + block
    env = dict(os.environ)
    env["DB_PATH"] = db_path
    if journal_mode is None:
        env.pop("SQLITE_JOURNAL_MODE", None)
    else:
        env["SQLITE_JOURNAL_MODE"] = journal_mode
    return subprocess.run(
        [SH, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


@pytest.mark.skipif(
    SH is None or not HAS_DEPLOY, reason="POSIX sh or deploy/ not available"
)
@pytest.mark.parametrize("fs_type", ["smb2", "cifs", "nfs", "nfs4"])
def test_wal_on_a_network_share_aborts_the_boot(fs_type):
    r = _run_guard(fs_type, "/data/project_data.db", "WAL")
    assert r.returncode == 1
    assert "network filesystem" in r.stdout


@pytest.mark.skipif(
    SH is None or not HAS_DEPLOY, reason="POSIX sh or deploy/ not available"
)
@pytest.mark.parametrize("fs_type", ["smb2", "cifs", "nfs"])
def test_delete_mode_on_a_share_boots(fs_type):
    r = _run_guard(fs_type, "/data/project_data.db", "DELETE")
    assert r.returncode == 0
    assert "journal_mode=DELETE" in r.stdout


@pytest.mark.skipif(
    SH is None or not HAS_DEPLOY, reason="POSIX sh or deploy/ not available"
)
@pytest.mark.parametrize("fs_type", ["ext2/ext3", "xfs", "btrfs", "overlayfs"])
def test_wal_on_a_local_filesystem_boots(fs_type):
    """The VM case. A path-based guard would have failed here."""
    r = _run_guard(fs_type, "/data/project_data.db", "WAL")
    assert r.returncode == 0
    assert "journal_mode=WAL" in r.stdout


@pytest.mark.skipif(
    SH is None or not HAS_DEPLOY, reason="POSIX sh or deploy/ not available"
)
def test_an_unset_journal_mode_on_a_share_is_still_caught():
    """Unset means WAL by default, which is the dangerous case -- the guard has
    to substitute the default before comparing."""
    r = _run_guard("cifs", "/data/project_data.db", None)
    assert r.returncode == 1


@pytest.mark.skipif(
    SH is None or not HAS_DEPLOY, reason="POSIX sh or deploy/ not available"
)
def test_a_database_outside_the_data_dir_is_not_policed():
    """An ephemeral in-container DB is a different (already warned about)
    problem; the share guard must not claim it."""
    r = _run_guard("cifs", "/app/project_data.db", "WAL")
    assert r.returncode == 0


# --- Which Dockerfile a remote deploy uses ----------------------------------


@pytest.mark.skipif(not HAS_DOCKER, reason="Dockerfile not present")
def test_the_root_dockerfile_is_the_plain_local_image():
    """It runs the API directly with no .NET and no nginx. Render and Cloud Run
    both auto-detect a root Dockerfile, so this keeps the distinction visible."""
    src = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "api.server" in src
    assert "adomd" not in src.lower()
    assert "nginx" not in src.lower()


@pytest.mark.skipif(not HAS_DOCKER and not HAS_DEPLOY, reason="Dockerfiles not present")
def test_no_dockerfile_still_launches_streamlit():
    """Streamlit is gone; a lingering CMD would fail at run time, not build."""
    for path in ("Dockerfile", "deploy/render/Dockerfile", "deploy/cloudrun/Dockerfile"):
        p = ROOT / path
        if p.exists():
            assert "streamlit" not in p.read_text(encoding="utf-8").lower(), path


@pytest.mark.skipif(not HAS_DEPLOY, reason="deploy/ not present")
def test_the_deploy_images_build_the_react_app():
    """nginx serves web/dist, so it has to exist in the runtime image."""
    for path in ("deploy/render/Dockerfile", "deploy/cloudrun/Dockerfile"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "npm run build" in src, path
        assert "--from=web /build/dist" in src, path


def test_render_pins_its_dockerfile_away_from_the_root_one():
    """render.yaml must live at the repo root (Render only reads a Blueprint
    from there) and must override the Dockerfile Render would otherwise
    auto-detect beside it."""
    src = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "dockerfilePath" in src
    assert "deploy/render/Dockerfile" in src


@pytest.mark.skipif(not HAS_DEPLOY, reason="deploy/ not present")
def test_cloud_run_builds_from_its_own_dockerfile():
    src = (ROOT / "deploy/cloudrun/cloudbuild.yaml").read_text(encoding="utf-8")
    assert "deploy/cloudrun/Dockerfile" in src


@pytest.mark.skipif(not HAS_DEPLOY, reason="deploy/ not present")
@pytest.mark.parametrize("rel", ["deploy/vm/Dockerfile", "deploy/aca/Dockerfile"])
def test_the_deployment_images_are_amd64(rel):
    """The ADOMD NuGet package is netcore.retail.amd64. An arm64 image boots
    fine and fails every XMLA call."""
    src = (ROOT / rel).read_text(encoding="utf-8").lower()
    assert "amd64" in src or "x86_64" in src


@pytest.mark.skipif(not HAS_DOCKERIGNORE, reason=".dockerignore not present")
def test_dockerignore_keeps_lib_out_of_the_image():
    """The ADOMD DLLs are placed at /app/lib during the build; a copied-in
    lib/ would clobber them."""
    src = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert re.search(r"^lib/?$", src, re.MULTILINE)
