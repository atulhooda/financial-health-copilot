"""The pre-push secret scan (Phase 5 review item 10) flags credential-like strings and never prints them."""
import importlib.util

from app.core.config import REPO_DIR

spec = importlib.util.spec_from_file_location("secret_scan", REPO_DIR / "tools" / "secret_scan.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


def test_flags_keys_without_echoing_them():
    fake = "sk-ant-api03-" + "Q" * 40  # built at runtime so this file holds no key-like literal
    hits = scan.scan_text("x.py", f'key = "{fake}"\nGROQ = "gsk_{"z" * 30}"\nfine = "₹5,415"')
    assert any("anthropic key" in h for h in hits) and any("groq key" in h for h in hits)
    assert all(fake not in h for h in hits) and not any(":3:" in h for h in hits)


def test_placeholders_and_opt_outs_pass():
    assert scan.scan_text("e", "ANTHROPIC_API_KEY=\nLLM_API_KEY=<your key>\nLLM_MODEL=claude-sonnet-5") == []
    fake = "sk-" + "a" * 30
    assert scan.scan_text("t", f'"{fake}"  # secret-scan: allow') == []


def test_env_reader_handles_comments_quotes_and_blanks(tmp_path, monkeypatch):
    import app.core.config as config

    (tmp_path / ".env").write_text('EVENT_BUS=redis   # comment\nQUOTED="abc # kept"\nEMPTY=\n')
    monkeypatch.setattr(config, "BACKEND_DIR", tmp_path)
    monkeypatch.delenv("EVENT_BUS", raising=False)
    assert config.secret("EVENT_BUS") == "redis" and config.secret("QUOTED") == "abc # kept"
    assert config.secret("EMPTY") is None and config.secret("ABSENT") is None
