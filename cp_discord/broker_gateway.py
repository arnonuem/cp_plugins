"""C1c — the Discord side of the broker: schedule work, never wait for it.

Split out of :mod:`.broker_threads`, which answers a different question.  That
module owns what a thread IS -- naming, creation, archiving, posting -- and can
be exercised with nothing but a fake channel.  This one owns HOW that work
reaches Discord: an event loop, a queue and the thread the two worlds meet on.

The seam is the dependency direction: this module imports the manager, never
the other way round, and :mod:`.broker_threads` re-exports the gateway so the
C1 layer keeps its single entry point.

Nothing in here is allowed to raise into a caller (INV-C1).  Discord being
down, rate-limiting or simply gone is a Discord problem; the terminal session
must not learn about it.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterable, List, Optional, Sequence

from .broker_gates import GateBoard, ViewFactory
from .broker_threads import ThreadManager

logger = logging.getLogger(__name__)


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
        self._board = GateBoard()
        self._channel_object: Optional[Any] = None
        self._loop: Optional[Any] = None
        self._own_loop_thread: Optional[threading.Thread] = None
        self._queue: "Optional[Any]" = None
        self._pump: Optional[Any] = None
        self._resolve_thread: Optional[Callable[[int], Any]] = None
        self._record_thread: Optional[Callable[[str, int], Any]] = None
        self._pending: List[Any] = []
        self._lock = threading.Lock()

    # -- wiring ---------------------------------------------------------

    def set_channel(self, channel: Any) -> None:
        """The bot has logged in and resolved the configured channel."""
        self._channel_object = channel

    def set_thread_resolver(self, resolver: Callable[[int], Any]) -> None:
        """How to turn a recorded ``thread_id`` back into a Discord thread."""
        self._resolve_thread = resolver

    def set_thread_recorder(self, recorder: Callable[[str, int], Any]) -> None:
        """Where to write down the id of a thread we just created.

        The counterpart of :meth:`set_thread_resolver`, and the reason
        adoption has anything to adopt: the id is only knowable HERE, and only
        useful THERE, in the register that outlives this broker.  A callable
        rather than the registry itself, so nothing in the Discord half has to
        import the bookkeeping half.
        """
        self._record_thread = recorder

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
        """Give *session_id* a thread, and write the id down (INV-C14).

        Creating one and forgetting its id is not half the job but none of it:
        the register is what a re-elected broker adopts from, so an id that
        never lands there means the next election rebuilds the thread and the
        history falls apart at the tab switch.

        Safe for a session that already HAS one -- ``ensure_thread`` returns
        the existing id without creating anything -- so this doubles as the
        repair for a record whose write-back was lost.
        """
        self._schedule(self._open_thread_job(session_id, title))

    def thread_id_for(self, session_id: str) -> Optional[int]:
        """The thread this gateway currently holds for *session_id*, if any.

        Read straight through rather than queued: the broker asks this while
        deciding whether to open a thread, and waiting on the Discord queue
        there would hand a TCP handler Discord's latency (INV-C1, INV-C4).
        It touches only the local mapping, so there is nothing to await.
        """
        return self._manager.thread_id_for(session_id)

    def gate_board(self) -> GateBoard:
        """Which gate owns which message.  The broker hands it to the views."""
        return self._board

    def post(self, session_id: str, body: str) -> None:
        self._schedule(lambda: self._manager.post(session_id, body))

    def post_gate(
        self,
        session_id: str,
        gate_id: str,
        body: str,
        view_factory: Optional[ViewFactory],
    ) -> None:
        """Queue a gate for the session's thread (§3.2b).  Returns at once."""
        self._schedule(
            lambda: self._manager.post_gate(
                session_id, gate_id, body, view_factory, self._board
            )
        )

    def finish_gate(self, session_id: str, gate_id: str, outcome: str) -> None:
        """Queue the closing edit for a gate.  Returns at once."""
        self._schedule(
            lambda: self._manager.finish_gate(session_id, gate_id, outcome, self._board)
        )

    def post_channel(self, body: str) -> None:
        self._schedule(lambda: self._manager.post_channel(body))

    def archive(self, session_id: str) -> None:
        self._board.forget_session(session_id)
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

    def _open_thread_job(self, session_id: str, title: str) -> Callable[[], Any]:
        async def job() -> None:
            thread_id = await self._manager.ensure_thread(session_id, title)
            recorder = self._record_thread
            if thread_id is None or recorder is None:
                return
            try:
                recorder(session_id, thread_id)
            except Exception:
                # A register we cannot write is a degraded broker, never a
                # failed session (INV-C1) -- but it costs the NEXT election
                # its adoption, so it is not a debug-level detail.
                logger.warning(
                    "cp_discord: could not record thread %s for session %s",
                    thread_id,
                    session_id,
                    exc_info=True,
                )

        return job

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


__all__: Sequence[str] = ("DiscordGateway",)
