"""docs/SCORING.md and config/scoring.yaml must agree; the worked example must reproduce."""
import dataclasses
import datetime as dt
import re

from app.core.config import REPO_DIR, load_yaml
from app.engines.financial import Metrics
from app.engines.score import compute_score, interpolate

DOC = (REPO_DIR / "docs" / "SCORING.md").read_text()


def _doc_table(title_regex: str) -> list[list[float]]:
    block = DOC[re.search(title_regex, DOC).end():]
    rows = [r for r in block.split("\n") if r.startswith("|")][:3]
    xs = [float(re.sub(r"[^\d.]", "", c)) for c in rows[0].strip("|").split("|")]
    ys = [float(c) for c in rows[2].strip("|").split("|")]
    return [[x, y] for x, y in zip(xs, ys, strict=True)]


def test_doc_and_config_agree():
    cfg = load_yaml("scoring")["pillars"]
    for key, title in [("savings", r"\*\*P1 Savings rate\*\*"), ("buffer", r"\*\*P2 Emergency buffer"),
                       ("debt", r"\*\*P3 EMI-to-income\*\*"), ("credit", r"U, utilisation:"),
                       ("stability", r"\*\*P6 Spending stability\.\*\*")]:
        assert _doc_table(title) == [[float(x), float(y)] for x, y in cfg[key]["breakpoints"]], key
    assert _doc_table(r"R, revolving \(carried past the due date\) as a share of the limit:") == [
        [float(x), float(y)] for x, y in cfg["credit"]["revolving_breakpoints"]]
    weights = dict(re.findall(r"\| P\d \| ([^|]+?) \| (\d+) \|", DOC))
    assert {v["title"]: v["weight"] for v in cfg.values()} == {k: int(v) for k, v in weights.items()}
    assert "0.5 × U(utilisation) + 0.5 × R(revolving / limit)" in DOC and cfg["credit"]["utilisation_share"] == 0.5
    assert "25 × observed_bounce_charges" in DOC and cfg["liquidity"]["bounce_penalty"] == 25


def _metrics(**kw) -> Metrics:
    base = {f.name: None for f in dataclasses.fields(Metrics)}
    base.update(as_of=dt.date(2026, 9, 20), cycle_basis="salary", cycles=[], income_pattern="salaried",
                income_monthly_paise=1, salary_level_paise=1, earmarked_loan_paise=0,
                other_income_monthly_paise=0, spend_monthly_paise=0, revolving_ratio=None,
                essential_monthly_paise=1, discretionary_monthly_paise=1, emi_monthly_paise=1,
                debt_outstanding_paise=0, cards=[], recurring=[], debt_growth=None, overlaps=[], spend_by_category=[],
                drift=[],
                cycle_lows=[], bounces=0, floor_paise=500000, coverage={"complete_cycles": 6})
    base.update(kw)
    return Metrics(**base)


def test_scoring_worked_example():
    lows = [{"clean": i < 3} for i in range(6)]  # 3 of 6 clean -> 50
    s = compute_score(_metrics(savings_rate=0.12, buffer_months=1.5, emi_to_income=0.18, credit_utilisation=None,
                               cycle_lows=lows, discretionary_ratio=1.10))
    assert s.total == 60 and s.band == "Fair" and s.score_coverage == 85
    assert sum(p.contribution for p in s.pillars) == s.total
    assert [p.key for p in s.pillars if p.status != "ok"] == ["credit"]


def test_credit_pillar_is_continuous_and_bounces_penalise_liquidity():
    lows = [{"clean": True}] * 4
    def credit(ratio, util):
        s = compute_score(_metrics(savings_rate=0.2, buffer_months=3, emi_to_income=0.1, credit_utilisation=util,
                                   revolving_paise=1, revolving_ratio=ratio, cycle_lows=lows, bounces=1,
                                   discretionary_ratio=1.0, cards=[object()]))
        by_key = {p.key: p.score for p in s.pillars}
        return by_key["credit"], by_key["liquidity"]
    scores = [credit(r / 100, 0.05 + r / 100)[0] for r in range(0, 60)]
    assert all(a >= b for a, b in zip(scores, scores[1:], strict=False))  # less revolving -> never lower
    assert max(abs(a - b) for a, b in zip(scores, scores[1:], strict=False)) < 5  # no cliff anywhere
    assert credit(0.0, 0.05)[0] == 100 and credit(0.0, 0.05)[1] == 75


def test_interpolation_is_clamped_and_monotone():
    bp = load_yaml("scoring")["pillars"]["debt"]["breakpoints"]
    assert interpolate(0, bp) == 100 and interpolate(99, bp) == 0 and interpolate(18, bp) == 84
