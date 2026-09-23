# Demo script: Persona A replay

Status: **Approved 2026-09-23.** The reason codes in the "Must emit" column are asserted by `tests/test_replay.py`. The "Expected" column is the story we expect. It is **not** asserted, and it gets corrected against real engine output in `docs/DEMO_NUMBERS.md`.

## 1. Commands
```bash
make up        # postgres + redis + migrations
make seed      # categoriser, personas A/B/C, A at T0
make demo      # replay T0 → +3, print timeline, run golden ask (none + real LLM if key set)
hisaab ask --user demo-a "Kya main ₹60,000 ka phone 12 months ki EMI pe le sakta hoon?"
```
**No WiFi:** `LLM_PROVIDER=none make demo`. **No Redis:** `EVENT_BUS=inprocess make demo`. Both paths are exercised in CI.

## 2. Demo clock (SPEC D5)
The demo never reads wall-clock time. All time comes from the injected `FixedClock`. Seed: `HISAAB_SEED=20260920`. Every step is a slice of **one** simulated world (SPEC §9), so balances are continuous.

| Step | as_of | What is ingested | Via |
|---|---|---|---|
| **T0** | 2026-09-20 | 6 months (2026-03-21 → 09-20): salary a/c, savings a/c, existing loan (with its EMI schedule) via AA; ~15% of salary-account UPI debits also arrive via on-device-parsed SMS. Card **not linked**, and its bill payments are the only view of card spend. | aa, sms |
| **+1** | 2026-09-20 | Credit card linked via AA (CREDIT_CARD: limit, APR, 6 months of transactions incl. interest and GST). The pipeline recomputes history: bill payments → `transfer_self`, itemised purchases count (D4). | aa |
| **+2** | 2026-11-20 | Two more months of all linked accounts, with the Oct 1 and Nov 1 salary credits **hiked** (generator param: +12%). | aa, sms |
| **+3** | 2026-11-20 (**no clock jump**) | A new personal loan appears as a **newly linked AA loan account with its EMI schedule** (first EMI 2026-12-05), plus the disbursal credit on 2026-11-20 in the salary account. It is known immediately through the contractual schedule (D5a). | aa |

## 3. Expected changes
| Step | Must emit (asserted) | Expected story (not asserted) |
|---|---|---|
| **T0** | trigger `baseline`, no diff | Fair band. Card pillar excluded (coverage < 100%). 3 of 4 known accounts linked → confidence discounted. Dip risk before the Oct 1 salary > 50% (₹5,000 floor, D17). Bounce risk shown for a named debit. Recs include `cancel_overlapping_subs:ott_video`. Whether `auto_sweep` appears is the D7 rule's call. |
| **+1** | `ACCOUNT_LINKED`, `HIGH_COST_DEBT_FOUND`, `REC_ADDED` (for `pay_down_card:*`, caused_by `HIGH_COST_DEBT_FOUND`) | Revolving ~₹40k surfaces. **Spend rises and savings rate falls** (D4: the unpaid part of card spend had been funded by revolving debt). Credit pillar enters at ≤ 40 → score drops. Confidence rises (4 of 4 linked). `pay_down_card` probably ranks #1. |
| **+2** | `INCOME_INCREASED` | Dip risk falls (`DIP_RISK_DOWN` likely). Score rises (savings pillar). A sweep may appear or grow; that is an engine outcome of the D7 rule, not scripted. |
| **+3** | `NEW_EMI_ADDED`, `ACCOUNT_LINKED` | EMI-to-income rises. Dip/bounce risk rises (`DIP_RISK_UP` likely; bounce risk on the new EMI on 2026-12-05). `change_emi_tenure` rank moves. The disbursal is **not** income. |

## 4. Golden question (after +3)
`Kya main ₹60,000 ka phone 12 months ki EMI pe le sakta hoon?`
- Language `hinglish`, intent `afford_emi`, user inputs U1 = ₹60,000 and U2 = 12 months.
- Tools: get_metrics → forecast → simulate_action(new_emi, 60000, 12). Rate unspecified → assumption 15% p.a. (A1).
- EMI must equal **₹5,415 (±1)**. The alternative 18-month EMI is ₹3,743 (±1).
- Expected shape (numbers filled from the engine at demo time, not written here):
  - FACT: income and current EMI-to-income.
  - PREDICTION: dip probability before next salary, now vs with the phone EMI, with confidence.
  - RECOMMENDATION: EMI at the assumed 15%, EMI-to-income after, score delta, the 18-month alternative, and the rate stated as an assumption.
- It must pass the validator with a real LLM **and** with `LLM_PROVIDER=none` (template path).

## 5. Persona parameter changes log
| Date | Parameter | Change | Why |
|---|---|---|---|
| 2026-09-23 | Persona A `spend_down_*` (new) | Added weekend spend-down: 60% of operating balance above next-14-day obligations + ₹16,000 | Without it, surplus piled up (salary-account low rose ~₹10k/month) and there was no pre-salary squeeze. The behaviour is the one D7's `unswept_spend_share` names. |
| 2026-09-23 | Persona A `card_pay_ratio` | 0.40 → 0.32 | Revolving at link time was ~₹30k with 0.40, outside the ₹36k–₹44k story band. With 0.32 it is ~₹40.3k (ledger-level). The engine-level check lands in Phase 2. |
| 2026-09-23 | Persona A card statement before history | Added the March statement's payment (due 2026-04-07) | The first in-window payment was missing, which understated early card payments. |

## 6. Judge Q&A crib
- *"Does the AI make up numbers?"* No. Open the trace: every number has a registry id from a tool call, and the validator blocks anything else. Show a blocked attempt from `validator_blocks`.
- *"How confident is the forecast?"* Give the confidence sentence (SPEC §6.4).
- *"Are you an AA?"* No. The mock sits behind the interface a licensed FIU partner adapter would use.
- *"Does SMS leave the phone?"* No. The API has no field that can hold SMS text (422 on any extra field).
- *"Investment advice?"* No. The scope guard refuses before the LLM is called. We're not a SEBI RIA.
