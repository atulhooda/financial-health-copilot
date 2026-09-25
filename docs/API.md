# Hisaab API v1: contract

Status: **Frozen 2026-09-25 (end of Phase 6)** together with `docs/openapi.json`, which `hisaab export-openapi` generates from the app; a test fails if the app drifts from the committed file. The Flutter app is built against this doc and that file only. A deliberate change updates the code, this doc and `docs/openapi.json` (`make openapi`) in the same commit.

## 1. Conventions
- Base path `/v1`. JSON UTF-8. Run it with `make api` (uvicorn on :8000); the OpenAPI UI is at `/docs`.
- **Auth (DEV ONLY):** `X-User-Id: demo-a` (1–64 letters, digits, `_` or `-`). There is no real auth in the prototype. The header is described as `DEV ONLY` in the OpenAPI, and production would replace it with a signed session token. Missing → `400 MISSING_USER`; malformed → `400 BAD_USER`.
- **Money:** always a `Quantity` with integer paise plus a pre-formatted display. The app never formats money itself.
- **Language:** `?lang=en|hi|hinglish` (default `en`) localises every `title` and `text`. Numbers are digits in every language.
- **Labels (SPEC D10):** every item is a FACT, a PREDICTION (with confidence) or a RECOMMENDATION (with its simulated impact and assumptions). **Label by who proposed the action:** only an action Hisaab proposes is a RECOMMENDATION; a what-if the user asks about, or one Hisaab only offers to explore, is a *conditional* PREDICTION (`what_if: true`) whose text states its condition ("If you take the 12-month EMI, …").
- **Traceable text:** each item's `text` is built from the same registry, tools and templates as the copilot, and every number in it is one of the response's values. A test runs the copilot's validator over every item of every read endpoint, for A, B and C, in all three languages.
- **Errors:** `{"error": {"code": "NOT_FOUND", "message": "...", "details": {}}}` with 400 / 404 / 409 / 422 / 501 / 503. Schema violations are `422 VALIDATION_ERROR` with the field errors in `details.errors`.

### Shared types
```jsonc
Quantity   { "value": 4000000, "unit": "paise|pct|months|days|years|count|score|date", "display": "₹40,000" }
// pct: percent units (49.9); months/days numbers; date "2026-12-10". A Monte Carlo chance never displays as 0% or
// 100%: "under 1%" / "over 99%", localised (D42), while `value` keeps the measured percent.
Confidence { "label": "High|Medium|Low", "reason": "band held on 64% of past days, target 80%",
             "coverage": Quantity | null /* measured P10-P90 coverage: for the "why trust this" sheet ONLY */,
             "caps": ["1 account not linked"], "origins": 11, "days": 452, "explain": "one-sentence method" }
// The app shows label + reason next to a PREDICTION, never a second percentage (SPEC §6.4).

Fact           { "id": "F20", "key": "score", "title": "Health score", "value": Quantity, "text": "Health score: 53" }
Prediction     { "id": "P7", "key": "bounce.m1.probability", "title": "...", "value": Quantity, "confidence": Confidence,
                 "text": "Chance the Bajaj Finance payment bounces: 50% (Medium confidence)",
                 "what_if": false,
                 "impact": Impact | null, "assumptions": [Assumption] /* set for a what-if */ }
Recommendation { "id": "R1", "action_key": "pay_down_card:acc_…", "type": "pay_down_card", "rank": 1, "title": "...",
                 "text": "Use ₹55,000 of savings to clear your card, … Assumes savings earn 3% a year; you pay the card in full …",
                 "impact": Impact, "confidence": Confidence, "assumptions": [Assumption],
                 "params": { ... "steps": [..] for pay_down_card, "binding" for sweeps (D48) ... },
                 "extra": { "downside": {...}, "post_clear_check": {...} } }
Impact     { "kind": "simulation" | "data",
             "annual_impact": Quantity,            // interest saved - savings interest foregone + cash freed, 12 months
             "monthly_cashflow", "card_interest_saved_12m", "loan_interest_saved_12m", "savings_interest_foregone_12m",
             "lifetime_cost": Quantity | null,
             "score_now", "score_12m_baseline", "score_12m_with_action", "score_delta_12m": Quantity,  // never FACT (D10)
             "months_to_clear_card", "buffer_months_now_after", "buffer_months_12m_baseline",
             "buffer_months_12m_with_action": Quantity | null,
             "bounce_risk_before", "bounce_risk_after": Quantity,   // largest NACH/ACH mandate risk (D34)
             "dip_probability_before", "dip_probability_after": Quantity, "dip_saturated": bool,
             // kind "data" (D40): "accounts_linked_before", "accounts_linked_after", "accounts_known": Quantity, "unlocks": [..]
           }
Assumption { "id": "A1", "key": "savings_interest|pay_in_full_after|unswept_spend_share|emi_rate|tenure|…",
             "text": "interest your savings earn per year (assumed)", "value": Quantity | null /* null: a yes/no */,
             "source": "config|statement|user" }
```

### Envelope (every read endpoint and `/simulate`)
```jsonc
{
  "user_id": "demo-a", "snapshot_id": "snap_…", "as_of": "2026-11-02",
  "facts": [Fact], "predictions": [Prediction], "recommendations": [Recommendation],
  "data": { ... endpoint-specific, typed in the OpenAPI ... },
  "meta": { "engine_version": "0.6.0", "scoring_version": 2, "language": "en", "generated_at": "...+05:30" }
}
```
`facts/predictions/recommendations` are the labelled items the app renders as cards. `data` holds structured values for charts. The app never assigns a label itself. A read with no snapshot yet is `404 NO_SNAPSHOT`.

## 2. Endpoints

### GET `/v1/home`
Facts: the score and each pillar's points. Predictions: the largest EMI bounce risk (if ≥ 10%, D34) and the dip chance (if > 20%). Recommendations: the top 1.
`data`: `{ score: Quantity, band: "Poor|Fair|Good", score_coverage: Quantity, pillars: [{ key, title, weight, value: Quantity|null, pillar_score, contribution, status, note }], top_drag, top_alert: { kind, ref /* the item id */ } | null, linked_accounts, visible_accounts, known_accounts, accounts: [{ account_id, kind, institution, masked: "••4321", status: "linked|sms_only|known_unlinked", role }] }`

### GET `/v1/insights`
All items are facts: spending in the past month by category, drifting categories, and the ratios.
`data`: `{ spending: { period: "last_30d", categories: [{ category, title, amount, share, usual_month, vs_usual: Quantity|null, drifting }], top_merchants: [{ merchant, amount }] /* merchants only, never people */ }, recurring: [{ merchant, kind, cadence, direction, amount, next_due }], overlaps: [{ group, members, monthly_total }], ratios: { savings_rate, buffer_months, emi_to_income, debt_to_income, credit_utilisation, revolving: Quantity|null } }`

### GET `/v1/forecast`
Facts: floor, horizon, next salary date. Predictions: the dip chance, likely dip date, lowest balance, balance before next income, and the top EMI/SIP and other bounce risks.
`data`: `{ available, horizon_days, floor, account: { account_id, institution, masked }, opening, next_income_date, income_basis: "salary|irregular", series: [{ date, p10, p50, p90 }] /* paise, end of day; negative = shortfall */, dip_probability, dip_saturated, likely_dip_date, projected_low, pre_income: { p10, p50, p90 }, bounce_risks: [{ name, kind, mandate, due_date, amount, probability }], scheduled: [{ date, name, kind, direction, amount, source, estimated }], assumptions: [{ key, text, amount, dip_probability_if_kept }] /* D33 */, dip_probability_if_kept, confidence: Confidence, backtest: { origins, days, coverage_p10_p90: Quantity /* the number we quote */, dip_brier, calibration_gap } }`

### GET `/v1/recommendations`
Recommendations: the ranked actions Hisaab proposes. Predictions: the explore-only what-ifs (e.g. a longer loan tenure when bounce risk ≥ 20%, D37), as conditional predictions with their impact and assumptions.
`data`: `{ ranked: [action_key], combined_plan: { actions, impact: Impact, confidence } | null, what_if_offers: [prediction id] }`

### GET `/v1/timeline`
Facts: the score before and now (when there is a previous snapshot).
`data`: `{ snapshots: [{ snapshot_id, seq, trigger, as_of, created_at, score, band, dip_probability, max_mandate_bounce, top_recs: [action_key] }], diffs: [{ from, to, reason_codes: [code], score: {...}, pillar_deltas: [...], rec_changes: [{ action_key, change: "added|removed|rank_changed", from_rank, to_rank, caused_by: [code] }], ... }] }`

### POST `/v1/simulate`
Body: `{ "action": { "type": "new_emi", "params": { "principal_paise": 6000000, "tenure_months": 12, "annual_rate_bps": null } } }`
Types and params: `new_emi {principal_paise (whole rupees), tenure_months 1–120 (default 12), annual_rate_bps?}` · `delay_purchase {amount_paise}` · `change_emi_tenure {loan?}` · `pay_down_card | redirect_sweep | auto_sweep | cancel_overlapping_subs {}` (your own recommendation of that type). Bad params → `422 BAD_PARAMS`.
Response: an envelope. A **what-if** (`new_emi`, `delay_purchase`, `change_emi_tenure`) comes back as `predictions[0]` with `what_if: true`, its impact and assumptions; one of **Hisaab's own action types** comes back as `recommendations[0]`. `data`: `{ kind: "what_if|recommendation", emi?, alt_tenure?: { tenure_months, emi }, total_interest?, emi_to_income_after?, emi_dates?: { options: [{ option, first_due, new_emi_bounce_risk, max_mandate_bounce_risk }], advice } /* D39 */ }`. Nothing is stored.

### POST `/v1/ask[?debug=true]`
Body: `{ "message": "Kya main ₹60,000 ka phone 12 months ki EMI pe le sakta hoon?" }` (1–1,000 characters).
```jsonc
{
  "ask_id": "ask_…", "user_id": "demo-a", "as_of": "2026-11-02", "language": "hinglish",
  "path": "llm|llm_retry|template|guard",
  "fallback_reason": null | "validator" | "tool_cap" | "no_respond" | "refusal" | "deadline" | "llm_error: …" | "llm_unavailable: …",
  "guard": null | "scope" | "distress" | "distress_checkin",
  "checkin": null | "one gentle line with the helpline (tier 2 only)",
  "message": null | "the guard response (tier 1: Tele-MANAS and 112; scope: not a SEBI-registered adviser)",
  "suggestions": [] /* scope only: in-scope questions to ask instead */,
  "facts":           [{ "text": "...", "refs": ["F21","F26"] }],
  "predictions":     [{ "text": "Agar aap 12 mahine ke liye ₹60,000 udhaar lete hain, toh EMI ₹5,415 hogi …", "refs": ["U2","U1","P12","A1"] }],
  "recommendations": [{ "text": "...", "refs": ["R1","A2","A3"] }],
  "sources": { "P12": { "kind": "prediction", "display": "₹5,415", "desc": "the new EMI" } /* every cited id */ },
  "trace": null | { /* COPILOT.md §10 */ }
}
```
Every number in `text` is one of the cited `sources`.

### POST `/v1/ingest/aa` · `/v1/ingest/statement` · `/v1/ingest/sms` · `/v1/ingest/manual`
- `aa`: the AA mock envelope (SPEC §4) as JSON.
- `statement`: `multipart/form-data` with `file` (CSV), `bank` (a `config/banks/<bank>.yaml` key) and `account_masked` (e.g. `XX4321`). A PDF → `501 PDF_NOT_SUPPORTED`.
- `sms`: `{ "transactions": [StructuredSmsTxn] }`, **structured only**. Any unknown field (e.g. `body`, `text`) → `422` (`extra="forbid"`).
- `manual`: `{ "transactions": [ManualTxn] }`.
Response `202`: `{ "ingest_id", "raw_count", "canonical_new", "duplicates_merged", "event_id", "snapshot_id" }`. Recompute is asynchronous (the worker); `?wait=true` recomputes in the request and returns the new `snapshot_id`, as the demo does. A payload the adapter rejects → `422 INGEST_ERROR`.

### POST `/v1/loan-cash/{txn_id}`
D33. Body: `{ "use": "reserve" | "purpose" }`. `reserve` lifts the loan-cash earmark in both the buffer and the forecast; `purpose` restores it. `txn_id` must be a loan disbursal (else `422 NOT_A_LOAN_DISBURSAL`). It triggers a recompute at the current snapshot's date; `?wait=true` returns `{ event_id, snapshot_id }` once it is done.

### POST `/v1/merchant-rules`
A user correction (SPEC D21). Body: `{ "category": "dining", "merchant_key": "m:vaishali_restaurant" }` or `{ "category": "dining", "txn_id": "txn_…" }` (resolves to that transaction's merchant). It applies to every past and future transaction of that merchant (`category_source: "user"`) and triggers a recompute (`?wait=true` as above). An unknown category → `422 UNKNOWN_CATEGORY`; the list is `config/categories.yaml` and includes `uncategorised`.

### DELETE `/v1/user/data`
DPDP erasure. It deletes every row for the user in every table (SPEC D14). Response `200`: `{ "erased": { "transactions": 1234, ... }, "receipt_id", "erased_at" }`. Only the anonymous global validator-block counter survives (tested).

### POST `/v1/demo/replay/{step}`: only when `DEMO_MODE=1`, otherwise 404
`step ∈ t0 | 1 | 2 | 3`. `t0` resets `demo-a` to baseline. The steps apply in order (`409 OUT_OF_ORDER` otherwise). Response: `{ step, snapshot_id, as_of, diff }`.

### GET `/v1/health`
Liveness: `{ db, redis /* null with the in-process bus */, llm_provider, demo_mode, engine_version }`.
