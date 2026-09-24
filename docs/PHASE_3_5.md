# Phase 3.5: balance-aware spending in the forecast

Status: **criteria and baseline frozen before the new model was written or run** (this file's first commit).
Results are appended below the freeze line afterwards, never edited above it.

## Why
Persona A's dip probability sits at ~100% at every replay step. The headline prediction carries no information,
and no Phase 4 action could move it. Bounce risk comes from the same spend model. Cause: the bootstrap samples
discretionary spend independently of the balance, but people spend less when the account runs low.

## Method (parameters fixed now; no tuning after seeing results)
- **Spare balance** at the start of day *d* = end-of-day balance of *d − 1* − scheduled debits due in *[d, d + 13]*.
  In history, "scheduled debits" are the actual debits of the account's scheduled series (recurring items, card
  payments, sweeps). In the forecast they are the schedule, and each path's spare uses its own simulated balance.
- **Buckets:** K = 3, by terciles of the user's own historical spare values over the pool window (≤ 180 days).
- **Discretionary debits** are sampled from the stratum *(spare bucket, day of week)*. With < 4 days in the stratum,
  fall back to the spare bucket alone, then to all days. An account without balance history keeps the previous
  unconditional sampling.
- **Discretionary credits** (e.g. gig payouts, refunds) stay unconditional on balance: *(day of week,
  day-of-month bucket)* as before. Income doesn't respond to the balance.
- **Draws:** per-path uniforms fixed up front, so draws don't depend on the schedule. A simulation that changes the
  balance can move a path to another bucket; that is the model's behavioural response, by design.
- Learned per user from their own data. If spending doesn't depend on balance, the buckets look alike.

## Acceptance criteria (from the Phase 3.5 review, made exact)
Evaluated **once** with `hisaab backtest-report` on the same data and seeds as the baseline:
1. **A moves toward 80% coverage:** for each A row, |coverage_new − 0.80| < |coverage_base − 0.80|.
2. **B and C coverage drops ≤ 2 points:** coverage_new ≥ coverage_base − 0.02.
3. **Dip calibration**, gap = |mean predicted dip probability − observed breach frequency| over backtest origins
   whose outcome is observed: for each A row gap_new < gap_base; for B and C gap_new ≤ gap_base.
4. **No generator changes:** nothing under `backend/app/demo/` changes between this commit and the evaluation.

If any criterion fails: revert the model, keep the unconditional bootstrap, and document the limitation here.

## Baseline (current unconditional model), exact
| Row | Origins | P10–P90 coverage | Mean predicted dip | Observed breach rate | Calibration gap | Brier | Dip now |
|---|---|---|---|---|---|---|---|
| A t0 | 8 | 0.6542 | 0.6521 | 0.5714 | 0.0807 | 0.2451 | 1.000 |
| A 1 | 8 | 0.6542 | 0.6521 | 0.5714 | 0.0807 | 0.2451 | 1.000 |
| A 2 | 12 | 0.6660 | 0.6411 | 0.5455 | 0.0956 | 0.1861 | 0.998 |
| A 3 | 12 | 0.6620 | 0.6411 | 0.5455 | 0.0956 | 0.1861 | 0.998 |
| B T0 | 8 | 0.6991 | 0.3493 | 0.0000 | 0.3493 | 0.1858 | 0.190 |
| C T0 | 7 | 0.7937 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.000 |

### Baseline error split, income side vs spend side (predicted − actual, ₹ per 30 days)
| Row | Income side | Spend side | Net |
|---|---|---|---|
| A t0 | +0 (+0.0%) | +2,617 (+2.8%) | -2,617 |
| A 1 | +0 (+0.0%) | +2,617 (+2.8%) | -2,617 |
| A 2 | -3,951 (-4.3%) | +111 (+0.1%) | -4,062 |
| A 3 | -27,245 (-23.7%) | +111 (+0.1%) | -27,355 |
| B T0 | -9,428 (-38.5%) | -6,817 (-30.7%) | -2,611 |
| C T0 | +29,371 (+16.9%) | -881 (-0.6%) | +30,251 |

---
*Freeze line. Everything below was added after the evaluation run.*
