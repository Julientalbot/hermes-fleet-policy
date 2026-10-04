"""``hermes fleet-policy status|crons [--json]`` (registered via ``ctx.register_cli_command``)."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Dict, List, Optional

from . import PLUGIN_NAME, __version__, policy, store

SettingsGetter = Callable[[str, Any], Any]


def build_status() -> Dict[str, Any]:
    """The machine contract of ``hermes fleet-policy status --json`` (keys and order fixed)."""
    return {
        "plugin": PLUGIN_NAME,
        "version": __version__,
        "managed": policy.managed_status(),
        "effective": policy.effective_values(),
        "observed": store.read_observed(),
        "crons": policy.cron_jobs(),
    }


def build_crons(get_setting: Optional[SettingsGetter] = None, *, cost: bool = False,
                days: int = 7) -> Dict[str, Any]:
    allowed: List[str] = []
    if get_setting is not None:
        try:
            raw = get_setting("allowed_cron_models", [])
            allowed = [str(x) for x in raw] if isinstance(raw, list) else []
        except Exception:
            allowed = []
    jobs = policy.cron_jobs()
    effective = policy.effective_values()
    report = {
        "plugin": PLUGIN_NAME,
        "version": __version__,
        "crons": jobs,
        "audit": policy.cron_audit(jobs, effective, allowed),
    }
    if cost:  # optional block: existing keys and their order are unchanged
        from . import cost as cost_mod

        report["cost"] = cost_mod.build_cost(policy.raw_cron_jobs(), store.hermes_home(),
                                             window_days=days)
    return report


def _print_json(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


def _render_status(status: Dict[str, Any]) -> str:
    lines = [f"fleet-policy {status['version']}"]
    managed = status["managed"]
    if managed["dir"] is None:
        lines.append("managed scope: none")
    else:
        state = f"ERROR — {managed['error']}" if managed["error"] else f"{managed['keys']} key(s) pinned"
        digest = (managed["sha256"] or "")[:12] or "no config.yaml"
        lines.append(f"managed scope: {managed['dir']} ({state}; sha256 {digest})")
    lines.append("effective:")
    for key, value in status["effective"].items():
        lines.append(f"  {key:<30} {value!r}")
    lines.append("observed (last call per role):")
    if not status["observed"]:
        lines.append("  (nothing yet — enable the plugin and run one turn)")
    for role, entry in sorted(status["observed"].items()):
        served = entry["response_service_tier"]
        lines.append(
            f"  {role:<28} {entry['model']}  effort={entry['reasoning_effort']}  "
            f"tier={entry['service_tier']}" + (f" served={served}" if served else "") + f"  {entry['ts']}")
    lines.append(f"cron jobs: {len(status['crons'])}")
    return "\n".join(lines)


def _render_crons(report: Dict[str, Any]) -> str:
    audit = report["audit"]
    lines = [
        f"cron jobs: {audit['total']}  pinned model: {len(audit['pinned_model'])}  "
        f"pinned effort: {len(audit['pinned_effort'])}  failing: {len(audit['failing'])}"
        + (f"  outside allow-list: {len(audit['outside_allowlist'])}" if audit["allowed_cron_models"] else "")
    ]
    for finding in audit["findings"]:
        flag = "" if finding["enabled"] else " (disabled)"
        lines.append(f"- {finding['id']} {finding['name'] or ''}{flag}: " + "; ".join(finding["reasons"]))
    if audit["pinned_model"]:
        lines.append("Release a model pin with: hermes cron edit <id> --unpin")
    cost = report.get("cost")
    if cost:
        lines.append(f"cost over {cost['window_days']} d (since {cost['since']}): "
                     f"{cost['total_cost_per_day_usd']:.4f} $/day in total")
        if not cost["sources"]["executions"] or not cost["sources"]["state_db"]:
            lines.append(f"  sources: executions={cost['sources']['executions']} "
                         f"state_db={cost['sources']['state_db']}")
        for job in sorted(cost["jobs"], key=lambda j: -j["cost_per_day_usd"]):
            share = "n/a" if job["empty_share"] is None else f"{job['empty_share']:.0%}"
            lines.append(
                f"  {job['id']} {job['name'] or ''}: {job['runs']} runs ({job['runs_per_day']}/day), "
                f"empty {share}, {job['cost_per_day_usd']:.4f} $/day, gate={job['gate'] or 'none'}"
                + (f"  [{', '.join(job['flags'])}]" if job["flags"] else ""))
    return "\n".join(lines)


def setup_parser(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="fleet_policy_command")
    status = subs.add_parser("status", help="Managed scope, effective policy, observed calls, cron jobs")
    status.add_argument("--json", action="store_true", help="Machine-readable output (stable contract)")
    crons = subs.add_parser("crons", help="Audit cron jobs: model/effort pins, failures, allow-list")
    crons.add_argument("--json", action="store_true", help="Machine-readable output")
    crons.add_argument("--cost", action="store_true",
                       help="Add measured runs, empty share and LLM cost per job (cron/executions.db "
                            "+ state.db)")
    crons.add_argument("--days", type=int, default=7, help="Window for --cost (default 7)")
    watch = subs.add_parser(
        "watch", help="Report NEW items of one MCP read tool, else print {\"wakeAgent\": false}",
        description="Call one MCP tool, report items whose id was never seen, stay silent "
                    "otherwise. Meant as the pre-run script of a cron job (stock wake gate).")
    watch.add_argument("--config", required=True, help="Watch config (JSON), see README")
    watch.add_argument("--dry-run", action="store_true",
                       help="Show what would be reported; never write the state")


def make_handler(get_setting: Optional[SettingsGetter] = None) -> Callable[[argparse.Namespace], int]:
    def handle(args: argparse.Namespace) -> int:
        command = getattr(args, "fleet_policy_command", None) or "status"
        if command == "watch":
            from . import watch

            return watch.run(args.config, dry_run=bool(getattr(args, "dry_run", False)))
        as_json = bool(getattr(args, "json", False))
        if command == "crons":
            report = build_crons(get_setting, cost=bool(getattr(args, "cost", False)),
                                 days=max(1, int(getattr(args, "days", 7) or 7)))
            _print_json(report) if as_json else print(_render_crons(report))
            return 0
        status = build_status()
        _print_json(status) if as_json else print(_render_status(status))
        return 0

    return handle
