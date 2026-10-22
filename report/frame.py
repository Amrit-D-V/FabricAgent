import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from config import (
    POWERBI_FORBIDDEN_VISUALS,
    POWERBI_NATIVE_VISUALS,
    normalize_visual_type,
)
from db import database as db
from llm.llm_client import (
    OLLAMA_NUM_CTX,
    UsageInfo,
    get_model_name,
    get_provider,
    stream_text,
)
from util.logger_setup import get_logger

logger = get_logger(__name__)

POWERBI_VISUALS = ", ".join(POWERBI_NATIVE_VISUALS)
POWERBI_FORBIDDEN = (
    ", ".join(POWERBI_FORBIDDEN_VISUALS) + ", or any other custom/AppSource visual"
)
_POWERBI_VISUAL_RULE = (
    "=== POWER BI VISUAL CONSTRAINT (HARD RULE — the design must be buildable) ===\n"
    "Use ONLY visuals that exist natively in Power BI. Allowed visuals:\n"
    f"{POWERBI_VISUALS}\n"
    f"NEVER use: {POWERBI_FORBIDDEN}.\n"
    "If a fancy visual seems ideal, substitute the closest native equivalent — "
    "trend → line/area chart; composition → stacked column/bar or donut; "
    "ranking → bar chart; part-to-whole → donut/pie/treemap; flow → funnel or "
    "waterfall; single number → card/KPI/gauge; geography → filled/bubble map; "
    "detail → table/matrix."
)


def svg_to_png(content: str, width: int = 1400, height: int = 900) -> bytes:
    import subprocess
    import sys
    import tempfile

    stripped = content.lstrip()
    if stripped.lower().startswith("<!doctype") or stripped.lower().startswith("<html"):
        html = content
    else:
        html = (
            f"<html><body style='margin:0;padding:0;overflow:hidden;'>"
            f"<div style='width:{width}px;height:{height}px;'>{content}</div>"
            f"</body></html>"
        )

    with tempfile.NamedTemporaryFile(
        suffix=".html", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(html)
        html_path = f.name

    png_path = html_path[:-5] + ".png"

    script = (
        "import os; from playwright.sync_api import sync_playwright\n"
        "hp=os.environ['PW_HTML']; pp=os.environ['PW_PNG']\n"
        "w=int(os.environ['PW_W']); h=int(os.environ['PW_H'])\n"
        "with sync_playwright() as p:\n"
        "    b=p.chromium.launch(headless=True,channel='msedge')\n"
        "    pg=b.new_page(viewport={'width':w,'height':h})\n"
        "    pg.goto('file:///'+hp.replace('\\\\','/'))\n"
        "    pg.wait_for_load_state('networkidle')\n"
        "    pg.screenshot(type='png',path=pp,clip={'x':0,'y':0,'width':w,'height':h})\n"
        "    b.close()\n"
    )

    env = {
        **os.environ,
        "PW_HTML": html_path,
        "PW_PNG": png_path,
        "PW_W": str(width),
        "PW_H": str(height),
    }
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            env=env,
            capture_output=True,
            timeout=120,
            creationflags=creationflags,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace"))
        with open(png_path, "rb") as fh:
            return fh.read()
    finally:
        for p in (html_path, png_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def _llm_complete(
    system: str,
    user: str,
    max_tokens: int = 8000,
    temperature: float | None = None,
    on_chunk: Callable | None = None,
    allow_read: bool = False,
    agent: str = "wireframe",
) -> tuple[str, UsageInfo]:
    provider = get_provider(agent)
    if provider == "ollama":
        max_user_chars = max(2000, (OLLAMA_NUM_CTX - 1000) * 4)
        if len(user) > max_user_chars:
            user = (
                user[:max_user_chars]
                + "\n\n[...document truncated to fit local model context...]"
            )

    chunks: list[str] = []
    usage: UsageInfo | None = None
    for item in stream_text(
        system=system,
        user=user,
        max_tokens=max_tokens,
        agent=agent,
        temperature=temperature,
        allow_read=allow_read,
    ):
        if isinstance(item, str):
            chunks.append(item)
            if on_chunk:
                try:
                    on_chunk(item, "".join(chunks))
                except Exception:
                    pass
        elif isinstance(item, UsageInfo):
            usage = item

    if usage is None:
        usage = UsageInfo(
            input_tokens=0, output_tokens=0, model=get_model_name(agent)
        )

    return "".join(chunks), usage


@dataclass
class MeetingData:
    client_name: str
    client_info: str
    requirement: str
    problem_statement: str
    additional_context: str | None = None


@dataclass
class WireframeSpec:
    title: str
    description: str
    layout_structure: str
    key_elements: list[str]
    design_notes: str
    differentiator: str = ""



def parse_markdown_spec_to_wireframe_reqs(text: str) -> dict[str, Any] | None:
    if not text or "## " not in text:
        return None

    sections: dict[str, list[str]] = {}
    current_sec = ""
    for line in text.splitlines():
        line_s = line.strip()
        if line_s.startswith("## "):
            sec_title = line_s[3:].split("(")[0].strip().lower()
            current_sec = sec_title
            sections[current_sec] = []
        elif current_sec and line_s:
            sections[current_sec].append(line_s)

    kpi_lines = []
    for k, v in sections.items():
        if "metric" in k or "kpi" in k:
            kpi_lines = v
            break

    if not kpi_lines:
        return None

    goal = ""
    for k, v in sections.items():
        if "objective" in k or "goal" in k:
            goal = " ".join(v[:2])
            break

    users = []
    for k, v in sections.items():
        if "audience" in k or "user" in k:
            for l in v:
                clean = l.lstrip("-*• ").strip()
                if clean:
                    users.append(clean)
            break

    kpis = []
    for l in kpi_lines:
        clean = l.lstrip("-*• ").strip()
        if not clean:
            continue
        parts = clean.split("—") if "—" in clean else clean.split("-")
        name = parts[0].strip()
        desc = parts[1].strip() if len(parts) > 1 else name
        kpis.append({"name": name, "description": desc})

    charts = []
    for k, v in sections.items():
        if "visual" in k or "chart" in k or "page" in k:
            for l in v:
                clean = l.lstrip("-*• ").strip()
                if not clean:
                    continue
                t = "bar"
                lower = clean.lower()
                if "line" in lower or "trend" in lower:
                    t = "line"
                elif "donut" in lower or "pie" in lower:
                    t = "donut"
                elif "table" in lower or "grid" in lower:
                    t = "table"
                elif "gauge" in lower:
                    t = "gauge"
                elif "matrix" in lower:
                    t = "matrix"
                charts.append({"type": t, "metric": clean, "purpose": clean})
            break

    filters = []
    for k, v in sections.items():
        if "filter" in k or "slicer" in k:
            for l in v:
                clean = l.lstrip("-*• ").strip()
                if clean:
                    filters.append(clean)
            break

    res = {
        "primary_goal": goal or "Power BI operational & executive dashboard",
        "target_users": users or ["Business Users", "Leadership"],
        "business_questions": [goal] if goal else [],
        "kpis": kpis,
        "charts_needed": charts,
        "filters": filters,
        "domain_terms": [],
        "visual_style_from_images": "",
        "must_have_sections": [],
    }
    for _ch in res.get("charts_needed") or []:
        if isinstance(_ch, dict) and _ch.get("type"):
            _ch["type"] = normalize_visual_type(_ch["type"])
    return res

