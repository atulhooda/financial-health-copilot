# Copilot: tools, registry, validator, guards

Status: **Approved 2026-09-23.** Contract for `backend/app/copilot/`.

## 1. Turn pipeline
```
user text
  │ 1. normalise (NFC, Devanagari digits ०-९ → 0-9)
  │ 2. distress guard ─tier1─▶ helpline response (no LLM, no advice)      path=guard
  │                   ─tier2─▶ prepend check-in + helpline, continue
  │ 3. scope guard    ──hit──▶ not-an-adviser + redirect                  path=guard
  │ 4. detect language (hi = Devanagari majority; hinglish = Latin + Hindi lexicon hits; else en)
  │ 5. extract user-typed numbers → registry kind=user_input
  │ 6. LLM_PROVIDER=none? ──yes──▶ template(intent)                        path=template
  │ 7. LLM loop (≤6 tool calls, else template), every payload masked (user message included)
  │ 8. `respond` → validator
  │      pass ─▶ answer                                                   path=llm
  │      fail ─▶ retry once, errors attached ─▶ pass ─▶ answer            path=llm_retry
  │                                           └ fail ─▶ template(intent)  path=template (fallback)
  │ 9. re-hydrate masked tokens (e.g. [CONTACT_2] → name) locally, after validation
  ▼ group into facts / predictions / recommendations, log, (trace if debug)
```
Guards and language detection are deterministic and run **before** any network call. Tier-1 distress and scope hits never reach the LLM (tested with an adapter that fails the test if it's called).

## 2. LLMClient
```python
class LLMClient(Protocol):
    def chat(self, system: str, messages: list[Msg], tools: list[ToolSpec],
             tool_choice: Literal["any", "auto"]) -> LLMReply  # text | tool_calls[]
```
- Adapters: `AnthropicClient` (Messages API, `tool_choice={"type":"any"}`), `OpenAICompatClient` (`/chat/completions`, `tool_choice="required"`, works with Groq, Cerebras and OpenRouter by changing `LLM_BASE_URL`), `NoneClient` (raises `LLMUnavailable`, which routes to templates).
- If a provider rejects forced tool choice (HTTP 400 mentioning tool_choice), the adapter retries that call with `auto` and remembers this for the process. A plain-text reply under `auto` counts as a validation failure (`NO_RESPOND`): one retry, then template.
- Loop cap: **6 tool calls** in total (data tools + `respond` attempts). Hitting the cap → template.
- Config comes only from env: `LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY`. **No model name in code.** If `LLM_MODEL` is unset with a real provider, startup fails with a clear error.
- Temperature 0. Timeout 20 s. Any transport error → template path (logged).
- **Masking is inside `LLMClient.chat`**, a wrapper applied to every adapter, so no adapter can bypass it. `system`, `messages` and tool results all pass through `mask()`.

## 3. Masking (`masking.py`)
Applied to every string in the outbound payload (recursively through JSON):

| PII | Rule | Replacement |
|---|---|---|
| Card numbers | 13–19 digits, optional spaces/dashes, Luhn-valid | `[CARD]` |
| Aadhaar | `\d{4}\s?\d{4}\s?\d{4}` | `[AADHAAR]` |
| Account numbers | 9–18 consecutive digits, or masked forms like `XX1234` / `****1234` | `[ACCOUNT]` |
| Phone | `(\+91[\s-]?)?[6-9]\d{9}` | `[PHONE]` |
| Email | standard | `[EMAIL]` |
| UPI ID | `[\w.\-]+@[a-z]{2,}` with no dot after `@` (e.g. `@okaxis`, `@ybl`, `@paytm`) | `[UPI]` |
| PAN | `[A-Z]{5}\d{4}[A-Z]` | `[PAN]` |
| IFSC | `[A-Z]{4}0[A-Z0-9]{6}` | `[IFSC]` |
| Known names | user display name, P2P counterparties, AA holder/nominee names (per-user dictionary, case-insensitive, token-boundary) | `[NAME_n]` / `[CONTACT_n]` |
| Unknown names | best-effort only, **not asserted by tests**: capitalised tokens in the bundled Indian first-name list (SPEC D11) | `[NAME_n]` |

The user's own message goes through the same regex masking before it is sent (D11).

**Numbers are safe from masking by construction.** Tools never send raw paise integers to the LLM, only display strings (`₹1,20,000`) and registry ids. Registry display strings are placed on an allowlist that masking skips. Test: a fake adapter records every payload across the adversarial and golden conversations. None of them may match any regex PII class or contain any **known** name (persona profile names, counterparties, AA holders) or any seeded account number, phone, email or UPI ID. This includes user messages seeded with a phone, email, UPI ID and account number. Free-typed unknown names are out of the test's scope by design.

## 4. Tools
All tools are read-only and scoped to the turn's `user_id` (never an LLM argument). They read the **latest snapshot**, except `simulate_action`, which runs the simulator. Every numeric field is returned as a **registry entry** instead of a bare number:
```json
{"id": "F7", "kind": "fact", "desc": "monthly income (median, last 3 cycles)", "display": "₹92,000"}
```
Id prefixes: `F` fact, `P` prediction, `R` recommendation, `A` assumption, `U` user input. Ids are unique per turn.

| Tool | Args (JSON Schema, summarised) | Returns |
|---|---|---|
| `get_metrics` | `{}` | score, band, pillars[{name, value, score, contribution, status}], income, spend, savings_rate, buffer_months, emi_to_income, debt_to_income, credit_utilisation, revolving_balance, floor. All `fact`. |
| `get_spending` | `{period: "last_30d"\|"last_cycle"\|"trailing_3"="last_30d", top_n: int 1..10 = 6}` | categories[{category, amount, share, vs_median_pct, drifting}], top_merchants[{merchant, amount}] (never persons), all `fact`. |
| `get_recurring` | `{kind: "all"\|"income"\|"emi"\|"sip"\|"rent"\|"subscription" = "all"}` | items[{merchant, kind, cadence, amount, next_due}], overlaps[{group, count, members[], monthly_total}]. `fact`. |
| `forecast` | `{}` | horizon_days, next_income_date, floor, dip_probability, likely_dip_date, projected_low, balance_p10/p50/p90 at next_income_date − 1, bounce_risks[{name, kind, due_date, amount, probability}] (top 3), confidence{pct, label}. `prediction` (floor, horizon, debit amounts and due dates: `fact`). |
| `list_recommendations` | `{limit: int 1..5 = 3}` | recs[{rank, action_key, title, impact{monthly, annual, score_delta, score_12m, dip_before, dip_after, buffer_after}, confidence, assumptions[]}], combined_plan. `recommendation` / `assumption`. |
| `simulate_action` | `{action: {type: "new_emi"\|"pay_down_card"\|"cancel_overlapping_subs"\|"auto_sweep"\|"change_emi_tenure"\|"delay_purchase", params: {...}}}`. `new_emi.params = {principal_rupees: int, tenure_months: int, annual_rate_pct?: number}` | same impact shape plus `emi`, `alt_tenure{tenure, emi}`, `emi_to_income_after`, `total_interest`. `recommendation` / `assumption`. |
| `explain_change` | `{from: "previous"\|snapshot_id = "previous"}` | score_before/after, band change, pillar deltas, reason_codes[], rec changes[{action_key, change, from_rank, to_rank, caused_by[]}]. Values `fact`; forecast deltas `prediction`. |

Tool args that carry money (`principal_rupees`) must also appear as `user_input` numbers in the user's text or as registry values. The orchestrator rejects a tool call whose numeric args aren't in the registry. So the LLM can't simulate a number it made up.

## 5. `respond` (the only way to answer)
```json
{
  "name": "respond",
  "input_schema": {
    "type": "object",
    "required": ["language", "statements"],
    "properties": {
      "language": {"enum": ["en", "hi", "hinglish"]},
      "statements": {
        "type": "array", "minItems": 1, "maxItems": 8,
        "items": {
          "type": "object", "required": ["label", "text", "refs"],
          "properties": {
            "label": {"enum": ["FACT", "PREDICTION", "RECOMMENDATION"]},
            "text":  {"type": "string", "maxLength": 400},
            "refs":  {"type": "array", "items": {"type": "string"}}
          }
        }
      }
    }
  }
}
```
**System prompt rules** (summarised): use only the tool outputs; copy numbers exactly from `display`; digits only, no number words; one claim per statement; every PREDICTION states its confidence; every RECOMMENDATION states its simulated impact; reply in the user's language and script (Hinglish in Latin script); never recommend specific securities, funds or crypto.

## 6. Number registry (`registry.py`)
`Entry {id, kind: fact|prediction|recommendation|assumption|user_input, unit: inr|pct|months|days|count|date|score, value: Decimal|date, display: [forms…], confidence_ref?}`.
Display forms are generated by `format_inr` and friends, e.g. ₹1,18,540 → `₹1,18,540`, `1,18,540`, `118540`, `₹1.19 lakh`, `1.2 lakh`, `118.5k`. The validator doesn't depend on the list, though: it parses text numbers back to values (below).

## 7. Validator (`validator.py`)
**Extraction** (`numbers.py`, tested in isolation). It normalises Devanagari digits first, then finds:
- Money: `(₹|Rs\.?|INR|रु\.?)\s?N` or `N\s?(rupees|rupaye|रुपये)`, where N is Indian or Western grouping with optional decimals and an optional scale `k|K|thousand|hazaar|हज़ार|lakh|lac|L|लाख|cr|crore|करोड़`.
- Percent: `N\s?(%|percent|pratishat|प्रतिशत)`.
- Durations: `N\s?(months?|mahine|mahino|महीने|महीनों|days?|din|दिन|years?|saal|साल)`.
- Dates: `D Mon [YYYY]` (English and Hindi month names), `YYYY-MM-DD`, `DD/MM/YYYY`.
- Score: `N/100`, or N next to `score|स्कोर`.
- Bare numbers: anything else.

Each extracted number carries `(value, unit, precision)`, where precision is the place value of the last stated digit (`1.2 lakh` → ₹10,000; `₹1,18,540` → ₹1; `52.5%` → 0.1).

**Matching.** A text number matches a registry entry if the units are compatible (a bare number is compatible with count/score/days/months) and `round(entry.value, precision) == text.value`. Precision must keep **≥ 2 significant figures** (`1 lakh` for ₹1,49,000 is rejected). Dates must match exactly.

**Rules.** Each violation gives an error with a code; any error blocks the answer.
| Code | Rule |
|---|---|
| `NUM_UNMATCHED` | Every extracted number must match ≥1 registry entry from **this turn**. |
| `REF_UNKNOWN` | Every `ref` must exist in the registry. |
| `REF_MISSING` | Each matched entry must be listed in the statement's `refs`. |
| `LABEL_KIND` | Allowed kinds of matched entries: **FACT** → fact, user_input. **PREDICTION** → prediction, fact, user_input, assumption. **RECOMMENDATION** → recommendation, assumption, fact, user_input. Projected scores (12-month, combined plan, what-if) are registered as `recommendation`, so a FACT quoting one is blocked (D10). |
| `PRED_NO_CONF` | A PREDICTION must contain its confidence **label** (High/Medium/Low, or ऊँचा/मध्यम/कम, zyada/medium/kam). It may add the reason line ("band held on 65% of past days, target 80%"). |
| `CONF_AS_PCT` | Confidence is never phrased as a percentage ("82% confident", "65% sure", "confidence of 70%"). The only confidence number is the backtest coverage, and only in the reason-line form (SPEC §6.4). |
| `REC_NO_IMPACT` | A RECOMMENDATION must match ≥1 `recommendation`-kind entry (a simulated impact). |
| `NUM_WORDS` | Money scale words with no digit (`do lakh`, `ek hazaar`, `two thousand`), or English number words `one…twenty` next to a unit. Best-effort. |
| `LANG_MISMATCH` | `language` must equal the detected language, and the script must fit (hi → Devanagari majority; en/hinglish → Latin majority). |
| `PII_LEAK` | The masking detector runs on the output as well. |

**Retry.** The error list goes back as the tool result of the failed `respond`: `{"ok": false, "errors": [{"statement": 2, "code": "NUM_UNMATCHED", "detail": "'₹5,500' not in registry"}]}`. After one retry, the answer falls back to the template. Every block is written to `validator_blocks`.

**Adversarial suite** (must block): wrong amount (₹5,500 for a ₹5,415 EMI); invented percentage; a FACT citing a dip probability; a PREDICTION without confidence; a RECOMMENDATION with no impact number; a refs list pointing to an id from a different turn; `do lakh` in words; the ≥2-significant-figure rounding abuse. **Must pass:** the golden correct answer, in all three languages.

## 8. Guards (`guards.py`, lexicons in `config/guards.yaml`)
- **Distress, tier 1** (checked first): explicit self-harm intent in English, Hindi (Devanagari) or Hinglish, e.g. "suicide", "kill myself", "end my life", "khudkushi", "aatmahatya", "marna chahta hoon", "jeena nahi chahta", "आत्महत्या", "मरना चाहता". It triggers with or without money context (SPEC D9). Response: all advice stops; one short, warm, non-judgemental message in the user's language plus the helpline. `path=guard`, `guard="distress"`, and the LLM is never called.
- **Distress, tier 2**: money-stress idioms, e.g. "EMI ne jaan le li", "mar gaya", "this loan is killing me", "dimaag kharab", "EMI se pareshan", "जान ले ली". Response: one gentle check-in line with the helpline is **prepended**, then the question is answered normally (LLM or template). `guard="distress_checkin"`. The check-in line is fixed text from config and is not validated as a statement.
- Tier 1 wins over tier 2. Both lexicons live in `config/guards.yaml`, and each tier has positive and negative test cases.
- **Helpline** (`config/helplines.yaml`, verified): **Tele-MANAS: 14416 or 1-800-891-4416, free, 24x7, multilingual.**
- **Scope**: securities/crypto/tips/"guaranteed return" lexicon ("which stock", "share tip", "multibagger", "crypto", "bitcoin", "F&O", "intraday", "IPO", "best mutual fund", "guaranteed return", "double my money", "kaunsa share", "paisa double"). Response: *not a SEBI-registered investment adviser*, then a redirect with 2–3 in-scope suggestions (budget, debt, cash-flow). Allowed: questions about the cash-flow effect of an existing SIP (e.g. "should I pause my SIP to clear the card?" gets simulated as cash flow, with no fund-specific advice).

## 9. Intents and templates (`intents.py`, `templates/{en,hi,hinglish}.yaml`)
Deterministic keyword + slot intents, used for `LLM_PROVIDER=none` and as the fallback:
| Intent | Triggers (examples) | Tool plan | Statements |
|---|---|---|---|
| `afford_emi` | afford, EMI, "le sakta", "kharid", "ले सकता" plus an amount | get_metrics → forecast → simulate_action(new_emi, amount, tenure or 12) | FACT income & current EMI/income · PREDICTION dip before/after with confidence · RECOMMENDATION EMI, EMI/income after, score delta, alt tenure, rate assumption |
| `where_money` | "where is my money", "paisa kahan", "खर्च" | get_spending → get_recurring | FACT top 3 categories · FACT drift · RECOMMENDATION top rec if spend-related |
| `run_short` | "run short", "kam padega", "salary se pehle", "bounce" | forecast → list_recommendations(1) | PREDICTION dip probability + date + confidence · PREDICTION top bounce risk (named debit) · RECOMMENDATION top rec |
| `score_change` | "score", "kyun badla", "why changed" | explain_change | FACT score before/after + band · FACT top reason codes (localised text) · RECOMMENDATION top new rec |
| `summary` (default) | anything else | get_metrics → list_recommendations(1) | FACT score/band/top drag · RECOMMENDATION top rec |

Templates fill slots only from registry display strings, and the output **is run through the same validator** (a test asserts every template passes for A, B and C at every replay step).

## 10. Trace (`/v1/ask?debug=true` or `COPILOT_DEBUG_TRACE=1`)
`trace = {path, provider, model, language, guard, tool_calls[{name, args, result_masked}], registry[entries], attempts[{candidate, verdict, errors[]}], masked_prompt_sha256}`. It drives the app's "why trust this" sheet. Masked content only.
