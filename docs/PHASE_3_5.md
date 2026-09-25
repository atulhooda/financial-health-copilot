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

## Result: **FAIL**, so the model was reverted (evaluated once, 2026-09-24)
Criterion 4 held (no changes under `backend/app/demo/`). Criteria 1–3, per row:

| Row | Coverage base → new | Coverage criterion | Calibration gap base → new | Gap criterion | Mean predicted dip base → new | Dip now base → new |
|---|---|---|---|---|---|---|
| A t0 | 0.6542 → 0.8629 | PASS | 0.0807 → 0.2130 | FAIL | 0.65 → 0.78 (observed 0.57) | 1.000 → 1.000 |
| A 1 | 0.6542 → 0.8629 | PASS | 0.0807 → 0.2130 | FAIL | 0.65 → 0.78 (observed 0.57) | 1.000 → 1.000 |
| A 2 | 0.6660 → 0.8151 | PASS | 0.0956 → 0.1966 | FAIL | 0.64 → 0.74 (observed 0.55) | 0.998 → 0.997 |
| A 3 | 0.6620 → 0.8111 | PASS | 0.0956 → 0.1966 | FAIL | 0.64 → 0.74 (observed 0.55) | 0.998 → 0.997 |
| B T0 | 0.6991 → 0.6426 | FAIL | 0.3493 → 0.4734 | FAIL | 0.35 → 0.47 (observed 0.00) | 0.190 → 0.189 |
| C T0 | 0.7937 → 0.7203 | FAIL | 0.0000 → 0.0000 | PASS | 0.00 → 0.00 (observed 0.00) | 0.000 → 0.000 |

**Decision:** as pre-committed, the balance-aware model was reverted. `engines/forecast.py` is back to this freeze
commit's version (byte-identical) and the unconditional bootstrap stays. No parameter was tuned after the run.

### What happened
- **A:** bands got wider (coverage 65–67% → 81–86%), but dip predictions got *more* pessimistic (mean predicted
  65% → 78% against 57% observed). The headline dip stayed at ~100%, so the saturation was not caused by the spend
  model alone.
- **B:** coverage fell 5.7 points and over-warning grew (35% → 47% predicted, 0% observed).
- **C:** coverage fell 7.3 points. The SBI account's large spare balance put it in the top bucket, whose rare big days
  turned a −₹881/month spend error into +₹7,829/month.

### Likely cause (a hypothesis only, not tested here)
The frozen definition, *spare = balance − scheduled debits due in the next 14 days*, ignores **scheduled credits** in
the same window. On the ~10 days before payday, next month's rent, EMI and card payment fall inside the lookahead
but the salary doesn't. So late-cycle days look deeply "short" even for users who are fine, and the low bucket mixes
genuinely broke days with pre-payday days. A variant that nets scheduled credits (spare until the next income) is
the obvious next candidate. It needs its **own** pre-registered evaluation; trying it now would be tuning on the
results above.

### Limitations we keep (documented, not hidden)
1. **A's headline dip probability stays near 100%.** At T0, A opens at ₹5,209 against a ₹5,000 floor with 11 days
   to payday, so almost any normal day breaches it. That is a real, near-certain event, not a model artefact. Phase 4
   leads with **bounce risk**, which does move (8% → 13% → 30% across T0, +2, +3), and emits **no dip-based reason
   codes** (`DIP_RISK_UP`/`DIP_RISK_DOWN` are withheld) because the dip metric is saturated.
2. **Balance-dependent spenders (A) get bands that miss 1 day in 3** (coverage 65–67% vs 80% target). Confidence says
   so: Medium, "band held on 65% of past days, target 80%".
3. **B's over-warning is mostly income-side** (see `docs/FORECAST.md`): payouts are under-predicted by ~38% against
   ~31% on spend. An unrepresentative three-week payout drought at the start of B's history sits in every bootstrap
   pool.

### Separate bug fix made after the revert (not part of this evaluation)
Diagnosing C's +17% income error found that **"last working day of the month" salaries** (C: 31, 30, 29, 30, 31) were
anchored to a fixed day (29), so a salary was predicted inside windows where it really landed just outside. Month-end
anchoring (D23) fixed it: C's coverage went 79.4% → 84.3%, and A and B were unchanged. `docs/FORECAST.md` has the
current numbers.

### Future work (logged after review, 2026-09-24)
- **Salary-aware spare balance:** spare = balance + scheduled credits − scheduled debits until the next income. It is the obvious fix for the hypothesis above. It needs its own pre-registered criteria and a single evaluation, like this one. Deferred.
