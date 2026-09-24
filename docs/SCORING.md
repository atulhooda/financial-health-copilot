# Hisaab Financial Health Score

Status: **Version 2 (pre-Phase-5 fix, 2026-09-24): the score never punishes paying down debt.** v1 capped the credit pillar at 40 whenever any balance revolved, and counted savings sitting against card debt as a buffer. So redirecting money to the card *lowered* the projected score. Floor per SPEC D17. Parameters live in `backend/config/scoring.yaml`, and this doc and that file must agree (a test checks the weights and breakpoints).

## 1. Principles
- **0–100, six pillars, fixed weights.** Every pillar maps a single observed metric to 0–100 by **piecewise-linear interpolation** between documented breakpoints. Values beyond the end breakpoints are clamped.
- **Observed data only.** The current score is a FACT (SPEC D10). Nothing in it comes from the forecast. Any projected score (12-month, combined plan, what-if) is a simulation output, labelled RECOMMENDATION with its assumptions and never FACT.
- **Transparent.** The API returns each pillar's input value, pillar score, weight and **contribution** (points added to the total).
- **Missing data is excluded, not guessed.** If a pillar can't be computed, it gets `status: "insufficient_data"`, its weight is redistributed proportionally across the other pillars, and `score_coverage` (sum of original weights of included pillars) is returned. Example: a user with no credit card, or a card not linked yet.

## 2. Pillars

| # | Pillar | Weight | Metric (definition in SPEC §6.1) |
|---|---|---|---|
| P1 | Savings | 20 | `savings_rate`, trailing 3 cycles |
| P2 | Emergency buffer | 20 | `emergency_buffer_months`, net of visible revolving card debt |
| P3 | Debt load | 20 | `emi_to_income` |
| P4 | Credit health | 15 | half utilisation, half revolving/limit (continuous) |
| P5 | Pre-salary liquidity | 15 | share of clean cycles (stayed above floor, no bounce), last ≤6 cycles |
| P6 | Spending stability | 10 | discretionary spend drift ratio |

### Breakpoints (value → pillar score)

**P1 Savings rate**
| ≤ 0% | 10% | 20% | ≥ 30% |
|---|---|---|---|
| 0 | 50 | 75 | 100 |

**P2 Emergency buffer (months of essential spend).** Buffer money = liquid balance − unspent recent loan cash (D29/D33) − **visible revolving card balance** (savings sitting against card debt of the same size are not a buffer; an unlinked card's unknown balance is not guessed). Essential spend includes Uncategorised (D21). Negative buffer money counts as 0.
| 0 | 1 | 3 | ≥ 6 |
|---|---|---|---|
| 0 | 30 | 70 | 100 |

**P3 EMI-to-income** (lower is better; 40–50% is where Indian lenders' FOIR limits start to bite)
| ≤ 10% | 30% | 40% | ≥ 60% |
|---|---|---|---|
| 100 | 60 | 30 | 0 |
No EMIs → 100.

**P4 Credit health** (continuous: paying card debt down can only raise it)
`P4 = 0.5 × U(utilisation) + 0.5 × R(revolving / limit)`, with utilisation = latest statement balance / limit:

U, utilisation:
| ≤ 10% | 30% | 50% | ≥ 90% |
|---|---|---|---|
| 100 | 75 | 40 | 0 |

R, revolving (carried past the due date) as a share of the limit:
| 0% | 5% | 15% | 30% | ≥ 50% |
|---|---|---|---|---|
| 100 | 70 | 40 | 15 | 0 |

No card known → excluded (`no credit card`). Card known but not linked, **including a card visible only through SMS** (no limit, no statement) → excluded (`card not linked`), and it lowers forecast *confidence* instead.

**Properties (tested):** moving money from savings to revolving card debt never lowers the current score (P2's buffer money is unchanged, P4 rises). Paying card debt down faster with the same spending never lowers the 12-month projected score.

**P5 Pre-salary liquidity (observed).** Over the last ≤6 complete pay cycles (calendar months if income is irregular), a cycle is **clean** if the operating balance never went below the safety floor and no bounce/return charge was posted.
`P5 = 100 × clean_cycles / cycles − 25 × observed_bounce_charges`, clamped to 0–100. Needs ≥2 cycles.
(The floor is fixed, not spend-scaled; see §4. Depth of cushion in weeks of spending is P2's job.)

**P6 Spending stability.** `ratio` = discretionary spend (non-essential, non-transfer, non-investment) in the last 30 days / trailing 3-cycle median.
| ≤ 1.00 | 1.15 | 1.30 | ≥ 1.60 |
|---|---|---|---|
| 100 | 70 | 40 | 0 |

## 3. Total, contributions and bands
```
w'_i          = w_i / Σ_{included} w_j            (renormalised weight)
contribution_i = w'_i × pillar_i                   (points)
score         = round(Σ contribution_i)
```
Displayed contributions are rounded with the **largest-remainder method**, so they sum exactly to `score`.

| Band | Score |
|---|---|
| **Poor** | 0–49 |
| **Fair** | 50–69 |
| **Good** | 70–100 |

The API also returns `top_drag`: the included pillar with the largest `(w'_i × 100 − contribution_i)`, i.e. the most points left on the table. The home screen alert and the templates use it.

## 4. Safety floor
`floor` = the operating account's minimum balance requirement if known (AA summary or `config/institutions.yaml`), else **₹5,000**. The user can override it (`users.safety_floor_paise`). It is deliberately **not** scaled with spending: the floor means "about to run dry". A spend-scaled floor would keep the alert on for most users and make the headline dip probability meaningless.
One sentence: *"Your floor is the point where your account is about to run dry: your bank's minimum balance, or ₹5,000."*

Bounce risk (a PREDICTION, not part of the score) is a separate signal: the probability that the balance is below a named scheduled debit on its due date (SPEC D18).

## 5. Worked example (illustrative inputs, **not** persona numbers)
Savings rate 12% → P1 = 50 + (2/10)·25 = 55. Buffer 1.5 months → P2 = 30 + (0.5/2)·40 = 40. EMI/income 18% → P3 = 100 − (8/20)·40 = 84. Card unlinked → P4 excluded. 3 of 6 cycles clean, no bounces → P5 = 50. P6 ratio 1.10 → 80.
Included weights: 20+20+20+15+10 = 85. Score = (20·55 + 20·40 + 20·84 + 15·50 + 10·80) / 85 = 5130 / 85 = 60.35 → **60, Fair**, `score_coverage = 85%`.

## 6. Versioning
`scoring.yaml` carries `version`. Snapshots store the version. Changing weights or breakpoints bumps the version and is recorded here.
