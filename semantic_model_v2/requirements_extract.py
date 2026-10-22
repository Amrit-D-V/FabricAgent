from __future__ import annotations

import re
import time

from llm.llm_client import (
    UsageInfo,
    call_with_images,
    get_model_name,
    get_provider,
    stream_text,
)
from semantic_model_v2.prompts import REQUIREMENTS_EXTRACT_PROMPT
from util.logger_setup import get_logger

logger = get_logger(__name__)


def call_llm(
    system_prompt: str,
    user_msg: str,
    max_tokens: int = 16000,
    progress_callback=None,
) -> tuple[str, UsageInfo | None]:
    parts: list[str] = []
    usage: UsageInfo | None = None
    total_chars = 0
    last_tick = 0
    for item in stream_text(
        system_prompt,
        user_msg,
        max_tokens=max_tokens,
        agent="small_task",
        temperature=0,
    ):
        if isinstance(item, UsageInfo):
            usage = item
        else:
            parts.append(item)
            if progress_callback:
                total_chars += len(item)
                if total_chars - last_tick >= 500:
                    last_tick = total_chars
                    progress_callback(f"LLM streaming... {total_chars:,} chars received")
    raw = "".join(parts)
    # Reasoning models emit a <think> block the planner must never see.
    return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip(), usage


def extract_document_requirements(
    documents_context: str,
    project_id: int | None = None,
    image_data: list[dict] | None = None,
    progress_callback=None,
) -> str:
    from db.database import save_api_usage

    start = time.time()
    if progress_callback:
        progress_callback(
            f"Calling {get_model_name('semantic_model')} — "
            f"{len(documents_context):,} chars of doc text"
            + (f" + {len(image_data)} image(s)" if image_data else "")
        )

    if image_data and get_provider("semantic_model") == "anthropic":
        if progress_callback:
            progress_callback(
                "LLM analyzing documents + images (vision call — no token stream)..."
            )
        raw, usage = call_with_images(
            REQUIREMENTS_EXTRACT_PROMPT,
            documents_context,
            image_data,
            max_tokens=8192,
            agent="semantic_model",
        )
    else:
        raw, usage = call_llm(
            REQUIREMENTS_EXTRACT_PROMPT,
            documents_context,
            max_tokens=8192,
            progress_callback=progress_callback,
        )

    latency_ms = int((time.time() - start) * 1000)
    logger.info(f"Requirements extraction: {latency_ms}ms")
    if progress_callback:
        progress_callback(
            f"LLM finished in {latency_ms / 1000:.1f}s — "
            f"extracted {len(raw):,} chars of requirements"
        )
    if project_id:
        save_api_usage(
            project_id=project_id,
            agent="requirements_extraction",
            model=get_model_name("semantic_model"),
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            latency_ms=latency_ms,
            status="success",
        )
    return raw


def project_rules_context(project_id: int | None) -> str:
    if not project_id:
        return ""
    from db.database import list_project_rules

    try:
        rules = list_project_rules(project_id, active_only=True)
    except Exception:
        return ""
    if not rules:
        return ""

    grouped: dict[str, list[str]] = {}
    for rule in rules:
        grouped.setdefault(rule["category"], []).append(rule["rule_text"])

    lines = [
        "PROJECT MEMORY (rules saved from prior runs — apply BEFORE any default heuristic):"
    ]
    for category, items in grouped.items():
        lines.append(f"  [{category}]")
        lines.extend(f"    - {item}" for item in items)
    return "\n".join(lines)
