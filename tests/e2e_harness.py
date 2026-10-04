"""End-to-end harness: a throwaway HERMES_HOME + managed dir + stub provider, then real
``hermes`` commands. Used by tests/test_e2e.py and runnable by hand::

    python tests/e2e_harness.py /path/to/venv/bin/hermes

Never touches the real ~/.hermes: every path lives under a fresh temporary directory.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional

REPO = Path(__file__).resolve().parents[1]
PLUGIN_SRC = REPO / "fleet_policy"
STUB = Path(__file__).with_name("fake_provider.py")

MANAGED_YAML = """\
agent:
  reasoning_effort: high
  service_tier: priority
compression:
  threshold_tokens: 120000
"""

JOBS = {"jobs": [
    {"id": "job-pinned", "name": "pinned report", "prompt": "x", "enabled": True,
     "schedule": {"kind": "cron", "expr": "0 6 * * *"}, "model": "other-model",
     "provider": "openrouter", "reasoning_effort": "xhigh", "last_status": "error",
     "failure_streak": 3},
    {"id": "job-free", "name": "follows policy", "prompt": "y", "enabled": True,
     "schedule": {"kind": "cron", "expr": "0 7 * * *"}, "last_status": "ok", "failure_streak": 0},
]}


class Env:
    def __init__(self, root: Path, hermes: str, port: int):
        self.root, self.hermes, self.port = root, hermes, port
        self.home = root / "home"
        self.managed = root / "managed"
        self.stub_log = root / "stub.log"

    def env(self, **extra: str) -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "OPENAI_", "XAI_"))}
        env.update(HERMES_HOME=str(self.home), HERMES_MANAGED_DIR=str(self.managed),
                   NO_COLOR="1", HERMES_SKIP_UPDATE_CHECK="1", HERMES_ACCEPT_HOOKS="1")
        env.update(extra)
        return env

    def run(self, *args: str, timeout: int = 240, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([self.hermes, *args], cwd=str(self.root), env=self.env(**extra),
                              capture_output=True, text=True, timeout=timeout)

    def status(self) -> dict:
        proc = self.run("fleet-policy", "status", "--json")
        if proc.returncode != 0:
            raise AssertionError(f"status failed rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        return json.loads(proc.stdout)

    def stub_requests(self) -> List[dict]:
        if not self.stub_log.exists():
            return []
        return [json.loads(line) for line in self.stub_log.read_text().splitlines() if line.strip()]


@contextlib.contextmanager
def hermes_env(hermes: str, *, install: str = "dir") -> Iterator[Env]:
    root = Path(tempfile.mkdtemp(prefix="fleet-policy-e2e-"))
    proc: Optional[subprocess.Popen] = None
    try:
        port_file = root / "port"
        proc = subprocess.Popen([sys.executable, str(STUB), str(port_file), str(root / "stub.log")])
        for _ in range(100):
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.05)
        port = int(port_file.read_text())
        env = Env(root, hermes, port)
        (env.home / "plugins").mkdir(parents=True)
        (env.home / "cron").mkdir(parents=True)
        env.managed.mkdir()
        if install == "dir":
            shutil.copytree(PLUGIN_SRC, env.home / "plugins" / "fleet-policy",
                            ignore=shutil.ignore_patterns("__pycache__"))
        (env.home / "config.yaml").write_text(
            "model:\n"
            "  provider: custom\n"
            f"  base_url: http://127.0.0.1:{port}/v1\n"
            "  default: stub-model\n"
            "agent:\n"
            "  reasoning_effort: low\n"
            "plugins:\n"
            "  enabled: [fleet-policy]\n"
            "  entries:\n"
            "    fleet-policy:\n"
            "      settings:\n"
            "        allowed_cron_models: [stub-model]\n"
        )
        (env.home / ".env").write_text("OPENAI_API_KEY=stub-key-not-a-secret\n")
        (env.managed / "config.yaml").write_text(MANAGED_YAML)
        (env.home / "cron" / "jobs.json").write_text(json.dumps(JOBS, indent=1))
        yield env
    finally:
        if proc is not None:
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    hermes = sys.argv[1] if len(sys.argv) > 1 else shutil.which("hermes") or "hermes"
    with hermes_env(hermes) as env:
        print("$ hermes fleet-policy status --json   # before any request")
        print(json.dumps(env.status(), indent=2))
        chat = env.run("-z", "ping")
        print(f"$ hermes -z ping   -> rc={chat.returncode} stdout={chat.stdout.strip()[:80]!r}")
        print("stub provider received:", json.dumps(env.stub_requests()))
        print("$ hermes fleet-policy status --json   # after one real request")
        print(json.dumps(env.status(), indent=2))
        print("$ hermes fleet-policy crons --json")
        print(env.run("fleet-policy", "crons", "--json").stdout)
        (env.managed / "config.yaml").write_text("agent: [unclosed\n")
        print("$ hermes fleet-policy status --json   # managed config.yaml broken")
        print(json.dumps(env.status()["managed"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
