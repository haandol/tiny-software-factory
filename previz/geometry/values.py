"""Codex 측정값 읽기: 어떤 값이 와도 유한한 숫자, #rrggbb 색, (x, y) 픽셀 점으로 바꾼다."""

from __future__ import annotations

import math
import re

HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
NAN = float("nan")


def to_float(v, default: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def to_hex(v, default: str) -> str:
    m = HEX_RE.match(str(v or "").strip())
    return "#" + m.group(1).lower() if m else default


def to_point(p) -> tuple[float, float] | None:
    """{"x", "y"} → (u, v). dict가 아니거나 좌표가 유한하지 않으면 None."""
    if not isinstance(p, dict):
        return None
    u, v = to_float(p.get("x"), NAN), to_float(p.get("y"), NAN)
    return (u, v) if math.isfinite(u) and math.isfinite(v) else None


def points(seq) -> list[tuple[float, float]]:
    """점 목록(없으면 빈 목록)에서 읽을 수 있는 점만 순서대로 남긴다."""
    return [p for p in (to_point(q) for q in (seq or [])) if p]
