from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path


def _skills_roots() -> list[Path]:
    roots: list[Path] = []
    local = Path(__file__).resolve().parent / "skills"
    if local.is_dir():
        roots.append(local)
    try:
        import pbi_cli

        roots.append(Path(pbi_cli.__file__).resolve().parent / "skills")
    except Exception:
        pass
    return roots


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    meta: dict[str, str] = {}
    if not m:
        return meta
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line or line[0] in " \t":
            continue
        key, sep, val = line.partition(":")
        if not sep:
            continue
        val = val.strip()
        if val in (">", ">-", "|", "|-"):
            # YAML block scalar: consume the following indented lines.
            block: list[str] = []
            while i < len(lines) and (not lines[i] or lines[i][0] in " \t"):
                block.append(lines[i].strip())
                i += 1
            joiner = "\n" if val.startswith("|") else " "
            val = joiner.join(b for b in block if b).strip()
        meta[key.strip()] = val
    return meta


@lru_cache(maxsize=1)
def _index() -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for root in _skills_roots():
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            f = d / "SKILL.md"
            if not f.is_file() or d.name in idx:
                continue
            meta = _frontmatter(f.read_text(encoding="utf-8"))
            idx[d.name] = {
                "slug": d.name,
                "name": meta.get("name", d.name),
                "description": meta.get("description", ""),
                "path": str(f),
                "source": "built-in",
            }
    return idx


def _full_index(include_custom: bool = True) -> dict[str, dict]:
    idx = dict(_index())  # shallow copy so we don't mutate the cache

    if include_custom:
        try:
            from db.database import list_custom_skills

            for cs in list_custom_skills(active_only=True):
                idx[cs["slug"]] = {
                    "slug": cs["slug"],
                    "name": cs["name"],
                    "description": cs["description"],
                    "path": None,  # stored in DB, not filesystem
                    "source": "custom",
                    "db_id": cs["id"],
                    "body": cs["body"],
                }
        except Exception:
            pass  # DB not available — degrade gracefully

    return idx


def invalidate_cache() -> None:
    _index.cache_clear()


def list_skills() -> list[dict[str, str]]:
    return [
        {
            "slug": s["slug"],
            "name": s["name"],
            "description": s["description"],
            "source": s.get("source", "built-in"),
        }
        for s in sorted(_full_index().values(), key=lambda s: s["slug"])
    ]


def list_skills_text() -> str:
    skills = list_skills()
    if not skills:
        return "(no skills found)"
    return "\n".join(f"- {s['slug']} — {s['description']}" for s in skills)


def list_skills_for_project(project_id: int | None) -> list[dict[str, str]]:
    all_skills = list_skills()
    if project_id is None:
        return all_skills

    try:
        from db.database import get_project_skills

        overrides = get_project_skills(project_id)
    except Exception:
        return all_skills

    if not overrides:
        return all_skills

    return [s for s in all_skills if overrides.get(s["slug"], True)]


def list_skills_text_for_project(project_id: int | None) -> str:
    skills = list_skills_for_project(project_id)
    if not skills:
        return "(no skills available for this project)"
    return "\n".join(f"- {s['slug']} — {s['description']}" for s in skills)


def load_skill(skill: str, project_id: int | None = None) -> str:
    key = (skill or "").strip().lower()
    # "slug/reference-name" loads a file from the skill's references/ folder
    # (progressive disclosure: SKILL.md lists what is available).
    ref = None
    if "/" in key:
        key, _, ref = key.partition("/")
        ref = ref.strip().removesuffix(".md")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", ref):
            return f"ERROR: invalid reference name {ref!r}."
    full = _full_index()
    match = next(
        (s for s in full.values() if key in (s["slug"].lower(), s["name"].lower())),
        None,
    )
    if match is None:
        avail = ", ".join(sorted(full))
        return f"ERROR: unknown skill '{skill}'. Available: {avail}"

    if project_id is not None:
        try:
            from db.database import get_project_skills

            overrides = get_project_skills(project_id)
            if not overrides.get(match["slug"], True):
                return (
                    f"ERROR: skill '{match['slug']}' is disabled for this project. "
                    f"Ask an admin to enable it under Project Management -> Skills."
                )
        except Exception:
            pass

    if ref is not None:
        if not match.get("path"):
            return f"ERROR: skill '{match['slug']}' has no reference files."
        ref_path = Path(match["path"]).parent / "references" / f"{ref}.md"
        if not ref_path.is_file():
            refs_dir = Path(match["path"]).parent / "references"
            avail = (
                ", ".join(sorted(p.stem for p in refs_dir.glob("*.md")))
                if refs_dir.is_dir()
                else "(none)"
            )
            return (
                f"ERROR: no reference '{ref}' in skill '{match['slug']}'. "
                f"Available: {avail}"
            )
        return ref_path.read_text(encoding="utf-8")

    if match.get("source") == "custom" and match.get("body"):
        return (
            f"---\nname: {match['name']}\n"
            f"description: {match['description']}\n---\n\n"
            f"{match['body']}"
        )

    if match.get("path"):
        return Path(match["path"]).read_text(encoding="utf-8")

    return f"ERROR: skill '{skill}' has no content."
