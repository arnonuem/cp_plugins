"""One-shot warnings for the failures that would otherwise be invisible.

**Measured premise: every ``logger.debug`` in this plugin is discarded.**
code-puppy's core installs no logging configuration at all (no
``basicConfig`` / ``dictConfig`` / ``fileConfig`` anywhere in non-test core),
so ``logging.lastResort`` applies -- and that handler is fixed at
``WARNING``. Verified: ``logger.isEnabledFor(logging.DEBUG)`` is ``False``
and a debug marker produced no output on any stream, while a warning marker
appeared on stderr.

The consequence is the whole reason this module exists: **"it is logged at
debug" is not a mitigation.** Where a failure means the plugin is DEAD or the
pane is now WRONG, the user gets nothing at all -- which is precisely the
zero-symptom class of failure this plugin was written to eliminate.

So those failures warn instead. The cost of a warning is that it is loud, and
this plugin's failures are almost all PERSISTENT (a bad token stays bad; a
raising sweep raises every 60 s; an unreachable pipe stays unreachable).
Warning per occurrence would therefore bury the user's terminal under the
same line forever. :func:`warn_once` keeps the signal and drops the spam:
the FIRST occurrence of each distinct key is warned, every later one is
demoted to debug.

Deduping is **per process, never per report** -- a per-report reset would
reintroduce the spam this exists to prevent.
"""

from __future__ import annotations

import logging
import threading
from typing import Set

logger = logging.getLogger(__name__)

#: Keys already surfaced. Guarded because warnings originate from the worker
#: thread, the loop thread and the human-prompt path alike.
_WARNED: Set[str] = set()
_LOCK = threading.Lock()

#: Longest attacker-influenced fragment allowed into a log line. The
#: actionable part of a server error is the CODE; the free text is
#: decoration, so a generous single line is plenty.
MAX_LOG_DETAIL = 120


def sanitize_for_log(text: str) -> str:
    r"""Make attacker-influenced text safe to put in front of a human.

    **A warning is written to a terminal the developer TRUSTS.** Measured
    (fix round 2, G3): a reply of
    ``{"error":{"message":"\u001b[2J\u001b[HFAKE OUTPUT","code":1}}``
    reached the stderr warning verbatim as
    ``'\x1b[2J\x1b[HFAKE OUTPUT (code 1)'``, and a 5000-char message passed
    through at full length. So a pipe squatter could clear the screen or
    repaint fake output. :func:`warn_once` caps the COUNT at one, not the
    SIZE -- and a terminal escape needs exactly one shot.

    Two rules, each load-bearing:

    * Non-printables are **escaped, never dropped**. Dropping would let
      ``\x1b[2J`` read back as the innocent ``[2J`` and hide that anything
      hostile ever arrived. ``\r`` and ``\n`` are included precisely
      because either one alone lets a squatter forge what looks like a
      separate, trustworthy log line.
    * Truncation is **visible** (``...``), so a reader knows text was cut
      rather than silently reading a half-message as the whole one.
    """
    cleaned = "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in text)
    if len(cleaned) > MAX_LOG_DETAIL:
        return cleaned[:MAX_LOG_DETAIL] + "..."
    return cleaned


def warn_once(key: str, message: str, *args: object) -> bool:
    """Warn the first time ``key`` is seen; log at debug every time after.

    ``key`` is the DEDUPE identity, deliberately separate from ``message``:
    the message usually carries varying detail (an error string, a method
    name) that must not defeat the dedupe.

    Returns whether this call actually warned -- which is what makes
    "exactly one warning" assertable rather than merely hoped for.
    """
    with _LOCK:
        first = key not in _WARNED
        if first:
            _WARNED.add(key)
    if first:
        logger.warning(message, *args)
    else:
        logger.debug(message, *args)
    return first


def reset_warn_once() -> None:
    """Forget every deduped key. A TEST seam, never called in production.

    Production code must not call this: re-arming a warning is exactly the
    per-report reset that would spam the user's terminal.
    """
    with _LOCK:
        _WARNED.clear()


__all__ = ["MAX_LOG_DETAIL", "sanitize_for_log", "warn_once", "reset_warn_once"]
