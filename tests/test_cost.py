"""``crons --cost``: measured runs / empty share / cost per job from executions.db + state.db.

The fixtures build the two SQLite files with the columns Hermes uses (subset of the real schema;
``test_real_hermes_schema`` re-checks against Hermes' own DDL when Hermes is importable).
"""
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from fleet_policy import cli, cost, policy, store

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat()


def make_home(tmp_path):
    (tmp_path / "cron").mkdir()
    ex = sqlite3.connect(tmp_path / "cron" / "executions.db")
    ex.execute("""CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
        source TEXT NOT NULL, process_id TEXT NOT NULL, pid INTEGER NOT NULL,
        process_started_at INTEGER, status TEXT NOT NULL, handoff_pending INTEGER NOT NULL DEFAULT 0,
        handoff_started_at REAL, claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
        error TEXT, delivery_outcome TEXT, scheduled_instant TEXT)""")
    ex.commit()
    st = sqlite3.connect(tmp_path / "state.db")
    st.executescript("""
    CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT,
        parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL,
        input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
        estimated_cost_usd REAL, actual_cost_usd REAL);
    CREATE TABLE session_model_usage (session_id TEXT NOT NULL, model TEXT NOT NULL,
        billing_provider TEXT NOT NULL DEFAULT '', billing_base_url TEXT NOT NULL DEFAULT '',
        billing_mode TEXT NOT NULL DEFAULT '', task TEXT NOT NULL DEFAULT '',
        api_call_count INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        estimated_cost_usd REAL NOT NULL DEFAULT 0, actual_cost_usd REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task));
    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
        role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL);
    """)
    st.commit()
    return ex, st


_n = [0]


def add_run(ex, job_id, start, status="completed"):
    _n[0] += 1
    ex.execute("INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at, "
               "started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (f"e{_n[0]}", job_id, "tick", "p", 1, status, iso(start), iso(start + 1),
                iso(start + 60)))


def add_turn(st, job_id, start, reply, cost_usd=0.10, aux=0.0, child_cost=0.0):
    sid = f"cron_{job_id}_{datetime.fromtimestamp(start, timezone.utc):%Y%m%d_%H%M%S}"
    st.execute("INSERT INTO sessions (id, source, started_at, input_tokens, output_tokens, "
               "estimated_cost_usd) VALUES (?,?,?,?,?,?)", (sid, "cron", start + 2, 100, 10, cost_usd))
    st.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
               (sid, "assistant", reply, start + 30))
    if aux:
        st.execute("INSERT INTO session_model_usage (session_id, model, task, estimated_cost_usd, "
                   "input_tokens) VALUES (?,?,?,?,?)", (sid, "m", "title_generation", aux, 5))
    if child_cost:
        st.execute("INSERT INTO sessions (id, source, parent_session_id, started_at, "
                   "estimated_cost_usd) VALUES (?,?,?,?,?)",
                   (sid + "_c", "cron", sid, start + 20, child_cost))
    return sid


JOBS = [
    {"id": "aa11", "name": "agent every 15m", "prompt": "check", "schedule": {"kind": "interval",
     "minutes": 15, "display": "every 15m"}, "created_at": iso(NOW - 30 * 86400)},
    {"id": "bb22", "name": "gated watch", "prompt": "judge", "script": "watch-x.sh",
     "schedule": {"kind": "interval", "minutes": 15}, "created_at": iso(NOW - 30 * 86400)},
    {"id": "cc33", "name": "script only", "no_agent": True, "script": "x.sh",
     "schedule": {"kind": "cron", "expr": "0 * * * *"}},
    {"id": "dd44", "name": "monitor", "prompt": "p", "monitor_url": "https://example.com"},
]


@pytest.fixture()
def home(tmp_path):
    ex, st = make_home(tmp_path)
    # aa11: 8 agent runs in the last 2 days, 6 silent ([SILENT], blank, NO_REPLY), 1 real, 1 failed
    replies = ["[SILENT]", "", "NO_REPLY", "[SILENT] nothing new", "SILENT", "[SILENT]",
               "New invoice from ACME", None]
    for i, reply in enumerate(replies):
        start = NOW - (i + 1) * 6 * HOUR
        add_run(ex, "aa11", start, "failed" if reply is None else "completed")
        if reply is not None:
            add_turn(st, "aa11", start, reply, cost_usd=0.10, aux=0.01 if i == 0 else 0.0,
                     child_cost=0.05 if i == 6 else 0.0)
    add_run(ex, "aa11", NOW - 9 * 86400)  # outside the window
    add_turn(st, "aa11", NOW - 9 * 86400, "old")
    # bb22: 10 runs, the agent woke twice
    for i in range(10):
        start = NOW - (i + 1) * HOUR
        add_run(ex, "bb22", start)
        if i in (3, 7):
            add_turn(st, "bb22", start, "2 new items: ...", cost_usd=0.02)
    # cc33: no_agent, 3 runs
    for i in range(3):
        add_run(ex, "cc33", NOW - (i + 1) * HOUR)
    ex.commit()
    st.commit()
    ex.close()
    st.close()
    return tmp_path


def by_id(report):
    return {j["id"]: j for j in report["jobs"]}


def test_cost_per_job(home):
    report = cost.build_cost(JOBS, home, window_days=7, now=NOW)
    assert report["sources"] == {"executions": True, "state_db": True,
                                 "executions_coverage_from": None}
    jobs = by_id(report)
    a = jobs["aa11"]
    assert a["gate"] is None and a["schedule"] == "every 15m"
    assert a["runs"] == 8 and a["runs_failed"] == 1 and a["agent_turns"] == 7
    assert a["empty_runs"] == 6 and a["empty_share"] == 0.75
    assert a["cost_usd"] == pytest.approx(7 * 0.10 + 0.01 + 0.05)
    assert a["cost_per_day_usd"] == pytest.approx(a["cost_usd"] / 7, rel=1e-3)
    assert a["span_days"] == 7
    b = jobs["bb22"]
    assert b["gate"] == "script" and b["runs"] == 10 and b["agent_turns"] == 2
    assert b["empty_runs"] == 8 and b["empty_share"] == 0.8
    assert b["cost_usd"] == pytest.approx(0.04)
    c = jobs["cc33"]
    assert c["gate"] == "no_agent" and c["runs"] == 3 and c["empty_runs"] is None
    assert c["cost_usd"] == 0 and c["empty_share"] is None
    assert jobs["dd44"]["gate"] == "monitor" and jobs["dd44"]["runs"] == 0
    assert report["total_cost_per_day_usd"] == pytest.approx(
        sum(j["cost_per_day_usd"] for j in report["jobs"]))


def test_flags(home):
    ex = sqlite3.connect(home / "cron" / "executions.db")
    st = sqlite3.connect(home / "state.db")
    for i in range(200):  # ee55: agent every 30 min, always silent
        start = NOW - (i + 1) * 1800
        add_run(ex, "ee55", start)
        add_turn(st, "ee55", start, "[SILENT]", cost_usd=0.01)
    ex.commit()
    st.commit()
    jobs = JOBS + [{"id": "ee55", "name": "noisy", "prompt": "p",
                    "created_at": iso(NOW - 30 * 86400)}]
    report = cost.build_cost(jobs, home, now=NOW)
    e = by_id(report)["ee55"]
    assert e["empty_share"] == 1.0 and set(e["flags"]) == {"frequent_without_gate", "mostly_empty"}
    assert "ee55" in report["flagged"]
    assert by_id(report)["bb22"]["flags"] == []  # gated, 80 % is not above the threshold


def test_young_job_uses_its_own_span(home):
    jobs = [dict(JOBS[1], created_at=iso(NOW - 12 * HOUR))]
    b = cost.build_cost(jobs, home, now=NOW)["jobs"][0]
    assert b["span_days"] == 0.5 and b["runs_per_day"] == 20.0


def test_missing_sources(tmp_path):
    report = cost.build_cost(JOBS, tmp_path, now=NOW)
    assert report["sources"]["executions"] is False and report["sources"]["state_db"] is False
    a = by_id(report)["aa11"]
    assert a["runs"] == 0 and a["cost_usd"] == 0 and a["empty_runs"] is None


def test_turns_without_ledger(home):
    (home / "cron" / "executions.db").unlink()
    a = by_id(cost.build_cost(JOBS, home, now=NOW))["aa11"]
    assert a["runs"] == 7 and a["empty_runs"] == 6  # silent replies are still measured


def test_pruned_ledger_reports_coverage(home, monkeypatch):
    ex = sqlite3.connect(home / "cron" / "executions.db")  # what Hermes' pruning leaves behind
    old = [r[0] for r in ex.execute("SELECT id, claimed_at FROM executions").fetchall()
           if cost._parse_ts(ex.execute("SELECT claimed_at FROM executions WHERE id=?",
                                        (r[0],)).fetchone()[0]) < NOW - 2 * 86400]
    ex.executemany("DELETE FROM executions WHERE id=?", [(i,) for i in old])
    ex.commit()
    monkeypatch.setattr(cost, "EXECUTIONS_PRUNE_CAP", 5)
    report = cost.build_cost(JOBS, home, now=NOW)
    cov = report["sources"]["executions_coverage_from"]
    assert cov is not None
    assert by_id(report)["aa11"]["span_days"] < 7


def test_like_pattern_is_escaped(home):
    st = sqlite3.connect(home / "state.db")
    add_turn(st, "aa11x", NOW - HOUR, "real", cost_usd=5.0)  # must not count for aa11
    st.commit()
    a = by_id(cost.build_cost(JOBS, home, now=NOW))["aa11"]
    assert a["cost_usd"] == pytest.approx(0.76)


def test_silence_matcher_fallback(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_gateway(name, *a, **k):
        if name.startswith("gateway"):
            raise ImportError
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_gateway)
    assert cost.is_silence("[SILENT] no change")
    assert cost.is_silence("nothing\nNO_REPLY")
    assert cost.is_silence("   ")
    assert not cost.is_silence("The word [SILENT] mid-sentence is content")


def test_crons_contract_keeps_keys(home, monkeypatch):
    monkeypatch.setattr(policy, "_list_cron_jobs", lambda: JOBS)
    monkeypatch.setattr(policy, "_load_effective_config", lambda: {})
    monkeypatch.setattr(store, "hermes_home", lambda: home)
    plain = cli.build_crons(None)
    assert list(plain) == ["plugin", "version", "crons", "audit"]
    with_cost = cli.build_crons(None, cost=True, days=7)
    assert list(with_cost) == ["plugin", "version", "crons", "audit", "cost"]
    assert with_cost["crons"] == plain["crons"] and with_cost["audit"] == plain["audit"]
    assert list(with_cost["cost"]) == ["window_days", "since", "until", "sources", "definition",
                                       "jobs", "total_cost_per_day_usd", "flagged"]
    assert list(with_cost["cost"]["jobs"][0]) == [
        "id", "name", "enabled", "schedule", "gate", "span_days", "runs", "runs_failed",
        "agent_turns", "empty_runs", "empty_share", "runs_per_day", "tokens", "cost_usd",
        "cost_per_day_usd", "cost_per_run_usd", "flags"]
    json.dumps(with_cost)
    assert "check" not in json.dumps(with_cost["cost"])  # no prompt leaks


def test_cli_text_render(home, monkeypatch, capsys):
    monkeypatch.setattr(policy, "_list_cron_jobs", lambda: JOBS)
    monkeypatch.setattr(policy, "_load_effective_config", lambda: {})
    monkeypatch.setattr(store, "hermes_home", lambda: home)
    import argparse

    parser = argparse.ArgumentParser()
    cli.setup_parser(parser)
    assert cli.make_handler(None)(parser.parse_args(["crons", "--cost"])) == 0
    out = capsys.readouterr().out
    assert "$/day in total" in out and "aa11" in out and "gate=script" in out


def _hermes_schema():
    try:
        from hermes_state import SessionDB  # noqa: F401
        import cron.executions  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _hermes_schema(), reason="Hermes not importable")
def test_real_hermes_schema(tmp_path, monkeypatch):
    """Create both files with Hermes' own code, then read them back."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from cron import executions
    from hermes_state import SessionDB

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    rec = executions.create_execution("ff66", source="tick")
    executions.mark_execution_running(rec["id"])
    executions.finish_execution(rec["id"], success=True)
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "cron_ff66_20261004_120000"
    db.create_session(sid, source="cron", model="m")
    db.update_token_counts(sid, input_tokens=10, output_tokens=2, model="m",
                           estimated_cost_usd=0.5, api_call_count=1, billing_provider="p")
    db.append_message(sid, role="assistant", content="[SILENT]")
    db.close()
    job = {"id": "ff66", "prompt": "p"}
    report = cost.build_cost([job], tmp_path, now=datetime.now(timezone.utc).timestamp() + 60)
    assert report["sources"]["executions"] and report["sources"]["state_db"]
    f = report["jobs"][0]
    assert f["runs"] == 1 and f["agent_turns"] == 1 and f["empty_runs"] == 1
    assert f["cost_usd"] == pytest.approx(0.5)
