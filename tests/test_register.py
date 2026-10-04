"""register(): hooks, CLI command and both bundled skills, through a minimal fake context."""
import argparse

import fleet_policy


class Ctx:
    def __init__(self):
        self.hooks, self.middleware, self.commands, self.skills = [], [], [], {}

    def register_hook(self, name, fn):
        self.hooks.append(name)

    def register_middleware(self, name, fn):
        self.middleware.append(name)

    def register_cli_command(self, **kw):
        self.commands.append(kw)

    def register_skill(self, name, path, description=""):
        assert path.is_file() and len(description) <= 60
        self.skills[name] = path

    def get_config(self, key, default=None):
        return default


def test_register_everything():
    ctx = Ctx()
    fleet_policy.register(ctx)
    assert set(ctx.skills) == {"fleet-policy-admin", "recurring-tasks"}
    assert ctx.commands[0]["name"] == "fleet-policy"
    parser = argparse.ArgumentParser()
    ctx.commands[0]["setup_fn"](parser)
    args = parser.parse_args(["watch", "--config", "x.json", "--dry-run"])
    assert args.fleet_policy_command == "watch" and args.dry_run
    args = parser.parse_args(["crons", "--cost", "--json", "--days", "3"])
    assert args.cost and args.days == 3


def test_skill_frontmatter():
    from pathlib import Path

    text = (Path(fleet_policy.__file__).parent / "skills" / "recurring-tasks" / "SKILL.md").read_text()
    head = text.split("---")[1]
    assert "name: recurring-tasks" in head
    for needle in ("wakeAgent", "COMPOSIO_MULTI_EXECUTE_TOOL", "monitor_url", "--dry-run",
                   "watch-<name>.sh", "[SILENT]"):
        assert needle in text
