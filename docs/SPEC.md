# Hisaab: backend specification

Status: **Phase 0 reviewed and approved (2026-09-23), with the changes recorded below.** This document describes what gets built.

---

## 0. Decisions log (approved)

| # | Topic | Decision |
|---|---|---|
| D1 | Repo layout | The git repo root *is* the `hisaab/` monorepo: `backend/`, `mobile/`, `shared/`, `docs/`, `CLAUDE.md`. |
| D2 | Python | Pinned to **3.12** via uv. LightGBM on macOS needs `libomp`. |
| D3 | Internal transfers | A debit and credit **both visible** on the user's own accounts become `transfer_self` and never count as income or spend. **A transfer to an account we can't see counts as spend.** |
| D4 | Card spend before/after linking | While the card is unlinked, its bill payments are our only view of card spend, so they count as spend (`card_bill_unlinked`). When the card links, the pipeline recomputes the whole history: bill payments become `transfer_self` and itemised card purchases count instead. Card interest, late fees and GST on them always count as spend. Consequence: at +1 spend rises and savings rate falls, because the unpaid part of card spend had been funded by revolving debt. |
| D5 | Clock | All time comes from one injectable `Clock` (`app/core/clock.py`). A test fails on any direct `datetime.now()`, `datetime.utcnow()`, `date.today()` or `time.time()` outside it. Demo clock: T0/+1 as_of 2026-09-20, +2 as_of 2026-11-20 (two hiked paydays), +3 **same as_of** as +2. |
| D5a | Contractual schedules | Linked loan accounts carry an EMI schedule (amount, next due, remaining tenure). Scheduled items = detected recurring ∪ contractual schedules, and the contract wins on overlap. A new loan is therefore known immediately, without waiting for 3 EMIs. |
| D6 | Forecast scope | The forecast covers the **operating account**: the one carrying spends and mandates (salary credit, most UPI debits, NACH/ACH mandates). The emergency buffer counts **all liquid accounts**. |
| D7 | Auto-sweep | The engine picks X: the **largest ₹500 step** such that sweeping X from operating to reserve on each salary day raises the sweep-evaluation dip probability by ≤ `sweep_max_dip_increase_pp` (default 2 pp) over baseline. If X = 0, there's no recommendation. Evaluation window: the first full pay cycle after the next salary (capped at the 45-day horizon), because the first sweep happens on the next salary day. The benefit uses the named parameter **`unswept_spend_share` = 0.5**: that share of unswept money would otherwise be spent, so the net monthly saving = 0.5 × X. It is returned as an assumption. Sweep confidence is **capped at Medium**. Whether a sweep appears at any replay step is an engine outcome. |
| D8 | What-ifs | `delay_purchase` and `new_emi` are what-ifs only (simulate/copilot). They are never auto-recommended. |
| D9 | Distress guard | **Two tiers.** Tier 1, explicit self-harm intent (en / Devanagari / Hinglish): stop all advice, reply briefly and warmly, show the helpline. Tier 2, money-stress idioms ("EMI ne jaan le li", "mar gaya", "this loan is killing me"): one gentle check-in line with the helpline, then answer normally. Keyword lists live in config; both tiers are tested. Helpline (verified): **Tele-MANAS, 14416 or 1-800-891-4416, free, 24x7, multilingual.** |
| D10 | Labels for scores | The current score is a FACT (observed data only). **Any projected score** (combined plan, what-if, 12-month) is a simulation output, labelled RECOMMENDATION with its assumptions and never FACT. |
| D11 | Masking | Regex masking (phones, emails, UPI IDs, account-like digit strings, cards, PAN, Aadhaar, IFSC) applies to **everything** sent to the LLM, **user messages included**. Known names (profile, counterparties, AA holders) are guaranteed; free-typed names are best-effort. The PII test asserts exactly the guaranteed set. |
| D12 | SMS platform | Android only. The demo runs as a sideloaded APK and nothing depends on Play Store approval. |
| D13 | SMS pattern portability | `sms_patterns.yaml` uses a regex subset valid in both Python `re` and Dart `RegExp`: no named groups, no lookbehind, no inline flags. A `fields:` map names each positional group. |
| D14 | Erasure | `DELETE /v1/user/data` hard-deletes every user row, snapshots and logs included. The only survivor is an **anonymous global counter** of validator blocks (`global_counters`, with no user_id and no content), so the demo can still say "validator blocked N answers". This table is the one documented exception to "user_id on every table". |
| D15 | Categoriser accuracy | Reported on unseen merchants, with the caveat that synthetic narrations are optimistic compared with real bank data. |
| D16 | Tool loop | Anthropic `tool_choice={"type":"any"}`, OpenAI-compat `"required"`. The loop ends on `respond`. Cap: **6 tool calls**, then template. If a provider rejects forced tool choice, fall back to `auto` and treat a plain-text reply as a validation failure (one retry, then template). |
| D17 | Safety floor | `floor` = the account's **minimum balance requirement** if known (AA summary or `config/institutions.yaml`), else **₹5,000**, with a per-user override. It is *not* scaled with spending: the floor means "about to run dry", while weeks of spending is what the buffer pillar measures. |
| D18 | Bounce risk | A separate PREDICTION: for each scheduled debit (EMI, SIP, rent, subscription, card bill) in the horizon, `P(operating balance just before the debit < debit amount)` on its due date, reported with the debit's name. It is the harm the deck leads with. |

---

## 1. Architecture

```
Sources ─────────────▶ Pipeline ───────────────▶ Intelligence core ─────────────▶ Copilot ───────▶ API /v1
aa_mock                normalise merchant         financial (FACT)                 guards           home, insights,
statements (CSV/PDF*)  match transfers (D3)       health score (FACT)              tools+registry   forecast, recs,
sms (structured only)  de-dupe across sources     forecast (PREDICTION)            LLMClient+mask   timeline, simulate,
manual                 categorise rule→LightGBM   simulation (RECOMMENDATION)      validator        ask, ingest, erase
        │                         │                        ▲                       templates
        └──── RawTransaction ─────┘   data.ingested ──▶ EventBus ──▶ worker ──▶ immutable Snapshot ──▶ diff
```
`*` PDF: interface plus stub.

### 1.1 Layout
```
backend/
  pyproject.toml  .python-version  alembic.ini  Makefile (root Makefile delegates)
  app/
    core/        config.py, clock.py (the only place time is read), money.py (paise, format_inr, parse_inr), dates.py (IST), seeding.py, ids.py, pii.py
    db/          models.py, session.py, repo.py (user-scoped helpers), migrations/
    ingest/      base.py (SourceAdapter, RawTransaction), aa_mock.py, statements.py, pdf_stub.py, sms.py, manual.py
    pipeline/    merchants.py, transfers.py, dedupe.py, categorise/ (rules.py, model.py, train.py), run.py
    engines/     financial.py, recurring.py, score.py, forecast.py, backtest.py, simulate.py, actions/*.py, emi.py
    events/      bus.py (EventBus, RedisStreamsBus, InProcessBus), worker.py, snapshots.py, diff.py, reasons.py
    copilot/     llm/ (client.py, anthropic.py, openai_compat.py, none.py), tools.py, registry.py, validator.py,
                 numbers.py (extractor), masking.py, guards.py, language.py, intents.py, templates/, orchestrator.py
    api/         main.py, deps.py, schemas.py, routes/*.py
    demo/        generator.py, personas.py, replay.py
    cli.py       Typer: hisaab seed | demo | replay | ask | worker | train-categoriser | export-openapi
  config/        banks/*.yaml (CSV column maps), merchants.yaml, categories.yaml, subscriptions.yaml,
                 guards.yaml, helplines.yaml, assumptions.yaml, scoring.yaml, institutions.yaml
  tests/
shared/sms_patterns.yaml, shared/sms_fixtures/*.yaml
docker-compose.yml (postgres:16, redis:7)
```

## 2. Core conventions
- **Money:** `int` paise everywhere (DB `BIGINT`). `format_inr(paise, style="full"|"compact")` → `₹1,20,000` / `₹1.2 lakh`. Rupees are shown without decimals unless under ₹100.
- **Rates:** stored as basis points (`int`) where persisted. Engines may use float internally but round at the boundary.
- **Dates and time:** `date` for business dates (IST calendar). `timestamptz` for events, converted with `zoneinfo("Asia/Kolkata")`. Time is read **only** through the injected `Clock` (`SystemClock` / `FixedClock`), and engines take `as_of: date`. A test scans the source for direct clock calls (D5).
- **Seeding:** `seed_for(user_id, purpose, extra="") = int(sha256(f"{GLOBAL_SEED}|{user_id}|{purpose}|{extra}"))[:8 bytes]`. NumPy `Generator(PCG64(seed))`. LightGBM: `seed`, `deterministic=True`, `num_threads=1`. Polars: always sort explicitly before any order-dependent step.
- **IDs:** `txn_…`, `acc_…`, `snap_…`, and so on (ULID-like, but generated from seeded counters in demo mode so reruns match).

## 3. Data model (PostgreSQL, SQLAlchemy 2, Alembic)
Every table has `user_id TEXT NOT NULL` and an index leading with `user_id`.

| Table | Key columns |
|---|---|
| `users` | user_id, display_name (PII, never sent to LLM), safety_floor_paise (nullable → D17 default), created_at |
| `counterparties` | user_id, pseudonym (`contact_03`), name (PII, never sent to LLM), first_seen |
| `consents` | consent_id, user_id, purpose_code, fi_types[], scope_accounts[], data_from, data_to, expires_at, status |
| `accounts` | account_id, user_id, kind (savings/current/credit_card/loan), institution, masked_number (`XX1234`), role (operating/reserve/liability), link_status (linked/known_unlinked), known_via (aa/statement/sms/manual/inferred), credit_limit_paise, min_balance_paise, apr_bps, loan_principal_paise, loan_outstanding_paise, loan_rate_bps, loan_tenure_months, emi_paise, emi_next_due, emi_day |
| `balances` | user_id, account_id, as_of_date, balance_paise, source |
| `raw_transactions` | raw_id, user_id, source (aa/statement/sms/manual), source_ref, account_hint, payload_hash, received_at, **no raw SMS text** |
| `transactions` (canonical) | txn_id, user_id, account_id, txn_date, amount_paise (>0), direction (debit/credit), channel (upi/nach/pos/neft/imps/atm/card/other), narration_masked, merchant_key, counterparty_type (merchant/person/self/bank), category, category_source (rule/ml/user), category_confidence, transfer_group_id |
| `transaction_sources` | user_id, txn_id, raw_id, source (all sources of a canonical txn) |
| `recurring_items` | user_id, snapshot_id, merchant_key, kind (income/emi/sip/rent/subscription/bill/other), cadence_days, amount_paise, next_due, active |
| `snapshots` | snapshot_id, user_id, seq, trigger (baseline/data.ingested:<source>/demo:<step>), as_of, created_at, input_hash, payload JSONB (metrics, score, forecast summary+bands, recommendations), **insert-only** (DB trigger rejects UPDATE) |
| `snapshot_diffs` | user_id, from_snapshot_id, to_snapshot_id, reason_codes[], payload JSONB |
| `ask_logs` | user_id, ask_id, language, path (guard/llm/llm_retry/template), validator verdicts JSONB, trace JSONB (masked), created_at |
| `validator_blocks` | user_id, ask_id, attempt, errors JSONB, masked_candidate JSONB |
| `global_counters` | name, value. **No user_id, no content** (D14 exception); only `validator_blocks_total` |

## 4. Sources (`app/ingest/`)
Every adapter implements `SourceAdapter.parse(payload, user_id) -> list[RawTransaction]` plus optional account/balance upserts.

`RawTransaction`: `source, source_ref, account_hint {institution, masked_number, kind}, txn_date, value_date?, amount_paise, direction, narration, balance_after_paise?, channel_hint?, extra{}`.

- **aa_mock:** a JSON envelope `{consent: {id, purpose{code,text}, fiTypes[], dataRange{from,to}, expiry, accounts[]}, fi: [{fiType: DEPOSIT|CREDIT_CARD|TERM_LOAN|RECURRING_DEPOSIT, account{maskedAccNumber, type, fipName}, summary{currentBalance|creditLimit|principal,rate,tenure...}, transactions[{txnId, type: DEBIT|CREDIT, mode, amount, currentBalance, transactionTimestamp, valueDate, narration, reference}]}]}`. It is loosely modelled on the ReBIT FI schemas (field names borrowed, not validated against XSD). The adapter rejects expired consents, and FI types outside the consent scope. A disclaimer is in code and docs: *we are not an FIU. This mock sits behind the same interface a licensed AA/FIU partner adapter would implement.*
- **statements:** CSV. `config/banks/<bank>.yaml` maps columns (`date`, `narration`, `debit`, `credit` or `amount`+`dr_cr`, `balance`), date format, skip rows and sign conventions. Adding a bank needs no code. PDF: a `PdfStatementAdapter` with the same interface, raising `NotImplementedError("PDF import stubbed")`.
- **sms:** `shared/sms_patterns.yaml` has entries `{id, bank, kind: debit|credit|emi|card_spend, sender_ids[], regex, fields: {1: amount, 2: account_last4, 3: merchant, 4: date}, date_format}`. Fixtures in `shared/sms_fixtures/` pair each sample SMS with its expected output. `app/ingest/sms.py` has two parts: (a) `reference_parse(sms_text)` for tests and demo replay only; (b) the API adapter, which accepts **only** `StructuredSmsTxn {client_ref, pattern_id, sender_id, account_last4, amount_paise, direction, merchant_raw, txn_date, reference?}`. `client_ref` is an opaque on-device id used for idempotency. `reference` is the UPI ref / UTR used for cross-source dedupe. The API schema has no free-text body field, and a test asserts this.
- **manual:** `{account_id?, txn_date, amount_paise, direction, merchant, category?}`. `category_source=user` if given.

## 5. Pipeline (`app/pipeline/`)
1. **Merchant normalisation:** channel-specific regexes turn narrations into `(channel, merchant_raw, counterparty_type, ref)`. Examples: `UPI/DR/412345/SWIGGY/YESB/…`, `NACH-DR-BAJAJFIN-…`, `POS 4321XXXX AMAZON`, `NEFT-HDFC0001234-RAHUL SHARMA-…`. `merchants.yaml` maps aliases to a canonical `merchant_key` (`swiggy`, `netflix`, …). A UPI ID ending in a personal VPA, or NEFT/IMPS to a name that isn't in the merchant map, gives `counterparty_type=person`. The name is replaced with a per-user stable pseudonym (`contact_03`), and the mapping lives only in the DB.
2. **Transfer matching (D3/D4):** pair a debit and a credit across the user's *visible* accounts with the same amount, within 3 days, and a bill-pay or self-transfer signature (or a narration naming the other account's last 4). Both get `category=transfer_self` and a shared `transfer_group_id`. Unmatched legs: a card bill payment to a known-unlinked card → `card_bill_unlinked` (**spend**). A self-transfer out to an unseen account → `transfer_unseen` (**spend**). A self-transfer in from an unseen account → `transfer_in_unseen` (neither income nor spend). Loan EMI debits stay `emi` even when the loan account is linked; liability-account legs (`loan_repayment_in`, `loan_disbursal_out`) are excluded from income and spend. Card purchases, `card_interest` and `fees_charges` (incl. GST on them) on a linked card are always spend. The pipeline **recomputes a user's full history on every ingest** (cheap at this scale), which is what makes the D4 reclassification retroactive. Card bill payments also let the pipeline infer a `known_unlinked` card account (institution + last 4).
3. **De-dupe:** a candidate pair has the same `user_id` and account (matched on masked last-4 + institution), the same `amount_paise` and direction, `|date diff| ≤ 1 day`, and merchant similarity ≥ 0.8 (normalised token-set ratio; equal `merchant_key` passes). Source priority for the canonical record is AA > statement > SMS > manual. Every source is kept in `transaction_sources`. The process is idempotent: re-ingesting the same payload (same `payload_hash`) is a no-op.
4. **Categorise:** (a) `user` overrides; (b) rules: `merchant_key → category` plus channel rules (NACH to a lender → `emi`, SIP ACH to an AMC/BSE → `sip_investment`); (c) the LightGBM fallback. Features: char 2–5-gram TF-IDF on the normalised narration, plus log-amount, direction, channel one-hot, day-of-month. It is trained on seeded synthetic labelled narrations (`hisaab train-categoriser`); the model artefact is written to `backend/artifacts/` along with the train seed and its hash. Evaluation holds out **unseen merchants** (a group split by `merchant_key`) and reports accuracy and macro-F1 in `docs/CATEGORISER.md`, with caveat D15. If ML confidence < 0.5, the category is `other` with `category_source=ml`.

Category set (`categories.yaml`; `essential` flag in brackets; `spend: false` for transfer_self, transfer_in_unseen, loan_repayment_in, loan_disbursal_out, income_*, refund, loan_disbursal, sip_investment): income_salary, income_gig, income_other, refund, loan_disbursal, rent[E], emi[E], sip_investment, groceries[E], utilities[E], telecom[E], education[E], health[E], insurance[E], fuel[E], transport, food_delivery, dining, shopping, ott_subscription, entertainment, travel, cash_withdrawal, fees_charges, card_interest, transfer_self, transfer_person, transfer_unseen, transfer_in_unseen, card_bill_unlinked, loan_repayment_in, loan_disbursal_out, other.

## 6. Intelligence core (`app/engines/`)
All engines are pure: `f(frames, accounts, as_of, params) -> dataclass`. Money outputs are in paise.

### 6.1 Financial engine (FACTS)
Windows: "month" = pay cycle when a salary cadence is detected, otherwise the calendar month. "Trailing" = the last 3 complete cycles.
- `income_monthly`: median monthly income credits (income_* categories; excludes refunds, disbursals and self transfers).
- `spend_by_category`: per cycle and for the last 30 days, with share of total.
- `savings_rate` = (income − consumption spend − EMIs − fees/interest) / income. SIPs count as saving.
- `emergency_buffer_months` = liquid balance (all linked savings/current) / monthly essential spend (essential categories incl. rent & EMIs).
- `emi_to_income` = monthly EMIs / income. `debt_to_income` = total outstanding (loans + card balance) / annual income.
- `credit_utilisation` = card statement balance / limit, per card and overall. `revolving_balance` = the unpaid portion carried past the due date (payment < statement balance).
- `recurring` (below). `overlapping_subscriptions`: ≥2 active subscriptions in the same group from `subscriptions.yaml` (e.g. `ott_video`: Netflix, Prime Video, JioHotstar, SonyLIV, Zee5).
- `spending_drift`: per category, last 30 days vs the trailing 3-cycle median. Flagged if > +25% **and** > ₹1,000.

### 6.2 Recurring detection
Group debits and credits by `(merchant_key, direction)`. Candidate cadences are 7, 14, 30, 91 and 365 days. A group qualifies if its median inter-arrival gap is within tolerance (±2 days for 7/14, ±4 for 30, ±10 for 91), the coefficient of variation of amounts is ≤ 0.15 (EMIs, SIPs and subscriptions are usually exact), and it has ≥3 occurrences (≥2 for 91-day cadence). Salary gets a day-of-month anchor with a weekend/holiday roll-back. `next_due` = the last date plus the cadence, snapped to the anchor. `amount` = the median of the last 3. `kind` comes from the category. Irregular income (Persona B) is *not* recurring; the forecast treats it through the bootstrap.

### 6.3 Health score: see `docs/SCORING.md`.

### 6.4 Forecast engine (PREDICTIONS)
- **Balance:** the operating balance (D6) at `as_of`. Horizon: 45 days.
- **Scheduled component:** every scheduled item (detected recurring ∪ contractual loan schedules, D5a) projected to its due dates in the horizon (salary, EMIs, SIPs, rent, subscriptions, card bill payment). Card purchases hit cash only via the card bill payment, whose amount = the latest statement's expected payment (full, or the observed payment ratio if the user revolves).
- **Discretionary component:** non-recurring operating-account flows per historical day. For each future day we sample one historical day's net discretionary flow, stratified by `(day_of_week, dom_bucket)` where dom_bucket = 1–10 / 11–20 / 21–31. If a stratum has < 4 days, fall back to day-of-week only. 1,000 paths, seed `seed_for(user_id,"forecast")`.
- **Outputs:** daily P10/P50/P90; `next_income_date` (next salary, or +30 days if irregular); `dip_probability` = share of paths whose minimum before `next_income_date` is < floor; `likely_dip_date` = the mode of first-dip dates among dipping paths (null if the probability is < 5%); `projected_low` = P50 of per-path minimum over the next 30 days; `floor` (D17, SCORING.md §4).
- **Bounce risk (D18):** for each scheduled debit in the horizon, `bounce_probability` = share of paths where the operating balance at the start of its due date (after that day's earlier credits, before debits) is below the debit amount. Returned as a list `{name, kind, due_date, amount, probability}` sorted by probability. `name` is the merchant/lender name; P2P payees are pseudonymised, e.g. "Rent (contact_01)".
- **Confidence:**
  - `calibration` = min(1, band_hit_rate / 0.80), where band_hit_rate = share of actual daily balances inside P10–P90 over a **rolling-origin backtest**. Origins every 15 days from day 60 of history to `as_of − 15`. Each origin refits recurring and bootstrap on data before the origin only, and forecasts up to 45 days or until `as_of`. If there are < 2 origins, calibration = 0.5.
  - `history` = min(1, months_of_history / 6). `linkage` = linked accounts / known accounts.
  - `confidence_pct` = round(100 × calibration × history × linkage). Label: **High ≥ 75, Medium 50–74, Low < 50**.
  - **One sentence for a judge:** *"Confidence is how often our 10-to-90% band actually contained your real balance when we replayed the forecast over your own past months, discounted if we have under six months of history or can see you have accounts you haven't linked."*

### 6.5 Simulation engine (RECOMMENDATIONS)
An action is a pure transform on the engine inputs: `(recurring schedule, balances, liabilities, txn frame) → modified inputs`. The simulator re-runs the financial, score and forecast engines on the modified inputs **with the same seed** (common random numbers), so deltas come from the action and not from Monte Carlo noise.

Impact returned per action:
`monthly_cashflow_paise` (+ = more money), `annual_cashflow_paise`, `lifetime_cost_paise` (debt actions: total extra/saved interest), `score_now → score_after`, `score_delta`, `score_12m_baseline → score_12m_with_action`, `dip_probability_before/after` (pp change), `buffer_months_after`, `confidence` (from the forecast), `assumptions[]` (each with id, text, value, source=`config|statement|user`).

**12-month projection:** a deterministic monthly roll-forward. Buffer += expected net saving, loans amortise (reducing balance), a revolving card accrues at APR minus the observed payment, and subscriptions continue. The score is recomputed on the month-12 state. Every projected score is labelled **RECOMMENDATION** and carries its assumptions (D10).

**Generators** (auto-recommended, D8):
| action | Generated when | Transform | Key assumption |
|---|---|---|---|
| `pay_down_card` | revolving_balance > 0 | Pay `min(revolving, reserve − floor − next-30d net scheduled outflow)` from the reserve account. Partial if cash is short. | APR from the statement, else `assumptions.yaml: card_apr_bps = 4200` (~3.5%/month) |
| `cancel_overlapping_subs` | an overlap group exists | Keep the longest-running sub in the group and drop the rest | none (observed prices) |
| `auto_sweep` | engine-chosen X > 0 (D7: largest ₹500 step raising dip probability by ≤ 2 pp) | Move X from operating to reserve on each salary day | `unswept_spend_share = 0.5`; confidence capped at Medium |
| `change_emi_tenure` | EMI-to-income > 30% or dip ≥ 40%, with an active loan | Extend the remaining tenure by 12 months at the same rate | loan rate from AA, else `assumptions.yaml` |

**What-ifs** (never auto): `new_emi {principal, tenure_months, annual_rate_bps?}`: EMI = P·r·(1+r)^n / ((1+r)^n − 1), r = annual/12, rounded to the nearest rupee. If no rate is given, `assumptions.yaml: consumer_emi_rate_bps = 1500` is used and returned as an explicit assumption. The result also includes the EMI at an alternative tenure when feasible. `delay_purchase {amount, from_date, to_date}`: a one-off outflow moved in the schedule.

**Ranking:** sort by `score_12m_with_action − score_12m_baseline` descending, then by `annual_cashflow_paise` descending, then by action key (stable). **Combined plan:** apply the top 3 in rank order, each re-simulated on the previous result (so cash used by pay-down isn't double-spent), and report the combined 12-month score.

Each recommendation has a stable `action_key` (e.g. `pay_down_card:acc_card1`, `cancel_overlapping_subs:ott_video`) so diffs can track it across snapshots.

## 7. Events and snapshots (`app/events/`)
- `EventBus` interface: `publish(topic, event)` and `subscribe(topic, group, handler)`. `RedisStreamsBus` is the default (stream `hisaab:events`, consumer group `recompute`, XACK after the snapshot commits). `InProcessBus` is used for tests and is available via `EVENT_BUS=inprocess` (the demo fallback if Redis dies).
- Ingest commits the transactions, then publishes `data.ingested {user_id, source, raw_ids, as_of}`.
- The worker recomputes metrics, score, forecast and recommendations for the user, then inserts a snapshot. `input_hash` = hash of the canonical txns + accounts + as_of + engine version. If it matches the latest snapshot, nothing new is written (idempotent).
- **Diff** (`diff.py`) of snapshot n−1 → n gives: score/band change, per-pillar deltas, metric deltas, dip-probability change, and recommendations new/removed/rank-changed. Each change gets ≥1 reason code.
- **Reason codes** (`reasons.py`, deterministic rules):
  `ACCOUNT_LINKED`, `HIGH_COST_DEBT_FOUND` (revolving balance or APR ≥ 24% debt newly visible), `INCOME_INCREASED` / `INCOME_DECREASED` (last 2 income credits ≥ 5% above / below the prior median), `NEW_EMI_ADDED` (new EMI recurring item or loan account/disbursal), `EMI_CLOSED`, `SUBSCRIPTION_OVERLAP_FOUND`, `SUBSCRIPTION_CANCELLED`, `SPENDING_DRIFT_UP`, `DIP_RISK_UP` / `DIP_RISK_DOWN` (|Δ| ≥ 10 pp), `SCORE_BAND_CHANGED`, `CONFIDENCE_CHANGED` (label change), `REC_ADDED`, `REC_REMOVED`, `REC_RANK_CHANGED`. Each `REC_*` carries `caused_by: [reason codes]`.

## 8. Copilot: see `docs/COPILOT.md`. API: see `docs/API.md`. Demo: see `docs/DEMO.md`.

## 9. Synthetic data (`app/demo/`)
`generator.simulate(persona, seed) -> World`: one continuous, seeded, day-by-day simulation of each persona's accounts. `World` renders *raw source payloads* (AA JSON, bank CSVs, raw SMS text) sliced by date and account, so the demo goes through the real pipeline. Cross-source duplicates: ≈15% of Persona A's UPI debits also arrive as SMS, rendered as raw text and parsed by the on-device reference parser into structured SMS transactions. Replay steps are **slices of one simulated world**, so balances stay consistent across steps.

Persona parameters are **generator inputs**, not engine outputs. Engine numbers are whatever the engines compute; they are written to `docs/DEMO_NUMBERS.md` after `make demo`.

- **Behaviour model (all personas).** Discretionary spend is Poisson-count / log-normal-amount streams with weekend and pay-cycle effects. Persona A also has a **spend-down** behaviour: on weekends it spends a share of the operating balance above (obligations due in the next 14 days + a threshold). That is the "unswept money gets spent" behaviour D7's `unswept_spend_share` names, so the sweep assumption is grounded in how the persona is generated. The ledger enforces balances: an overdrawing UPI payment is declined, and an overdrawing mandate bounces with a return charge.
- **A (golden), Pune, salaried.** History 2026-03-21 → 2026-09-20 (inclusive; `as_of` = last day of data). Accounts: salary (operating), savings (reserve), existing loan (AA TERM_LOAN), credit card (**unlinked at T0**, visible only through bill payments from the salary account). The world runs to 2026-11-20. The salary hike (+12%) starts with the Oct 1 credit, and a new personal loan (AA TERM_LOAN with EMI schedule, disbursal credited 2026-11-20, first EMI 2026-12-05) is released only at +3. Net salary ₹92,000 on the 1st (rolled back for weekends). Rent via NEFT to the landlord (a personal name, which exercises PII masking). SIP on the 7th. Three OTT subs (Netflix, Prime Video, JioHotstar). Card: limit ₹1,50,000, pays ~40% of the statement each month, so revolving converges to ~₹40,000 (target band ₹36k–₹44k). Discretionary spend is tuned so balances dip before salary.
- **B, gig worker, Bengaluru.** Weekly irregular platform payouts (CV ~0.35), rent, a phone EMI, no card, 6 months.
- **C, family, Delhi NCR.** Two salaries, home loan + car loan EMIs, quarterly school fees, a card paid in full, 6 months.

**Story checks** (tests on the *generator + engines*, to catch persona drift; they fail loudly instead of us tuning the numbers by hand): A at T0 is in the Fair band; A at +1 has a revolving balance within ₹36k–₹44k; A has 1 overlap group with 3 subs; A at T0 has pre-salary dip probability > 50% (against the D17 floor); A's spend rises and savings rate falls from T0 to +1 (D4). If one of these fails, we change **persona parameters** and record the change in `docs/DEMO.md`. We never special-case the engine.

## 10. Operations
- `docker-compose.yml`: postgres:16 (host port **5433** by default, `HISAAB_PG_PORT` to change; 5432 is often taken by another local Postgres) and redis:7, with healthchecks. An init script creates `hisaab_test` for the Postgres-only tests.
- `Makefile`:
  - `up`: compose up + wait + `alembic upgrade head`
  - `seed`: train the categoriser if the artefact is missing, generate A/B/C, ingest A at T0 and B/C in full, run the worker once
  - `demo`: reset demo-a, replay T0 → +3 through the real bus, print the timeline, run the golden `ask` with `LLM_PROVIDER=none` and (if a key is set) the configured provider, write `docs/DEMO_NUMBERS.md`
  - `test`: pytest
- Env: `DATABASE_URL`, `REDIS_URL`, `EVENT_BUS=redis|inprocess`, `LLM_PROVIDER=anthropic|openai_compat|none`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY`, `DEMO_MODE=0|1`, `HISAAB_SEED` (default 20260920), `COPILOT_DEBUG_TRACE=0|1`.

## 11. Test plan (all green before each commit)
EMI math (₹60,000 @ 15%: 12m → ₹5,415 ±1, 18m → ₹3,743 ±1) · AA+SMS dedupe → 1 canonical txn with 2 sources · transfer matching (no double count after card link; retroactive reclassification at +1) · no direct clock calls outside `clock.py` · recurring and overlap detection on A · forecast determinism (same seed → identical bands and dip probability, bit-for-bit) · validator adversarial suite · PII masking over every LLM payload (fake adapter captures payloads) · guards short-circuit before the LLM (tier 1) or prepend a check-in (tier 2) · replay T0→+3 emits the reason codes in DEMO.md · SMS patterns compile under the shared subset and match their fixtures · the SMS API rejects any free-text body · erasure leaves zero rows for the user in every table · categoriser holdout report is produced.
