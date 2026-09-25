"""Health score (docs/SCORING.md). A FACT: observed data only (D10). Pure: Metrics -> Score."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.core.config import load_yaml
from app.engines.financial import Metrics


@dataclass
class Pillar:
    key: str
    title: str
    weight: int
    value: float | None  # the metric in its display unit (pct, months, ratio)
    unit: str
    score: float | None  # 0-100
    status: str  # ok | insufficient_data
    reason: str | None = None
    weight_effective: float = 0.0
    contribution: int = 0  # points, largest-remainder rounded so they sum to the total
    contribution_exact: float = 0.0


@dataclass
class Score:
    total: int
    band: str
    pillars: list[Pillar]
    score_coverage: int  # sum of original weights of included pillars
    top_drag: str | None
    version: int
    extra: dict = field(default_factory=dict)


def interpolate(x: float, breakpoints: list[list[float]]) -> float:
    """Piecewise-linear between breakpoints (x ascending), clamped at both ends."""
    pts = sorted(breakpoints)
    if x <= pts[0][0]:
        return float(pts[0][1])
    if x >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    raise AssertionError("unreachable")


def band_for(total: int, bands: dict[str, int]) -> str:
    return max((lo, name) for name, lo in bands.items() if total >= lo)[1]


def _largest_remainder(values: list[float], total: int) -> list[int]:
    floors = [math.floor(v) for v in values]
    rest = total - sum(floors)
    order = sorted(range(len(values)), key=lambda i: (-(values[i] - floors[i]), i))
    for i in order[:max(rest, 0)]:
        floors[i] += 1
    return floors


def compute_score(m: Metrics) -> Score:
    cfg = load_yaml("scoring")
    P = cfg["pillars"]
    pillars: list[Pillar] = []

    def add(key: str, value: float | None, score: float | None, reason: str | None = None) -> None:
        p = P[key]
        ok = score is not None
        pillars.append(Pillar(key, p["title"], p["weight"], value, p["unit"], score,
                              "ok" if ok else "insufficient_data", None if ok else reason))

    has_cycles = m.coverage.get("complete_cycles", 0) >= 1
    # P1 savings rate
    if m.savings_rate is not None and has_cycles:
        v = m.savings_rate * 100
        add("savings", v, interpolate(v, P["savings"]["breakpoints"]))
    else:
        add("savings", None, None, "no income or no complete cycle yet")
    # P2 buffer
    if m.buffer_months is not None:
        add("buffer", m.buffer_months, interpolate(m.buffer_months, P["buffer"]["breakpoints"]))
    else:
        add("buffer", None, None, "no balances or essential spend yet")
    # P3 EMI-to-income
    if m.emi_to_income is not None:
        v = m.emi_to_income * 100
        add("debt", v, 100.0 if m.emi_monthly_paise == 0 else interpolate(v, P["debt"]["breakpoints"]))
    else:
        add("debt", None, None, "no income observed")
    # P4 credit health: needs a linked card (limit + statement); unlinked/SMS-only cards are excluded.
    # Continuous in both utilisation and revolving/limit, so paying card debt down can only raise it.
    if m.credit_utilisation is not None:
        v = m.credit_utilisation * 100
        share = P["credit"]["utilisation_share"]
        rev = (m.revolving_ratio or 0.0) * 100
        s = share * interpolate(v, P["credit"]["breakpoints"]) + (1 - share) * interpolate(
            rev, P["credit"]["revolving_breakpoints"])
        add("credit", v, s)
    else:
        add("credit", None, None, "card not linked" if m.cards else "no credit card")
    # P5 pre-salary liquidity: share of clean cycles minus bounce penalty
    if len(m.cycle_lows) >= P["liquidity"]["min_cycles"]:
        clean = sum(c["clean"] for c in m.cycle_lows) / len(m.cycle_lows)
        s = max(0.0, min(100.0, 100 * clean - P["liquidity"]["bounce_penalty"] * m.bounces))
        add("liquidity", clean * 100, s)
    else:
        add("liquidity", None, None, "fewer than 2 complete cycles with balances")
    # P6 spending stability
    if m.discretionary_ratio is not None:
        add("stability", m.discretionary_ratio, interpolate(m.discretionary_ratio, P["stability"]["breakpoints"]))
    else:
        add("stability", None, None, "no discretionary history yet")

    included = [p for p in pillars if p.status == "ok"]
    wsum = sum(p.weight for p in included)
    if not included:
        return Score(0, "Poor", pillars, 0, None, cfg["version"], {"reason": "insufficient data"})
    for p in included:
        p.weight_effective = p.weight / wsum
        p.contribution_exact = p.weight_effective * p.score
    total = math.floor(sum(p.contribution_exact for p in included) + 0.5)  # half-up, not banker's rounding
    for p, c in zip(included, _largest_remainder([p.contribution_exact for p in included], total), strict=True):
        p.contribution = c
    drag = max(included, key=lambda p: (p.weight_effective * 100 - p.contribution_exact, p.key)).key
    return Score(total, band_for(total, cfg["bands"]), pillars, wsum, drag, cfg["version"])
