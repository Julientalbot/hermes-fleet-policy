"""Creation-time gate for frequent cron jobs: no ungated model run more often than the admin allows.

A recurring job that runs the agent every few minutes to *watch* something pays a full model turn
on every tick, even when nothing happened (measured on a fleet: 2,684 turns in 30 days for one
mail watch, 56 % of them ``[SILENT]``). Hermes already has the cure — a pre-run ``script`` that
prints ``{"wakeAgent": false}``, a ``monitor`` source whose unchanged output skips the agent, or
``no_agent`` — but nothing makes the agent use it when a user says "tell me as soon as…".

This module is that missing default, as a ``pre_tool_call`` hook on the stock ``cronjob_manage``
tool. With the setting ``max_ungated_interval_minutes`` > 0 (off by default), a ``create``,
``update`` or ``resume`` whose resulting job would run the agent more often than that, with no
gate, is refused; the refusal (the tool result the model reads) explains how to build the gated
shape with the bundled ``recurring-tasks`` skill. The model then retries, gated, in the same turn:
the user gets the watch they asked for, without the cost.

Public surfaces only: the hook contract (``{"action": "block", "message"}``) and, lazily,
``cron.jobs.parse_schedule`` / ``cron.jobs.get_job``. Any error fails open (never blocks).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

TOOL_NAMES = frozenset({"cronjob_manage", "cronjob"})
GATED_ACTIONS = frozenset({"create", "update", "resume"})
_CRON_ALIASES = {"@hourly": 60, "@daily": 1440, "@midnight": 1440, "@weekly": 10080}


# ── schedule → shortest gap between two runs ──────────────────────────────────────────────────

def _field_values(field: str, low: int, high: int) -> Optional[list]:
    seen = set()
    for part in field.split(","):
        base, _, step = part.partition("/")
        try:
            step = int(step) if step else 1
            if base == "*":
                start, end = low, high
            elif "-" in base:
                start, end = (int(x) for x in base.split("-", 1))
            else:
                start = end = int(base)
                if step != 1:
                    end = high
        except ValueError:
            return None
        if step < 1 or start < low or end > high or start > end:
            return None
        seen.update(range(start, end + 1, step))
    return sorted(seen)


def period_minutes(schedule: Any) -> Optional[int]:
    """Shortest gap between two runs, in minutes, for a parsed Hermes schedule dict.

    None for one-shot jobs and anything unreadable (the hook then stays out of the way)."""
    if not isinstance(schedule, dict):
        return None
    kind = schedule.get("kind")
    if kind == "interval":
        try:
            return int(schedule.get("minutes")) or None
        except (TypeError, ValueError):
            return None
    if kind != "cron":
        return None
    expr = str(schedule.get("expr") or "").strip()
    if expr.startswith("@"):
        return _CRON_ALIASES.get(expr)
    fields = expr.split()
    if len(fields) < 5:
        return None
    if len(fields) == 6:  # seconds field (croniter style): treat as the minute grid it lands on
        fields = fields[1:]
    minutes, hours = _field_values(fields[0], 0, 59), _field_values(fields[1], 0, 23)
    if not minutes or not hours:
        return None
    instants = sorted(h * 60 + m for h in hours for m in minutes)
    if len(instants) == 1:
        return 1440
    gaps = [b - a for a, b in zip(instants, instants[1:])]
    gaps.append(instants[0] + 1440 - instants[-1])
    return min(gaps)


# ── the job that would result from the call ───────────────────────────────────────────────────

def _given(args: Dict[str, Any], *keys: str) -> Optional[Any]:
    """First key present with a non-None value ('' is a value: it clears)."""
    for key in keys:
        if key in args and args[key] is not None:
            return args[key]
    return None


def _parse(schedule: Any) -> Optional[dict]:
    if isinstance(schedule, dict):
        return schedule
    if not isinstance(schedule, str) or not schedule.strip():
        return None
    from cron.jobs import parse_schedule

    return parse_schedule(schedule)


def resulting_job(args: Dict[str, Any], existing: Optional[dict]) -> Dict[str, Any]:
    """Schedule + gate fields of the job after this call (create: args only; update: merged)."""
    job = dict(existing or {})
    schedule = _given(args, "schedule")
    if schedule is not None:
        job["schedule"] = _parse(schedule)
    else:
        job["schedule"] = _parse(job.get("schedule"))
    script = _given(args, "script")
    if script is not None:
        job["script"] = script or None
    monitor = _given(args, "monitor", "monitor_script", "monitor_url")
    if monitor is not None:
        job["monitor_script"] = monitor or None
        job["monitor_url"] = None
    if args.get("no_agent") is not None:
        job["no_agent"] = bool(args["no_agent"])
    return job


def gate_of(job: Dict[str, Any]) -> Optional[str]:
    if job.get("no_agent"):
        return "no_agent"
    if job.get("monitor_script") or job.get("monitor_url"):
        return "monitor"
    if job.get("script"):
        return "script"
    return None


def refusal(period: int, limit: int) -> str:
    return (
        f"Not created: this job would run the full agent every {period} min with no gate, and this "
        f"Hermes allows an ungated agent job at most every {limit} min (administrator policy). "
        "Watching something on a schedule must be gated so the model only wakes when there is "
        "something new. Load the skill `fleet-policy:recurring-tasks` now and set it up that way — "
        "for news from a tool (mail, calendar, CRM, sheet…): a `watch` config + a one-line script, "
        "then the job with `script=\"watch-<name>.sh\"` (it prints {\"wakeAgent\": false} when "
        "nothing is new); for a web page: `monitor=\"<url>\"`; for a fixed alert: `no_agent=true` + "
        f"script. A plain report or reminder may instead run every {limit} min or less often. Keep "
        "the user's request as is (same source, criterion, delivery); no need to ask them again."
    )


def check(args: Dict[str, Any], limit: int, get_job: Optional[Callable[[str], Optional[dict]]] = None
          ) -> Optional[str]:
    """Refusal message for a cronjob_manage call that breaks the limit, else None."""
    if limit <= 0 or not isinstance(args, dict):
        return None
    action = str(args.get("action") or "").strip().lower()
    if action not in GATED_ACTIONS:
        return None
    existing = None
    if action != "create":
        job_id = args.get("job_id")
        if not job_id:
            return None
        if get_job is None:
            from cron import jobs as _jobs  # lazy: public cron API, absent outside Hermes

            get_job = _jobs.get_job
        existing = get_job(str(job_id))
        if not existing:
            return None
        if action == "update" and existing.get("enabled") is False and args.get("schedule") is None:
            return None  # editing a paused job's prompt/delivery costs nothing; resume is checked
    job = resulting_job(args, existing)
    if gate_of(job):
        return None
    period = period_minutes(job.get("schedule"))
    if period is None or period >= limit:
        return None
    return refusal(period, limit)


def make_hook(get_setting: Callable[..., Any]):
    """``pre_tool_call`` hook bound to the plugin settings (read at call time: managed changes apply)."""

    def on_pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **_: Any):
        if tool_name not in TOOL_NAMES:
            return None
        try:
            limit = int(get_setting("max_ungated_interval_minutes", 0) or 0)
            message = check(args or {}, limit)
        except Exception:  # fail open: a broken gate must never block the user's scheduling
            logger.debug("fleet-policy: cron gate skipped", exc_info=True)
            return None
        if message:
            logger.info("fleet-policy: refused an ungated frequent cron job (%s)", (args or {}).get("action"))
            return {"action": "block", "message": message}
        return None

    return on_pre_tool_call
