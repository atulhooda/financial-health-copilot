# Demo script: Persona A replay

Status: **Approved 2026-09-23.** The reason codes in the "Must emit" column are asserted by `tests/test_replay.py`. The "Expected" column is the story we expect. It is **not** asserted, and it gets corrected against real engine output in `docs/DEMO_NUMBERS.md`.

## 1. Commands
```bash
make up        # postgres + redis + migrations
make seed      # categoriser, personas A/B/C, A at T0
make demo      # replay T0 → +3, print timeline, run golden ask (none + real LLM if key set)
hisaab ask --user demo-a "Kya main ₹60,000 ka phone 12 months ki EMI pe le sakta hoon?"
```
**No WiFi:** `LLM_PROVIDER=none make demo`. **No Redis:** `EVENT_BUS=inprocess make demo`. **Neither:** `make demo-offline`. All three paths are exercised by the tests. `make demo` also writes `docs/DEMO_NUMBERS.md` (the engine's numbers for every step, B and C, and the golden answer with its sources). The app can drive the same replay through the API: `DEMO_MODE=1 make api`, then `POST /v1/demo/replay/t0` … `/3`.

### Demo-day runbook (Phase 5 review, D52)
- **Key:** the demo machine has its **own** provider key, with a spending limit set in the provider's console. It lives only in that machine's `backend/.env` (gitignored; names in `backend/.env.example`), never in chat, logs or git. `make hooks` installs the pre-push secret scan.
- **Model:** the one `hisaab eval-copilot --candidates` picked (`docs/COPILOT_EVAL.md`), with `COPILOT_DEADLINE_S` set from its p95.
- **Rehearse the fallback** before going on stage: `make demo-offline` (no Redis, no LLM) must print the full timeline and the golden answer from templates. If the network or the key fails live, answers fall back to templates by themselves (`fallback_reason` says why); nothing else changes, because every number comes from the engine either way.

## 2. Demo clock (SPEC D5, D41)
The demo never reads wall-clock time. All time comes from the injected `FixedClock`. Seed: `HISAAB_SEED=20260920`. Every step is a slice of **one** simulated world (SPEC §9), so balances are continuous. **Every step sits 2–3 days after A's payday** (pre-Phase-5 fix 6): our pitch is early warning, with a month of runway, not a certainty 11 days out.

| Step | as_of | What is ingested | Via |
|---|---|---|---|
| **T0** | 2026-09-03 | ~5.5 months (2026-03-21 → 09-03): salary a/c, savings a/c, existing loan (with its EMI schedule) via AA; ~15% of salary-account UPI debits and all card spends arrive via on-device-parsed SMS. The card is visible (SMS) but not linked. | aa, sms |
| **+1** | 2026-09-03 | Credit card linked via AA (CREDIT_CARD: limit, APR, statements incl. interest and GST). | aa |
| **+2** | 2026-11-02 | Two more months of all linked accounts, with the Oct 1 and Oct 30 salary credits **hiked** (+12%). | aa, sms |
| **+3** | 2026-11-02 (**no clock jump**) | A new personal loan appears as a **newly linked AA loan account with its EMI schedule** (first EMI 2026-12-05), plus the disbursal credit on 2026-11-02, the +3 date. It is known immediately through the contractual schedule (D5a). | aa |

### Decision: card SMS exist before +1 (SPEC D19)
Android SMS permission is all-or-nothing, so the phone sees the Axis card's spend SMS from day one. At T0 the card is **visible** (`sms_only`), its purchases count as spend, and the card bill payments are transfers. Nothing is counted twice. **The +1 reveal is the statement:** the revolving balance, interest and GST, the limit and the utilisation. It is not the purchases. (`PersonaA(card_sms=False)` is the invisible-card variant used by the D4 retro-reclassification test.)

## 3. Expected changes
| Step | Must emit (asserted) | What the engine shows (not asserted; numbers in `DEMO_NUMBERS.md`) |
|---|---|---|
| **T0** | trigger `baseline`, no diff | Fair. Card visible via SMS but not linked → credit pillar excluded, 3 of 4 linked → confidence capped at Medium. **Dip before the Oct 1 salary ≈ 91% (not saturated)**, with the Oct 10 EMI a named bounce risk. **Top suggestion: "Link your AXIS card statement so we can see its balance"** (data action, D40). No savings push while card debt is unknown (D30e). The tenure-extension what-if is offered (bounce ≥ 20%), extra interest first. |
| **+1** | `ACCOUNT_LINKED`, `NEW_DATA_REVEALED`, `HIGH_COST_DEBT_FOUND`, `REC_ADDED` (for `pay_down_card:*`, caused_by `HIGH_COST_DEBT_FOUND`) | The whole score drop is new data (D24). The buffer now nets the revealed revolving balance (D36); the band may change. `DEBT_GROWING` fires (revolving grew over the last 3 statements). **#1: clear the card from savings, then set autopay to the full statement**, keeping a cushion, cash-neutral, confidence Medium, with the rebuild downside shown (D38). The link action is removed, caused by `ACCOUNT_LINKED`. The dip saturates (≥ 98%) once the real card payments are known, so no dip codes. |
| **+2** | `INCOME_INCREASED` | `BOUNCE_RISK_DOWN` (the hike lowers the largest EMI bounce risk). Card-first actions stay on top; **no savings sweep is ever suggested while the card revolves**. No payday card sweep appears: A has no observed monthly surplus to sweep (D48: income − spend − SIP − the ₹8,000 transfer is below zero in the trailing cycles), so the sustainability limit is zero before the bounce rule is even tried. |
| **+3** | `NEW_EMI_ADDED`, `ACCOUNT_LINKED`; and **not** `NEW_DATA_REVEALED` (a brand-new loan is behaviour) | `BOUNCE_RISK_UP` (the new EMI lifts the largest EMI bounce risk). Loan cash is earmarked out of buffer and forecast (D33), with the "if you keep it" alternative. Tenure extension is offered as an explore-only what-if (D37). |

Rule: if an expected story item doesn't hold, we first check the persona parameters and then the engine logic. We never special-case A. Any persona parameter change is recorded in §5.

### Demo beat: Uncategorised → correction → rule (Phase 3 review, point 7)
Persona A pays a home-tiffin service, **GHARGUTI DABBA**, ₹1,800 on the 3rd of each month on the Axis card. The ML fallback can't place the name (confidence < 0.9), so it shows as **Uncategorised**, which counts as essential. In the app, the user taps it and picks "Dining". `POST /v1/merchant-rules` creates a per-user rule: every past and future Gharguti Dabba transaction becomes `dining` with `category_source: user`, and the next snapshot reflects it. We do not calibrate the categoriser for the demo; the correction flow is the point.

## 4. Golden question (after +3)
`Kya main ₹60,000 ka phone 12 months ki EMI pe le sakta hoon?`
- Language `hinglish`, intent `afford_emi`, user inputs U1 = ₹60,000 and U2 = 12 months.
- Tools: get_metrics → forecast → simulate_action(new_emi, 60000, 12) → list_recommendations(1). Rate unspecified → assumption 15% p.a. (A1).
- EMI must equal **₹5,415 (±1)**. The alternative 18-month EMI is ₹3,743 (±1).
- Expected shape (numbers filled from the engine at demo time, not written here; the latest run is in `docs/DEMO_NUMBERS.md`):
  - FACT: income and current EMI-to-income.
  - PREDICTION: the largest EMI bounce risk today, with confidence.
  - **Conditional PREDICTIONs** (the user's what-if, D10 as corrected in the Phase 5 review): "If you borrow ₹60,000 over 12 months, the EMI would be ₹5,415 …" with EMI-to-income after and the projected score; "If you take it, the biggest EMI bounce risk would be X (Bajaj Finance), against Y now"; "If you take it over 18 months instead, the EMI would be ₹3,743". Each states its confidence and the assumed 15% rate.
  - RECOMMENDATION: the EMI-date advice when D39 gives one, and Hisaab's own top recommendation.
- It must pass the validator with a real LLM **and** with `LLM_PROVIDER=none` (template path).

## 5. Persona parameter changes log
| Date | Parameter | Change | Why |
|---|---|---|---|
| 2026-09-23 | Persona A `spend_down_*` (new) | Added weekend spend-down: 60% of operating balance above next-14-day obligations + ₹16,000 | Without it, surplus piled up (salary-account low rose ~₹10k/month) and there was no pre-salary squeeze. Persona design only; not evidence for `unswept_spend_share` (SPEC D25). |
| 2026-09-23 | Persona A `card_pay_ratio` | 0.40 → 0.32 | Revolving at link time was ~₹30k with 0.40, outside the ₹36k–₹44k story band. With 0.32 it is ~₹40.3k (ledger-level). The engine-level check lands in Phase 2. |
| 2026-09-24 | Persona A `card_sms` (new, True) | Card spend SMS rendered for every card purchase | The D19 decision above. |
| 2026-09-24 | Persona A `sweep_to_savings`, `spend_down_threshold`, `card_pay_ratio` | 4,000 → 8,000; 16,000 → 20,000; 0.32 → 0.30 | Engine run at T0 gave 45 (Poor): savings rate −2.8%, 0 of 5 clean cycles. The first two changes make A a "save first, run short later" user (T0: 63 Fair, 2 of 5 clean cycles, latest cycle low ≈ ₹0). The third restores revolving to ₹38.7k at +1 (engine-level), inside the band. |
| 2026-09-24 | Persona C `annual_prime`, `netflix_monthly` (new) | Annual Prime (₹1,499, May) + Netflix monthly on card | Exercises annual-plan detection and an overlap with an annual member (D23). |
| 2026-09-24 | Replay dates (D41), not a persona parameter | T0/+1 2026-09-20 → 2026-09-03; +2/+3 2026-11-20 → 2026-11-02; the new loan's disbursal moves with the +3 date | Early warning with a month of runway (pre-Phase-5 fix 6). No behaviour parameter changed. |
| 2026-09-24 | Persona A `tiffin` (new) | ₹1,800 on the 3rd, on the Axis card | The Uncategorised demo beat. It sits on the card so it only reaches cash through the 30% card payment: on the salary account it pushed A's T0 balance below the floor (₹4,302), turning the dip "forecast" into a fact dated today. On the card, T0 opens at ₹5,209 and revolving at +1 is ₹42.4k (in band). |
| 2026-09-23 | Persona A card statement before history | Added the March statement's payment (due 2026-04-07) | The first in-window payment was missing, which understated early card payments. |

## 6. Judge Q&A crib
- *"Does the AI make up numbers?"* No. Open the trace: every number has a registry id from a tool call, and the validator blocks anything else. Show a blocked attempt from `validator_blocks`.
- *"How confident is the forecast?"* Give the confidence sentence (SPEC §6.4) and the coverage from `docs/FORECAST.md`, e.g. "our band held on 65% of Aarav's past days; target 80%, so we say Medium."
- *"Are you an AA?"* No. The mock sits behind the interface a licensed FIU partner adapter would use.
- *"Does SMS leave the phone?"* No. The API has no field that can hold SMS text (422 on any extra field).
- *"Investment advice?"* No. The scope guard refuses before the LLM is called. We're not a SEBI RIA.
