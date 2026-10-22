from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field

from config import OPENAI_COMPATIBLE_PROVIDERS
from db.database import (
    get_project_context_block,
    get_project_requirements,
    list_documents,
    read_document_text,
    set_project_requirements,
)
from llm.llm_client import call_with_images, get_provider, stream_text

_PER_DOC_CHARS = 30000
_MAX_CORPUS = 120000
_MAX_IMAGES = 8
_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_SYSTEM = (
    "You are a BI requirements analyst. From the project's source material you "
    "produce a single, concise, authoritative requirements spec for a Power BI "
    "dashboard project. Attached images — mockups, screenshots, wireframes — are "
    "source documents too: read the titles, KPI names, chart types, axis labels, "
    "and filters visible in them. Use ONLY information present in the material "
    "provided — never invent KPIs, columns, or filters. Preserve exact names "
    "(reports, KPIs, measures, columns, filters) as written. If something isn't "
    "stated, omit it or list it under Open Questions.\n\n"
    "Grounding rules when a database schema or an existing semantic model is "
    "provided:\n"
    "- Every KPI and dimension you list must name the real table and column that "
    "backs it, using the exact names from the schema.\n"
    "- A KPI the documents ask for that has no backing column goes under Open "
    "Questions, flagged as unbacked — do not quietly drop it and do not invent a "
    "column for it.\n"
    "- A measure that already exists in the semantic model is a REUSE, not a "
    "build. List it under Existing Assets to Reuse with its exact name.\n"
    "- Org memory shows what this team built on other projects. Prefer their "
    "established names and patterns over inventing new ones, but never present "
    "org memory as a requirement of THIS project.\n\n"
    "Output GitHub-flavored Markdown only."
)

_INSTRUCTION = """Distil the material below into a requirements spec with these
sections (omit a section if the material says nothing about it):

## Objective
## Business Understanding   (what the business does, who decides what, the
                             decisions this dashboard has to support)
## Audience
## Key Metrics / KPIs   (name — definition/formula if given — backing table.column)
## Dimensions & Grouping
## Filters / Slicers
## Pages & Visuals requested
## Data Sources / Tables
## Existing Assets to Reuse   (measures/tables already in the semantic model)
## Constraints & Branding
## Open Questions   (include every unbacked KPI, flagged as such)
## Decisions   (reproduce every ESTABLISHED DECISION below verbatim, if any)

Anything under ESTABLISHED DECISIONS has already been answered by the team.
Treat it as authoritative fact, apply it to the sections above, and never list
it as an open question again.

Be specific and terse — bullet points, not prose. This spec is the shared
contract handed to the wireframe, semantic-model, and chat agents.

{sections}
"""

_NO_TEXT = "(No readable text documents — extract everything from the attached images.)"


@dataclass
class ExtractionResult:
    text: str = ""
    read: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    graphed: int = 0


def _media_type(doc) -> str:
    ext = os.path.splitext(doc.file_path)[1].lower()
    if ext in _IMAGE_MEDIA_TYPES:
        return _IMAGE_MEDIA_TYPES[ext]
    file_type = (doc.file_type or "").lower()
    return file_type if file_type.startswith("image/") else ""


def _encode_image(path: str, media_type: str) -> dict | None:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None
    if not data or len(data) > _IMAGE_MAX_BYTES:
        return None
    return {
        "base64": base64.b64encode(data).decode("ascii"),
        "media_type": media_type,
        "name": os.path.basename(path),
        "path": os.path.abspath(path),
    }


def _gather(project_id: int) -> tuple[str, list[dict], ExtractionResult]:
    result = ExtractionResult()
    parts: list[str] = []
    images: list[dict] = []

    for d in list_documents(project_id):
        media_type = _media_type(d)
        if media_type:
            if len(images) >= _MAX_IMAGES:
                result.skipped.append(
                    f"{d.original_name} — over the {_MAX_IMAGES}-image limit"
                )
                continue
            encoded = _encode_image(d.file_path, media_type)
            if not encoded:
                result.skipped.append(
                    f"{d.original_name} — unreadable, empty, or larger than 5 MB"
                )
                continue
            images.append(encoded)
            result.images.append(d.original_name)
            continue

        text = read_document_text(d)
        if text.startswith(f"[{d.original_name}:"):
            result.skipped.append(text.strip("[]"))
            continue
        if not text.strip():
            result.skipped.append(f"{d.original_name} — produced no text")
            continue
        if len(text) > _PER_DOC_CHARS:
            result.truncated.append(
                f"{d.original_name} — {len(text):,} chars cut to {_PER_DOC_CHARS:,}"
            )
        parts.append(f"### {d.original_name}\n{text[:_PER_DOC_CHARS]}")
        result.read.append(d.original_name)

    corpus = "\n\n".join(parts)
    if len(corpus) > _MAX_CORPUS:
        result.truncated.append(
            f"combined text — {len(corpus):,} chars cut to {_MAX_CORPUS:,}, so "
            f"later documents were dropped"
        )
        corpus = corpus[:_MAX_CORPUS]
    return corpus, images, result


# Providers that take images inline as base64 in the request body.
def _supports_vision(provider: str) -> bool:
    return provider == "anthropic" or provider in OPENAI_COMPATIBLE_PROVIDERS


# Providers that are agents with filesystem access — they can't take inline
# images, but they can open the file themselves when given the path.
# Only claude_cli: stream_text plumbs allow_read through to `--allowedTools Read`
# for it. codex_cli drops the flag, so it would be told to read files it may not
# be able to open.
_READ_TOOL_PROVIDERS = {"claude_cli"}


def _org_memory(project_id: int) -> str:
    try:
        from db import graph_memory

        parts = [
            graph_memory.recall(project_id),
            graph_memory.recall_kpis(project_id),
            graph_memory.recall_sources(project_id),
        ]
        return "\n".join(p for p in parts if p).strip()
    except Exception:
        return ""


# Section heading -> how that section's bullets land in the graph. Headings come
# from _INSTRUCTION, so this mapping is only as stable as that prompt.
# A KPI line is "name — definition — backing table.column". Em/en dash only:
# a plain hyphen also appears inside formulas ("(Revenue - Cost) / Revenue") and
# splitting on it mangles them. Reader and writer share these so a bound KPI
# always round-trips back through parse_spec().
_KPI_SPLIT = re.compile(r"\s+[—–]\s+")
KPI_JOIN = " — "
_NO_DEFINITION = "definition not given"

_HEADING_KPI = "key metrics / kpis"
_HEADING_OPEN_Q = "open questions"
_HEADING_DECISIONS = "decisions"

# parse_spec() normalises a line with lstrip(_BULLET_CHARS), which strips a *set*
# of characters — so "- **Bold question." loses "- **", not just the dash. Writers
# must strip identically or they will never match what the reader produced.
_BULLET_CHARS = "-*• "

# ...but that set-strip is a *matching* key, never something to write back: it
# eats the opening "**" of a bold bullet and leaves the closing one dangling.
# A markdown list marker is one character followed by whitespace, which is what
# tells the "*" in "* item" apart from the one starting "**bold**".
_LIST_MARKER = re.compile(r"^(?:[-•]|\*(?=\s))\s*")

_SECTION_KINDS = {
    "key metrics / kpis": "kpi",
    "dimensions & grouping": "dimension",
    "filters / slicers": "filter",
    "pages & visuals requested": "page",
    "data sources / tables": "data_source",
    "existing assets to reuse": "reuse",
    "constraints & branding": "constraint",
    "open questions": "open_question",
}


def _clean(part: str) -> str:
    return part.strip(" `")


def _split_bullet(raw: str) -> tuple[str, str]:
    indent = raw[: len(raw) - len(raw.lstrip())]
    stripped = raw.strip()
    body = stripped.lstrip(_BULLET_CHARS)
    return indent + stripped[: len(stripped) - len(body)], body.strip()


def _bullet_body(raw: str) -> str:
    return _LIST_MARKER.sub("", raw.strip(), count=1).strip()


def _join(lines: list[str], original: str) -> str:
    text = "\n".join(lines)
    return text + "\n" if original.endswith("\n") else text


def _heading_of(raw: str) -> str | None:
    line = raw.strip()
    return line.lstrip("#").strip().lower() if line.startswith("#") else None


def parse_spec(text: str) -> dict:
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            current = line.lstrip("#").strip().lower()
            sections.setdefault(current, [])
            continue
        if not current or not line:
            continue
        item = line.lstrip("-*• ").strip()
        if not item:
            continue  # a "---" rule strips to nothing — not a real item
        sections[current].append(item)

    def prose(name: str) -> str:
        return " ".join(sections.get(name, [])).strip()

    kpis: list[dict] = []
    for line in sections.get(_HEADING_KPI, []):
        parts = [_clean(p) for p in _KPI_SPLIT.split(line, maxsplit=2)]
        if not parts or not parts[0]:
            continue
        kpis.append(
            {
                "name": parts[0],
                "definition": parts[1] if len(parts) > 1 else "",
                "backing": parts[2] if len(parts) > 2 else "",
            }
        )

    items: list[dict] = []
    for heading, kind in _SECTION_KINDS.items():
        if kind == "kpi":
            continue
        for line in sections.get(heading, []):
            items.append({"kind": kind, "text": line})

    return {
        "objective": prose("objective"),
        "business": prose("business understanding"),
        "audience": prose("audience"),
        "kpis": kpis,
        "items": items,
    }


def bind_kpi(text: str, kpi_name: str, backing: str) -> str:
    backing = _clean(backing or "")
    if not (text and kpi_name and backing):
        return text

    out: list[str] = []
    in_section = False
    bound = False
    for raw in text.splitlines():
        heading = _heading_of(raw)
        if heading is not None:
            in_section = heading == _HEADING_KPI
            out.append(raw)
            continue
        if bound or not in_section or not raw.strip():
            out.append(raw)
            continue
        prefix, body = _split_bullet(raw)
        parts = [_clean(p) for p in _KPI_SPLIT.split(body, maxsplit=2)]
        if not parts or parts[0] != kpi_name:
            out.append(raw)
            continue
        # A name-only KPI has no definition; without a placeholder the backing
        # would land in the definition slot and still parse as unbacked.
        definition = parts[1] if len(parts) > 1 and parts[1] else _NO_DEFINITION
        out.append(f"{prefix}{parts[0]}{KPI_JOIN}{definition}{KPI_JOIN}{backing}")
        bound = True
    return _join(out, text)


def _append_decision_entry(lines: list[str], body: str) -> list[str]:
    entry = f"- {body}"
    start = next(
        (i for i, raw in enumerate(lines) if _heading_of(raw) == _HEADING_DECISIONS),
        None,
    )
    if start is None:
        tail = lines + ([""] if lines and lines[-1].strip() else [])
        return tail + ["## Decisions", entry]
    end = start + 1
    while end < len(lines) and _heading_of(lines[end]) is None:
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1  # insert before the blank line separating the next section
    return lines[:end] + [entry] + lines[end:]


def _append_decision(lines: list[str], question: str, answer: str) -> list[str]:
    return _append_decision_entry(lines, f"{question}{KPI_JOIN}{answer}")


def carried_decisions(text: str) -> list[str]:
    out: list[str] = []
    in_section = False
    for raw in (text or "").splitlines():
        heading = _heading_of(raw)
        if heading is not None:
            in_section = heading == _HEADING_DECISIONS
            continue
        if not in_section or not raw.strip():
            continue
        body = _bullet_body(raw)
        if body:
            out.append(body)
    return out


def open_questions(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    in_section = False
    for raw in (text or "").splitlines():
        heading = _heading_of(raw)
        if heading is not None:
            in_section = heading == _HEADING_OPEN_Q
            continue
        if not in_section or not raw.strip():
            continue
        _prefix, key = _split_bullet(raw)
        if key:
            out.append((key, _bullet_body(raw)))
    return out


def _ensure_decisions(text: str, decisions: list[str]) -> str:
    if not decisions:
        return text
    present = set(carried_decisions(text))
    missing = [d for d in decisions if d not in present]
    if not missing:
        return text
    lines = text.splitlines()
    for entry in missing:
        lines = _append_decision_entry(lines, entry)
    return _join(lines, text)


def resolve_open_question(text: str, question: str, answer: str = "") -> str:
    if not (text and question):
        return text

    out: list[str] = []
    in_section = False
    matched = ""
    removed = False
    for raw in text.splitlines():
        heading = _heading_of(raw)
        if heading is not None:
            in_section = heading == _HEADING_OPEN_Q
            out.append(raw)
            continue
        if removed or not in_section or not raw.strip():
            out.append(raw)
            continue
        _prefix, body = _split_bullet(raw)
        if body != question:
            out.append(raw)
            continue
        # `question` is the set-stripped match key; the decision keeps the line as
        # it was written, bold and all.
        matched = _bullet_body(raw)
        removed = True  # drop the line entirely

    if removed and answer.strip():
        out = _append_decision(out, matched or question, answer.strip())
    return _join(out, text)


_SUGGEST_SYSTEM = (
    "You map BI metrics onto real database columns. You are given unbacked KPIs "
    "and the exact list of columns available in the connected sources. Choose the "
    "single best column for each KPI. Never invent a column — every value you "
    "return must appear verbatim in the provided list. If no column is a "
    "reasonable fit for a KPI, omit that KPI entirely. Reply with JSON only."
)

_SUGGEST_MAX_COLUMNS = 400


def _parse_suggestions(raw: str, names: set[str], allowed: dict[str, str]) -> dict:
    import json

    body = (raw or "").strip()
    if body.startswith("```"):  # strip a ```json fence if the model added one
        body = body.split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        data = json.loads(body[start : end + 1])
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    out: dict[str, str] = {}
    for name, column in data.items():
        if name not in names or not isinstance(column, str):
            continue
        # Only accept columns that actually exist — the model can still
        # hallucinate one despite the instruction.
        real = allowed.get(_clean(column).lower())
        if real:
            out[name] = real
    return out


def suggest_kpi_bindings(
    kpis: list[dict], columns: list[str], agent: str = "default"
) -> dict:
    if not kpis or not columns:
        return {}

    shortlist = columns[:_SUGGEST_MAX_COLUMNS]
    allowed = {c.lower(): c for c in shortlist}
    names = {k["name"] for k in kpis}

    listed = "\n".join(
        f"- {k['name']}"
        + (f"{KPI_JOIN}{k['definition']}" if k.get("definition") else "")
        for k in kpis
    )
    user = (
        f"Unbacked KPIs:\n{listed}\n\n"
        f"Available columns:\n" + "\n".join(f"- {c}" for c in shortlist) + "\n\n"
        "Return ONLY a JSON object mapping each KPI name (exactly as written "
        "above) to the single best column from the list. Omit any KPI with no "
        "good match."
    )
    chunks = [
        piece
        for piece in stream_text(_SUGGEST_SYSTEM, user, max_tokens=2000, agent=agent)
        if isinstance(piece, str)
    ]
    return _parse_suggestions("".join(chunks), names, allowed)


def _remember(project_id: int, project_name: str, result: ExtractionResult) -> None:
    try:
        from db import graph_memory

        spec = parse_spec(result.text)
        graph_memory.remember_requirements(
            project_id,
            project_name=project_name,
            objective=spec["objective"],
            business=spec["business"],
            audience=spec["audience"],
            kpis=spec["kpis"],
            items=spec["items"],
        )
        result.graphed = len(spec["kpis"]) + len(spec["items"])
        graph_memory.remember_turn(
            project_id,
            project_name=project_name,
            note=f"Requirements extracted: {spec['objective']}"[:500],
        )
    except Exception:
        pass


def _build_sections(project_id: int, answers: dict | None = None):
    corpus, images, result = _gather(project_id)

    provider = get_provider("default")
    vision_mode = ""
    if images:
        if _supports_vision(provider):
            vision_mode = "inline"
        elif provider in _READ_TOOL_PROVIDERS:
            vision_mode = "read_tool"
        else:
            result.skipped.extend(
                f"{name} — provider '{provider}' cannot read images"
                for name in result.images
            )
            result.images = []
            images = []

    sql_block = get_project_context_block(project_id, "sql")
    model_block = get_project_context_block(project_id, "semantic_model")
    org_block = _org_memory(project_id)
    decisions = carried_decisions(get_project_requirements(project_id))
    for question, answer in (answers or {}).items():
        if str(question).strip() and str(answer).strip():
            entry = f"{str(question).strip()}{KPI_JOIN}{str(answer).strip()}"
            if entry not in decisions:
                decisions.append(entry)

    if not corpus.strip() and not images and not sql_block and not model_block:
        return None, images, result, vision_mode, decisions

    sections = [f"=== PROJECT DOCUMENTS ===\n{corpus or _NO_TEXT}"]
    if vision_mode == "read_tool":
        sections.append(
            "=== MOCKUP IMAGES ===\n"
            "Open EACH of these with the Read tool — they are mockups, screenshots, "
            "or wireframes and are authoritative source material. Read the titles, "
            "KPI names, chart types, axis labels, and filters visible in them:\n"
            + "\n".join(f"  - {img['path']}" for img in images)
        )
        result.sources.append("images (read from disk)")
    if sql_block:
        sections.append(f"=== CONNECTED DATABASE (read-only) ===\n{sql_block}")
        result.sources.append("SQL schema")
    if model_block:
        sections.append(f"=== EXISTING SEMANTIC MODEL (read-only) ===\n{model_block}")
        result.sources.append("semantic model")
    if decisions:
        sections.append(
            "=== ESTABLISHED DECISIONS (already answered — authoritative, never "
            "re-ask) ===\n" + "\n".join(f"  - {d}" for d in decisions)
        )
        result.sources.append(f"{len(decisions)} carried decision(s)")
    if org_block:
        sections.append(
            "=== ORG MEMORY (other projects — context, NOT a requirement) ===\n"
            + org_block
        )
        result.sources.append("org memory")

    return sections, images, result, vision_mode, decisions


_QUESTION_SYSTEM = (
    "You are a BI requirements analyst about to write a dashboard specification. "
    "Before writing anything you ask the few questions whose answers would most "
    "change the resulting spec. You never ask something the material already "
    "answers, and you never ask more than you need."
)

_QUESTION_INSTRUCTION = """Read the material below. List at most {n} questions that
must be answered before a correct requirements spec can be written.

Ask only about what the material genuinely leaves open — an ambiguous metric
definition, a missing grain or date range, a column that could back a KPI in more
than one way, a business rule that changes the numbers. Do NOT ask for anything
already present in the material, and do NOT ask generic project-management
questions ("who is the stakeholder?", "what is the deadline?").

If the material is already complete enough to spec, return an empty array.

Return ONLY a JSON array of question strings, e.g. ["...", "..."]

{sections}
"""


def _parse_questions(raw: str, limit: int) -> list[str]:
    import json

    body = (raw or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1].rsplit("```", 1)[0]

    out: list[str] = []
    start, end = body.find("["), body.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(body[start : end + 1])
            if isinstance(data, list):
                out = [str(q).strip() for q in data if str(q).strip()]
        except Exception:
            out = []

    if not out:
        # Fall back to bullet lines when the model ignores the JSON instruction.
        for line in body.splitlines():
            text = line.strip().lstrip("-*•0123456789. ").strip()
            if text.endswith("?"):
                out.append(text)

    seen: set[str] = set()
    unique = [q for q in out if not (q in seen or seen.add(q))]
    return unique[:limit]


def ask_clarifying_questions(project_id: int, *, max_questions: int = 6) -> list[str]:
    sections, images, _result, vision_mode, _decisions = _build_sections(project_id)
    if not sections:
        return []

    user = _QUESTION_INSTRUCTION.format(n=max_questions, sections="\n\n".join(sections))
    if vision_mode == "inline":
        text, _usage = call_with_images(
            _QUESTION_SYSTEM, user, images, max_tokens=1500, agent="default"
        )
    else:
        text = "".join(
            piece
            for piece in stream_text(
                _QUESTION_SYSTEM,
                user,
                max_tokens=1500,
                agent="default",
                allow_read=(vision_mode == "read_tool"),
            )
            if isinstance(piece, str)
        )
    return _parse_questions(text or "", max_questions)


def extract_project_requirements(
    project_id: int,
    *,
    persist: bool = True,
    project_name: str = "",
    answers: dict | None = None,
) -> ExtractionResult:
    sections, images, result, vision_mode, decisions = _build_sections(
        project_id, answers
    )
    if not sections:
        return result

    user = _INSTRUCTION.format(sections="\n\n".join(sections))
    if vision_mode == "inline":
        text, _usage = call_with_images(
            _SYSTEM, user, images, max_tokens=8000, agent="default"
        )
        result.text = (text or "").strip()
    else:
        chunks = [
            piece
            for piece in stream_text(
                _SYSTEM,
                user,
                max_tokens=8000,
                agent="default",
                allow_read=(vision_mode == "read_tool"),
            )
            if isinstance(piece, str)
        ]
        result.text = "".join(chunks).strip()

    if result.text:
        result.text = _ensure_decisions(result.text, decisions)

    if result.text and persist:
        set_project_requirements(project_id, result.text)
        _remember(project_id, project_name, result)
    return result
