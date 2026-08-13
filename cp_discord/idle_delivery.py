"""C8 — idle delivery: a queued steer becomes the next turn, with no keystroke.

A Discord message that arrives WHILE the agent works is already handled: C5
puts it into the steer queue and the runtime drains it between turns.  A
message that arrives while the session sits idle at the prompt was the gap --
it waited in the queue until somebody touched the keyboard at the PC, because
nothing on the idle path is watching that queue.

This layer is that watcher, and it lives here rather than in the core on
purpose: the same fix in ``run_ui`` would be overwritten by the next
``uv tool upgrade``.  Everything below therefore runs against PURE upstream
``399effcf`` and must never reach for a symbol the core-side fix introduced.

**The order inside :func:`_deliver` is not a style choice.**  Each step exists
because the obvious alternative was measured and was worse:

* The pop does NOT happen in the listener.  ``_fire_steer_queue_listeners``
  hands every listener the count computed BEFORE the round started, so a
  listener that pops leaves the next one painting ``(1 pending)`` over an
  empty queue -- a ghost number whose appearance depends on which plugin
  registered first.  ``call_soon_threadsafe`` moves the pop behind the round;
  the pop then fires its own, correct round, and that one paints last.
* The check happens BEFORE the pop.  The other way round the text is simply
  gone when no persistent UI exists: ``_push_idle`` discards silently
  (``upstream run_ui.py:420-423``).
* ``run_ui._lock`` is held for the SNAPSHOT ONLY.  It is a plain
  ``threading.Lock`` (``upstream :56``), and ``_push_idle`` (``:417``),
  ``_get_loop`` (``:71``) and ``wait_for_idle_submission`` (``:325``) all take
  it themselves -- calling one of them under the held lock hangs the event
  loop permanently.  Popping under it would additionally run foreign plugin
  code (the listeners, ``pause_controller.py:318``) beneath a core lock.
* ``None`` from the pop is the NORMAL case, not an edge case: our own listener
  runs inside the pop's round and schedules a second ``_deliver`` that finds
  an empty queue.  ``wait_for_idle_submission`` returns its item unchanged
  (``upstream :332``), so a ``None`` pushed through would come back as user
  text and be executed as a turn.

**Why the private core names.**  The public ``is_persistent()`` /
``is_run_active()`` each take the lock themselves, so two calls in a row are
not atomic -- and atomicity is the whole point of the snapshot.  For
``_idle_queue`` and ``_loop`` there is no public accessor at all.  Because
those names are private, :func:`install` verifies every one of them up front
and declines to install if one is missing: an exception here would make
``_install_components`` roll back the WHOLE bridge
(``register_callbacks.py:432-433``), so a mismatched core version would not
cost C8 -- it would cost Discord entirely.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from code_puppy.messaging import run_ui

logger = logging.getLogger(__name__)

#: Everything this module reads out of ``run_ui``.  Checked once at install
#: time; the access sites below are plain attribute reads afterwards.
REQUIRED_CORE_NAMES: Tuple[str, ...] = (
    "_lock",
    "_loop",
    "_get_loop",
    "_persistent",
    "_run_active",
    "_idle_queue",
    "_push_idle",
)

_installed = False


# --------------------------------------------------------------------------- #
# The delivery path
# --------------------------------------------------------------------------- #


def _on_steer_queued(_count: int) -> None:
    """Steer-queue listener.  Does nothing but nudge the event loop.

    A module-level named function, NOT a lambda or a per-install closure: the
    PauseController is a process singleton that dedups and unregisters
    listeners by identity (``pause_controller.py:173``), so a fresh function
    object per start would stack up listeners nobody can remove.

    Runs synchronously in whichever thread queued the steer -- the broker's,
    for a Discord message -- so it stays cheap and never raises: the core
    swallows listener exceptions without a trace (``:195-198``), which is why
    the logging happens here.
    """
    try:
        loop = run_ui._get_loop()
        if loop is None or loop.is_closed():
            # No persistent UI (or it is going away): leave the text in the
            # steer queue.  Someone else's path will still find it there.
            return
        loop.call_soon_threadsafe(_deliver)
    except Exception:
        logger.debug("cp_discord: C8 could not schedule a delivery", exc_info=True)


def _deliver() -> None:
    """Hand one queued steer to the idle prompt.  Runs ON the event loop.

    The five steps are fixed; the module docstring says why.
    """
    # Guarded like every other entry point of this module: between the pop and
    # the push the text belongs to NOBODY -- it has left the steer queue and
    # has not arrived anywhere.  A throw in that gap loses it, which would be a
    # THIRD loss window next to the two R3 documents.  Unlikely (``_push_idle``
    # catches its own RuntimeError, ``put_nowait`` on an unbounded queue does
    # not raise), but this runs via ``call_soon_threadsafe``, so an exception
    # would land in asyncio's handler and never in the plugin's log.
    popped = False
    try:
        with run_ui._lock:
            persistent = run_ui._persistent
            run_active = run_ui._run_active
            idle_queue = run_ui._idle_queue
            loop = run_ui._loop

        if (
            not persistent
            or run_active
            or idle_queue is None
            or loop is None
            or loop.is_closed()
        ):
            return

        text = _pop_next_queued_steer()
        if text is None:
            return

        popped = True
        run_ui._push_idle(text)
    except Exception:
        # Two different failures, and telling them apart matters to whoever
        # reads this: before the pop the text is still safe in the steer
        # queue, after it the text belongs to nobody.  One message for both
        # would send the operator hunting in the wrong window.
        if popped:
            logger.warning(
                "cp_discord: C8 lost a queued message between the steer "
                "queue and the prompt",
                exc_info=True,
            )
        else:
            logger.warning(
                "cp_discord: C8 could not deliver; the message is still in "
                "the steer queue",
                exc_info=True,
            )


def _controller():
    """The process-wide PauseController.

    Imported per call, never at module scope: ``register_callbacks`` imports
    this module during plugin load, and pulling the core's messaging stack in
    that early would invert the load order.  Three call sites shared one copy
    of these two lines before this helper existed.
    """
    from code_puppy.messaging.pause_controller import get_pause_controller

    return get_pause_controller()


def _pop_next_queued_steer() -> Optional[str]:
    """Pop the oldest ``queue``-mode steer.  Outside the core lock, always."""
    return _controller().pop_next_steer_queued()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def is_installed() -> bool:
    return _installed


def reset_state() -> None:
    """Forget whether we are installed.  Used by :func:`uninstall` and tests."""
    global _installed

    _installed = False


def _missing_core_names() -> Tuple[str, ...]:
    return tuple(name for name in REQUIRED_CORE_NAMES if not hasattr(run_ui, name))


def install(config: Any = None) -> None:
    """Bring C8 up.  Returns at once (INV-C1), and never raises.

    ``config`` is accepted and ignored: C8 has no configuration key of its own
    (delivery IS the point of the chat path), but ``_install_components``
    passes the bridge config to every layer.
    """
    global _installed

    if _installed:
        uninstall()

    missing = _missing_core_names()
    if missing:
        logger.warning(
            "cp_discord: C8 idle delivery stays off, this Code Puppy's "
            "messaging.run_ui is missing: %s",
            ", ".join(missing),
        )
        return

    # Guarded like uninstall(): a throw here does NOT just cost C8.
    # ``_install_components`` answers an exception with
    # ``_uninstall_components()`` + ``return False``
    # (``register_callbacks.py:432-433``), so the WHOLE bridge is rolled back --
    # Discord goes dark because the idle delivery could not register.  The
    # ``run_ui`` names are checked above; this covers the OTHER core surface,
    # ``pause_controller``, which no name check reaches.
    try:
        _controller().add_steer_queue_listener(_on_steer_queued)
    except Exception:
        logger.warning(
            "cp_discord: C8 idle delivery stays off, the steer queue would "
            "not take a listener",
            exc_info=True,
        )
        return

    _installed = True
    logger.debug("cp_discord: C8 idle delivery installed")


def uninstall() -> None:
    """Take C8 down.  Idempotent, and never raises: teardown must reach every
    layer, and C8 is the first one it walks through."""
    try:
        _controller().remove_steer_queue_listener(_on_steer_queued)
    except Exception:
        logger.debug("cp_discord: C8 listener removal failed", exc_info=True)
    reset_state()
