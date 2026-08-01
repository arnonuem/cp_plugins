"""Session ids: two formats, one parser, no second opinion.

A session id answers "is this one of ours, and which session is it?".  Two
halves of the plugin ask it, and for a while they each answered it their own
way: one with ``int()``, the other with ``str.isdigit()``.  They disagreed.
``" discord: +42 "`` was routed by one half and refused by the other, which is
exactly the shape of bug where text reaches a channel whose gates cannot.

So the check lives here once, and every half imports it.

There are TWO forms, and they are deliberately kept apart:

``discord:<channel_id>`` (legacy)
    A Discord channel drove the run, so the id carries the channel.  Parsed by
    :func:`channel_id_of`.

``cp_discord:<nonce>`` (terminal bridge, INV-C9)
    A terminal session announced itself; there is no channel to encode, so the
    CLIENT mints an opaque nonce.  Parsed by :func:`nonce_of`.

**Why not one prefix for both** -- this is the trap in this rename: a nonce is
not a digit string.  Sharing ``discord:`` would send every new id through the
legacy digits check below, which would answer ``None`` (the fail-closed
answer), and every approval would be refused.  Two prefixes that differ before
the colon keep the forms from bleeding into each other.

Strictness is a feature, not fastidiousness:

* ``int()`` accepts surrounding whitespace, a leading sign, underscores
  (``4_2``) and non-ASCII digits;
* ``str.isdigit()`` is unicode-aware, so ``discord:٤٢`` passes it (measured).
  Hence the explicit :meth:`str.isascii` -- cheap defence in depth even though
  the core cannot currently produce such an id;
* a caller that passes positionally can hand the session parameter a TITLE
  string instead (measured).  ``"Shell Command"`` must fail this check -- that
  is what turns a misbinding into a refusal rather than a misrouted gate.
"""

from __future__ import annotations

import secrets
from typing import Any, Optional

#: Legacy prefix: ``discord:<channel_id>``, minted by the channel-driven path.
SESSION_PREFIX = "discord"

#: Terminal-bridge prefix: ``cp_discord:<nonce>`` (INV-C9).  Distinct from
#: :data:`SESSION_PREFIX` on purpose -- see the module docstring.
CP_SESSION_PREFIX = "cp_discord"

#: Bytes of entropy per minted nonce (16 bytes = 32 hex characters).  The id is
#: never guessed, only compared: this is collision resistance, not secrecy.
_NONCE_BYTES = 16


def session_id_for(channel_id: int) -> str:
    """The legacy session id for *channel_id* (``discord:<channel_id>``)."""
    return f"{SESSION_PREFIX}:{channel_id}"


def new_session_id() -> str:
    """Mint a fresh terminal-bridge session id (``cp_discord:<nonce>``).

    Minted by the CLIENT, never by the broker: a session that finds no broker
    must still have an id (INV-C1, INV-C9).
    """
    return f"{CP_SESSION_PREFIX}:{secrets.token_hex(_NONCE_BYTES)}"


def _payload_of(session_id: Any, prefix: str) -> Optional[str]:
    """The part after ``<prefix>:``, or ``None`` if *session_id* is not one.

    ``None`` is the fail-closed answer: the caller must treat it as "not ours"
    and never guess anything out of it.
    """
    if not isinstance(session_id, str):
        return None
    found, separator, payload = session_id.partition(":")
    if separator != ":" or found != prefix or not payload:
        return None
    return payload


def channel_id_of(session_id: Any) -> Optional[int]:
    """The channel behind a legacy ``discord:<channel_id>`` id, else ``None``."""
    raw = _payload_of(session_id, SESSION_PREFIX)
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdigit():
        return None
    return int(raw)


def nonce_of(session_id: Any) -> Optional[str]:
    """The nonce behind a ``cp_discord:<nonce>`` id, else ``None``.

    Exactly as strict as :func:`channel_id_of`, for the same reason: an id we
    half-understand is worse than one we reject.  A minted nonce is hex, hence
    ASCII and alphanumeric -- anything else did not come from us.
    """
    raw = _payload_of(session_id, CP_SESSION_PREFIX)
    if raw is None:
        return None
    if not raw.isascii() or not raw.isalnum():
        return None
    return raw


def is_session_id(session_id: Any) -> bool:
    """Whether *session_id* is one of ours, in EITHER form."""
    return channel_id_of(session_id) is not None or nonce_of(session_id) is not None
