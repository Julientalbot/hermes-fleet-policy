"""Pure unit tests: no Hermes import needed."""
import json

from fleet_policy import extract, store


def test_roles_from_platform():
    assert extract.role_for_main_loop({"platform": ""}) == "main"
    assert extract.role_for_main_loop({"platform": "telegram"}) == "main"
    assert extract.role_for_main_loop({"platform": "cron"}) == "cron"
    assert extract.role_for_main_loop({"platform": "subagent"}) == "delegation"
    assert extract.role_for_main_loop({}) == "unknown"
    assert extract.role_for_main_loop({"platform": None}) == "unknown"


def test_roles_for_auxiliary():
    assert extract.role_for_auxiliary({"aux_task": "compression"}) == "auxiliary:compression"
    assert extract.role_for_auxiliary({"aux_task": "a b/c"}) == "auxiliary:a_b_c"
    assert extract.role_for_auxiliary({}) == "unknown"


def test_effort_shapes():
    assert extract.effort_from_body({"reasoning": {"effort": "high", "summary": "auto"}}) == "high"
    assert extract.effort_from_body({"reasoning_effort": "low"}) == "low"
    assert extract.effort_from_body({"output_config": {"effort": "xhigh"}}) == "xhigh"
    assert extract.effort_from_body({"extra_body": {"reasoning": {"enabled": True, "effort": "medium"}}}) == "medium"
    assert extract.effort_from_body({"thinking": {"type": "enabled", "budget_tokens": 4096}}) == "budget:4096"
    assert extract.effort_from_body({"thinking": {"type": "disabled"}}) == "none"
    assert extract.effort_from_body({"extra_body": {"reasoning": {"enabled": False}}}) == "none"
    assert extract.effort_from_body({"model": "x"}) is None


def test_tier_shapes():
    assert extract.tier_from_body({"service_tier": "priority"}) == "priority"
    assert extract.tier_from_body({"extra_body": {"service_tier": "flex"}}) == "flex"
    assert extract.tier_from_body({}) is None


def test_summarize_full_and_truncated():
    full = {"method": "POST", "body": {"model": "grok-4.6", "reasoning": {"effort": "high"},
                                       "service_tier": "priority", "input": [{"content": "secret"}]}}
    out = extract.summarize_request(full)
    assert out == {"model": "grok-4.6", "reasoning_effort": "high", "service_tier": "priority",
                   "request_truncated": False}
    preview = json.dumps({"method": "POST", "body": {
        "model": "grok-4.6", "instructions": 'say "service_tier": "fake" please',
        "reasoning": {"effort": "low", "summary": "auto"}}})[:400]
    out = extract.summarize_request({"_truncated": True, "preview": preview}, fallback_model="m")
    assert out["model"] == "grok-4.6"
    assert out["reasoning_effort"] == "low"
    assert out["service_tier"] is None  # the escaped quotes inside a string never match
    assert out["request_truncated"] is True


def test_response_tier():
    class R:
        service_tier = "Priority "
    assert extract.response_service_tier(R()) == "Priority"
    assert extract.response_service_tier({"service_tier": "default"}) == "default"
    assert extract.response_service_tier(None) is None


def test_store_atomic_contract(tmp_path):
    path = tmp_path / "fleet-policy" / "observed.json"
    store.write_entry("main", {"model": "m1", "reasoning_effort": "high", "service_tier": None,
                               "response_service_tier": None, "ts": "2026-10-04T00:00:00Z",
                               "messages": ["must not be stored"]}, path=path)
    store.write_entry("main", {"response_service_tier": "priority"}, path=path, patch=True)
    data = json.loads(path.read_text())
    assert set(data) == {"main"}
    assert set(data["main"]) == set(store.ENTRY_FIELDS)
    assert data["main"]["model"] == "m1"
    assert data["main"]["reasoning_effort"] == "high"
    assert data["main"]["response_service_tier"] == "priority"
    assert "must not be stored" not in path.read_text()
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".observed-")]


def test_store_unreadable_is_empty(tmp_path):
    path = tmp_path / "observed.json"
    path.write_text("{not json")
    assert store.read_observed(path) == {}
