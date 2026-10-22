from __future__ import annotations

import glob
import json
import os

from util.logger_setup import get_logger

logger = get_logger(__name__)


def _pages_dir(report_path: str) -> str:
    rp = report_path
    if os.path.isfile(rp) and rp.lower().endswith(".pbip"):
        base = os.path.dirname(rp)
        stem = os.path.splitext(os.path.basename(rp))[0]
        cand = os.path.join(base, stem + ".Report")
        if os.path.isdir(cand):
            rp = cand
        else:
            for sub in glob.glob(os.path.join(base, "*.Report")):
                if os.path.isdir(sub):
                    rp = sub
                    break
    direct = os.path.join(rp, "definition", "pages")
    if os.path.isdir(direct):
        return direct
    for d in glob.glob(os.path.join(rp, "**", "definition", "pages"), recursive=True):
        if os.path.isdir(d):
            return d
    return ""


def _literal(node) -> str:
    try:
        v = node["expr"]["Literal"]["Value"]
        return v.strip("'") if isinstance(v, str) else v
    except Exception:
        return ""


def _visual_title(vjson: dict) -> str:
    try:
        node = vjson["visual"]["visualContainerObjects"]["title"][0]["properties"][
            "text"
        ]
        return _literal(node)
    except Exception:
        return ""


def _fields(vjson: dict) -> list[dict]:
    out: list[dict] = []
    qs = ((vjson.get("visual") or {}).get("query") or {}).get("queryState") or {}
    for role, spec in qs.items():
        for p in spec.get("projections") or []:
            fld = p.get("field") or {}
            kind = (
                "Measure" if "Measure" in fld else ("Column" if "Column" in fld else "")
            )
            ref = p.get("queryRef") or ""
            if not ref and kind:
                inner = fld.get(kind, {})
                ent = (
                    ((inner.get("Expression") or {}).get("SourceRef") or {}).get(
                        "Entity"
                    )
                ) or ""
                prop = inner.get("Property") or ""
                ref = f"{ent}.{prop}" if ent else prop
            if ref:
                out.append({"role": role, "kind": kind, "ref": ref})
    return out


def analyze_dashboard(report_path: str) -> dict:
    pages_dir = _pages_dir(report_path)
    if not pages_dir:
        return {"error": f"No PBIR pages found under {report_path!r}"}

    order: list[str] = []
    try:
        with open(os.path.join(pages_dir, "pages.json"), encoding="utf-8") as f:
            order = json.load(f).get("pageOrder") or []
    except Exception:
        pass

    folders = [
        d
        for d in os.listdir(pages_dir)
        if os.path.isfile(os.path.join(pages_dir, d, "page.json"))
    ]
    if order:
        folders.sort(key=lambda d: order.index(d) if d in order else 10**6)

    pages: list[dict] = []
    total_visuals = 0
    for pf in folders:
        try:
            with open(os.path.join(pages_dir, pf, "page.json"), encoding="utf-8") as f:
                pjson = json.load(f)
        except Exception as e:
            logger.debug("skipping unreadable page.json for %s: %s", pf, e)
            continue
        visuals: list[dict] = []
        vdir = os.path.join(pages_dir, pf, "visuals")
        if os.path.isdir(vdir):
            for vf in sorted(os.listdir(vdir)):
                vpath = os.path.join(vdir, vf, "visual.json")
                if not os.path.isfile(vpath):
                    continue
                try:
                    with open(vpath, encoding="utf-8") as f:
                        vjson = json.load(f)
                except Exception as e:
                    logger.debug("skipping unreadable %s: %s", vpath, e)
                    continue
                vis = vjson.get("visual") or {}
                pos = vjson.get("position") or {}
                visuals.append(
                    {
                        "name": vjson.get("name", vf),
                        "type": vis.get("visualType")
                        or ("group" if vjson.get("visualGroup") else "?"),
                        "title": _visual_title(vjson),
                        "x": pos.get("x"),
                        "y": pos.get("y"),
                        "w": pos.get("width"),
                        "h": pos.get("height"),
                        "fields": _fields(vjson),
                    }
                )
        total_visuals += len(visuals)
        pages.append(
            {
                "name": pjson.get("name", pf),
                "display": pjson.get("displayName", pf),
                "width": pjson.get("width"),
                "height": pjson.get("height"),
                "visuals": visuals,
            }
        )

    return {
        "report_path": report_path,
        "page_count": len(pages),
        "visual_count": total_visuals,
        "pages": pages,
    }


_PROPOSE_SYSTEM = (
    "You are a Power BI report editor that PROPOSES changes to an existing PBIR "
    "dashboard. You NEVER apply changes — you only return a proposal for a human "
    "to approve. Given the full dashboard analysis and the user's request, return "
    "ONLY JSON:\n"
    '{"summary": "<one-line plain-English summary>", "changes": [ '
    '{"op": "<op>", "description": "<what & why>", ...params} ]}\n'
    "Allowed op + params:\n"
    "  add_visual    {page, visual_type, title, fields:[{role, ref}], x, y, w, h}\n"
    "  move_resize   {page, visual, x, y, w, h}\n"
    "  restyle       {page, visual, property, value}\n"
    "  bind_field    {page, visual, role, ref}\n"
    "  remove_visual {page, visual}\n"
    "  add_page      {name, display}\n"
    "  remove_page   {page}\n"
    "  set_theme     {theme}\n"
    "Use the EXACT page names and visual names from the analysis. Reference fields "
    "as 'Entity.Property' exactly as shown. Propose the MINIMAL set of changes that "
    "satisfies the request. Output JSON only — no prose, no code fences."
)


def _parse_json(raw: str) -> dict:
    import re

    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1:
        s = s[i : j + 1]
    try:
        return json.loads(s)
    except Exception as e:
        return {
            "error": f"could not parse proposal JSON: {e}",
            "raw": (raw or "")[:1500],
        }


def propose_changes(analysis: dict, user_request: str) -> dict:
    from llm.llm_client import stream_text

    user = (
        f"DASHBOARD ANALYSIS:\n{summary_text(analysis)}\n\n"
        f"USER REQUEST:\n{user_request}\n\n"
        "Return ONLY the change-set JSON."
    )
    chunks: list[str] = []
    for piece in stream_text(_PROPOSE_SYSTEM, user, max_tokens=4000, agent="default"):
        if isinstance(piece, str):
            chunks.append(piece)
    result = _parse_json("".join(chunks))
    if "error" not in result:
        result = {
            "summary": result.get("summary", ""),
            "changes": result.get("changes") or [],
        }
    return result


def summary_text(analysis: dict) -> str:
    if analysis.get("error"):
        return f"[analysis error: {analysis['error']}]"
    lines = [
        (f"DASHBOARD: {analysis['page_count']} page(s), "
        f"{analysis['visual_count']} visual(s).")
    ]
    for p in analysis["pages"]:
        lines.append(
            f"\nPAGE '{p['display']}' ({p['name']}) — {len(p['visuals'])} visuals "
            f"[{p.get('width')}x{p.get('height')}]:"
        )
        for v in p["visuals"]:
            flds = ", ".join(f"{f['role']}={f['ref']}" for f in v["fields"])
            ttl = f' "{v["title"]}"' if v["title"] else ""
            lines.append(
                f"  - {v['type']}{ttl} [{v['name']}] "
                f"@({v['x']},{v['y']} {v['w']}x{v['h']})"
                f"{' | ' + flds if flds else ''}"
            )
    return "\n".join(lines)
