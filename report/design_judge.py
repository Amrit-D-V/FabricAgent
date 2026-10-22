from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

from config import DESIGN_BUDGETS, JUDGE_MAX_ROUNDS, JUDGE_PASS_SCORE
from util.logger_setup import get_logger

logger = get_logger(__name__)

DIMENSIONS: dict[str, str] = {
    "layout": "alignment, grid discipline, balance, use of space — does the page read top-left to bottom-right with the most important content first?",
    "chart_fit": "is each visual the right chart type for its analytical question (trend/ranking/composition/correlation)?",
    "color_accessibility": "palette discipline (accents reserved for meaning), color-blind safety, contrast",
    "kpi_hierarchy": "do KPI cards carry context (target/variance/trend), is there a clear #1 metric, sizes proportional to importance?",
    "titles_labels": "do titles state insights (not chart mechanics), are labels/units readable and consistent?",
    "density_clarity": "is the page scannable — neither cluttered nor empty, detail pushed to drill pages?",
}


def collect_page_spec(report_path: str, page: str) -> dict[str, Any] | None:
    page_dir = os.path.join(report_path, "definition", "pages", page)
    page_json = os.path.join(page_dir, "page.json")
    if not os.path.isfile(page_json):
        return None
    with open(page_json, encoding="utf-8") as f:
        pj = json.load(f)
    spec: dict[str, Any] = {
        "page": {
            "display_name": pj.get("displayName", page),
            "width": pj.get("width"),
            "height": pj.get("height"),
        },
        "visuals": [],
    }
    visuals_dir = os.path.join(page_dir, "visuals")
    if os.path.isdir(visuals_dir):
        for v_name in sorted(os.listdir(visuals_dir)):
            v_path = os.path.join(visuals_dir, v_name, "visual.json")
            if not os.path.isfile(v_path):
                continue
            try:
                with open(v_path, encoding="utf-8") as f:
                    v = json.load(f)
            except Exception as e:
                logger.debug("skipping unreadable %s: %s", v_path, e)
                continue
            vis = v.get("visual", {})
            title = (
                ((vis.get("visualContainerObjects", {}).get("title") or [{}])[0])
                .get("properties", {})
                .get("text", {})
                .get("expr", {})
                .get("Literal", {})
                .get("Value", "")
            )
            bindings: dict[str, list[str]] = {}
            for role, body in (
                vis.get("query", {}).get("queryState", {}) or {}
            ).items():
                fields = []
                for p in (body or {}).get("projections", []) or []:
                    fld = p.get("field")
                    if isinstance(fld, dict):
                        kind = next(iter(fld), "")
                        if kind in ("Measure", "Column"):
                            ent = (
                                fld[kind]
                                .get("Expression", {})
                                .get("SourceRef", {})
                                .get("Entity", "")
                            )
                            fields.append(f"{ent}[{fld[kind].get('Property', '')}]")
                if fields:
                    bindings[role] = fields
            spec["visuals"].append(
                {
                    "name": v.get("name", v_name),
                    "type": vis.get("visualType", ""),
                    "rect": v.get("position", {}),
                    "title": str(title).strip("'"),
                    "bindings": bindings,
                    "sorted": bool(vis.get("query", {}).get("sortDefinition")),
                }
            )
    try:
        import glob

        from report.dashboard_polish import THEME_NAME

        for tp in glob.glob(
            f"{report_path}/**/RegisteredResources/{THEME_NAME}.json", recursive=True
        ):
            with open(tp, encoding="utf-8") as f:
                theme = json.load(f)
            spec["theme"] = {
                "dataColors": theme.get("dataColors"),
                "accent": theme.get("tableAccent"),
            }
            break
    except Exception:
        pass
    return spec


_SHAPE = json.dumps(
    {"scores": {d: {"score": 0, "rationale": "", "repair": ""} for d in DIMENSIONS}}
)

_SYSTEM = f"""You are a senior Power BI dashboard design reviewer. Judge the page
spec the user provides — visual types, rects, titles, bindings, palette.
Score each dimension 1-10 (10 = client-grade) and give a one-sentence
rationale. Dimensions: {json.dumps(DIMENSIONS, indent=1)}
For any dimension scoring below {JUDGE_PASS_SCORE}, add ONE concrete
repair instruction phrased as a tool action the build agent can execute
(reposition/resize/retitle/rebind/recolor — name the visual).
Design budgets in force: max {DESIGN_BUDGETS["max_cards"]} KPI cards,
{DESIGN_BUDGETS["max_slicers"]} slicers, {DESIGN_BUDGETS["max_visuals"]} visuals per page.
Deterministic audit findings are listed separately — do NOT repeat them; judge
what rules cannot see.
Respond with JSON ONLY, exactly this shape:
{_SHAPE}"""


def judge_page(
    report_path: str,
    page: str,
    audit_findings: list | None = None,
    complete: Callable[[str, str], str] | None = None,
    round_num: int = 1,
    prev_overall: float | None = None,
) -> dict[str, Any]:
    spec = collect_page_spec(report_path, page)
    if spec is None:
        return {"error": f"page '{page}' not found under {report_path}"}
    if not spec["visuals"]:
        return {"error": f"page '{page}' has no visuals to judge"}
    round_num = max(1, round_num)

    user = "PAGE SPEC:\n" + json.dumps(spec, indent=1)
    if audit_findings:
        user += "\n\nDETERMINISTIC AUDIT FINDINGS (do not repeat):\n" + json.dumps(
            audit_findings, indent=1
        )

    if complete is None:

        def complete(system: str, prompt: str) -> str:
            from llm.llm_client import stream_text

            return "".join(
                c
                for c in stream_text(
                    system, prompt, max_tokens=2000, agent="design_judge"
                )
                if isinstance(c, str)
            )

    raw = complete(_SYSTEM, user)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {"error": "judge returned no JSON", "raw": raw[:600]}
    try:
        parsed = json.loads(m.group(0))
    except Exception as e:
        return {"error": f"judge JSON unparseable: {e}", "raw": raw[:600]}

    scores: dict[str, Any] = {}
    repairs: list[str] = []
    for dim in DIMENSIONS:
        entry = (parsed.get("scores") or {}).get(dim) or {}
        try:
            score = max(1, min(10, int(entry.get("score", 0))))
        except (TypeError, ValueError):
            score = 1
        scores[dim] = {
            "score": score,
            "rationale": str(entry.get("rationale", ""))[:300],
        }
        repair = str(entry.get("repair", "")).strip()
        if score < JUDGE_PASS_SCORE and repair:
            scores[dim]["repair"] = repair[:300]
            repairs.append(f"[{dim}] {repair[:300]}")

    overall = round(sum(s["score"] for s in scores.values()) / len(scores), 1)
    verdict = "pass" if overall >= JUDGE_PASS_SCORE and not repairs else "needs_repair"
    out = {
        "page": page,
        "overall": overall,
        "pass_threshold": JUDGE_PASS_SCORE,
        "scores": scores,
        "repairs": repairs,
        "verdict": verdict,
    }
    out["next_step"] = next_step(verdict, overall, round_num, prev_overall)
    return out


def next_step(
    verdict: str,
    overall: float,
    round_num: int,
    prev_overall: float | None = None,
    max_rounds: int = JUDGE_MAX_ROUNDS,
) -> dict[str, Any]:
    if verdict == "pass":
        return {"action": "done", "reason": "passed the design bar"}
    if round_num >= max_rounds:
        return {
            "action": "done",
            "reason": f"reached the {max_rounds}-round budget; shipping best result",
        }
    if prev_overall is not None and overall <= prev_overall:
        return {
            "action": "done",
            "reason": f"score did not improve ({prev_overall} → {overall}); stopping",
        }
    return {
        "action": "apply_repairs",
        "reason": "apply the listed repairs, then call pbi_design_judge again "
        f"with round={round_num + 1} and prev_overall={overall}",
    }
