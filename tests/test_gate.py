"""Creation-time cron gate (gate.py): which cronjob_manage calls are refused, and the hook contract."""
import pytest

from fleet_policy import gate


def parse(s):
    """Same shapes as cron.jobs.parse_schedule, for the forms used here."""
    s = s.strip()
    if s.startswith("every ") and s.endswith("m"):
        return {"kind": "interval", "minutes": int(s[6:-1]), "display": s}
    if s.endswith("m") and s[:-1].isdigit():
        return {"kind": "interval", "minutes": int(s[:-1]), "display": "every " + s}
    if s.startswith("in "):
        return {"kind": "once", "run_at": "2026-11-02T10:00:00+04:00", "display": "once " + s}
    return {"kind": "cron", "expr": s, "display": s}


@pytest.fixture(autouse=True)
def _fake_parse(monkeypatch):
    monkeypatch.setattr(gate, "_parse", lambda v: v if isinstance(v, dict) else (parse(v) if v else None))


@pytest.mark.parametrize("schedule,period", [
    ({"kind": "interval", "minutes": 15}, 15),
    ({"kind": "cron", "expr": "*/30 8-19 * * 1-5"}, 30),
    ({"kind": "cron", "expr": "20 8,11,15,18 * * 1-5"}, 180),
    ({"kind": "cron", "expr": "0 9 * * *"}, 1440),
    ({"kind": "cron", "expr": "0,30 9 * * *"}, 30),
    ({"kind": "cron", "expr": "@hourly"}, 60),
    ({"kind": "once", "run_at": "x"}, None),
    ({"kind": "cron", "expr": "nonsense"}, None),
    (None, None),
])
def test_period(schedule, period):
    assert gate.period_minutes(schedule) == period


def test_create_frequent_ungated_is_refused_with_the_way_out():
    msg = gate.check({"action": "create", "schedule": "every 15m", "prompt": "check my inbox"}, 60)
    assert msg and "every 15 min" in msg and "fleet-policy:recurring-tasks" in msg
    assert "wakeAgent" in msg and "monitor" in msg and "no_agent" in msg


@pytest.mark.parametrize("extra", [
    {"script": "watch-inbox.sh"},
    {"monitor": "https://example.com/page"},
    {"monitor_script": "fingerprint.py"},
    {"monitor_url": "https://example.com"},
    {"no_agent": True, "script": "ping.sh"},
])
def test_create_frequent_gated_passes(extra):
    assert gate.check({"action": "create", "schedule": "*/15 * * * *", "prompt": "x", **extra}, 60) is None


@pytest.mark.parametrize("schedule", ["0 9 * * *", "20 8,11,15,18 * * 1-5", "every 60m", "in 30m", "60m"])
def test_create_rare_or_one_shot_passes(schedule):
    assert gate.check({"action": "create", "schedule": schedule, "prompt": "x"}, 60) is None


def test_off_by_default_and_other_actions_ignored():
    assert gate.check({"action": "create", "schedule": "every 5m", "prompt": "x"}, 0) is None
    for action in ("list", "pause", "remove", "run"):
        assert gate.check({"action": action, "job_id": "a"}, 60, get_job=lambda _: {}) is None


JOBS = {
    "fast": {"id": "fast", "enabled": False, "schedule": {"kind": "interval", "minutes": 15}},
    "fast_on": {"id": "fast_on", "enabled": True, "schedule": {"kind": "interval", "minutes": 15}},
    "gated": {"id": "gated", "enabled": True, "schedule": {"kind": "interval", "minutes": 15},
              "script": "watch-a.sh"},
    "daily": {"id": "daily", "enabled": False, "schedule": {"kind": "cron", "expr": "0 8 * * *"}},
}


def test_resume_of_an_ungated_frequent_job_is_refused():
    assert gate.check({"action": "resume", "job_id": "fast"}, 60, get_job=JOBS.get)
    assert gate.check({"action": "resume", "job_id": "daily"}, 60, get_job=JOBS.get) is None
    assert gate.check({"action": "resume", "job_id": "gated"}, 60, get_job=JOBS.get) is None


def test_update_paths():
    get = JOBS.get
    # removing the gate of a frequent job
    assert gate.check({"action": "update", "job_id": "gated", "script": ""}, 60, get_job=get)
    # making a daily job frequent
    assert gate.check({"action": "update", "job_id": "daily", "schedule": "every 10m"}, 60, get_job=get)
    # gating an ungated frequent job (the conversion) passes
    assert gate.check({"action": "update", "job_id": "fast_on", "script": "watch-a.sh"}, 60, get_job=get) is None
    # slowing it down to the allowed pace passes
    assert gate.check({"action": "update", "job_id": "fast_on", "schedule": "20 8,11,15,18 * * *"}, 60,
                      get_job=get) is None
    # editing the prompt of a PAUSED frequent job costs nothing → allowed (resume is checked)
    assert gate.check({"action": "update", "job_id": "fast", "prompt": "y"}, 60, get_job=get) is None
    # unknown job: let the tool report it
    assert gate.check({"action": "update", "job_id": "nope", "schedule": "every 5m"}, 60, get_job=get) is None


def test_hook_contract_and_fail_open(monkeypatch):
    settings = {"max_ungated_interval_minutes": 60}
    hook = gate.make_hook(lambda k, d=None: settings.get(k, d))
    out = hook(tool_name="cronjob_manage", args={"action": "create", "schedule": "every 15m", "prompt": "x"},
               task_id="t", session_id="s")
    assert out["action"] == "block" and out["message"]
    assert hook(tool_name="terminal", args={"command": "ls"}) is None
    assert hook(tool_name="cronjob_manage", args={"action": "create", "schedule": "every 15m",
                                                   "prompt": "x", "script": "w.sh"}) is None

    def boom(*a, **k):
        raise RuntimeError("cron store unreadable")

    monkeypatch.setattr(gate, "check", boom)
    assert hook(tool_name="cronjob_manage", args={"action": "create", "schedule": "every 1m"}) is None
    off = gate.make_hook(lambda k, d=None: d)
    monkeypatch.undo()
    assert off(tool_name="cronjob_manage", args={"action": "create", "schedule": "every 1m", "prompt": "x"}) is None


def test_real_parse_schedule_when_hermes_is_importable(monkeypatch):
    jobs = pytest.importorskip("cron.jobs")
    monkeypatch.setattr(gate, "_parse", lambda v: v if isinstance(v, dict) else (jobs.parse_schedule(v) if v else None))
    assert gate.check({"action": "create", "schedule": "every 15m", "prompt": "x"}, 60)
    assert gate.check({"action": "create", "schedule": "*/30 8-19 * * 1-5", "prompt": "x"}, 60)
    assert gate.check({"action": "create", "schedule": "every 2h", "prompt": "x"}, 60) is None
    assert gate.check({"action": "create", "schedule": "in 10m", "prompt": "x"}, 60) is None
