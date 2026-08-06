"""C1c — pulling the approvers into a freshly created thread.

Discord does not put a thread in anybody's sidebar and does not notify anybody
about it until they have JOINED it.  The bot joins by creating and posting;
the human does not.  So a thread the bridge opens is, for the person it was
opened for, invisible: it sits in the channel's thread overview and never says
a word.

That is not a cosmetic gap, it is the whole feature.  "Start at the PC, leave
the house, carry on from the phone" only works if the phone rings when the
agent hits an approval gate.  Without a join it does not ring, and the user
would have to go looking on their own -- at which point the bridge is built
but useless.

**Whom to add is already known.**  The APPROVER principals sit in the
authorization database, where C6 loaded ``DISCORD_APPROVERS`` at startup
(``register_callbacks.sync_identities``).  Reading the configuration a second
time here would make two readers of one fact, and two readers drift.

Everything in here is subordinate to INV-C1: **a failed join is a blemish,
never a defect.**  The thread stands, the bridge runs, the session is never
told.  And the catching is per USER, not per call -- one ex-member must not
be able to cost every other approver their notification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Sequence

from . import bindings, constants

logger = logging.getLogger(__name__)

#: Set once a join has failed, so the reason is stated ONCE per session.  A
#: missing permission is missing for every thread alike, and a bridge that
#: repeats itself per thread is a bridge whose log nobody reads.
_WARNED = False


def reset_state() -> None:
    """Forget that we already warned.  Used by teardown and by tests."""
    global _WARNED
    _WARNED = False


@dataclass(frozen=True, slots=True)
class _Snowflake:
    """The only thing ``Thread.add_user`` reads off its argument: an ``id``.

    py-cord's ``add_user`` calls ``http.add_user_to_thread(self.id, user.id)``
    (``discord/threads.py``, measured against py-cord 2.8.1) and its
    ``Snowflake`` is a structural protocol, so this satisfies it.  Constructed
    here rather than importing ``discord.Object`` to keep this module free of
    py-cord entirely -- the bookkeeping it does needs no bot.
    """

    id: int


def approver_ids() -> List[str]:
    """Every Discord user id holding APPROVER, sorted for a stable order.

    Sorted because :func:`bindings.principals_with_role` returns a SET, whose
    iteration order varies per process -- and an order that changes between
    runs is an order no test can pin down.

    Never raises: whom to notify is a lookup, and a lookup that fails must
    cost the notification, not the thread (INV-C1).
    """
    try:
        principals = sorted(bindings.principals_with_role(bindings.Role.APPROVER))
    except Exception:
        logger.debug("cp_discord: could not read the approvers", exc_info=True)
        return []

    external_ids: List[str] = []
    for principal in principals:
        try:
            identities = bindings.identities_of(principal)
        except Exception:
            logger.debug(
                "cp_discord: could not read the identities of %s",
                principal,
                exc_info=True,
            )
            continue
        external_ids.extend(
            external_id
            for channel, external_id in identities
            if channel == constants.AUTHZ_CHANNEL
        )
    return external_ids


def _c6() -> Any:
    """C6, imported lazily.

    As everywhere else that reaches from a layer back into C6
    (``broker_activation.py:307``): C6 imports the layers, so a top-level
    import here would close the circle.
    """
    from . import register_callbacks

    return register_callbacks


def _enabled() -> bool:
    """Whether the operator left the auto-join on (default: on)."""
    try:
        return _c6().autojoin_enabled()
    except Exception:
        logger.debug("cp_discord: could not read the autojoin switch", exc_info=True)
        return True


async def add_approvers(thread: Any, session_id: str) -> None:
    """Add every approver to *thread*, one at a time.  Never raises.

    Per-user isolation is the point of the loop: somebody who left the guild,
    or whom we lack the permission to add, is the ordinary case, and it must
    not decide whether the OTHERS get their notification.
    """
    if not _enabled():
        return

    add_user = getattr(thread, "add_user", None)
    if add_user is None:
        # A forum or news thread object need not carry it.  Nothing to do, and
        # nothing worth saying: no permission is missing.
        return

    for external_id in approver_ids():
        try:
            await add_user(_Snowflake(int(external_id)))
        except Exception as error:
            _warn_once(external_id, session_id, error)


def _warn_once(external_id: str, session_id: str, error: Exception) -> None:
    """Say why the thread will stay silent -- once per session.

    At WARNING, not debug: the symptom is a thread that exists, looks healthy
    and never reaches the phone, and at debug level that symptom carries no
    route back to its cause.
    """
    global _WARNED
    if _WARNED:
        logger.debug(
            "cp_discord: could not add %s to the thread for session %s",
            external_id,
            session_id,
            exc_info=True,
        )
        return
    _WARNED = True
    logger.warning(
        "cp_discord: could not add the approver %s to the thread for session %s "
        "(%s) -- that thread will not appear in their sidebar and will not "
        "notify them. Check that they are on the server and that the bot may "
        "add members to threads. Set '%s = 0' to switch this off.",
        external_id,
        session_id,
        error,
        _switch_hint(),
        exc_info=True,
    )


def _switch_hint() -> str:
    """How to turn this off, named by C6 rather than repeated here.

    A second copy of the key would be a second thing to rename, and the copy
    that goes stale is the one printed in a message nobody re-reads.
    """
    try:
        return _c6().AUTOJOIN_CONFIG_KEY
    except Exception:  # pragma: no cover - C6 is always importable in practice
        return "the autojoin switch"


__all__: Sequence[str] = ("add_approvers", "approver_ids", "reset_state")
