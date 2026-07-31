"""Fail-soft adapters from code-puppy internals to wmux report payloads.

This is the single seam between the plugin and the rest of code-puppy.
Every function catches ordinary runtime failures, logs at debug level and
returns a safe fallback -- reporting state must never be able to disturb the
agent. Reporter and client code depend only on the return shapes here, and
the tests mock this module.

Imports are deliberately function-local. Two of these sources have side
effects at call time (``get_current_agent`` can trigger ``load_agent``;
``get_current_session_name`` MINTS a name on first call), and plugins are
imported partway through ``cli_runner``'s own import -- so nothing may be
resolved at module scope.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from wmux.diagnostics import warn_once

logger = logging.getLogger(__name__)


def _compact(n: int) -> str:
    """Render a token count compactly: 999 -> ``999``; 48200 -> ``48k``.

    Floor division, decimal k (1000, not 1024).
    """
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n // 1000}k"
    return f"{n // 1_000_000}M"


def current_model() -> Optional[str]:
    """Return the current model name, or ``None`` when unresolvable."""
    try:
        from code_puppy.agents.agent_manager import get_current_agent

        agent = get_current_agent()
        if agent is None:
            return None
        name = agent.get_model_name()
        return str(name) if name else None
    except Exception:
        # The FIRST failure warns, every later one is demoted to debug
        # (`warn_once`). These handlers logged at debug ONLY -- and measured,
        # every debug log in this plugin is discarded (see diagnostics.py),
        # so a renamed core accessor disabled the source FOREVER with zero
        # symptom. Deduped rather than warned per call because this runs on
        # every turn and the failure is persistent.
        warn_once(
            "source-model",
            "wmux: could not resolve the current model; the pane will show "
            "no model name (the code-puppy API this reads may have changed)",
        )
        logger.debug("wmux: could not resolve current model", exc_info=True)
        return None


def current_metadata() -> Optional[Dict[str, Any]]:
    """Return ``{model?, tokens, context_pct}``, or ``None`` when unavailable.

    ``None`` means "send nothing" so the pane keeps its last good values
    (which the metadata TTL eventually clears). ``model`` is omitted -- not
    nulled -- when unknown.
    """
    try:
        from code_puppy.token_usage import get_current_usage

        usage = get_current_usage()
        if usage is None:
            return None
        payload: Dict[str, Any] = {
            "tokens": f"{_compact(usage.total_tokens)}/{_compact(usage.capacity)}",
            "context_pct": int(round(usage.percent)),
        }
        model = current_model()
        if model:
            payload["model"] = model
        return payload
    except Exception:
        warn_once(
            "source-metadata",
            "wmux: pane metadata is unavailable; token/context numbers will "
            "stop updating (the code-puppy API this reads may have changed)",
        )
        logger.debug("wmux: metadata unavailable", exc_info=True)
        return None


def current_session_id() -> Optional[str]:
    """Return the durable session name, or ``None`` on any failure.

    Called only from the ``user_prompt_submit`` hook: this mints a session
    name on first call, so it must never run at import time.
    """
    try:
        from code_puppy.config import get_current_session_name

        name = get_current_session_name()
        return str(name) if name else None
    except Exception:
        warn_once(
            "source-session",
            "wmux: the session id is unavailable, so the pane cannot link "
            "back to this session (the code-puppy API this reads may have "
            "changed)",
        )
        logger.debug("wmux: session id unavailable", exc_info=True)
        return None


__all__ = ["current_metadata", "current_model", "current_session_id"]
