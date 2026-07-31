"""wmux integration -- report code-puppy's state to the wmux pane it runs in. Inside a wmux pane, code-puppy declares its own state authoritatively -- working / blocked (with a reason) / idle -- so the wmux sidebar can answer "which of my ten sessions needs me?" without guessing from the screen. Outside a wmux pane the plugin is completely inert: no thread, no pipe handle, not a single callback registered. ......... ACTIVATION is automatic and needs no configuration: wmux injects WMUX=1 and WMUX_SURFACE_ID into every shell it spawns, and both must be present. WMUX_PIPE defaults to \\\\.\\pipe\\wmux when unset. The auth token comes from WMUX_PIPE_TOKEN, else %APPDATA%\\wmux[-<WMUX_INSTANCE>]\\pipe-token; with neither, the plugin still activates but logs one warning, because unauthenticated reports are rejected in a way that looks exactly like success. ......... WHAT YOU SEE: the pane turns amber while a run is in flight, violet "Needs you" with the reason (e.g. "permission: run_shell_command") whenever code-puppy asks you something, and back to idle when control is yours. Model, token count and context percentage ride along as pane metadata after each interactive turn. On exit the pane is released, so a dead session shows "unknown" instead of a ghost "working". ......... TWO HONEST LIMITS: a /fork started at an idle prompt leaves the pane on idle while the fork works (sub-agent runs do not fire the run-start hook), and with a Discord or ACP approval backend installed the human is asked out-of-band, so the pane stays working. Both are core-level and unfixable from a plugin. Full detail: README.md next to this file.

The paragraph above is one long line on purpose: the /plugins TUI shows only
the FIRST paragraph and collapses newlines into spaces, so a blank line
truncates everything after it (``plugin_list/plugin_meta.py:38-41``).

Wiring -- twelve callbacks, registered ONLY behind the activation guard:

===========================  =================================================
``startup``                  one unconditional claim report (crash recovery)
``agent_run_start``          add ``group_id`` to the live-run set
``agent_run_end``            remove ``group_id`` if present (idempotent)
``agent_run_cancel``         remove ``group_id`` if present (idempotent)
``interactive_turn_end``     metadata refresh (never clears live runs)
``interactive_turn_cancel``  recompute only
``awaiting_user_input``      ``awaitingHuman`` + inferred reason
``pre_tool_call``            in-flight tool key + activity
``post_tool_call``           remove one key + activity done
``user_prompt_submit``       durable session reference, on change
``session_end`` /
``shutdown``                 ``pane.release_agent``, bounded and idempotent
===========================  =================================================

Handlers are plain sync functions that swallow every argument: the dispatcher
passes hook args positionally and runs sync callbacks from both async and
worker-thread contexts, so ``*args`` is the robust choice. A callback that
returned a coroutine would be dropped with a warning, never awaited
(``callbacks.py:275-285``).

``pre_tool_call`` and ``post_tool_call`` MUST return ``None``: a dict with
``blocked`` aborts the real tool call (``pydantic_patches.py:373-393``).

Nothing is patched at module scope, and no source is resolved at import
time -- ``get_current_session_name`` MINTS a session name on first call.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from code_puppy.callbacks import register_callback

from wmux.client import WmuxClient
from wmux.reporter import WmuxReporter

logger = logging.getLogger(__name__)

_client = WmuxClient()
_reporter = WmuxReporter(_client)
# The client owns the tick, the reporter owns the decision. Wiring them is
# this module's job -- the client must not know what a run is.
_client.set_idle_hook(_reporter.sweep_once)


def _arg(args: tuple, index: int) -> Any:
    return args[index] if len(args) > index else None


def _identity(args: tuple, kwargs: dict, key: str, position: int) -> Optional[str]:
    """Extract the run identity, which each hook places differently.

    ``agent_run_start`` / ``agent_run_end`` pass it THIRD-positionally as
    ``session_id`` (``callbacks.py:939-945``, ``:998-1007``);
    ``agent_run_cancel`` passes it FIRST as ``group_id``
    (``callbacks.py:1260``). Both carry the same uuid4 -- an opaque value we
    remember and never parse.

    The position is passed in rather than guessed: ``agent_run_end``'s first
    argument is the AGENT NAME, so a positional fallback chain would happily
    treat it as a run id.
    """
    value = kwargs.get(key, _arg(args, position))
    return str(value) if isinstance(value, str) and value else None


def _on_startup(*_args, **_kw) -> None:
    _reporter.on_startup()


def _on_user_prompt(*_args, **_kw) -> None:
    _reporter.on_user_prompt()


def _on_run_start(*args, **kwargs) -> None:
    _reporter.on_run_start(_identity(args, kwargs, "session_id", 2))


def _on_run_end(*args, **kwargs) -> None:
    _reporter.on_run_terminal(_identity(args, kwargs, "session_id", 2))


def _on_run_cancel(*args, **kwargs) -> None:
    # Identical handling to run_end -- remove-if-present. A cancelled run
    # fires BOTH for the same id, so whichever lands second is a no-op.
    _reporter.on_run_terminal(_identity(args, kwargs, "group_id", 0))


def _on_turn_end(*_args, **_kw) -> None:
    _reporter.on_turn_end()


def _on_turn_cancel(*_args, **_kw) -> None:
    _reporter.on_turn_cancel()


def _on_tool_start(*args, **kwargs):
    # (tool_name, tool_args, context=None). Observer only -> return None so
    # the tool is never blocked or transformed.
    tool_name = _arg(args, 0) if args else kwargs.get("tool_name")
    _reporter.on_tool_start(str(tool_name) if tool_name is not None else "")
    return None


def _on_tool_complete(*args, **kwargs):
    # (tool_name, tool_args, result, duration_ms, context=None). Observer only.
    tool_name = _arg(args, 0) if args else kwargs.get("tool_name")
    _reporter.on_tool_complete(str(tool_name) if tool_name is not None else "")
    return None


def _on_awaiting_user_input(*args, **_kw) -> None:
    _reporter.on_awaiting_user_input(bool(_arg(args, 0)))


def _on_shutdown(*_args, **_kw) -> None:
    _reporter.on_shutdown()


_HOOKS = (
    ("startup", _on_startup),
    ("user_prompt_submit", _on_user_prompt),
    ("agent_run_start", _on_run_start),
    ("agent_run_end", _on_run_end),
    ("agent_run_cancel", _on_run_cancel),
    ("interactive_turn_end", _on_turn_end),
    ("interactive_turn_cancel", _on_turn_cancel),
    ("pre_tool_call", _on_tool_start),
    ("post_tool_call", _on_tool_complete),
    ("awaiting_user_input", _on_awaiting_user_input),
    ("session_end", _on_shutdown),
    ("shutdown", _on_shutdown),
)

if _reporter.active:
    for _phase, _handler in _HOOKS:
        register_callback(_phase, _handler)
    logger.debug("wmux plugin active for surface %s", _client._surface_id)
else:
    logger.debug("wmux plugin inactive (not running inside a wmux pane)")


__all__ = ["WmuxClient", "WmuxReporter"]
