"""fleet-policy — see what a Hermes agent actually sends to its LLM provider.

Observer-only plugin for administrators who pin Hermes settings with the managed scope
(``/etc/hermes/config.yaml`` or ``$HERMES_MANAGED_DIR``). It records, per role, the model,
reasoning effort and service tier of the last real provider request, and exposes them with the
managed-scope state, the effective config and the cron jobs::

    hermes fleet-policy status --json
    hermes fleet-policy crons --json [--cost]
    hermes fleet-policy watch --config <watch.json> [--dry-run]

Public surfaces only: ``ctx.register_hook``, ``ctx.register_middleware`` (observer pass-through),
``ctx.register_cli_command``, ``ctx.register_skill``, ``ctx.get_config``; plus the public
``hermes_cli.config.load_config``, ``hermes_cli.managed_scope`` and ``cron.jobs.list_jobs``
functions at CLI time. No core object is patched.
"""
from __future__ import annotations

import logging
from pathlib import Path

__version__ = "0.2.1"
PLUGIN_NAME = "fleet-policy"
SKILLS = (
    ("fleet-policy-admin", "Administer a managed Hermes policy and verify it."),
    ("recurring-tasks", "Watch jobs that wake the model only on new items."),
)

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    from . import cli, observer

    ctx.register_hook("pre_api_request", observer.on_pre_api_request)
    ctx.register_hook("pre_auxiliary_call", observer.on_pre_auxiliary_call)

    observe_execution = True
    try:
        observe_execution = bool(ctx.get_config("observe_execution", True))
    except Exception:  # get_config rejects nothing for a plain key; stay on the safe default
        pass
    if observe_execution:
        # Pass-through: calls next_call() once and returns its result unchanged. It is the only
        # public surface that sees the untruncated request and the served ``service_tier``.
        ctx.register_middleware("llm_execution", observer.observe_llm_execution)

    def _setting(key, default=None):
        return ctx.get_config(key, default)

    ctx.register_cli_command(
        name=PLUGIN_NAME,
        help="Managed policy vs. what is actually sent to the provider",
        setup_fn=cli.setup_parser,
        handler_fn=cli.make_handler(_setting),
        description="Show the managed scope, the effective policy, the last observed provider "
                    "request per role, and audit cron jobs.",
    )

    for name, description in SKILLS:
        skill_md = Path(__file__).parent / "skills" / name / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            ctx.register_skill(name, skill_md, description=description)
        except Exception:
            logger.debug("fleet-policy: skill %s registration skipped", name, exc_info=True)
