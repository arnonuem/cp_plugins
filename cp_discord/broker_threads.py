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
import os
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

#: Re-exported so that ``broker_threads`` stays the single entry point for the
#: C1 layer: :mod:`.broker_server` and the suites reach the registry through
#: this module, and the split is a file boundary, not an API change.
from .broker_registry import (  # noqa: F401
    HEARTBEAT_GRACE,
    SessionRecord,
    SessionRegistry,
    as_optional_float,
    as_optional_int,
)
from .chunking import chunk_message

logger = logging.getLogger(__name__)

#: Discord's longest ``auto_archive_duration``: 7 days, in minutes.
AUTO_ARCHIVE_MAX = 10080

#: Reason recorded in Discord's audit log, so a human reading it later can
#: tell OUR archiving apart from Discord's own.
ARCHIVE_REASON = "cp_discord: session ended"
REVIVE_REASON = "cp_discord: session is active again"


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
    """``<directory>/<branch>``, or *override* if one was given (§3.3).

    The branch part is simply absent outside a repository (AC-10) -- not
    ``None``, not ``unknown``: a title is read by a human on a phone.
    """
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

    # -- lifecycle ------------------------------------------------------

    async def ensure_thread(self, session_id: str, title: str) -> Optional[int]:
        """The thread for *session_id*, created only if there is none.

        Returns the thread id, or ``None`` when Discord is unavailable -- a
        session with no thread is a degraded session, never a failed one.
        """
        existing = self._threads.get(session_id)
        if existing is not None:
            return getattr(existing, "id", None)

        channel = self._channel()
        if channel is None:
            return None

        unique = disambiguate(title, self.taken_titles())
        try:
            thread = await channel.create_thread(
                name=unique, auto_archive_duration=AUTO_ARCHIVE_MAX
            )
        except Exception:
            logger.debug("cp_discord: creating a thread failed", exc_info=True)
            return None

        self._threads[session_id] = thread
        self._titles[session_id] = unique
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


class DiscordGateway:
    """The broker's Discord side: schedule work, never wait for it.

    Two worlds meet here.  py-cord runs an asyncio event loop; the broker runs
    a plain TCP thread.  Every method below is SYNCHRONOUS and returns as soon
    as the work is queued, because a broker handler that awaited Discord would
    hand Discord's latency -- and its outages -- to the session that is merely
    reporting its state (INV-C1, INV-C4).

    Order is preserved: a single serialising task drains the queue, so the
    report still reaches the thread before the state edge that announces it
    (SS8b).  Scheduling with ``run_coroutine_threadsafe`` per call would not
    guarantee that.
    """

    def __init__(self) -> None:
        self._manager = ThreadManager(self._channel)
        self._channel_object: Optional[Any] = None
        self._loop: Optional[Any] = None
        self._own_loop_thread: Optional[threading.Thread] = None
        self._queue: "Optional[Any]" = None
        self._pump: Optional[Any] = None
        self._resolve_thread: Optional[Callable[[int], Any]] = None
        self._pending: List[Any] = []
        self._lock = threading.Lock()

    # -- wiring ---------------------------------------------------------

    def set_channel(self, channel: Any) -> None:
        """The bot has logged in and resolved the configured channel."""
        self._channel_object = channel

    def set_thread_resolver(self, resolver: Callable[[int], Any]) -> None:
        """How to turn a recorded ``thread_id`` back into a Discord thread."""
        self._resolve_thread = resolver

    def attach_loop(self, loop: Any) -> None:
        """Use *loop* (the bot's own) for all Discord work."""
        self._start_pump(loop)

    def start_loop(self) -> None:
        """Run a private event loop on a daemon thread.

        Used when the broker has no bot of its own to borrow a loop from --
        and by the tests, which is why it is a real, supported mode rather
        than a fixture: the code path has to be one that ships.
        """
        import asyncio

        ready = threading.Event()
        loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        self._own_loop_thread = threading.Thread(
            target=run, name="cp_discord-gateway", daemon=True
        )
        self._own_loop_thread.start()
        ready.wait(5)
        self._start_pump(loop)

    def _start_pump(self, loop: Any) -> None:
        import asyncio

        self._loop = loop
        self._queue = asyncio.Queue()
        self._pump = asyncio.run_coroutine_threadsafe(self._drain(), loop)
        with self._lock:
            pending, self._pending = self._pending, []
        for job in pending:
            self._schedule(job)

    # -- the queue ------------------------------------------------------

    def _schedule(self, job: Callable[[], Any]) -> None:
        """Queue *job*.  Never raises, never waits (INV-C1)."""
        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            # No loop yet: the bot is still logging in.  Holding the work is
            # better than dropping it, but bounded -- an unbounded backlog
            # from a bot that never arrives is a leak.
            with self._lock:
                self._pending.append(job)
                del self._pending[:-_MAX_PENDING_JOBS]
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, job)
        except RuntimeError:
            logger.debug("cp_discord: the gateway loop is gone", exc_info=True)

    async def _drain(self) -> None:
        queue = self._queue
        assert queue is not None
        while True:
            job = await queue.get()
            if job is _STOP:
                return
            try:
                await job()
            except Exception:
                logger.debug("cp_discord: a Discord job failed", exc_info=True)
            finally:
                queue.task_done()

    def wait_idle(self, timeout: float = 5.0) -> None:
        """Block until the queue is empty.  For tests and for shutdown ONLY.

        Never called from a broker handler: waiting there is exactly what the
        queue exists to avoid.
        """
        import asyncio

        loop, queue = self._loop, self._queue
        if loop is None or queue is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(queue.join(), loop).result(timeout)
        except Exception:
            logger.debug("cp_discord: waiting for the gateway timed out", exc_info=True)

    # -- what the broker calls ------------------------------------------

    def adopt(self, records: Iterable[Any]) -> None:
        for record in list(records):
            thread_id = getattr(record, "thread_id", None)
            session_id = getattr(record, "session_id", None)
            if thread_id is None or session_id is None:
                continue
            self._schedule(self._adopt_job(session_id, int(thread_id)))

    def open_thread(self, session_id: str, title: str) -> None:
        self._schedule(lambda: self._manager.ensure_thread(session_id, title))

    def post(self, session_id: str, body: str) -> None:
        self._schedule(lambda: self._manager.post(session_id, body))

    def post_channel(self, body: str) -> None:
        self._schedule(lambda: self._manager.post_channel(body))

    def archive(self, session_id: str) -> None:
        self._schedule(lambda: self._manager.archive(session_id))

    def close(self) -> None:
        """Stop the pump and the private loop.  Never raises."""
        loop, queue = self._loop, self._queue
        if loop is not None and queue is not None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, _STOP)
            except RuntimeError:
                pass
        pump = self._pump
        if pump is not None:
            try:
                pump.result(2.0)
            except Exception:
                logger.debug("cp_discord: the gateway pump did not stop", exc_info=True)
        if self._own_loop_thread is not None and loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
            self._own_loop_thread.join(2.0)
            self._own_loop_thread = None
        self._loop = None
        self._queue = None
        self._pump = None

    # -- internals ------------------------------------------------------

    def _adopt_job(self, session_id: str, thread_id: int) -> Callable[[], Any]:
        async def job() -> None:
            thread = await self._resolve(thread_id)
            if thread is not None:
                self._manager.adopt(session_id, thread)

        return job

    async def _resolve(self, thread_id: int) -> Optional[Any]:
        """Turn a recorded id back into a thread, tolerating every failure."""
        resolver = self._resolve_thread
        if resolver is None:
            return None
        try:
            resolved = resolver(thread_id)
            if hasattr(resolved, "__await__"):
                resolved = await resolved
            return resolved
        except Exception:
            logger.debug(
                "cp_discord: could not resolve thread %s", thread_id, exc_info=True
            )
            return None

    def _channel(self) -> Optional[Any]:
        return self._channel_object


#: Sentinel that ends the drain loop.  A ``None`` job would be indistinguish-
#: able from a scheduling bug.
_STOP = object()

#: Upper bound on work held while the bot logs in.  Beyond this the OLDEST
#: entries go: the newest state of a session is the one worth posting.
_MAX_PENDING_JOBS = 200


__all__: Sequence[str] = (
    "ARCHIVE_REASON",
    "AUTO_ARCHIVE_MAX",
    "HEARTBEAT_GRACE",
    "REVIVE_REASON",
    "DiscordGateway",
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
