from __future__ import annotations

import re
from pathlib import Path


def project_slug(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name or "").strip("_")


def _store() -> Path:
    from config import PROJECTS_DIR

    return Path(PROJECTS_DIR).resolve()


def _scope() -> tuple[Path | None, str | None]:
    from auth.tool_access import authorized_project

    store = _store()
    project = authorized_project()
    if project is None:
        return store, None  # Standalone operator CLI.
    slug = project_slug(project)
    if not slug:
        return None, project
    root = store / slug
    # A project-directory symlink must not turn one client's scope into another.
    if root.resolve() != root:
        return None, project
    return root, project


def resolve_dest(raw: str, active_project: str = "") -> tuple[Path | None, str]:
    store = _store()
    scope, authorized = _scope()
    if scope is None:
        return None, "ERROR — select an authorized project before accessing reports."
    if authorized is not None:
        active_project = authorized
    cleaned = (raw or "").replace("\\", "/")

    name = Path(cleaned).name.strip()
    if not name:
        return None, "ERROR — no file name given."
    if not name.lower().endswith(".rdl"):
        name += ".rdl"

    parts = [x for x in Path(cleaned).parts if x not in ("/", "")]
    project = ""
    if Path(cleaned).is_absolute():
        try:  # an absolute path already inside the store names its own project
            project = Path(cleaned).resolve().relative_to(store).parts[0]
        except (ValueError, IndexError, OSError):
            project = ""
    elif len(parts) >= 2 and parts[0] != "paginated":
        project = parts[0]

    project = project_slug(project) or project_slug(active_project)
    if not project:
        return None, (
            "ERROR — no project to write into. Select a project in the sidebar, "
            "or pass a path like '<project>/paginated/<Report>.rdl'."
        )

    dest = (store / project / "paginated" / name).resolve()
    if dest.suffix.lower() != ".rdl":
        return None, "ERROR — the resolved file must be an .rdl report."
    if not dest.is_relative_to(scope):
        return None, "ERROR — that report is outside the authorized project."
    return dest, ""


def find_existing(raw: str, active_project: str = "") -> Path | None:
    scope, _authorized = _scope()
    if scope is None:
        return None
    if raw:
        try:
            cand = Path(raw.replace("\\", "/")).resolve()
            if (
                cand.suffix.lower() == ".rdl"
                and cand.is_relative_to(scope)
                and cand.is_file()
            ):
                return cand
        except (OSError, ValueError):
            pass
    dest, _err = resolve_dest(raw, active_project)
    return dest if dest and dest.is_file() else None


def all_rdls() -> list[str]:
    scope, _authorized = _scope()
    if scope is None or not scope.exists():
        return []
    found = set()
    for candidate in scope.rglob("*"):
        if candidate.suffix.lower() != ".rdl" or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved.suffix.lower() == ".rdl" and resolved.is_relative_to(scope):
            found.add(str(resolved))
    return sorted(found)
