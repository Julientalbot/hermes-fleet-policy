"""Atomic, content-free store of the last observed provider call per role.

File: ``$HERMES_HOME/fleet-policy/observed.json`` — a JSON object ``{"<role>": entry}`` where
``entry`` has exactly the fields of the public contract::

    {"model": str, "reasoning_effort": str|null, "service_tier": str|null,
     "response_service_tier": str|null, "ts": "<ISO-8601 UTC>"}

No message content, prompt, header or key is ever written: callers hand in scalars only, and
``write_entry`` re-validates every value (short strings or null).
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

try:  # POSIX cross-process lock; Windows falls back to the in-process lock only.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore

ENTRY_FIELDS = ("model", "reasoning_effort", "service_tier", "response_service_tier", "ts")
STORE_DIRNAME = "fleet-policy"
STORE_FILENAME = "observed.json"
_MAX_VALUE_CHARS = 200
_MAX_ROLES = 64  # bounded: roles are main/delegation/cron/unknown + auxiliary:<task>

_LOCK = threading.Lock()


def hermes_home() -> Path:
    """Active Hermes home (profile-aware).

    ``hermes_constants.get_hermes_home`` is the documented way for code running inside Hermes
    (it honours per-context profile overrides of the multiplexed gateway); outside Hermes we fall
    back to ``$HERMES_HOME`` then ``~/.hermes``.
    """
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME", "").strip()
        return Path(env).expanduser() if env else Path.home() / ".hermes"


def store_path(home: Optional[Path] = None) -> Path:
    return (home or hermes_home()) / STORE_DIRNAME / STORE_FILENAME


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value[:_MAX_VALUE_CHARS] if value else None


def _clean_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    out = {field: _clean(entry.get(field)) for field in ENTRY_FIELDS}
    out["model"] = out["model"] or "unknown"
    out["ts"] = out["ts"] or now_iso()
    return out


def read_observed(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Contract-shaped ``observed`` mapping; ``{}`` when absent or unreadable."""
    path = path or store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(role): _clean_entry(entry)
        for role, entry in data.items()
        if isinstance(entry, dict)
    }


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            yield
            return
        with open(path.with_name(path.name + ".lock"), "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".observed-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_entry(role: str, entry: Dict[str, Any], *, path: Optional[Path] = None,
                patch: bool = False) -> None:
    """Record *entry* as the last call of *role* (read-modify-replace under a lock).

    ``patch=True`` keeps the stored entry and only overwrites the fields of *entry* that are
    not ``None`` (used by the execution observer to add the served tier, or to fill request
    fields the size-capped hook payload could not show). Without a stored entry it behaves
    like a plain write.
    """
    path = path or store_path()
    role = (_clean(role) or "unknown")[:80]
    with _locked(path):
        current = read_observed(path)
        if patch and role in current:
            base = dict(current[role])
            base.update({k: v for k, v in entry.items() if k in ENTRY_FIELDS and v is not None})
            new_entry = _clean_entry(base)
        else:
            new_entry = _clean_entry(entry)
        current[role] = new_entry
        if len(current) > _MAX_ROLES:  # drop the oldest roles, keep the file bounded
            keep = sorted(current.items(), key=lambda kv: kv[1]["ts"], reverse=True)[:_MAX_ROLES]
            current = dict(keep)
        _atomic_write(path, current)
