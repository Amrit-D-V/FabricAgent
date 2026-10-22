
from __future__ import annotations

import glob
import json
import os

from util.logger_setup import get_logger

logger = get_logger(__name__)

# Full-width chrome that is expected to sit behind other visuals.
_EXPECTED_UNDERNEATH = {"header_banner"}

# The audit treats <=1px as touching rather than overlapping; match it so the
# two never disagree about the same layout.
_TOLERANCE_PX = 1.0


def _rect(pos: dict) -> tuple[float, float, float, float]:
    x, y = float(pos.get("x", 0) or 0), float(pos.get("y", 0) or 0)
    return x, y, x + float(pos.get("width", 0) or 0), y + float(pos.get("height", 0) or 0)


def overlapping_visuals(
    report_path: str,
    page: str,
    visual_name: str,
    x: float,
    y: float,
    width: float,
    height: float,
) -> list[tuple[str, float, float]]:
    if not report_path or not page:
        return []
    ax1, ay1, ax2, ay2 = _rect(
        {"x": x, "y": y, "width": width, "height": height}
    )
    hits: list[tuple[str, float, float]] = []
    pattern = os.path.join(report_path, "**", "pages", page, "visuals", "*", "visual.json")
    for vp in glob.glob(pattern, recursive=True):
        other = os.path.basename(os.path.dirname(vp))
        if other == visual_name or other in _EXPECTED_UNDERNEATH:
            continue
        try:
            with open(vp, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.debug("unreadable %s: %s", vp, e)
            continue
        if (data.get("name") or other) in _EXPECTED_UNDERNEATH:
            continue
        bx1, by1, bx2, by2 = _rect(data.get("position", {}) or {})
        ix = min(ax2, bx2) - max(ax1, bx1)
        iy = min(ay2, by2) - max(ay1, by1)
        if ix > _TOLERANCE_PX and iy > _TOLERANCE_PX:
            hits.append((data.get("name") or other, round(ix, 1), round(iy, 1)))
    return sorted(hits)


def overlap_note(
    report_path: str,
    page: str,
    visual_name: str,
    x: float,
    y: float,
    width: float,
    height: float,
) -> str:
    hits = overlapping_visuals(report_path, page, visual_name, x, y, width, height)
    if not hits:
        return ""
    detail = ", ".join(f"{n} ({ix:.0f}x{iy:.0f}px)" for n, ix, iy in hits)
    return (
        f"\n⚠ OVERLAP — '{visual_name}' covers {detail}. Both will render, one on "
        f"top of the other. Use pbi_dashboard_layout / pbi_layout_compute to place "
        f"visuals instead of choosing pixel positions, then re-apply the rects it "
        f"returns. (Intentional layering — a shape behind a KPI row — is fine; "
        f"this is only a warning.)"
    )
