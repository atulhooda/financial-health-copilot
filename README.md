# Hisaab: AI Financial Health Copilot (backend)

Hackathon prototype. Read [CLAUDE.md](CLAUDE.md) for the rules and [docs/SPEC.md](docs/SPEC.md) for the design.

```bash
brew install libomp            # macOS: LightGBM runtime
make up                        # Postgres (host port 55432) + Redis, migrations
make seed                      # trains the categoriser if needed, loads personas A (T0), B, C
make demo                      # persona A replay T0 -> +3
make test

cd backend
uv run hisaab ask --user demo-a "Kya main ₹60,000 ka phone 12 months ki EMI pe le sakta hoon?"   # --debug for the trace
uv run hisaab eval-copilot     # 30-question eval -> docs/COPILOT_EVAL.md
```

The copilot answers from templates when `LLM_PROVIDER=none` (the default). For a model, set `LLM_PROVIDER=anthropic`
(or `openai_compat` with `LLM_BASE_URL`), `LLM_MODEL` and `LLM_API_KEY`; see [docs/COPILOT.md](docs/COPILOT.md).

Layout: `backend/` (FastAPI brain), `mobile/` (Flutter, later), `shared/` (SMS patterns used on device and in tests), `docs/`.
