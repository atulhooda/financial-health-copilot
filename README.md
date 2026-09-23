# Hisaab: AI Financial Health Copilot (backend)

Hackathon prototype. Read [CLAUDE.md](CLAUDE.md) for the rules and [docs/SPEC.md](docs/SPEC.md) for the design.

```bash
brew install libomp            # macOS: LightGBM runtime
make up                        # Postgres (host port 5433) + Redis, migrations
make seed                      # trains the categoriser if needed, loads personas A (T0), B, C
make demo                      # persona A replay T0 -> +3
make test
```

Layout: `backend/` (FastAPI brain), `mobile/` (Flutter, later), `shared/` (SMS patterns used on device and in tests), `docs/`.
