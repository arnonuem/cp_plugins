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
from typing import Any, Dict, List, Optional, Sequence

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
        self._load()

    # -- reading --------------------------------------------------------

    def get(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock:
            return self._records.get(session_id)

    def records(self) -> List[SessionRecord]:
        with self._lock:
            return list(self._records.values())

    # -- writing --------------------------------------------------------

    def upsert(self, record: SessionRecord) -> None:
        with self._lock:
            self._records[record.session_id] = record
            self._save_locked()

    def remove(self, session_id: str) -> None:
        with self._lock:
            if self._records.pop(session_id, None) is None:
                return
            self._save_locked()

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
