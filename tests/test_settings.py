import pytest
from mt5_agents.settings import Settings


def test_real_mode_requires_two_independent_unlocks(monkeypatch):
    monkeypatch.setenv("MT5_LOGIN", "123456")
    monkeypatch.setenv("MT5_PASSWORD", "demo-secret")
    monkeypatch.setenv("MT5_SERVER", "Test-Demo")
    monkeypatch.setenv("MODE", "LIVE")
    monkeypatch.delenv("LIVE_ENABLE_TEXT", raising=False)
    with pytest.raises(ValueError, match="text authorization"):
        Settings.load()


def test_default_dry_run_is_valid(monkeypatch):
    monkeypatch.setenv("MT5_LOGIN", "123456")
    monkeypatch.setenv("MT5_PASSWORD", "demo-secret")
    monkeypatch.setenv("MT5_SERVER", "Test-Demo")
    monkeypatch.setenv("MODE", "DRY_RUN")
    monkeypatch.setenv("AI_VETO_ENABLED", "false")
    assert Settings.load().mode == "DRY_RUN"
