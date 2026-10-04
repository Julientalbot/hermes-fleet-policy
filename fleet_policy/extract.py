"""Pure helpers: derive (role, model, effort, tier) from documented hook / middleware kwargs.

Nothing here imports Hermes. Every function is total: unknown shapes yield ``None`` (or the
``"unknown"`` role), never an exception. No message content is ever returned.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional

UNKNOWN_ROLE = "unknown"

# Agent platforms Hermes stamps on its own non-interactive callers (verified in the code:
# cron/scheduler.py -> platform="cron", tools/delegate_tool.py -> platform="subagent").
_PLATFORM_ROLES = {"cron": "cron", "subagent": "delegation"}

_AUX_TASK_SAFE = re.compile(r"[^A-Za-z0-9_.:-]+")


def role_for_main_loop(kwargs: Mapping[str, Any]) -> str:
    """Role for a main-loop call (``pre_api_request`` / ``llm_execution`` middleware).

    ``platform`` is always passed by Hermes ("" outside a gateway/cron/subagent); a payload
    without the key at all means a caller we do not know, hence ``"unknown"``.
    """
    if "platform" not in kwargs:
        return UNKNOWN_ROLE
    platform = kwargs.get("platform")
    if not isinstance(platform, str):
        return UNKNOWN_ROLE
    return _PLATFORM_ROLES.get(platform.strip().lower(), "main")


def role_for_auxiliary(kwargs: Mapping[str, Any]) -> str:
    """``auxiliary:<aux_task>`` for ``pre_auxiliary_call``; ``"unknown"`` without a task name."""
    task = kwargs.get("aux_task")
    if not isinstance(task, str) or not task.strip():
        return UNKNOWN_ROLE
    clean = _AUX_TASK_SAFE.sub("_", task.strip())[:64]
    return f"auxiliary:{clean}"


def _str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def effort_from_body(body: Mapping[str, Any]) -> Optional[str]:
    """Reasoning effort as it appears on the wire, across the transports Hermes ships.

    * Responses API (OpenAI, xAI, Codex):       ``reasoning.effort``
    * Chat Completions:                          ``reasoning_effort`` or ``extra_body.reasoning.effort``
    * Anthropic adaptive thinking:               ``output_config.effort``
    * Anthropic manual thinking:                 ``thinking.budget_tokens`` -> ``"budget:<n>"``
    * explicit off switches:                     ``"none"``
    """
    reasoning = _dict(body.get("reasoning"))
    if _str(reasoning.get("effort")):
        return _str(reasoning.get("effort"))
    if _str(body.get("reasoning_effort")):
        return _str(body.get("reasoning_effort"))
    output_config = _dict(body.get("output_config"))
    if _str(output_config.get("effort")):
        return _str(output_config.get("effort"))
    extra = _dict(body.get("extra_body"))
    extra_reasoning = _dict(extra.get("reasoning"))
    if _str(extra_reasoning.get("effort")):
        return _str(extra_reasoning.get("effort"))
    if _str(extra.get("reasoning_effort")):
        return _str(extra.get("reasoning_effort"))
    for thinking in (_dict(body.get("thinking")), _dict(extra.get("thinking"))):
        kind = _str(thinking.get("type"))
        if kind == "disabled":
            return "none"
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0:
            return f"budget:{budget}"
        if kind == "adaptive":
            return "adaptive"
    if reasoning.get("enabled") is False or extra_reasoning.get("enabled") is False:
        return "none"
    return None


def tier_from_body(body: Mapping[str, Any]) -> Optional[str]:
    """``service_tier`` actually present in the request (top level or ``extra_body``)."""
    return _str(body.get("service_tier")) or _str(_dict(body.get("extra_body")).get("service_tier"))


# Fallback for the size-capped hook payload ({"_truncated": True, "preview": "<json prefix>"}).
# Keys are matched with unescaped quotes, so text *inside* a message (where quotes are
# JSON-escaped) cannot produce a false positive.
_PREVIEW_PATTERNS = {
    "model": re.compile(r'"model":\s*"([^"\\]{1,200})"'),
    "service_tier": re.compile(r'"service_tier":\s*"([^"\\]{1,64})"'),
    "reasoning_effort": re.compile(r'"reasoning_effort":\s*"([^"\\]{1,32})"'),
    "reasoning": re.compile(r'"(?:reasoning|output_config)":\s*\{[^{}]*?"effort":\s*"([^"\\]{1,32})"'),
}


def _from_preview(preview: str) -> Dict[str, Optional[str]]:
    found: Dict[str, Optional[str]] = {}
    for key, pattern in _PREVIEW_PATTERNS.items():
        match = pattern.search(preview)
        found[key] = match.group(1) if match else None
    return {
        "model": found["model"],
        "reasoning_effort": found["reasoning"] or found["reasoning_effort"],
        "service_tier": found["service_tier"],
    }


def summarize_request(request: Any, fallback_model: Any = None) -> Dict[str, Any]:
    """``{"model", "reasoning_effort", "service_tier", "request_truncated"}`` from the hook's
    ``request`` kwarg (``{"method": "POST", "body": {...}}``, possibly size-capped)."""
    req = _dict(request)
    if req.get("_truncated"):
        preview = req.get("preview") if isinstance(req.get("preview"), str) else ""
        found = _from_preview(preview)
        return {
            "model": found["model"] or _str(fallback_model),
            "reasoning_effort": found["reasoning_effort"],
            "service_tier": found["service_tier"],
            "request_truncated": True,
        }
    body = _dict(req.get("body")) if "body" in req else req
    return {
        "model": _str(body.get("model")) or _str(fallback_model),
        "reasoning_effort": effort_from_body(body),
        "service_tier": tier_from_body(body),
        "request_truncated": False,
    }


def response_service_tier(response: Any) -> Optional[str]:
    """``service_tier`` the provider says it SERVED (OpenAI/xAI put it on the response object)."""
    if response is None:
        return None
    value = response.get("service_tier") if isinstance(response, dict) else getattr(response, "service_tier", None)
    return _str(value)
