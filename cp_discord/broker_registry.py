"""C1c — which sessions exist, and which of them are still alive.

Split out of :mod:`.broker_threads`, which answers a different question: that
module owns everything that touches a Discord OBJECT, this one owns the
bookkeeping that survives without Discord being reachable at all.  The seam is
the dependency direction -- nothing in here imports the Discord side, and the
registry is readable, writable and testable with no bot token and no channel.

Two rules shape it:

**The registry outlives the broker that wrote it** (§3.3a, AC-54).  It is
persisted eagerly, on every change, because the case it exists for is a broker
that DIED -- and a dead broker gets no chance to flush on shutdown.

**A session is declared dead by TWO signals, never one** (§7, INV-C13).  A
silent heartbeat alone is a session between beats; a missing PID alone is a
PID that got recycled.  Only both together are evidence, and the cost of
being wrong is asymmetric: a late archive is a stale entry in a list, an early
one removes a live session from view.

Nothing in here is allowed to raise into a caller (INV-C1): a registry that
cannot be written is a degraded broker, not a failed session.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Sequence, Set

from . import broker_election as election

logger = logging.getLogger(__name__)

#: How long a session may stay silent before its PID is checked (§7).
#: Heartbeats arrive every 30 s, so this is three missed ones -- generous on
#: purpose, because the cost of being wrong is asymmetric: a late archive is a
#: stale entry in a list, an early one deletes a live session from view.
HEARTBEAT_GRACE = 90.0


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One registered session, as it survives a broker change (§3.3a).

    ``last_seen`` is WALL CLOCK, not ``time.monotonic()``: the record outlives
    the process that wrote it, and a monotonic clock is meaningless across
    processes.  The grace period is sized accordingly.

    ``inbound_port`` is here rather than only in memory because a re-elected
    broker must be able to deliver a gate resolution to a session that has NOT
    re-registered yet (INV-C14/C17): without the address, every click in that
    window would be lost, and INV-C17's 100 ms budget would report it as an
    outright delivery failure.
    """

    session_id: str
    title: str
    pid: int
    started_at: Optional[float]
    inbound_port: Optional[int] = None
    thread_id: Optional[int] = None
    last_seen: float = 0.0
    last_seq: int = 0

    def as_json(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "pid": self.pid,
            "started_at": self.started_at,
            "inbound_port": self.inbound_port,
            "thread_id": self.thread_id,
            "last_seen": self.last_seen,
            "last_seq": self.last_seq,
        }

    @classmethod
    def from_json(cls, payload: Any) -> Optional["SessionRecord"]:
        """Parse one entry, or ``None`` if it is not usable.

        Strict on the two fields the liveness check needs: a record without a
        session id or a PID cannot be evaluated, and a record we half-
        understand is what leads to archiving somebody who is alive.
        """
        if not isinstance(payload, dict):
            return None
        session_id = payload.get("session_id")
        pid = payload.get("pid")
        if not isinstance(session_id, str) or not session_id:
            return None
        if not isinstance(pid, int):
            return None
        return cls(
            session_id=session_id,
            title=str(payload.get("title") or session_id),
            pid=pid,
            started_at=as_optional_float(payload.get("started_at")),
            inbound_port=as_optional_int(payload.get("inbound_port")),
            thread_id=as_optional_int(payload.get("thread_id")),
            last_seen=as_optional_float(payload.get("last_seen")) or 0.0,
            last_seq=as_optional_int(payload.get("last_seq")) or 0,
        )


def as_optional_int(value: Any) -> Optional[int]:
    """*value* if it is a genuine ``int``, else ``None``.

    ``bool`` is excluded deliberately: it IS an ``int`` in Python, and a
    ``True`` slipping through as a port number or a PID would be a lookup
    against process 1.  Shared with the wire decoder in :mod:`.broker_server`,
    which parses the very same fields from JSON.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def as_optional_float(value: Any) -> Optional[float]:
    """*value* as a ``float`` if it is numeric, else ``None``."""
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


class SessionRegistry:
    """Sessions on disk, so a re-election does not lose them (§3.3a, AC-54).

    Loaded on construction, written on every change, atomically and 0600.
    Persisting eagerly rather than on shutdown is the point: the case this
    exists for is a broker that DIED, and a dead broker writes nothing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, SessionRecord] = {}
        # Sessions a thread has already been requested for.  NOT persisted and
        # not part of a record: it covers the window before the id is written
        # back, and a fresh broker must stay free to ask again for a record
        # that never got one (see :meth:`claim_thread`).
        self._claimed: Set[str] = set()
        self._load()

    # -- reading --------------------------------------------------------

    def get(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock:
            return self._records.get(session_id)

    def records(self) -> List[SessionRecord]:
        with self._lock:
            return list(self._records.values())

    def session_for_thread(self, thread_id: Any) -> Optional[str]:
        """Which session owns *thread_id*, or ``None`` (INV-4, §6.0).

        DERIVED from the records rather than kept as a second map, and that is
        the whole design: :meth:`set_thread_id` refuses unknown sessions on
        purpose (see its docstring), so a separately maintained reverse map
        would reproduce exactly that consistency problem one level up -- it
        could hold a thread whose record is already gone, and the next chat
        message would be steered into a session that was released. A derived
        lookup cannot diverge by construction, and a linear scan over a
        handful of sessions costs nothing.

        An unusable id answers ``None`` rather than matching: ``thread_id`` is
        ``None`` between a registration and Discord's answer, so a bare
        equality test would make every threadless session a wildcard for every
        unmapped message.
        """
        if not isinstance(thread_id, int) or isinstance(thread_id, bool):
            return None
        with self._lock:
            for record in self._records.values():
                if record.thread_id == thread_id:
                    return record.session_id
        return None

    # -- writing --------------------------------------------------------

    def upsert(self, record: SessionRecord) -> None:
        with self._lock:
            self._records[record.session_id] = record
            self._save_locked()

    def remove(self, session_id: str) -> None:
        with self._lock:
            # The claim goes with the record: removal means the thread is being
            # archived, so a session that comes back needs a NEW one, and a
            # claim left behind would keep it silent for the rest of its life.
            self._claimed.discard(session_id)
            if self._records.pop(session_id, None) is None:
                return
            self._save_locked()

    def set_thread_id(self, session_id: str, thread_id: int) -> bool:
        """Record the Discord thread that now belongs to *session_id*.

        Whether it landed.  An unknown session is REFUSED rather than created:
        the id arrives asynchronously, so it can turn up after a ``release``
        removed the record, and re-inserting it would resurrect a session the
        next sweep would archive a thread for.

        Without this the column stays empty forever and
        ``adopt_registered_sessions`` finds nothing to adopt -- INV-C14 would
        hold in the suite and nowhere else.
        """
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record.thread_id == thread_id:
                return False
            self._records[session_id] = replace(record, thread_id=thread_id)
            self._save_locked()
            return True

    def claim_thread(self, session_id: str) -> bool:
        """Whether a Discord thread still has to be opened for this session.

        The question is "does it HAVE one?", never "is it new?".  The two look
        identical until a registration gets through while Discord does not:
        the record then exists with an empty ``thread_id``, "new?" answers no,
        and the session stays threadless for as long as it lives -- a restart
        does not heal it, because the record is precisely what survives one.

        Answers ``True`` at most ONCE per gap, so that the re-registrations of
        an election do not each open a thread.  The claim is deliberately
        in-memory: it exists to cover the window before the id is written
        back, and dropping it with the broker is what lets a record that never
        got a thread be retried at the next election rather than trusted
        forever.

        A ``True`` here is a REQUEST, not a promise that nothing exists: the
        Discord side has the last word and hands back the thread it already
        holds instead of creating a second one (INV-C14).
        """
        with self._lock:
            record = self._records.get(session_id)
            if record is None or record.thread_id is not None:
                return False
            if session_id in self._claimed:
                return False
            self._claimed.add(session_id)
            return True

    def touch(self, session_id: str, *, now: Optional[float] = None) -> None:
        """Record a heartbeat."""
        stamp = time.time() if now is None else now
        with self._lock:
            record = self._records.get(session_id)
            if record is None:
                return
            self._records[session_id] = replace(record, last_seen=stamp)
            self._save_locked()

    def accept_seq(self, session_id: str, seq: int) -> bool:
        """Whether *seq* is new for this session (AC-8).

        ``seq <= last_seq`` is discarded, which is exactly what makes a retry
        of an identical envelope idempotent -- the transport retries three
        times, so without this a state edge could be applied twice.

        An unknown session is refused: registering is what creates the
        sequence space, and accepting from outside it would let anyone who
        guessed a session id inject events.
        """
        with self._lock:
            record = self._records.get(session_id)
            if record is None or seq <= record.last_seq:
                return False
            self._records[session_id] = replace(record, last_seq=seq)
            self._save_locked()
            return True

    # -- liveness (§7, INV-C13) -----------------------------------------

    def dead_sessions(self, *, now: Optional[float] = None) -> List[str]:
        """Sessions whose threads may be archived.

        TWO conditions, in this order, and the order is the whole point:

        1. the heartbeat has been silent for longer than the grace period --
           a session that just reported in is alive by definition (AC-15);
        2. AND the process behind it is gone, judged by PID *and* start time
           (INV-C13), because a recycled PID would otherwise keep a dead
           session's thread alive forever (AC-51).

        Checking liveness first would be cheaper and wrong: it would archive
        the thread of a session that is merely between heartbeats.
        """
        stamp = time.time() if now is None else now
        dead: List[str] = []
        for record in self.records():
            if stamp - record.last_seen < HEARTBEAT_GRACE:
                continue
            if election.process_matches(record.pid, record.started_at):
                continue
            dead.append(record.session_id)
        return dead

    # -- persistence ----------------------------------------------------

    def _load(self) -> None:
        payload = election.read_json(election.registry_path())
        if not isinstance(payload, list):
            return
        for entry in payload:
            record = SessionRecord.from_json(entry)
            if record is not None:
                self._records[record.session_id] = record

    def _save_locked(self) -> None:
        """Persist.  A failed write must never take the broker down (INV-C1)."""
        try:
            election.write_json_atomic(
                election.registry_path(),
                [record.as_json() for record in self._records.values()],
            )
        except OSError:
            logger.debug(
                "cp_discord: writing the session registry failed", exc_info=True
            )


__all__: Sequence[str] = (
    "HEARTBEAT_GRACE",
    "SessionRecord",
    "SessionRegistry",
    "as_optional_float",
    "as_optional_int",
)
