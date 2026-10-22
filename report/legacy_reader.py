from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class LegacyVisual:
    page: str
    name: str
    visual_type: str
    bindings: list[str] = field(default_factory=list)
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0


@dataclass
class LegacyReport:
    path: str = ""
    pages: list[str] = field(default_factory=list)
    visuals: list[LegacyVisual] = field(default_factory=list)

    @property
    def visual_types(self) -> Counter:
        return Counter(v.visual_type for v in self.visuals)

    @property
    def all_bindings(self) -> set[str]:
        return {b for v in self.visuals for b in v.bindings}


def legacy_json_path(report_path: str) -> str | None:
    if not report_path or not os.path.isdir(report_path):
        return None
    if os.path.isdir(os.path.join(report_path, "definition", "pages")):
        return None  # genuine PBIR -- not our business
    for cand in (
        os.path.join(report_path, "report.json"),
        os.path.join(report_path, "Report", "report.json"),
    ):
        if os.path.isfile(cand):
            return cand
    return None


def is_legacy(report_path: str) -> bool:
    return legacy_json_path(report_path) is not None


def _walk_field_refs(node, out: list) -> None:
    if isinstance(node, dict):
        for kind in ("Measure", "Column"):
            sub = node.get(kind)
            if isinstance(sub, dict) and "Property" in sub:
                ent = sub.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
                out.append((kind, ent, sub.get("Property", "")))
        for v in node.values():
            _walk_field_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_field_refs(v, out)


def read(report_path: str) -> LegacyReport | None:
    jpath = legacy_json_path(report_path)
    if not jpath:
        return None
    try:
        with open(jpath, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None

    out = LegacyReport(path=report_path)
    for section in data.get("sections", []) or []:
        page = section.get("displayName") or section.get("name") or "(unnamed page)"
        out.pages.append(page)
        for vc in section.get("visualContainers", []) or []:
            # `config` is a JSON string, not a nested object -- the single
            # detail that makes this format awkward to grep by hand.
            try:
                cfg = json.loads(vc.get("config") or "{}")
            except (ValueError, TypeError):
                cfg = {}
            sv = cfg.get("singleVisual") or {}
            refs: list = []
            _walk_field_refs(sv, refs)
            seen: set = set()
            binds: list[str] = []
            for _kind, ent, prop in refs:
                ref = f"{ent}[{prop}]" if ent else f"[{prop}]"
                if ref not in seen:
                    seen.add(ref)
                    binds.append(ref)
            out.visuals.append(
                LegacyVisual(
                    page=page,
                    name=cfg.get("name") or "(unnamed)",
                    visual_type=sv.get("visualType") or "?",
                    bindings=binds,
                    x=vc.get("x", 0) or 0,
                    y=vc.get("y", 0) or 0,
                    width=vc.get("width", 0) or 0,
                    height=vc.get("height", 0) or 0,
                )
            )
    return out


def format_bindings(rep: LegacyReport, page: str | None = None) -> str:
    rows = [v for v in rep.visuals if not page or v.page == page]
    if not rows:
        where = f" on page '{page}'" if page else ""
        return f"No visuals found{where} under {rep.path}."
    lines = [
        (
            f"Field bindings (PBIR-Legacy) - {os.path.basename(rep.path)} "
            f"({len(rows)} visual(s)):"
        )
    ]
    for v in rows:
        binds = ", ".join(v.bindings) or "(no field bindings - textbox/shape/image)"
        lines.append(f"[{v.page}] {v.name} ({v.visual_type}): {binds}")
    return "\n".join(lines)


def format_summary(rep: LegacyReport) -> str:
    lines = [
        f"Report (PBIR-Legacy format): {os.path.basename(rep.path)}",
        f"  pages:   {len(rep.pages)}",
        f"  visuals: {len(rep.visuals)}",
        "",
        "Pages:",
    ]
    per_page = Counter(v.page for v in rep.visuals)
    for p in rep.pages:
        lines.append(f"  - {p}  ({per_page.get(p, 0)} visual(s))")

    lines.append("")
    lines.append("Visual types:")
    for vtype, count in rep.visual_types.most_common():
        lines.append(f"  {vtype}: {count}")

    binds = sorted(rep.all_bindings)
    lines.append("")
    lines.append(f"Distinct field bindings ({len(binds)}):")
    lines += [f"  {b}" for b in binds[:40]]
    if len(binds) > 40:
        lines.append(f"  ...and {len(binds) - 40} more")

    lines.append(
        "\nRead from the legacy report.json. Layout/formatting detail is less "
        "complete than PBIR, but pages, visuals, types and field bindings above "
        "are read from the file, not inferred."
    )
    return "\n".join(lines)
