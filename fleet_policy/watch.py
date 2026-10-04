"""``fleet-policy watch --config <file.json>``: report NEW items of one MCP read tool, else sleep.

Built for the stock cron wake gate of Hermes (``cron/scheduler_prompt.py::_parse_wake_gate``): a job
with ``script=watch-<name>.sh`` runs this command before the agent. When nothing is new, the last
stdout line is ``{"wakeAgent": false}`` and the agent is not started (no LLM call). When items are
new, one short line per item is printed; Hermes injects that output into the agent's prompt.

Generic on purpose: the code knows no service and holds no business rule. A watch is

* ONE tool call on ONE MCP server (streamable HTTP): ``server_url`` + ``tool`` + ``arguments``;
* the name of the field that identifies an item (``id_field``), searched at any depth of the
  result, including inside JSON documents embedded as strings (MCP relays often wrap the
  provider's answer that way);
* ``show_fields``: which fields of a new item to print for the agent.

"New" means an id never seen before (set difference against a bounded, atomically written state
file). Comparing ids, not the raw output, matters: a typical read tool returns volatile fields
(timestamps, request ids, signed URLs) that change on every call, and an item that disappears is
not news. Semantics:

* first run (no state file): record the baseline and stay silent;
* tool or transport failure, ``successful: false`` or an ``error`` in the response envelope:
  exit code 1, message on stderr, state untouched (Hermes then runs the agent with a
  "Script Error" block, so a broken watch is never silent and nothing is marked as seen);
* ``--dry-run``: print what would be reported, never write the state.

Config file (JSON)::

    {"server_url": "http://127.0.0.1:8787/mcp", "tool": "LIST_ITEMS", "arguments": {},
     "id_field": "id", "show_fields": ["title"], "max_new": 20,
     "state": "watches/<name>.state.json",
     "headers": {"Authorization": "Bearer ${TOKEN_ENV_VAR}"},   # optional, ${VAR} expanded
     "timeout_s": 60, "max_seen": 5000}                          # optional

Relative ``state`` paths resolve against the config file's folder; default
``<config stem>.state.json`` next to it.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

GATE_SLEEP = json.dumps({"wakeAgent": False})
DEFAULT_MAX_NEW = 20
DEFAULT_MAX_SEEN = 5000
DEFAULT_TIMEOUT_S = 60.0
MAX_VALUE_CHARS = 120
STATE_VERSION = 1
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class WatchError(Exception):
    """The source could not be read (tool, transport, response envelope or state)."""


class ConfigError(Exception):
    """The watch config is invalid."""


@dataclass
class WatchConfig:
    path: Path
    server_url: str
    tool: str
    arguments: Dict[str, Any]
    id_field: str
    state: Path
    show_fields: List[str] = field(default_factory=list)
    max_new: int = DEFAULT_MAX_NEW
    max_seen: int = DEFAULT_MAX_SEEN
    timeout_s: float = DEFAULT_TIMEOUT_S
    headers: Dict[str, str] = field(default_factory=dict)
    name: str = ""


# ── config ───────────────────────────────────────────────────────────────────────────────────

def _expand_env(value: str) -> str:
    def repl(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(f"header references ${{{name}}}, which is not set")
        return os.environ[name]

    return _ENV_REF.sub(repl, value)


def _positive_int(raw: Dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"'{key}' must be a positive integer")
    return value


def load_config(path: "str | Path") -> WatchConfig:
    path = Path(path).expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from None
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")
    server_url = raw.get("server_url")
    if not isinstance(server_url, str) or not server_url.startswith(("http://", "https://")):
        raise ConfigError("'server_url' must be an http(s) URL of a streamable-HTTP MCP endpoint")
    tool = raw.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise ConfigError("'tool' must name the MCP tool to call")
    arguments = raw.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ConfigError("'arguments' must be a JSON object")
    id_field = raw.get("id_field")
    if not isinstance(id_field, str) or not id_field.strip():
        raise ConfigError("'id_field' must name the field that identifies an item")
    show = raw.get("show_fields", [])
    if not isinstance(show, list) or not all(isinstance(f, str) and f for f in show):
        raise ConfigError("'show_fields' must be a list of field names")
    headers = raw.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
        raise ConfigError("'headers' must map header names to strings")
    timeout = raw.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("'timeout_s' must be a positive number")
    state_raw = raw.get("state") or f"{path.stem}.state.json"
    if not isinstance(state_raw, str):
        raise ConfigError("'state' must be a path")
    state = Path(os.path.expandvars(state_raw)).expanduser()
    if not state.is_absolute():
        state = path.parent / state
    return WatchConfig(
        path=path, server_url=server_url, tool=tool.strip(), arguments=arguments,
        id_field=id_field.strip(), state=state, show_fields=list(show),
        max_new=_positive_int(raw, "max_new", DEFAULT_MAX_NEW),
        max_seen=_positive_int(raw, "max_seen", DEFAULT_MAX_SEEN),
        timeout_s=float(timeout),
        headers={str(k): _expand_env(v) for k, v in headers.items()},
        name=str(raw.get("name") or path.stem),
    )


# ── response analysis (no knowledge of any service) ─────────────────────────────────────────

def _maybe_json(value: Any) -> Any:
    """Decode a string that holds a JSON object/array; anything else is returned unchanged."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[":
            try:
                return json.loads(text)
            except ValueError:
                return value
    return value


def _is_id(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) != ""


def _walk(node: Any, id_field: str, items: List[dict], errors: List[str], path: str) -> None:
    """Collect items (outermost dicts carrying a scalar ``id_field``) and envelope errors.

    Inside an item nothing is searched: its nested ids are attributes (an event's creator id),
    and an ``error`` field there is data about the item, not a failed call.
    """
    node = _maybe_json(node)
    if isinstance(node, dict):
        if _is_id(node.get(id_field)):
            items.append(node)
            return
        if node.get("successful") is False or node.get("success") is False:
            errors.append(f"{path or '$'}: successful=false"
                          + (f" ({_short(node.get('error'), 300)})" if node.get("error") else ""))
        elif node.get("error") not in (None, "", False, {}, []):
            errors.append(f"{path or '$'}.error: {_short(node.get('error'), 300)}")
        for key, value in node.items():
            _walk(value, id_field, items, errors, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, id_field, items, errors, f"{path}[{index}]")


def analyze(payload: Any, id_field: str) -> Tuple[List[dict], List[str]]:
    items: List[dict] = []
    errors: List[str] = []
    _walk(payload, id_field, items, errors, "")
    return items, errors


def extract_items(payload: Any, id_field: str) -> List[dict]:
    return analyze(payload, id_field)[0]


def item_id(item: dict, id_field: str) -> str:
    return str(item[id_field])


def _short(value: Any, limit: int = MAX_VALUE_CHARS) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _find_field(node: Any, name: str) -> Any:
    """``a.b`` follows a path; a bare name is the first scalar found at any depth (breadth-first)."""
    if "." in name:
        cur = node
        for part in name.split("."):
            cur = _maybe_json(cur)
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur
    queue = [node]
    while queue:
        cur = _maybe_json(queue.pop(0))
        if isinstance(cur, dict):
            value = cur.get(name)
            if value is not None and not isinstance(value, (dict, list)):
                return value
            queue.extend(cur.values())
        elif isinstance(cur, list):
            queue.extend(cur)
    return None


def render_item(item: dict, cfg: WatchConfig) -> str:
    parts = [f"id={_short(item_id(item, cfg.id_field), 80)}"]
    for name in cfg.show_fields:
        value = _find_field(item, name)
        if value is not None and value != "":
            parts.append(f"{name}: {_short(value)}")
    return "- " + " | ".join(parts)


# ── MCP call ─────────────────────────────────────────────────────────────────────────────────

def parse_tool_result(result: Any) -> Any:
    """``CallToolResult`` (mcp 1.x camelCase or 2.x snake_case) → JSON value; raises on error."""
    texts = [getattr(block, "text", "") or "" for block in (getattr(result, "content", None) or [])]
    text = "".join(texts)
    if getattr(result, "is_error", None) or getattr(result, "isError", None):
        raise WatchError(f"tool returned an error: {_short(text, 500) or '(no detail)'}")
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    try:
        return json.loads(text)
    except ValueError:
        raise WatchError(f"tool result is not JSON: {_short(text, 200)!r}") from None


async def _call_async(cfg: WatchConfig) -> Any:
    from mcp import ClientSession

    try:  # mcp >= 1.24 (2.x: the only spelling); client object carries headers/timeouts
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:  # older SDK
        streamable_http_client = None
    if streamable_http_client is not None:
        try:
            import httpx2 as http  # mcp 2.x moved its HTTP stack to httpx2
        except ImportError:
            import httpx as http  # mcp 1.x
        client = http.AsyncClient(headers=cfg.headers or None, follow_redirects=True,
                                  timeout=http.Timeout(cfg.timeout_s, read=cfg.timeout_s))
        transport = streamable_http_client(cfg.server_url, http_client=client)
    else:
        from mcp.client.streamable_http import streamablehttp_client

        client = None
        transport = streamablehttp_client(cfg.server_url, headers=cfg.headers or None,
                                          timeout=cfg.timeout_s)
    async with contextlib.AsyncExitStack() as stack:
        if client is not None:
            await stack.enter_async_context(client)
        streams = await stack.enter_async_context(transport)
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()
        result = await session.call_tool(cfg.tool, cfg.arguments)
    return parse_tool_result(result)


def call_tool(cfg: WatchConfig) -> Any:
    """Default caller: one ``tools/call`` over streamable HTTP with the ``mcp`` SDK Hermes ships."""
    import asyncio

    try:
        import mcp  # noqa: F401
    except ImportError:
        raise WatchError("the 'mcp' package is not importable: run with the Python of the "
                         "Hermes venv (it ships the MCP SDK)") from None

    async def bounded() -> Any:
        return await asyncio.wait_for(_call_async(cfg), timeout=cfg.timeout_s + 5)

    try:
        return asyncio.run(bounded())
    except WatchError:
        raise
    except asyncio.TimeoutError:
        raise WatchError(f"MCP call timed out after {cfg.timeout_s:g}s") from None
    except BaseException as exc:  # noqa: BLE001 - ExceptionGroup from anyio task groups too
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise WatchError(f"MCP call failed: {_describe(exc)}") from None


def _describe(exc: BaseException) -> str:
    inner = getattr(exc, "exceptions", None)
    if inner:
        return "; ".join(_describe(e) for e in inner)
    return f"{type(exc).__name__}: {_short(str(exc), 300)}"


# ── state ────────────────────────────────────────────────────────────────────────────────────

def read_state(path: Path) -> Optional[List[str]]:
    """Seen ids, oldest first; ``None`` when there is no state yet. Raises on a corrupt file:
    silently starting over would swallow whatever arrived in between."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WatchError(f"cannot read state {path}: {exc.strerror or exc}") from None
    try:
        data = json.loads(raw)
        seen = data["seen"]
        if not isinstance(seen, list):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise WatchError(f"state {path} is corrupt; delete it to record a new baseline") from None
    return [str(s) for s in seen]


def merge_seen(seen: List[str], current: List[str], max_seen: int) -> List[str]:
    """Append new ids; past the bound drop the oldest ids the tool no longer returns (a current
    id is never dropped, else it would be reported again on the next run)."""
    known = set(seen)
    merged = list(seen) + [i for i in current if i not in known]
    limit = max(max_seen, len(set(current)))
    excess = len(merged) - limit
    if excess <= 0:
        return merged
    live = set(current)
    out: List[str] = []
    for value in merged:
        if excess > 0 and value not in live:
            excess -= 1
            continue
        out.append(value)
    return out


def write_state(path: Path, seen: List[str], cfg: WatchConfig) -> None:
    payload = {"version": STATE_VERSION, "tool": cfg.tool, "id_field": cfg.id_field,
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "seen": seen}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    except OSError as exc:
        raise WatchError(f"cannot write state {path}: {exc.strerror or exc}") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        if isinstance(exc, OSError):
            raise WatchError(f"cannot write state {path}: {exc.strerror or exc}") from None
        raise


# ── command ──────────────────────────────────────────────────────────────────────────────────

Caller = Callable[[WatchConfig], Any]


def _unique(items: List[dict], id_field: str) -> Iterator[Tuple[str, dict]]:
    seen = set()
    for item in items:
        key = item_id(item, id_field)
        if key not in seen:
            seen.add(key)
            yield key, item


def run(config_path: str, *, dry_run: bool = False, call: Optional[Caller] = None,
        out=None, err=None) -> int:
    """Exit codes: 0 done (silent or report), 1 source/state failure, 2 invalid config."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        err.write(f"fleet-policy watch: invalid config: {exc}\n")
        return 2
    try:
        previous = read_state(cfg.state)
        try:
            payload = (call or call_tool)(cfg)
        except WatchError:
            raise
        except Exception as exc:  # an injected caller's own failure is a source failure too
            raise WatchError(f"MCP call failed: {_describe(exc)}") from None
        items, errors = analyze(payload, cfg.id_field)
        if errors:
            raise WatchError(f"{cfg.tool} reported a failure: " + "; ".join(errors[:3]))
        current = list(_unique(items, cfg.id_field))
        current_ids = [key for key, _ in current]
        known = set(previous or [])
        new = [(key, item) for key, item in current if key not in known]

        lines: List[str] = []
        if dry_run:
            if previous is None:
                lines.append(f"dry-run: {cfg.tool} returned {len(current_ids)} id(s) in "
                             f"'{cfg.id_field}'; no state yet, so a real run would record them as "
                             "the baseline and stay silent. Items found:")
                shown = current
            else:
                lines.append(f"dry-run: {cfg.tool} returned {len(current_ids)} id(s) in "
                             f"'{cfg.id_field}', {len(new)} new (state not written):")
                shown = new
            if not current_ids:
                lines.append(f"warning: no '{cfg.id_field}' found in the response; check "
                             "'id_field' and the tool arguments")
            lines.extend(render_item(item, cfg) for _, item in shown[: cfg.max_new])
            if len(shown) > cfg.max_new:
                lines.append(f"... and {len(shown) - cfg.max_new} more")
            out.write("\n".join(lines) + "\n")
            return 0

        if previous is None:
            write_state(cfg.state, merge_seen([], current_ids, cfg.max_seen), cfg)
            out.write(f"baseline recorded: {len(current_ids)} id(s); nothing is reported on "
                      f"the first run\n{GATE_SLEEP}\n")
            return 0
        if not new:  # state untouched: it already holds every current id
            out.write(GATE_SLEEP + "\n")
            return 0
        write_state(cfg.state, merge_seen(previous, current_ids, cfg.max_seen), cfg)
        lines.append(f"{len(new)} new item(s) from {cfg.tool} (watch '{cfg.name}'):")
        lines.extend(render_item(item, cfg) for _, item in new[: cfg.max_new])
        if len(new) > cfg.max_new:
            lines.append(f"... and {len(new) - cfg.max_new} more (not shown; all marked as seen)")
        out.write("\n".join(lines) + "\n")
        return 0
    except WatchError as exc:
        err.write(f"fleet-policy watch '{cfg.name}': {exc}\n")
        return 1


def script_line(config: "str | Path", *, python: str, plugin_dir: str) -> str:
    """The one line of ``$HERMES_HOME/scripts/watch-<name>.sh`` (cron scripts take no args)."""
    return f"exec {python} {plugin_dir} watch --config {config}"
