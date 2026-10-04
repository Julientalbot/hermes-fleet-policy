"""End-to-end: a real cron job gated by ``watch``, real ``hermes cron tick``, real MCP server.

Opt-in like tests/test_e2e.py (``FLEET_POLICY_HERMES=/path/to/venv/bin/hermes``); the venv must
ship the ``mcp`` SDK (Hermes' ``[mcp]`` extra, present in the Docker image) and ``uvicorn``.

Scenario, in a throwaway HERMES_HOME with the plugin folder installed:
1. tick #1 — no state: baseline recorded, ``{"wakeAgent": false}``, NO request to the LLM stub;
2. tick #2 — same ids, fresh volatile fields in the raw answer: still no LLM request;
3. tick #3 — one new id: the agent runs once, its prompt holds the new item's line;
4. tick #4 — source fails: the agent gets a "Script Error" block, the state is unchanged.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from e2e_harness import hermes_env

TARGETS = [p for p in os.environ.get("FLEET_POLICY_HERMES", "").split(os.pathsep) if p]
STUB = Path(__file__).with_name("mcp_stub.py")

pytestmark = pytest.mark.skipif(not TARGETS, reason="set FLEET_POLICY_HERMES to run end-to-end tests")


def _python(hermes: str) -> str:
    return str(Path(hermes).with_name("python"))


@pytest.fixture(params=TARGETS or ["<none>"])
def env(request):
    python = _python(request.param)
    probe = subprocess.run([python, "-c", "import mcp.server.mcpserver, uvicorn"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("this Hermes venv has no mcp>=2 SDK / uvicorn")
    with hermes_env(request.param, install="dir") as env:
        port_file, items, log = env.root / "mcp.port", env.root / "items.json", env.root / "mcp.log"
        items.write_text(json.dumps(["m1", "m2"]))
        proc = subprocess.Popen([python, str(STUB), str(port_file), str(items), str(log)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(200):
                if port_file.exists() and port_file.read_text().strip():
                    break
                time.sleep(0.05)
            env.mcp_url = f"http://127.0.0.1:{port_file.read_text().strip()}/mcp"
            env.items, env.mcp_log = items, log
            yield env
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def _make_due(env, job_id):
    path = env.home / "cron" / "jobs.json"
    data = json.loads(path.read_text())
    for job in data["jobs"]:
        if job["id"] == job_id:
            job["next_run_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps(data, indent=1))


def _tick(env, job_id):
    _make_due(env, job_id)
    proc = env.run("cron", "tick")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    if os.environ.get("FLEET_POLICY_E2E_VERBOSE"):
        print(f"--- tick: llm_requests_total={len(env.stub_requests())} mcp_calls={_mcp_calls(env)}")
        print(proc.stdout.strip()[-600:])
    return proc


def _mcp_calls(env):
    return len(env.mcp_log.read_text().splitlines()) if env.mcp_log.exists() else 0


def test_watch_gates_a_real_cron_job(env):
    (env.home / "cron" / "jobs.json").unlink()
    watches, scripts = env.home / "watches", env.home / "scripts"
    watches.mkdir()
    scripts.mkdir()
    config = watches / "x.json"
    config.write_text(json.dumps({
        "server_url": env.mcp_url, "tool": "COMPOSIO_MULTI_EXECUTE_TOOL",
        "arguments": {"tools": [{"tool_slug": "FAKE_FETCH", "arguments": {}}]},
        "id_field": "messageId", "show_fields": ["sender", "subject"], "state": "x.state.json"}))
    plugin_dir = env.home / "plugins" / "fleet-policy"
    line = f'exec {_python(env.hermes)} {plugin_dir} watch --config {config} "$@"'
    (scripts / "watch-x.sh").write_text(line + "\n")

    created = env.run("cron", "create", "every 1h",
                      "Judge the new items below and write one line each for the user.",
                      "--script", "watch-x.sh", "--name", "watch-x", "--deliver", "local")
    assert created.returncode == 0, created.stdout + created.stderr
    job_id = json.loads((env.home / "cron" / "jobs.json").read_text())["jobs"][0]["id"]
    state = watches / "x.state.json"

    # 1. first tick: baseline, agent asleep
    _tick(env, job_id)
    assert _mcp_calls(env) == 1
    assert json.loads(state.read_text())["seen"] == ["m1", "m2"]
    assert env.stub_requests() == [], "first run must not wake the LLM"

    # 2. nothing new (raw answer differs: fresh etag/log_id): agent asleep
    _tick(env, job_id)
    assert _mcp_calls(env) == 2
    assert env.stub_requests() == [], "no new id must not wake the LLM"

    # 3. one new item: the agent runs once and sees exactly that item
    env.items.write_text(json.dumps(["m1", "m2", "m3"]))
    _tick(env, job_id)
    requests = env.stub_requests()
    assert requests, "a new id must wake the agent"
    prompt = requests[0]["last_user"]
    if os.environ.get("FLEET_POLICY_E2E_VERBOSE"):
        start = prompt.find("## Script Output")
        print("--- prompt received by the LLM stub (script block):\n" + prompt[start:start + 500])
    assert "1 new item(s) from COMPOSIO_MULTI_EXECUTE_TOOL" in prompt
    assert "id=m3 | sender: sender-m3@example.com | subject: subject m3" in prompt
    assert "id=m1" not in prompt and "etag" not in prompt
    assert "Script Output" in prompt
    assert json.loads(state.read_text())["seen"] == ["m1", "m2", "m3"]
    woken = len(requests)

    # 4. source failure: reported to the agent as a Script Error, state untouched
    before = state.read_text()
    env.items.write_text(json.dumps("FAIL"))
    _tick(env, job_id)
    requests = env.stub_requests()[woken:]
    assert requests and "Script Error" in requests[0]["last_user"]
    assert "connection expired" in requests[0]["last_user"]
    assert state.read_text() == before

    # 5. the measurement sees the runs and the gate
    report = json.loads(env.run("fleet-policy", "crons", "--cost", "--json").stdout)
    job = next(j for j in report["cost"]["jobs"] if j["id"] == job_id)
    assert job["gate"] == "script" and job["runs"] == 4 and job["agent_turns"] == 2
    assert job["empty_runs"] == 2
