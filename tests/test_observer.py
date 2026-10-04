"""Observer callbacks: fail-open hooks, single-use pass-through middleware. No Hermes import."""
import json

import pytest

from fleet_policy import observer, store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "hermes_home", lambda: tmp_path)
    return tmp_path


def _observed(home):
    return json.loads((home / "fleet-policy" / "observed.json").read_text())


def test_pre_api_request_records_main(home):
    observer.on_pre_api_request(
        platform="", model="fallback-model", session_id="s", user_message="secret prompt",
        request={"method": "POST", "body": {"model": "grok-4.6", "reasoning_effort": "high",
                                            "service_tier": "priority",
                                            "messages": [{"role": "user", "content": "secret prompt"}]}})
    data = _observed(home)
    assert data["main"]["model"] == "grok-4.6"
    assert data["main"]["reasoning_effort"] == "high"
    assert data["main"]["service_tier"] == "priority"
    assert "secret prompt" not in (home / "fleet-policy" / "observed.json").read_text()


def test_roles_cron_delegation_aux(home):
    observer.on_pre_api_request(platform="cron", model="m", request={"body": {"model": "m"}})
    observer.on_pre_api_request(platform="subagent", model="m", request={"body": {"model": "m"}})
    observer.on_pre_auxiliary_call(aux_task="compression", model="a", request={"body": {"model": "a"}})
    assert set(_observed(home)) == {"cron", "delegation", "auxiliary:compression"}


def test_hooks_fail_open(home, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(store, "write_entry", boom)
    observer.on_pre_api_request(platform="", request={"body": {"model": "m"}})  # no raise
    observer.on_pre_auxiliary_call(aux_task="x", request={"body": {"model": "m"}})


def test_middleware_pass_through_once(home):
    calls = []
    sentinel = type("Resp", (), {"service_tier": "priority"})()

    def next_call(payload=None):
        calls.append(payload)
        return sentinel

    out = observer.observe_llm_execution(
        request={"model": "gpt-5.5", "reasoning": {"effort": "xhigh"}, "service_tier": "priority"},
        next_call=next_call, platform="", model="gpt-5.5")
    assert out is sentinel and calls == [None]
    entry = _observed(home)["main"]
    assert entry["reasoning_effort"] == "xhigh"
    assert entry["response_service_tier"] == "priority"


def test_middleware_propagates_provider_errors(home):
    def next_call(payload=None):
        raise RuntimeError("provider 500")
    with pytest.raises(RuntimeError, match="provider 500"):
        observer.observe_llm_execution(request={}, next_call=next_call, platform="")


def test_middleware_survives_store_failure(home, monkeypatch):
    monkeypatch.setattr(store, "write_entry", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert observer.observe_llm_execution(request={}, next_call=lambda p=None: 42, platform="") == 42
