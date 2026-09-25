from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = BACKEND_DIR.parent
CONFIG_DIR = BACKEND_DIR / "config"
SHARED_DIR = REPO_DIR / "shared"
ARTIFACTS_DIR = BACKEND_DIR / "artifacts"


class Settings(BaseSettings):
    """Env vars (see SPEC §10). HISAAB_SEED and HISAAB_CLOCK are accepted as aliases."""

    model_config = SettingsConfigDict(env_file=(BACKEND_DIR / ".env",), extra="ignore")

    database_url: str = "postgresql+psycopg://hisaab:hisaab@localhost:55432/hisaab"
    redis_url: str = "redis://localhost:6379/0"
    event_bus: str = "redis"  # redis | inprocess
    llm_provider: str = "none"  # anthropic | openai_compat | none
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_timeout_s: float = 20.0  # per LLM call (COPILOT.md §2)
    llm_effort: str | None = "low"  # Anthropic output_config.effort; numbers come from tools, so no long thinking
    copilot_deadline_s: float = 20.0  # per question, across all calls; then the template answer is served
    demo_mode: bool = False
    seed: int = 20260920
    clock: str | None = None  # YYYY-MM-DD -> FixedClock
    copilot_debug_trace: bool = False


@lru_cache
def get_settings() -> Settings:
    import os

    s = Settings()
    # HISAAB_SEED / HISAAB_CLOCK are the documented names.
    if os.environ.get("HISAAB_SEED"):
        s.seed = int(os.environ["HISAAB_SEED"])
    if os.environ.get("HISAAB_CLOCK"):
        s.clock = os.environ["HISAAB_CLOCK"]
    return s


@lru_cache
def load_yaml(name: str) -> dict:
    """Load backend/config/<name>.yaml (cached; config is read-only at runtime)."""
    path = CONFIG_DIR / (name if name.endswith(".yaml") else f"{name}.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def secret(name: str) -> str | None:
    """A secret by env var name, from the process environment or the gitignored backend/.env. Never logged."""
    import os

    if os.environ.get(name):
        return os.environ[name]
    path = BACKEND_DIR / ".env"
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == name and not key.strip().startswith("#"):
            value = value.strip()
            if value and value[0] in "\"'":  # quoted: keep everything inside the quotes
                return value[1:].split(value[0], 1)[0] or None
            return value.split(" #", 1)[0].strip() or None  # unquoted: drop an inline comment
    return None
