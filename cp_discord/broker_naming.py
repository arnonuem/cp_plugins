"""C1e — what a session's thread is CALLED (§3.3).

Four pure functions, split out of :mod:`.broker_threads` because they answer a
question that has nothing to do with Discord: given a working directory and an
optional override, what should this session be called?  The only reason they
lived next to the thread manager is that the manager is their first caller.

The title is read by a human on a phone, and that shapes every rule here:

* ``<directory>/<branch>``, because those are the two things that tell one tab
  from another;
* outside a repository the branch part is simply ABSENT (AC-10) -- not
  ``None``, not ``unknown``, which would be noise in a thread list;
* a collision gets ``#n`` (AC-11), so two tabs on the same branch stay
  distinguishable;
* ``--session-name`` beats all of it (AC-12): the person naming a session
  knows better than a heuristic.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)


def detect_branch(cwd: str) -> Optional[str]:
    """The current git branch, or ``None``.

    Delegates to the core's ``statusline.payload.detect_git_branch`` rather
    than shelling out again: that one already carries a Windows fix (reader
    threads deadlocking a ``capture_output`` call from inside a hook) which
    this module would otherwise have to rediscover.
    """
    from code_puppy.plugins.statusline.payload import detect_git_branch

    return detect_git_branch(cwd)


def session_title(
    cwd: str, *, branch: Optional[str], override: Optional[str] = None
) -> str:
    """``<directory>/<branch>``, or *override* if one was given (§3.3)."""
    if override and override.strip():
        return override.strip()
    directory = os.path.basename(os.path.abspath(cwd)) or cwd
    if branch and branch.strip():
        return f"{directory}/{branch.strip()}"
    return directory


def derive_title(cwd: str, override: Optional[str]) -> str:
    """:func:`session_title` with the branch looked up, failures included.

    A missing, broken or slow git is not a reason to have no title (INV-C1).
    """
    if override and override.strip():
        return override.strip()
    try:
        branch = detect_branch(cwd)
    except Exception:
        logger.debug("cp_discord: branch detection failed", exc_info=True)
        branch = None
    return session_title(cwd, branch=branch)


def disambiguate(title: str, taken: Iterable[str]) -> str:
    """*title*, suffixed with ``#n`` if it is already in use (AC-11)."""
    used = set(taken)
    if title not in used:
        return title
    index = 1
    while f"{title} #{index}" in used:
        index += 1
    return f"{title} #{index}"


__all__: Sequence[str] = (
    "derive_title",
    "detect_branch",
    "disambiguate",
    "session_title",
)
