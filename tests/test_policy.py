"""Managed-scope error detection and the frozen ``status --json`` contract (no Hermes import)."""
import pytest

from fleet_policy import cli, policy, store

@pytest.fixture()
def managed(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_managed_dir", lambda: tmp_path)
    monkeypatch.setattr(policy, "_managed_key_count", lambda: 2)
    return tmp_path


def _has_yaml_parser():
    try:
        policy._yaml_load("a: 1")
        return True
    except Exception:
        return False


def test_no_managed_scope(monkeypatch):
    monkeypatch.setattr(policy, "_managed_dir", lambda: None)
    assert policy.managed_status() == {"dir": None, "keys": 0, "sha256": None, "error": None}


def test_managed_dir_without_file(managed):
    out = policy.managed_status()
    assert out["dir"] == str(managed) and out["sha256"] is None and out["error"] is None


@pytest.mark.parametrize("content, needle", [
    (b"", "empty file"),
    (b"   \n\n", "empty file"),
    (b"\xff\xfe\x00", "UTF-8"),
])
def test_managed_error_without_parser(managed, content, needle):
    (managed / "config.yaml").write_bytes(content)
    out = policy.managed_status()
    assert needle in out["error"] and out["keys"] == 0 and out["sha256"]


@pytest.mark.skipif(not _has_yaml_parser(), reason="no YAML parser in this interpreter")
@pytest.mark.parametrize("content, needle", [
    ("agent: [unclosed\n", "YAML parse error"),
    ("# only a comment\n", "empty document"),
    ("- a\n- b\n", "expected a mapping"),
    ("{}\n", "empty mapping"),
])
def test_managed_error_parsed(managed, content, needle):
    (managed / "config.yaml").write_text(content)
    assert needle in policy.managed_status()["error"]


@pytest.mark.skipif(not _has_yaml_parser(), reason="no YAML parser in this interpreter")
def test_managed_ok(managed):
    (managed / "config.yaml").write_text("agent:\n  reasoning_effort: high\n")
    out = policy.managed_status()
    assert out["error"] is None and out["keys"] == 2 and len(out["sha256"]) == 64


def test_status_contract_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "_managed_dir", lambda: None)
    monkeypatch.setattr(policy, "_load_effective_config", lambda: {"model": "bare-model"})
    monkeypatch.setattr(policy, "_list_cron_jobs", lambda: [
        {"id": "j1", "name": "n", "enabled": True, "model": "m", "provider": "p",
         "reasoning_effort": "high", "last_status": "ok", "failure_streak": 0, "prompt": "secret"}])
    monkeypatch.setattr(store, "hermes_home", lambda: tmp_path)
    store.write_entry("main", {"model": "m", "reasoning_effort": "low", "service_tier": None,
                               "response_service_tier": None})
    status = cli.build_status()
    assert list(status) == ["plugin", "version", "managed", "effective", "observed", "crons"]
    assert status["plugin"] == "fleet-policy"
    assert list(status["managed"]) == ["dir", "keys", "sha256", "error"]
    assert list(status["effective"]) == [
        "model.default", "model.provider", "agent.reasoning_effort", "agent.service_tier",
        "delegation.reasoning_effort", "delegation.model", "compression.threshold_tokens",
        "x_search.model"]
    assert status["effective"]["model.default"] == "bare-model"
    assert list(status["observed"]["main"]) == [
        "model", "reasoning_effort", "service_tier", "response_service_tier", "ts"]
    assert list(status["crons"][0]) == [
        "id", "name", "enabled", "provider", "model", "reasoning_effort", "last_status",
        "failure_streak"]
    assert "secret" not in repr(status)
