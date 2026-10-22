from __future__ import annotations

import html
import json
import os

from config import LOGS_DIR, SEMANTIC_BAD, SEMANTIC_MID, WIREFRAME_MAX_W
from util.logger_setup import get_logger

logger = get_logger(__name__)


def _flagged_visuals(page_findings: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for severity in ("warning", "error"):
        for f in page_findings.get(severity + "s", []) or []:
            names: list[str] = []
            if f.get("visual"):
                names.append(f["visual"])
            for key in ("visuals", "between"):
                if isinstance(f.get(key), list):
                    names.extend(n for n in f[key] if isinstance(n, str))
            if isinstance(f.get("cards"), dict):
                names.extend(f["cards"].keys())
            for n in names:
                out[n] = severity
    return out


def render_wireframes(report_path: str, audit: dict, out_dir: str = "") -> list[str]:
    out_dir = out_dir or os.path.join(str(LOGS_DIR), "wireframes")
    pages_dir = os.path.join(report_path, "definition", "pages")
    if not os.path.isdir(pages_dir):
        return []
    os.makedirs(out_dir, exist_ok=True)
    report_label = os.path.basename(report_path.rstrip("/\\")).replace(".Report", "")

    paths: list[str] = []
    for page_name, page_findings in (audit.get("pages") or {}).items():
        page_json = os.path.join(pages_dir, page_name, "page.json")
        if not os.path.isfile(page_json):
            continue
        try:
            with open(page_json, encoding="utf-8") as f:
                page = json.load(f)
            page_w = float(page.get("width", 1280))
            page_h = float(page.get("height", 720))
            scale = min(1.0, WIREFRAME_MAX_W / page_w)
            flagged = _flagged_visuals(page_findings or {})

            boxes: list[str] = []
            visuals_dir = os.path.join(pages_dir, page_name, "visuals")
            if os.path.isdir(visuals_dir):
                for v_name in sorted(os.listdir(visuals_dir)):
                    v_path = os.path.join(visuals_dir, v_name, "visual.json")
                    if not os.path.isfile(v_path):
                        continue
                    with open(v_path, encoding="utf-8") as f:
                        v = json.load(f)
                    pos = v.get("position", {})
                    x, y = (
                        float(pos.get("x", 0)) * scale,
                        float(pos.get("y", 0)) * scale,
                    )
                    w, h = (
                        float(pos.get("width", 0)) * scale,
                        float(pos.get("height", 0)) * scale,
                    )
                    vtype = v.get("visual", {}).get("visualType", "?")
                    name = v.get("name", v_name)
                    sev = flagged.get(name, "")
                    color = (
                        SEMANTIC_BAD
                        if sev == "error"
                        else (SEMANTIC_MID if sev == "warning" else "#94A3B8")
                    )
                    label = html.escape(
                        f"{name} · {vtype} · {int(pos.get('x', 0))},{int(pos.get('y', 0))} {int(pos.get('width', 0))}x{int(pos.get('height', 0))}"
                    )
                    boxes.append(
                        f"<div class='v{' flag' if sev else ''}' style=\"left:{x:.0f}px;top:{y:.0f}px;"
                        f"width:{w:.0f}px;height:{h:.0f}px;border-color:{color}\" title='{label}'>"
                        f"<span>{label}</span></div>"
                    )

            findings_html = ""
            for sev_key, css in (("errors", "err"), ("warnings", "warn")):
                for f in (page_findings or {}).get(sev_key, []) or []:
                    findings_html += (
                        f"<li class='{css}'><b>{html.escape(f.get('check', '?'))}</b> "
                        f"{html.escape(str(f.get('visual') or f.get('visuals') or f.get('between') or ''))} "
                        f"— {html.escape(f.get('fix', ''))}</li>"
                    )

            doc = f"""<!doctype html><meta charset="utf-8">
<title>{html.escape(report_label)} · {html.escape(page_name)}</title>
<style>
 body{{font:12px 'Segoe UI',sans-serif;background:#0F172A;color:#E2E8F0;margin:24px}}
 .canvas{{position:relative;width:{page_w * scale:.0f}px;height:{page_h * scale:.0f}px;
          background:#EEF2F7;border-radius:6px;overflow:hidden}}
 .v{{position:absolute;border:2px solid;border-radius:4px;background:rgba(255,255,255,.9);
     overflow:hidden;box-sizing:border-box}}
 .v span{{font-size:9px;color:#334155;padding:2px;display:block}}
 .v.flag{{background:rgba(254,243,199,.9)}}
 ul{{max-width:{page_w * scale:.0f}px}} li.err{{color:#FCA5A5}} li.warn{{color:#FDE68A}}
</style>
<h2>{html.escape(report_label)} — {html.escape(page_name)}
    <small>({int(page_w)}x{int(page_h)}, scale {scale:.2f})</small></h2>
<div class="canvas">{''.join(boxes)}</div>
<ul>{findings_html or '<li>no findings on this page</li>'}</ul>
"""
            out_path = os.path.join(out_dir, f"{report_label}_{page_name}.html")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(doc)
            paths.append(out_path)
        except Exception as e:
            logger.debug("skipping wireframe render for %s: %s", page_name, e)
            continue
    return paths
