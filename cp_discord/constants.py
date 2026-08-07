"""The values more than one layer has to agree on.

Two groups, and both earn their own module for the same reason: the
alternative failed in review, a literal repeated per layer.

**AUTHZ_CHANNEL is the ``channel`` half of the ``(channel, external_id)`` pair**
that :func:`bindings.bind`, :func:`bindings.resolve_principal` and
:func:`authz.check_message` are keyed on.  C6 WRITES under it
(``sync_from_config``), C4 and C5 READ under it.  If the writer and a reader
ever disagreed, every lookup would miss, and because the authorization path is
fail-closed the symptom would not be an error -- it would be a bot that
silently refuses everyone, with a database that looks correctly populated.

It is ``"discord"`` and it stays ``"discord"``: it names the PLATFORM an
external id belongs to, not this plugin.  The session-id prefix
(``session_ids.CP_SESSION_PREFIX``) is a different axis and deliberately does
NOT track this value -- see the note there.
"""

from __future__ import annotations

AUTHZ_CHANNEL = "discord"
"""Channel key for every authorization binding (INV-C28)."""


# --------------------------------------------------------------------------- #
# What became of one steered chat message (SPEC §4.3a)
# --------------------------------------------------------------------------- #
#
# THREE layers have to agree on these four words, which is why they are here
# and not in any of them: ``inbound`` produces them out of a ``Delivery``,
# ``client_inbound`` puts them on the wire, and ``broker_steer`` turns them
# back into a reaction.  ``inbound`` sits BELOW the broker (``inbound.py:33``)
# and may not import it, so a broker-side home would have inverted the layers.
#
# They are deliberately COARSE.  Four outcomes and nothing else: no principal,
# no authorization reason, no hint whether the sender is known at all.  The
# broker needs enough to tell a delivery from a refusal and may learn nothing
# more, because it is the broker that reacts and a reaction confirms to its
# reader that the session exists (INV-6).

STEER_DELIVERED = "delivered"
"""Handed to the agent; the answer also carries ``mode`` (``now``/``queue``)."""

STEER_EMPTY = "empty"
"""Nothing but whitespace arrived."""

STEER_UNDELIVERED = "undelivered"
"""Authorized and non-empty, but the steering queue would not take it."""

STEER_REFUSED = "refused"
"""The session did not accept the sender.  The ONE word the broker may not
react to -- see INV-6."""

STEER_DUPLICATE = "duplicate"
"""A repeat of a message already delivered (SPEC §4.4a).

Not a ``Delivery`` outcome: it is decided one layer above C5, by the
deduplication window, and it is acknowledged exactly like a delivery because
its normal cause is a first attempt that LANDED and lost its answer.
"""

__all__ = [
    "AUTHZ_CHANNEL",
    "STEER_DELIVERED",
    "STEER_DUPLICATE",
    "STEER_EMPTY",
    "STEER_REFUSED",
    "STEER_UNDELIVERED",
]
