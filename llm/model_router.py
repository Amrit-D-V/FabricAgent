"""Which connected semantic model is a question actually about?

Fabric Data Agents route across sources by weighing schema/metadata, a source
description and example queries. FabricBI has the first of those already: every
registry refresh writes a per-connection file under `.registry_cache/` whose
`_meta.model_names` records exactly which models it covers.

This scores the connected models against the user's question using that schema.
It deliberately does NOT choose. `_model_choice_gate` exists because the
assistant once inferred `MavenMarket_Report` from a project description while
`kaizen_v7` was connected, and acting on the wrong model produces real-looking
wrong numbers. Routing removes the *blindness* in that gate, not the consent:
the user still picks, but now sees which model the question's own words match.

No XMLA calls — this reads registries that a refresh already wrote. A model with
no cached registry is reported as UNKNOWN, never as a zero score, because
"nothing cached" and "nothing matched" must not look alike.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Words that match everything and therefore discriminate nothing.
_STOP = {
    "the",
    "and",
    "for",
    "with",
    "what",
    "which",
    "show",
    "give",
    "list",
    "from",
    "how",
    "many",
    "much",
    "are",
    "was",
    "were",
    "this",
    "that",
    "our",
    "all",
    "get",
    "can",
    "you",
    "please",
    "report",
    "data",
    "value",
    "values",
    "total",
    "count",
    "sum",
    "average",
    "avg",
    "top",
    "last",
    "first",
    "over",
    "per",
    "into",
    "each",
    "any",
    "have",
    "has",
    "had",
    "does",
    "did",
    "not",
    "but",
    "model",
    "table",
    "column",
    "measure",
    "field",
    "chart",
    "visual",
    "page",
}

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")


@dataclass
class ModelScore:
    model: str
    score: int = 0
    matched: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    known: bool = True
    note: str = ""


def _tokens(text: str) -> set[str]:
    """Words worth matching on, with camelCase and snake_case split apart.

    `writtenPremium` and `written_premium` must both surface the tokens
    `written` and `premium`, or a question phrased in English never matches a
    column named in code.
    """
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    raw = raw.replace("_", " ").replace("-", " ").replace(".", " ")
    return {w.lower() for w in _WORD.findall(raw)} - _STOP


def _registry_dir() -> str:
    from config import PROJECT_ROOT

    return os.path.join(str(PROJECT_ROOT), ".registry_cache")


def build_index(registry_dir: str | None = None) -> dict:
    """{model_name: {"columns": [...], "measures": [...], "workspace": str}}.

    Indexed by `_meta.model_names` rather than by the fingerprinted filename.
    The fingerprint folds in the workspace NAME, so a workspace rename orphans
    the path FabricBI would compute today while the file on disk is still perfectly
    good — the same rename trap that already bit FabricConnection.
    """
    directory = registry_dir or _registry_dir()
    index: dict[str, dict] = {}
    if not os.path.isdir(directory):
        return index

    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".json"):
            continue
        kind = (
            "columns"
            if fname.startswith("column")
            else ("measures" if fname.startswith("measure") else "")
        )
        if not kind:
            continue
        try:
            with open(os.path.join(directory, fname), encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            logger.debug("skipping unreadable registry file %s: %s", fname, e)
            continue
        meta = data.get("_meta") or {}
        names = meta.get("model_names") or []
        # A union file covering several models cannot attribute a field to one
        # of them, so it is not usable for routing. See HANDOVER open item 5.
        if len(names) != 1:
            continue
        entry = index.setdefault(
            names[0],
            {"columns": [], "measures": [], "workspace": meta.get("workspace", "")},
        )
        entry[kind].extend(k for k in data if not k.startswith("_"))
    return index


def score_models(question: str, models, index: dict | None = None) -> list[ModelScore]:
    """Rank `models` by how well the question's words match their schema."""
    idx = build_index() if index is None else index
    q = _tokens(question)
    out: list[ModelScore] = []

    for model in models:
        entry = idx.get(model)
        if not entry:
            out.append(
                ModelScore(
                    model=model,
                    known=False,
                    note="no cached schema — run pbi_refresh_registry for this model",
                )
            )
            continue
        if not q:
            out.append(ModelScore(model=model, note="nothing to match on"))
            continue

        matched: dict[str, list[str]] = {}
        for kind in ("columns", "measures"):
            for key in entry[kind]:
                hits = _tokens(key) & q
                for h in hits:
                    matched.setdefault(h, []).append(key)

        evidence = []
        for tok in sorted(matched, key=lambda t: -len(matched[t]))[:4]:
            sample = sorted(set(matched[tok]))[:2]
            evidence.append(f"{tok} -> {', '.join(sample)}")
        out.append(
            ModelScore(
                model=model,
                score=len(matched),
                matched=sorted(matched),
                evidence=evidence,
            )
        )

    out.sort(key=lambda s: (-s.score, s.model))
    return out


def routing_hint(question: str, models, index: dict | None = None) -> str:
    """A short block for the model-choice gate. "" when it cannot help.

    Silence is the right output when the evidence is weak: a confident-looking
    recommendation backed by one incidental word match is worse than none,
    because the user stops reading the model list and just takes the suggestion.
    """
    scored = score_models(question, models, index=index)
    ranked = [s for s in scored if s.known and s.score > 0]
    if not ranked:
        return ""

    best = ranked[0]
    runner_up = ranked[1].score if len(ranked) > 1 else 0
    if best.score < 2 or best.score == runner_up:
        # Too thin, or a tie — show the overlap without naming a winner.
        lines = [f"  {s.model}: matches {', '.join(s.matched[:6])}" for s in ranked[:3]]
        return "Schema overlap with your question:\n" + "\n".join(lines)

    lines = [f"Best schema match: {best.model} ({best.score} term(s) matched)"]
    lines += [f"  {e}" for e in best.evidence]
    for s in ranked[1:3]:
        lines.append(f"  (also {s.model}: {s.score})")
    unknown = [s.model for s in scored if not s.known]
    if unknown:
        lines.append(
            f"  no cached schema for {', '.join(unknown)} — not scored, not ruled out"
        )
    lines.append("  This is a schema hint, not a decision. Confirm before I act.")
    return "\n".join(lines)
