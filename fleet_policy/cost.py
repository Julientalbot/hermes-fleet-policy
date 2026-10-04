"""``hermes fleet-policy crons --cost``: runs, empty share and LLM cost per cron job (measured).

Read-only, from what Hermes itself stores under ``$HERMES_HOME`` (schemas checked against Hermes
v2026.9.24 and main):

* ``cron/executions.db`` table ``executions`` — one row per run attempt (``job_id``, ``status`` in
  claimed/running/completed/failed/unknown, ISO ``started_at``/``finished_at``). Hermes prunes it to
  the newest 1000 terminal rows across ALL jobs, so on a busy profile it may cover less than the
  window: ``coverage_from`` says from when, and per-day figures use the covered span.
* ``state.db`` table ``sessions`` — every agent turn of a cron run is a session whose id is
  ``cron_<job_id>_<YYYYmmdd_HHMMSS>``; compression/delegation children hang below it through
  ``parent_session_id``. Main-loop cost is ``COALESCE(actual_cost_usd, estimated_cost_usd)`` of each
  session; auxiliary calls (compression, title, vision, background review…) are only in
  ``session_model_usage`` rows with ``task != ''`` and are added. ``messages`` gives the final reply.

Definitions (also in the README):

* run: a terminal execution (completed, failed or unknown) claimed inside the window;
* agent turn: a root ``cron_<job_id>_*`` session started inside a run (or inside the window when the
  ledger has no row for it);
* empty run: a completed run that told the user nothing — the agent was not started (script gate
  ``wakeAgent: false``, empty script output, monitor ``no_change``) or its final reply was empty or a
  silence marker (``[SILENT]``, ``SILENT``, ``NO_REPLY``…, as the scheduler matches it). For
  ``no_agent`` jobs (no LLM, no cost) emptiness is not measured (null).
* cost per day: the cost of the job's agent turns in the covered span, divided by that span in days.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_WINDOW_DAYS = 7
EXECUTIONS_PRUNE_CAP = 1000  # cron/executions.py MAX_TERMINAL_EXECUTIONS
FREQUENT_RUNS_PER_DAY = 24.0  # more than once an hour
MOSTLY_EMPTY_SHARE = 0.8
_TERMINAL = ("completed", "failed", "unknown")
_SILENT_MARKERS = {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY", "[静默]", "静默", "[沉默]", "沉默"}

DEFINITION = (
    "run = terminal execution in the window; empty run = completed run whose agent was not "
    "started (wakeAgent false, empty script output, monitor no_change) or whose final reply was "
    "empty or a silence marker; no_agent jobs: empty not measured; cost = main-loop session cost "
    "(actual, else estimated) + auxiliary usage, for the job's cron_<id>_* sessions and their "
    "children; per-day figures divide by the covered span"
)


# ── helpers ──────────────────────────────────────────────────────────────────────────────────

def is_silence(text: Optional[str]) -> bool:
    """The scheduler's own matcher when importable, else the same rule re-stated."""
    try:
        from gateway.response_filters import is_autonomous_silence_response

        if not (text or "").strip():
            return True
        return bool(is_autonomous_silence_response(text))
    except Exception:
        pass
    stripped = (text or "").strip()
    if not stripped:
        return True
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]

    def bare(s: str) -> str:
        return s.strip().rstrip(".!。").upper()

    if stripped.upper().startswith(tuple(m for m in _SILENT_MARKERS if m.startswith("["))):
        return True
    return any(bare(c) in _SILENT_MARKERS for c in (stripped, lines[0], lines[-1]))


def _parse_ts(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive = local time, as hermes_now() writes it without a zone
    return dt.timestamp()


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _connect_ro(path: Path) -> Optional[sqlite3.Connection]:
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1").fetchone()
        return conn
    except sqlite3.Error:
        return None


def _columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def job_gate(job: Dict[str, Any]) -> Optional[str]:
    """What keeps the agent asleep: ``no_agent`` > ``monitor`` > ``script`` > None (agent every fire)."""
    if job.get("no_agent"):
        return "no_agent"
    if job.get("monitor_script") or job.get("monitor_url"):
        return "monitor"
    if job.get("script"):
        return "script"
    return None


def _schedule_text(job: Dict[str, Any]) -> Optional[str]:
    sched = job.get("schedule")
    if isinstance(sched, dict):
        return sched.get("display") or sched.get("expr") or (
            f"every {sched['minutes']}m" if sched.get("minutes") else sched.get("kind"))
    return str(sched) if sched else None


# ── sources ──────────────────────────────────────────────────────────────────────────────────

def read_executions(path: Path, since: float) -> Tuple[Dict[str, List[dict]], Optional[float], bool]:
    """``({job_id: [run]}, coverage_from, available)``; run = {status, start, end}."""
    conn = _connect_ro(path)
    if conn is None:
        return {}, None, False
    try:
        if "job_id" not in _columns(conn, "executions"):
            return {}, None, False
        rows = conn.execute(
            f"SELECT job_id, status, claimed_at, started_at, finished_at FROM executions "
            f"WHERE status IN ({','.join('?' * len(_TERMINAL))})", _TERMINAL).fetchall()
    except sqlite3.Error:
        return {}, None, False
    finally:
        conn.close()
    coverage_from = None
    if len(rows) >= EXECUTIONS_PRUNE_CAP:  # Hermes pruned older rows: coverage starts at the oldest
        coverage_from = min((t for t in (_parse_ts(r["claimed_at"]) for r in rows) if t), default=None)
    runs: Dict[str, List[dict]] = {}
    for row in rows:
        claimed = _parse_ts(row["claimed_at"])
        if claimed is None or claimed < since:
            continue
        runs.setdefault(str(row["job_id"]), []).append({
            "status": row["status"],
            "start": _parse_ts(row["started_at"]) or claimed,
            "end": _parse_ts(row["finished_at"]),
        })
    return runs, coverage_from, True


_LINEAGE_SQL = """
WITH RECURSIVE tree(root, id) AS (
    SELECT id, id FROM sessions WHERE id LIKE ? ESCAPE '\\' AND started_at >= ?
        AND COALESCE(parent_session_id, '') = ''
    UNION ALL
    SELECT tree.root, s.id FROM sessions s JOIN tree ON s.parent_session_id = tree.id
)
SELECT tree.root AS root, tree.id AS id FROM tree
"""


def read_turns(path: Path, job_ids: Iterable[str], since: float) -> Tuple[Dict[str, List[dict]], bool]:
    """``({job_id: [turn]}, available)``; turn = {start, cost_usd, silent, tokens}."""
    conn = _connect_ro(path)
    if conn is None:
        return {}, False
    cols = _columns(conn, "sessions")
    if not {"id", "started_at", "parent_session_id"} <= cols:
        conn.close()
        return {}, False
    has_smu = "task" in _columns(conn, "session_model_usage")
    has_msgs = {"session_id", "role", "content"} <= _columns(conn, "messages")
    out: Dict[str, List[dict]] = {}
    try:
        for job_id in job_ids:
            pattern = "cron\\_" + job_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "\\_%"
            members: Dict[str, List[str]] = {}
            for row in conn.execute(_LINEAGE_SQL, (pattern, since)):
                members.setdefault(row["root"], []).append(row["id"])
            turns = []
            for root, ids in members.items():
                marks = ",".join("?" * len(ids))
                srow = conn.execute(
                    f"SELECT MIN(CASE WHEN id = ? THEN started_at END) AS start, "
                    f"COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd, 0)), 0) AS cost, "
                    f"COALESCE(SUM(COALESCE(input_tokens,0) + COALESCE(output_tokens,0) "
                    f"+ COALESCE(cache_read_tokens,0) + COALESCE(cache_write_tokens,0)), 0) AS tokens "
                    f"FROM sessions WHERE id IN ({marks})", (root, *ids)).fetchone()
                cost, tokens = float(srow["cost"] or 0), int(srow["tokens"] or 0)
                if has_smu:
                    arow = conn.execute(
                        f"SELECT COALESCE(SUM(CASE WHEN actual_cost_usd > 0 THEN actual_cost_usd "
                        f"ELSE estimated_cost_usd END), 0) AS cost, "
                        f"COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens "
                        f"+ cache_write_tokens), 0) AS tokens "
                        f"FROM session_model_usage WHERE task != '' AND session_id IN ({marks})",
                        ids).fetchone()
                    cost += float(arow["cost"] or 0)
                    tokens += int(arow["tokens"] or 0)
                final = None
                if has_msgs:
                    mrow = conn.execute(
                        f"SELECT content FROM messages WHERE role = 'assistant' AND session_id IN "
                        f"({marks}) AND content IS NOT NULL AND TRIM(content) != '' "
                        f"ORDER BY timestamp DESC, id DESC LIMIT 1", ids).fetchone()
                    final = mrow["content"] if mrow else None
                turns.append({"start": float(srow["start"] or 0), "cost_usd": cost,
                              "tokens": tokens, "silent": is_silence(final) if has_msgs else None})
            out[job_id] = turns
    except sqlite3.Error:
        return out, bool(out)
    finally:
        conn.close()
    return out, True


# ── report ───────────────────────────────────────────────────────────────────────────────────

def _job_created(job: Dict[str, Any]) -> Optional[float]:
    return _parse_ts(job.get("created_at"))


def job_cost(job: Dict[str, Any], runs: List[dict], turns: List[dict], *, since: float, now: float,
             coverage_from: Optional[float], executions_ok: bool,
             state_ok: bool = True) -> Dict[str, Any]:
    gate = job_gate(job)
    starts = [since] + [t for t in (coverage_from, _job_created(job)) if t]
    span_start = min(max(starts), now)
    span_days = max((now - span_start) / 86400.0, 1 / 24.0)
    runs = [r for r in runs if r["start"] >= span_start - 1]
    turns = [t for t in turns if t["start"] >= span_start - 1]

    # Attach each agent turn to the run that contains its start time.
    matched_turns = set()
    run_turn: Dict[int, int] = {}
    for ri, run in enumerate(runs):
        end = run["end"] if run["end"] is not None else now
        for ti, turn in enumerate(turns):
            if ti not in matched_turns and run["start"] - 2 <= turn["start"] <= end + 2:
                matched_turns.add(ti)
                run_turn[ri] = ti
                break
    unmatched = [t for i, t in enumerate(turns) if i not in matched_turns]

    completed = [i for i, r in enumerate(runs) if r["status"] == "completed"]
    failed = len(runs) - len(completed)
    if executions_ok:
        n_runs = len(runs) + len(unmatched)  # turns without a ledger row still ran
    else:
        n_runs = len(turns)
    empty: Optional[int]
    if gate == "no_agent" or not state_ok:
        empty = None  # no LLM to measure / no session store to tell gated from answered
    else:
        gated = sum(1 for i in completed if i not in run_turn)
        silent = sum(1 for t in turns if t["silent"])
        empty = gated + silent if executions_ok else silent
    cost = sum(t["cost_usd"] for t in turns)
    runs_per_day = n_runs / span_days
    share = (empty / n_runs) if (empty is not None and n_runs) else None
    flags = []
    if gate is None and runs_per_day > FREQUENT_RUNS_PER_DAY:
        flags.append("frequent_without_gate")
    if share is not None and share > MOSTLY_EMPTY_SHARE and n_runs >= 5:
        flags.append("mostly_empty")
    return {
        "id": str(job.get("id") or ""),
        "name": job.get("name") or None,
        "enabled": bool(job.get("enabled", True)) and job.get("state") != "paused",
        "schedule": _schedule_text(job),
        "gate": gate,
        "span_days": round(span_days, 3),
        "runs": n_runs,
        "runs_failed": failed,
        "agent_turns": len(turns),
        "empty_runs": empty,
        "empty_share": round(share, 3) if share is not None else None,
        "runs_per_day": round(runs_per_day, 2),
        "tokens": sum(t["tokens"] for t in turns),
        "cost_usd": round(cost, 6),
        "cost_per_day_usd": round(cost / span_days, 6),
        "cost_per_run_usd": round(cost / n_runs, 6) if n_runs else None,
        "flags": flags,
    }


def build_cost(jobs: List[Dict[str, Any]], home: Path, *, window_days: int = DEFAULT_WINDOW_DAYS,
               now: Optional[float] = None) -> Dict[str, Any]:
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    since = now - timedelta(days=window_days).total_seconds()
    exec_path = home / "cron" / "executions.db"
    state_path = home / "state.db"
    runs, coverage_from, executions_ok = read_executions(exec_path, since)
    ids = [str(j.get("id") or "") for j in jobs if j.get("id")]
    turns, state_ok = read_turns(state_path, ids, since)
    report_jobs = [
        job_cost(job, runs.get(str(job.get("id")), []), turns.get(str(job.get("id")), []),
                 since=since, now=now,
                 coverage_from=coverage_from if coverage_from and coverage_from > since else None,
                 executions_ok=executions_ok, state_ok=state_ok)
        for job in jobs if job.get("id")
    ]
    return {
        "window_days": window_days,
        "since": _iso(since),
        "until": _iso(now),
        "sources": {
            "executions": executions_ok,
            "state_db": state_ok,
            "executions_coverage_from": _iso(coverage_from) if coverage_from and coverage_from > since else None,
        },
        "definition": DEFINITION,
        "jobs": report_jobs,
        "total_cost_per_day_usd": round(sum(j["cost_per_day_usd"] for j in report_jobs), 6),
        "flagged": [j["id"] for j in report_jobs if j["flags"]],
    }
