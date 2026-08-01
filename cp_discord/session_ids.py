"""INV-1 session ids: one format, one parser, no second opinion.

``discord:<channel_id>`` is the only session id this plugin owns.  Two halves
of the plugin have to decide whether a given id is one of ours -- L4 to route
an approval gate, L5 to route output -- and for a while they each answered it
their own way: L5 with ``int()``, L4 with ``str.isdigit()``.  They disagreed.
``" discord: +42 "`` was routed by the output router and refused by the
approval bridge, which is exactly the shape of bug where text reaches a channel
whose gates cannot.

So the check lives here once, and both halves import it.

Strictness is a feature, not fastidiousness:

* ``int()`` accepts surrounding whitespace, a leading sign, underscores
  (``4_2``) and non-ASCII digits;
* ``str.isdigit()`` is unicode-aware, so ``discord:٤٢`` passes it (measured).
  Hence the explicit :meth:`str.isascii` -- cheap defence in depth even though
  the core cannot currently produce such an id;
* when L1 has already rolled its patch back, the core calls L4's backend
  positionally with three arguments and the session parameter silently
  receives the TITLE string instead (measured, INV-7 clause 5).
  ``"Shell Command"`` must fail this check -- that is what turns a misbinding
  into a refusal rather than a misrouted gate.
"""

from __future__ import annotations

from typing import Any, Optional

#: The one and only session-id prefix.  Nobody uses the raw channel id.
SESSION_PREFIX = "discord"


def session_id_for(channel_id: int) -> str:
    """The INV-1 session id for *channel_id* (``discord:<channel_id>``)."""
    return f"{SESSION_PREFIX}:{channel_id}"


def channel_id_of(session_id: Any) -> Optional[int]:
    """The channel behind an INV-1 session id, or ``None`` if it is not one.

    ``None`` is the fail-closed answer: the caller must treat it as "not ours"
    and never guess a channel out of it.
    """
    if not isinstance(session_id, str):
        return None
    prefix, separator, raw = session_id.partition(":")
    if separator != ":" or prefix != SESSION_PREFIX:
        return None
    if not raw.isascii() or not raw.isdigit():
        return None
    return int(raw)


def is_session_id(session_id: Any) -> bool:
    """Whether *session_id* is one of ours."""
    return channel_id_of(session_id) is not None
