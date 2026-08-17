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

#: R3: how many envelope ids ONE session's replay memory holds before the
#: oldest is dropped.  Bounded because this is a replay WINDOW, not a ledger:
#: the transport retry lives about 150 ms (``SEND_ATTEMPTS`` x
#: ``SOCKET_TIMEOUT``), it does not survive a re-election, and a healed retry
#: carries a new id anyway.  The residual risk -- a replay after 256 further
#: envelopes -- is unreachable in that window.
ENVELOPE_MEMORY = 256


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


#: Longest identity we will remember.  A uuid4 hex is 32 characters; the cap is
#: generous enough for any sane sender and small enough that the memory cannot
#: be driven anywhere interesting.  Without it the only bound is the frame size
#: (1 MiB, ``wire.py``), and this registry holds up to ENVELOPE_MEMORY of them
#: PER SESSION -- measured at 230 MB for one session before the cap existed.
#: ``len`` counts CODEPOINTS, not bytes -- 64 astral characters are ~256
#: bytes of UTF-8.  The worst case stays around 86 KB per session, so the
#: bound holds; it is just not a byte budget.
#: An over-long identity is treated exactly like a missing one, which is the
#: same tolerance the rest of the rule already applies to a malformed ``seq``.
MAX_ENVELOPE_ID = 64


def _usable_identity(env_id: Any) -> bool:
    """Whether *env_id* may be remembered as an envelope identity.

    Empty is NOT usable, and that matters more than it looks: a shared empty
    identity would make every second frame of a session a "replay" and get it
    silently discarded with ``duplicate: True`` -- the exact failure this whole
    change exists to remove.
    """
    return isinstance(env_id, str) and 0 < len(env_id) <= MAX_ENVELOPE_ID


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
        # R1/R3: the envelope ids already applied, per session, oldest first.
        # NOT persisted and not part of a record, for the same reason as
        # ``_claimed`` -- but the argument here is the PAYLOAD, not the write
        # frequency: 256 uuids per session would go through an atomic disk
        # write on every heartbeat, and nobody reads them.  A dict is the
        # ordered set: insertion order is what makes the eviction FIFO.
        self._envelopes: Dict[str, Dict[str, None]] = {}
        # R2.1: the high-water mark for ``latest_wins`` frames, kept apart from
        # the shared ``last_seq`` so that one gate cannot raise the bar for
        # every state edge behind it.  Volatile too, so that ``as_json`` and
        # the loading of older registry files stay untouched -- the price is a
        # single unprotected state edge right after a re-election, which the
        # new broker has not posted yet anyway.
        self._state_seq: Dict[str, int] = {}
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
            # Both volatile memories go with the record, like the claim above:
            # otherwise a released session leaves up to ``ENVELOPE_MEMORY`` ids
            # behind for good, and its state mark would outlive the record
            # whose removal already resets ``last_seq`` to 0.
            self._envelopes.pop(session_id, None)
            self._state_seq.pop(session_id, None)
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

    def accept_envelope(
        self, session_id: str, seq: Any, env_id: Any, latest_wins: bool
    ) -> bool:
        """Whether this envelope is NEW and may be applied (R1, R2, R3).

        The ONE place the rule lives.  It sits in this module and not in
        :mod:`.broker_server` because the dependency may not run the other way:
        nothing in here imports the Discord side, so a
        ``from .broker_server import M_STATE`` would be an import cycle.  The
        caller therefore answers the only question that needs the method
        vocabulary it already owns -- *latest_wins*: "this method carries a
        WHOLE picture, and an older one must not overwrite a newer one", which
        is true of ``M_STATE`` and of nothing else.

        A ``False`` is ACKED by the caller on purpose: a retry must stop
        retrying (AC-8), and the ``duplicate`` flag it sends back is what lets
        a caller tell "applied" from "you already had it".

        The rule, in full:

        * an ``env_id`` that was already applied is a REPLAY and is discarded.
          This is what keeps the transport's three IDENTICAL attempts
          idempotent now that the number alone no longer does it.  A HEALED
          retry builds a fresh envelope with a fresh id, and that is correct:
          both of its causes (``unauthorized``, ``unknown_session``) are
          refused before anything is applied, so it was never applied once.
        * a *latest_wins* frame is judged by its OWN high-water mark, with or
          without an id.  Judging it by the shared ``last_seq`` would let a
          single gate discard every state edge still in flight behind it.
        * any OTHER frame keeps today's monotonicity against the shared
          ``last_seq`` -- but only while it carries NO id.  Without that
          narrowing a sender that knows nothing of ``env_id`` would lose every
          protection, and ``M_REPORT``, which has no second dedupe and posts
          its chunks straight out, would post three times for one lost ack.
        * a ``seq`` that is not a genuine ``int`` counts as NO ``seq``, and an
          ``env_id`` that is not a non-empty ``str`` counts as NO ``env_id``.
          The wire guard let such a frame through UNCOMPARED; comparing it here
          would raise instead, and an exception at the network edge turns an
          ``ok`` into a ``bad_request``.
        * an unknown session is refused, unchanged: registering is what creates
          the sequence space, and accepting from outside it would let anyone
          who guessed a session id inject events.  The cell "has an id, other
          method -> accept" does NOT override this.

        ``last_seq`` keeps being written for every applied frame, as a MAXIMUM
        and not as an assignment: an id-bearing frame is accepted without any
        comparison, and a plain assignment would let the counter run backwards.
        It is a diagnostic counter now, not a gate.
        """
        number = as_optional_int(seq)
        identity = env_id if _usable_identity(env_id) else None
        with self._lock:
            record = self._records.get(session_id)
            if record is None:
                return False
            seen = self._envelopes.get(session_id)
            if identity is not None and seen is not None and identity in seen:
                return False
            if latest_wins:
                mark = self._state_seq.get(session_id, 0)
            else:
                mark = record.last_seq
            compared = latest_wins or identity is None
            if compared and number is not None and number <= mark:
                return False
            if identity is not None:
                seen = self._envelopes.setdefault(session_id, {})
                seen[identity] = None
                while len(seen) > ENVELOPE_MEMORY:
                    del seen[next(iter(seen))]
            if number is None:
                return True
            if latest_wins:
                self._state_seq[session_id] = number
            if number > record.last_seq:
                self._records[session_id] = replace(record, last_seq=number)
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
    "ENVELOPE_MEMORY",
    "HEARTBEAT_GRACE",
    "SessionRecord",
    "SessionRegistry",
    "as_optional_float",
    "as_optional_int",
)
