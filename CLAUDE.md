# Hisaab — rules for working in this repo

Monorepo: `backend/` (FastAPI brain), `mobile/` (Flutter, later, consumes the frozen API only), `shared/` (artefacts both use, e.g. `sms_patterns.yaml`), `docs/` (source of truth for behaviour).

Read `docs/SPEC.md` first. `docs/SCORING.md`, `docs/COPILOT.md`, `docs/API.md` and `docs/DEMO.md` are contracts: change the doc in the same commit as the code, never after.

## Non-negotiables
1. **The LLM never produces a number.** Numbers come only from deterministic engines via tool calls. Every number in every answer is validated against this turn's registry (tool outputs + user-typed numbers). Fail → one retry with the error list → deterministic template. Every block is logged.
2. **Money is integer paise** (`int`, suffix `_paise`). No floats for money, ever. Dates are `Asia/Kolkata`. All ₹ formatting goes through `app/core/money.py::format_inr` (Indian grouping: ₹1,20,000).
3. **`user_id` on every table and every query.** No query without a user filter; repository helpers enforce it. Sole exception: `global_counters` (anonymous, no content; SPEC D14).
4. **Determinism.** Every random process takes a seed from `app/core/seeding.py::seed_for(user_id, purpose)` (sha256-based, never Python `hash()`). Time is read **only** via `app/core/clock.py` (`Clock`); direct `datetime.now()`/`date.today()`/`time.time()` elsewhere fails a test. Engines take `as_of`. Same data in → same numbers out.
5. **PII never reaches the LLM.** All LLM traffic goes through `LLMClient`, which masks every payload (account numbers, phones, emails, UPI IDs, PAN/Aadhaar, card numbers, personal names). P2P counterparties are pseudonymised before tools return them.
6. **No LangChain / agent frameworks.** Direct SDK calls behind `app/copilot/llm/client.py::LLMClient`.
7. **`LLM_PROVIDER=none` must always work** (template answers). Never hardcode a model name; read `LLM_MODEL`.
8. Every output item is labelled FACT, PREDICTION (with confidence) or RECOMMENDATION (with simulated impact).
9. If the spec looks wrong, say so before building around it.

## Working
- Python 3.12 via `uv` (`backend/.python-version`). `make up | seed | demo | test`.
- Tests green before every commit. One commit per phase.
- Decisions D1–D18 in `docs/SPEC.md` §0 are approved. Don't re-open them silently.
- Engines are pure functions over Polars frames + `as_of`; DB and I/O live at the edges.
- SMS: raw SMS never reaches the backend. `/v1/ingest/sms` accepts structured transactions only.
- Prototype auth is the `X-User-Id` header. It is dev-only and marked that way everywhere.
