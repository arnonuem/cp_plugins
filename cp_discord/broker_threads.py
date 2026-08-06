"""C1c — one Discord thread per session: naming, adoption, archiving.

Everything that touches Discord objects lives here, and nothing else does.
The server (:mod:`.broker_server`) owns sessions and sockets and calls into
this module; that split is what lets the whole transport be tested without a
bot token.  The bookkeeping that needs no Discord at all -- which sessions
exist, and which are still alive -- sits one file over in
:mod:`.broker_registry` and is re-exported here, so this module stays the
single entry point for the C1 layer.

Three rules shape it:

**Threads are ARCHIVED, never deleted** (INV-C3).  "Stops nagging" means "not
in the active list", not "history gone".  The fakes in the test suite give
their thread a ``delete`` method that fails loudly, because a fake without one
would let this rule pass by absence rather than by behaviour.

**A re-elected broker ADOPTS what it finds** (INV-C14, AC-53).  The registry
survives an election, so the new broker loads it and takes the existing
threads over.  Creating fresh ones instead would make the history fall apart
at every tab switch -- and archiving what looked orphaned would take out live
sessions.

**A new thread pulls its approvers in** (:mod:`.broker_autojoin`).  Discord
puts a thread in the sidebar, and notifies about it, only for members who
have JOINED -- so a thread the bot alone is in reaches nobody's phone, which
is the one thing this bridge exists to do.  Adding happens on CREATION only;
adoption stays silent.

**Discord archives on its own, so activity revives** (§3.3a, AC-56).  The
shortest ``auto_archive_duration`` Discord offers is 60 minutes, which is
exactly the situation this feature is FOR: a session nobody is sitting at.  So
threads are created with the longest duration available, and any post to an
archived thread un-archives it first.

Nothing in here is allowed to raise into a caller (INV-C1).  Discord being
down, rate-limiting or simply gone is a Discord problem; the terminal session
must not learn about it.
"""

from __future__ import annotations

import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
)

#: Re-exported so that ``broker_threads`` stays the single entry point for the
#: C1 layer: :mod:`.broker_server` and the suites reach the registry and the
#: naming rules through this module, and each split is a file boundary, not an
#: API change.
from . import approvals_ui, broker_autojoin
from .broker_gates import GateBoard, PostedGate, ViewFactory
from .broker_naming import (  # noqa: F401
    derive_title,
    detect_branch,
    disambiguate,
    session_title,
)
from .broker_registry import (  # noqa: F401
    HEARTBEAT_GRACE,
    SessionRecord,
    SessionRegistry,
    as_optional_float,
    as_optional_int,
)
from .chunking import chunk_message

if TYPE_CHECKING:  # pragma: no cover - the re-export below is what ships
    from .broker_gateway import DiscordGateway

logger = logging.getLogger(__name__)

#: Discord's longest ``auto_archive_duration``: 7 days, in minutes.
AUTO_ARCHIVE_MAX = 10080

#: Reason recorded in Discord's audit log, so a human reading it later can
#: tell OUR archiving apart from Discord's own.
ARCHIVE_REASON = "cp_discord: session ended"
REVIVE_REASON = "cp_discord: session is active again"


class ThreadManager:
    """Maps sessions to Discord threads.  The only Discord-aware object in C1.

    The channel arrives through a CALLABLE rather than as a value because the
    broker comes up before its Discord connection does: a session may register
    while the client is still logging in, and INV-C1 says that must not fail
    -- it just means there is no thread yet.
    """

    def __init__(self, channel_provider: Callable[[], Any]) -> None:
        self._channel_provider = channel_provider
        self._threads: Dict[str, Any] = {}
        self._titles: Dict[str, str] = {}

    # -- observation ----------------------------------------------------

    def thread_id_for(self, session_id: str) -> Optional[int]:
        thread = self._threads.get(session_id)
        return getattr(thread, "id", None) if thread is not None else None

    def known_sessions(self) -> Set[str]:
        return set(self._threads)

    def taken_titles(self) -> Set[str]:
        return set(self._titles.values())

    # -- adoption (INV-C14) ---------------------------------------------

    def adopt(self, session_id: str, thread: Any) -> None:
        """Take an existing thread over without touching it (AC-53).

        Deliberately does NOT post, rename or re-configure: a re-election has
        to be invisible in the channel.
        """
        self._threads[session_id] = thread
        name = getattr(thread, "name", None)
        if isinstance(name, str) and name:
            self._titles[session_id] = name

    def forget(self, session_id: str) -> None:
        """Drop the mapping without archiving (the session moved on)."""
        self._threads.pop(session_id, None)
        self._titles.pop(session_id, None)

    def has_thread(self, session_id: str) -> bool:
        return session_id in self._threads

    # -- lifecycle ------------------------------------------------------

    async def ensure_thread(self, session_id: str, title: str) -> Optional[int]:
        """The thread for *session_id*, created only if there is none.

        Returns the thread id, or ``None`` when Discord is unavailable -- a
        session with no thread is a degraded session, never a failed one.

        Both ``None`` paths log at WARNING, deliberately: they are the only
        two ways the bridge ends up looking exactly like a working one -- bot
        online, session registered, no thread anywhere -- and at ``debug`` the
        symptom carries no route back to the cause.
        """
        existing = self._threads.get(session_id)
        if existing is not None:
            return getattr(existing, "id", None)

        channel = self._channel()
        if channel is None:
            logger.warning(
                "cp_discord: no Discord channel yet, so session %s has no thread "
                "-- the bot is still logging in, or its token or channel id is "
                "wrong",
                session_id,
            )
            return None

        unique = disambiguate(title, self.taken_titles())
        try:
            thread = await channel.create_thread(
                name=unique, auto_archive_duration=AUTO_ARCHIVE_MAX
            )
        except Exception as error:
            logger.warning(
                "cp_discord: could not create the thread %r for session %s (%s) "
                "-- this session stays invisible in Discord",
                unique,
                session_id,
                error,
                exc_info=True,
            )
            return None

        self._threads[session_id] = thread
        self._titles[session_id] = unique
        # Only on a FRESHLY created thread, never on an adopted one: a
        # re-election has to stay invisible (AC-53), and re-adding everybody
        # at every tab switch would be a notification per election.
        await broker_autojoin.add_approvers(thread, session_id)
        return getattr(thread, "id", None)

    async def archive(self, session_id: str) -> None:
        """Archive the session's thread (INV-C3: archive, never delete).

        The mapping is dropped either way: a thread we could not archive is
        still one we are no longer responsible for, and holding the reference
        would make the next election adopt something stale.
        """
        thread = self._threads.pop(session_id, None)
        self._titles.pop(session_id, None)
        if thread is None:
            return
        try:
            await thread.edit(archived=True, reason=ARCHIVE_REASON)
        except Exception:
            logger.debug("cp_discord: archiving a thread failed", exc_info=True)

    async def archive_all(self, session_ids: Iterable[str]) -> None:
        for session_id in list(session_ids):
            await self.archive(session_id)

    # -- posting --------------------------------------------------------

    async def post(self, session_id: str, body: str) -> None:
        """Post *body* into the session's thread, chunked (AC-81b).

        Reviving first is not politeness: Discord auto-archives after an hour
        of quiet, and posting into an archived thread is exactly what happens
        when a long-idle session finally needs something (AC-56).
        """
        thread = self._threads.get(session_id)
        if thread is None:
            return
        await self._post_to(thread, body)

    async def post_gate(
        self,
        session_id: str,
        gate_id: str,
        body: str,
        view_factory: Optional[ViewFactory],
        board: GateBoard,
    ) -> None:
        """Post a gate into the session's thread, with its buttons (§3.2b).

        The view is built HERE, not by the caller: py-cord's ``View`` takes
        the running loop in its constructor, and the only loop that exists is
        this one.  *view_factory* is ``None`` for a gate the phone cannot
        answer -- then this is an ordinary, button-less post (INV-C23, AC-91).

        Deliberately NOT chunked: the buttons live on ONE message, and a gate
        split across two would put them under a fragment.  The body is capped
        by :func:`.approvals_ui.gate_text` instead, which is where the caller
        can see the limit.
        """
        thread = self._threads.get(session_id)
        if thread is None or board.is_open(session_id, gate_id):
            return
        view = (
            None
            if view_factory is None
            else view_factory(lambda: board.claim(session_id, gate_id))
        )
        try:
            await self._revive(thread)
            message = await thread.send(
                body[:_GATE_BODY_LIMIT],
                view=view,
                allowed_mentions=approvals_ui.allowed_mentions(),
            )
        except Exception:
            logger.debug("cp_discord: posting a gate failed", exc_info=True)
            return
        board.remember(session_id, gate_id, PostedGate(message=message, view=view))

    async def finish_gate(
        self, session_id: str, gate_id: str, outcome: str, board: GateBoard
    ) -> None:
        """Write the outcome onto the gate message and kill its buttons.

        Falls back to a plain post when the gate is not on the board: a
        decision the channel never learns about is how somebody ends up
        tapping at a message that was answered ten minutes ago.
        """
        posted = board.take(session_id, gate_id)
        if posted is None:
            await self.post(session_id, outcome)
            return
        approvals_ui.disable(posted.view)
        try:
            await posted.message.edit(content=outcome, view=posted.view)
        except Exception:
            logger.debug("cp_discord: finalising a gate failed", exc_info=True)

    async def post_channel(self, body: str) -> None:
        """Post *body* into the CHANNEL itself (AC-60b, AC-71b).

        Activation warnings go here and not into a thread, because the failure
        they describe is usually the reason no thread exists.
        """
        channel = self._channel()
        if channel is None:
            return
        await self._post_to(channel, body)

    async def _post_to(self, target: Any, body: str) -> None:
        for chunk in self._chunks(body):
            try:
                await self._revive(target)
                await target.send(chunk)
            except Exception:
                logger.debug("cp_discord: posting to Discord failed", exc_info=True)
                return

    @staticmethod
    def _chunks(body: str) -> List[str]:
        """Split *body* for Discord, never dropping it entirely.

        ``chunk_message`` returns nothing for whitespace-only input; that is
        correct for a report and wrong for a status line somebody is waiting
        on, so a non-empty body always yields at least one chunk.
        """
        if not body:
            return []
        chunks = chunk_message(body)
        return chunks if chunks else [body[:2000]]

    @staticmethod
    async def _revive(target: Any) -> None:
        """Un-archive *target* if Discord archived it behind our back."""
        if not getattr(target, "archived", False):
            return
        try:
            await target.edit(archived=False, reason=REVIVE_REASON)
        except Exception:
            logger.debug("cp_discord: reviving a thread failed", exc_info=True)

    # -- internals ------------------------------------------------------

    def _channel(self) -> Optional[Any]:
        try:
            return self._channel_provider()
        except Exception:
            logger.debug("cp_discord: no Discord channel available", exc_info=True)
            return None


#: A gate lives on ONE message (its buttons cannot be split across two), so
#: the body is capped at Discord's limit rather than chunked.
_GATE_BODY_LIMIT = 2000


def __getattr__(name: str) -> Any:
    """Serve ``DiscordGateway`` from :mod:`.broker_gateway` (PEP 562).

    This module stays the single entry point for the C1 layer, so the gateway
    is still reachable as ``broker_threads.DiscordGateway`` after the split.

    Resolved lazily rather than imported at the top, and that is not style:
    :mod:`.broker_gateway` imports :class:`ThreadManager` from here, so a
    module-level import would be a cycle -- and it would break in exactly one
    direction (importing the gateway FIRST), which is the kind of breakage
    that shows up in production and not in a suite that happens to import the
    other way.  Deferring to first ACCESS means the class always exists by the
    time anyone asks for it.
    """
    if name == "DiscordGateway":
        from .broker_gateway import DiscordGateway

        return DiscordGateway
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__: Sequence[str] = (
    "ARCHIVE_REASON",
    "AUTO_ARCHIVE_MAX",
    "HEARTBEAT_GRACE",
    "REVIVE_REASON",
    "DiscordGateway",
    "GateBoard",
    "SessionRecord",
    "SessionRegistry",
    "ThreadManager",
    "as_optional_float",
    "as_optional_int",
    "derive_title",
    "detect_branch",
    "disambiguate",
    "session_title",
)
