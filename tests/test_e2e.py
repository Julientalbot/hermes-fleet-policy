"""End-to-end: real ``hermes`` processes against tests/fake_provider.py in a throwaway HERMES_HOME.

Opt-in (spawns Hermes, ~20 s per target)::

    FLEET_POLICY_HERMES=/path/to/venv/bin/hermes python -m pytest tests/test_e2e.py -q
    FLEET_POLICY_INSTALL=entrypoint  # when the plugin is pip-installed in that venv (default: dir)

``FLEET_POLICY_HERMES`` may hold several executables separated by ``os.pathsep``.
"""
import json
import os

import pytest

from e2e_harness import hermes_env

TARGETS = [p for p in os.environ.get("FLEET_POLICY_HERMES", "").split(os.pathsep) if p]
INSTALL = os.environ.get("FLEET_POLICY_INSTALL", "dir")

pytestmark = pytest.mark.skipif(not TARGETS, reason="set FLEET_POLICY_HERMES to run end-to-end tests")

CONTRACT_KEYS = ["plugin", "version", "managed", "effective", "observed", "crons"]
ENTRY_KEYS = ["model", "reasoning_effort", "service_tier", "response_service_tier", "ts"]


@pytest.fixture(params=TARGETS or ["<none>"])
def env(request):
    with hermes_env(request.param, install=INSTALL, tier_extra_body=True, streaming=False) as env:
        yield env


def test_plugin_loaded_and_enabled(env):
    out = env.run("plugins", "list", "--plain").stdout
    line = next((ln for ln in out.splitlines() if ln.split()[-1:] == ["fleet-policy"]), "")
    assert line.startswith("enabled"), out


def test_main_turn_is_observed(env):
    before = env.status()
    assert list(before) == CONTRACT_KEYS
    assert before["observed"] == {}
    assert before["managed"]["error"] is None and before["managed"]["keys"] == 3
    assert before["effective"]["agent.reasoning_effort"] == "high"  # managed beats user "low"

    chat = env.run("-z", "ping")
    assert chat.returncode == 0 and "pong" in chat.stdout, chat.stdout + chat.stderr
    sent = env.stub_requests()[0]
    assert sent["reasoning_effort"] == "high" and sent["service_tier"] == "priority"

    observed = env.status()["observed"]
    main = observed["main"]
    assert list(main) == ENTRY_KEYS
    assert main["model"] == "stub-model"
    assert main["reasoning_effort"] == sent["reasoning_effort"]
    assert main["service_tier"] == "priority"
    assert main["response_service_tier"] == "priority"  # what the provider says it served
    # Title generation runs on a daemon thread that `hermes -z` may not wait for: check it only
    # when it made it to the provider before exit.
    aux = observed.get("auxiliary:title_generation")
    if aux is not None:
        assert aux["model"] == "stub-model" and list(aux) == ENTRY_KEYS
    assert set(observed) <= {"main", "auxiliary:title_generation"}

    raw = (env.home / "fleet-policy" / "observed.json").read_text()
    assert "ping" not in raw and "pong" not in raw and "stub-key" not in raw


def test_delegation_is_observed(env):
    chat = env.run("-z", "DELEGATE-ME now", "--toolsets", "delegation")
    assert chat.returncode == 0, chat.stdout + chat.stderr
    observed = env.status()["observed"]
    assert observed["delegation"]["model"] == "stub-model"
    assert observed["delegation"]["reasoning_effort"] == "high"


def test_cron_run_is_observed_and_listed(env):
    (env.home / "cron" / "jobs.json").unlink()
    created = env.run("cron", "create", "every 1h", "say ok", "--name", "probe", "--deliver", "local")
    assert created.returncode == 0, created.stdout + created.stderr
    job_id = json.loads((env.home / "cron" / "jobs.json").read_text())["jobs"][0]["id"]
    assert env.run("cron", "run", job_id).returncode == 0
    env.run("cron", "tick")
    status = env.status()
    assert status["observed"]["cron"]["model"] == "stub-model"
    job = next(j for j in status["crons"] if j["id"] == job_id)
    assert job["name"] == "probe" and job["last_status"] == "ok" and job["failure_streak"] == 0


def test_crons_audit(env):
    out = json.loads(env.run("fleet-policy", "crons", "--json").stdout)
    assert out["audit"]["pinned_model"] == ["job-pinned"]
    assert out["audit"]["failing"] == ["job-pinned"]
    assert out["audit"]["outside_allowlist"] == ["job-pinned"]


@pytest.mark.parametrize("content, needle", [("agent: [unclosed\n", "YAML parse error"),
                                             ("", "empty file")])
def test_broken_managed_config_is_reported(env, content, needle):
    (env.managed / "config.yaml").write_text(content)
    managed = env.status()["managed"]
    assert managed["keys"] == 0 and needle in managed["error"]
