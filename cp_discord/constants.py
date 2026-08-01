"""The values more than one layer has to agree on.

Right now that is exactly one value, and it earns its own module because the
alternative failed in review: a literal repeated per layer.

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

__all__ = ["AUTHZ_CHANNEL"]
