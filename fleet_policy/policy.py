"""Read-only views of the policy state: managed scope, effective config, cron jobs.

Hermes surfaces used (all public, no ``_private`` symbol):

* ``hermes_cli.managed_scope.get_managed_dir()`` — where the managed layer lives (or None);
* ``hermes_cli.managed_scope.managed_config_keys()`` — dotted keys the layer pins;
* ``hermes_cli.config.load_config()`` — effective config (defaults + user + managed overlay);
* ``cron.jobs.list_jobs(include_disabled=True)`` — cron jobs (tolerant parser, normalized).

The managed ``config.yaml`` is ALSO read raw here, on purpose: Hermes fails open on a malformed
file (logs a warning, applies nothing, ``hermes doctor`` stays green) and its public API returns
``{}`` for both "absent" and "broken". This module tells them apart (``managed.error``).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

EFFECTIVE_KEYS = (
    "model.default",
    "model.provider",
    "agent.reasoning_effort",
    "agent.service_tier",
    "delegation.reasoning_effort",
    "delegation.model",
    "compression.threshold_tokens",
    "x_search.model",
)

CRON_FIELDS = ("id", "name", "enabled", "provider", "model", "reasoning_effort", "last_status",
               "failure_streak")

_FAILED_STATUSES = {"error", "failed", "blocked_config", "timeout"}


# ── YAML (no third-party dependency of our own) ──────────────────────────────────────────────

def _yaml_load(text: str) -> Any:
    """Parse YAML with whatever loader the running Hermes ships (``hermes_yaml`` on main,
    PyYAML on 0.21.x). Raises on a parse error."""
    try:
        import hermes_yaml  # main (ruamel-based)

        return hermes_yaml.safe_load(text)
    except ImportError:
        pass
    import yaml  # PyYAML — a core dependency of Hermes <= 0.21.x

    return yaml.safe_load(text)


# ── managed scope ────────────────────────────────────────────────────────────────────────────

def _managed_dir() -> Optional[Path]:
    try:
        from hermes_cli.managed_scope import get_managed_dir

        return get_managed_dir()
    except Exception:
        override = os.environ.get("HERMES_MANAGED_DIR", "").strip()
        candidate = Path(override) if override else Path("/etc/hermes")
        return candidate if candidate.is_dir() else None


def _managed_key_count() -> int:
    try:
        from hermes_cli.managed_scope import managed_config_keys

        return len(managed_config_keys())
    except Exception:
        return 0


def managed_status() -> Dict[str, Any]:
    """``{"dir", "keys", "sha256", "error"}`` for the managed ``config.yaml``.

    * ``dir``: the active managed directory, or null when no managed scope is present;
    * ``keys``: number of dotted keys Hermes reports as pinned (0 when Hermes ignored the file);
    * ``sha256``: digest of the raw managed ``config.yaml`` bytes, null when the file is absent;
    * ``error``: set when the file exists but Hermes cannot apply it (unreadable, unparseable,
      empty, or not a mapping) — the case Hermes itself only logs.
    """
    out: Dict[str, Any] = {"dir": None, "keys": 0, "sha256": None, "error": None}
    managed_dir = _managed_dir()
    if managed_dir is None:
        return out
    out["dir"] = str(managed_dir)
    path = managed_dir / "config.yaml"
    if not path.exists():
        return out
    try:
        raw = path.read_bytes()
    except OSError as exc:
        out["error"] = f"unreadable: {exc.strerror or type(exc).__name__}"
        return out
    out["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        out["error"] = "not valid UTF-8"
        return out
    if not text.strip():
        out["error"] = "empty file: no managed policy is applied"
        return out
    try:
        parsed = _yaml_load(text)
    except Exception as exc:  # noqa: BLE001 - any parser error is the finding itself
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        out["error"] = f"YAML parse error: {first_line[:200]}"
        return out
    if parsed is None:
        out["error"] = "empty document: no managed policy is applied"
        return out
    if not isinstance(parsed, dict):
        out["error"] = f"top level is {type(parsed).__name__}, expected a mapping"
        return out
    if not parsed:
        out["error"] = "empty mapping: no managed policy is applied"
        return out
    out["keys"] = _managed_key_count()
    return out


# ── effective config ─────────────────────────────────────────────────────────────────────────

def _load_effective_config() -> Dict[str, Any]:
    from hermes_cli.config import load_config  # public: defaults + config.yaml + managed overlay

    cfg = load_config()
    return cfg if isinstance(cfg, dict) else {}


def _dig(cfg: Dict[str, Any], dotted: str) -> Any:
    model = cfg.get("model")
    if dotted == "model.default" and isinstance(model, str):
        return model or None  # bare ``model: <name>`` shorthand
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    if dotted == "model.default" and not node and isinstance(model, dict):
        return model.get("model") or model.get("name") or node  # legacy spellings
    return node


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def effective_values(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if cfg is None:
        try:
            cfg = _load_effective_config()
        except Exception:
            cfg = {}
    return {key: _jsonable(_dig(cfg, key)) for key in EFFECTIVE_KEYS}


# ── cron ─────────────────────────────────────────────────────────────────────────────────────

def _list_cron_jobs() -> List[Dict[str, Any]]:
    from cron.jobs import list_jobs  # public: tolerant parse + normalization

    return list(list_jobs(include_disabled=True) or [])


def _cron_record(job: Dict[str, Any]) -> Dict[str, Any]:
    streak = job.get("failure_streak")
    try:
        streak = int(streak or 0)
    except (TypeError, ValueError):
        streak = 0
    enabled = job.get("enabled", True)
    if job.get("state") == "paused" or job.get("paused_at"):
        enabled = False
    return {
        "id": str(job.get("id") or ""),
        "name": job.get("name") or None,
        "enabled": bool(enabled),
        "provider": job.get("provider") or None,
        "model": job.get("model") or None,
        "reasoning_effort": job.get("reasoning_effort") or None,
        "last_status": job.get("last_status") or None,
        "failure_streak": streak,
    }


def cron_jobs() -> List[Dict[str, Any]]:
    try:
        jobs = _list_cron_jobs()
    except Exception:
        return []
    return [_cron_record(job) for job in jobs if isinstance(job, dict)]


def cron_audit(jobs: List[Dict[str, Any]], effective: Dict[str, Any],
               allowed_models: Optional[List[str]] = None) -> Dict[str, Any]:
    """Classify jobs: pinned model/provider, pinned effort, failing, outside an allow-list.

    A *pin* is a job-level value that wins over ``model.default`` / ``agent.reasoning_effort``
    (and over the managed scope): ``job.model > cron.model > model.default`` in the scheduler.
    """
    allowed = {m.strip() for m in (allowed_models or []) if isinstance(m, str) and m.strip()}
    pinned_model, pinned_effort, failing, outside = [], [], [], []
    for job in jobs:
        if job["model"] or job["provider"]:
            pinned_model.append(job["id"])
        if job["reasoning_effort"]:
            pinned_effort.append(job["id"])
        if job["failure_streak"] > 0 or (job["last_status"] or "").lower() in _FAILED_STATUSES:
            failing.append(job["id"])
        if allowed and job["model"] and job["model"] not in allowed:
            outside.append(job["id"])
    managed_model = effective.get("model.default")
    findings = []
    for job in jobs:
        reasons = []
        if job["model"] and job["model"] != managed_model:
            reasons.append(f"model pinned to {job['model']} (effective default: {managed_model})")
        elif job["model"] or job["provider"]:
            reasons.append("model/provider pinned")
        if job["reasoning_effort"]:
            reasons.append(f"reasoning_effort pinned to {job['reasoning_effort']}")
        if job["id"] in outside:
            reasons.append("model not in allowed_cron_models")
        if job["id"] in failing:
            reasons.append(f"failing (last_status={job['last_status']}, streak={job['failure_streak']})")
        if reasons:
            findings.append({"id": job["id"], "name": job["name"], "enabled": job["enabled"],
                             "reasons": reasons})
    return {
        "total": len(jobs),
        "pinned_model": pinned_model,
        "pinned_effort": pinned_effort,
        "failing": failing,
        "outside_allowlist": outside,
        "allowed_cron_models": sorted(allowed),
        "findings": findings,
    }
