"""Hook and middleware callbacks: record what is actually sent to the LLM provider.

Registered through the documented plugin surfaces only:

* ``pre_api_request``  — every main-loop provider attempt (main agent, delegated children,
  cron runs). Role from the ``platform`` kwarg.
* ``pre_auxiliary_call`` — every auxiliary attempt (compression, titling, vision, MoA, ...).
  Role ``auxiliary:<aux_task>``.
* ``llm_execution`` middleware (observer-only, optional) — wraps the main-loop provider call,
  calls ``next_call()`` exactly once and returns its result unchanged. It sees the full,
  untruncated request kwargs and the provider response, hence the *served* ``service_tier``
  that no hook payload carries.

Every callback is fail-open: an exception here is swallowed (hooks) or never raised around
``next_call`` (middleware), so observation can never break a request.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from . import extract, store

logger = logging.getLogger(__name__)


def _record(role: str, summary: dict, response_tier: Optional[str]) -> None:
    store.write_entry(role, {
        "model": summary.get("model"),
        "reasoning_effort": summary.get("reasoning_effort"),
        "service_tier": summary.get("service_tier"),
        "response_service_tier": response_tier,
        "ts": store.now_iso(),
    })


def on_pre_api_request(**kwargs: Any) -> None:
    try:
        role = extract.role_for_main_loop(kwargs)
        summary = extract.summarize_request(kwargs.get("request"), kwargs.get("model"))
        _record(role, summary, None)
    except Exception:  # noqa: BLE001 - observer must never fail the request
        logger.debug("fleet-policy: pre_api_request observer failed", exc_info=True)


def on_pre_auxiliary_call(**kwargs: Any) -> None:
    try:
        role = extract.role_for_auxiliary(kwargs)
        summary = extract.summarize_request(kwargs.get("request"), kwargs.get("model"))
        _record(role, summary, None)
    except Exception:  # noqa: BLE001
        logger.debug("fleet-policy: pre_auxiliary_call observer failed", exc_info=True)


def observe_llm_execution(request: Any = None, next_call: Optional[Callable[..., Any]] = None,
                          **kwargs: Any) -> Any:
    """``llm_execution`` middleware: pass-through that records the full request + served tier."""
    if next_call is None:  # not called by Hermes' execution chain: nothing to wrap
        return None
    response = next_call()  # single use, current payload; exceptions propagate untouched
    try:
        role = extract.role_for_main_loop(kwargs)
        body = request if isinstance(request, dict) else {}
        summary = extract.summarize_request({"body": body}, kwargs.get("model"))
        _record(role, summary, extract.response_service_tier(response))
    except Exception:  # noqa: BLE001
        logger.debug("fleet-policy: llm_execution observer failed", exc_info=True)
    return response
