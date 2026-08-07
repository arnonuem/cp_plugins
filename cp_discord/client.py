"""C2 — the session's own end of the wire: register, send, beat.

Every session runs one of these, including the one that happens to host the
broker.  It does three things (§3.1, §3.2):

* **mints its own session id** (INV-C9) -- the broker never does, because a
  session that finds no broker must still have one;
* **registers** with whoever is serving, and re-registers after a re-election;
* **sends** state edges, reports and the final release, off the hot path.

The fourth duty, LISTENING for gate resolutions pushed back at us, lives in
:mod:`.client_inbound`: that is a server with its own socket and accept loop,
and the seam keeps this module about talking to a broker.

**INV-C1 is this layer's first duty, not its last.**  A missing, crashed or
slow broker must be invisible to the terminal: no blocking on a hot path, no
exception upwards, no cancelled turn.  Concretely that means the sink handed
to C3 never raises, every socket call carries a timeout, and giving up is a
normal outcome that costs one log line.

**The wire stays language-neutral** (§3.2): newline-delimited UTF-8 JSON, one
connection per message, any ``ok`` reply is an ack.  No ``pickle`` -- a
bearer-token socket that unpickled its input would be a remote code execution
hole rather than a protocol.

**The token heals in BOTH directions** (§3.1, AC-85c).  A re-elected broker
adopts the old token, but a cold start rotates it; after a rotation the broker
is perfectly REACHABLE, so §3.1a's "re-read when unreachable" never fires.  So
we re-read the portfile the moment a frame of ours is refused for
authorization AND the moment we refuse an incoming one -- plus on the
supervision tick as an upper bound.  Reading goes through C1's
``refresh_token_from_portfile`` rather than a second parser: one reader of the
portfile means one place for the token rules to live.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from . import broker_election as election
from . import broker_server, broker_threads, client_inbound, reporter, session_ids
from .broker_server import (
    ELECTION_ATTEMPTS,
    ELECTION_RETRY_DELAY,
    ERR_UNAUTHORIZED,
    ERR_UNKNOWN_SESSION,
    GATE_CLOSED,
    GATE_OPEN,
    M_GATE,
    M_HEARTBEAT,
    M_REGISTER,
    M_RELEASE,
    M_REPORT,
    M_STATE,
    SOCKET_TIMEOUT,
    SUPERVISION_INTERVAL,
)
from .client_inbound import (
    ERR_NO_HANDLER,
    InboundListener,
    ResolutionHandler,
    SteerHandler,
)
from .wire import read_frame

logger = logging.getLogger(__name__)

#: §3.2: three attempts, 50 ms apart, with the IDENTICAL envelope.  Keeping
#: the envelope is what makes the retry idempotent -- the receiver discards
#: ``seq <= last_seq``, which only helps if the sender does not renumber.
SEND_ATTEMPTS = 3
SEND_BACKOFF = 0.05

#: §7: a heartbeat every 30 s; three missed ones start the liveness check.
#: Same cadence as the broker's supervision tick, and deliberately so -- one
#: timer answers "am I still known?" and "has the token rotated?".
HEARTBEAT_INTERVAL = SUPERVISION_INTERVAL


class SessionClient:
    """One session's connection to the broker.  Owns two daemon threads.

    Neither thread is started by the constructor: an id is useful to a session
    that never talks to anyone, and :meth:`start` is what turns this into a
    participant.
    """

    #: Bound by NAME to C1's reader (AC-85c).  A class attribute rather than a
    #: default argument so a test can assert the seam without calling it.
    default_refresh_token = staticmethod(broker_server.refresh_token_from_portfile)

    def __init__(
        self,
        *,
        session_id: Optional[str] = None,
        title: Optional[str] = None,
        refresh_token: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self.session_id = session_id or session_ids.new_session_id()
        self._title = title or self.session_id
        self._refresh_token = refresh_token or self.default_refresh_token
        self._pid, self._started_at = election.process_identity()

        self._lock = threading.Lock()
        self._seq = 0
        self._token: Optional[str] = None
        self._address: Optional[election.BrokerAddress] = None
        self._registered = False

        self._handler: Optional[ResolutionHandler] = None
        self._steer_handler: Optional[SteerHandler] = None
        self._inbound = InboundListener(
            authorize=self._authorized,
            on_refused=self._refresh_token_now,
            handler_provider=lambda: self._handler,
            steer_provider=lambda: self._steer_handler,
        )
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- observation ----------------------------------------------------

    @property
    def registered(self) -> bool:
        with self._lock:
            return self._registered

    @property
    def inbound_port(self) -> int:
        """The port the broker pushes resolutions to.  ``0`` if not listening."""
        return self._inbound.port

    @property
    def inbound_host(self) -> str:
        return self._inbound.host

    def set_resolution_handler(self, handler: Optional[ResolutionHandler]) -> None:
        """Point inbound gate resolutions at C4 (or at nothing)."""
        self._handler = handler

    def set_steer_handler(self, handler: Optional[SteerHandler]) -> None:
        """Point inbound chat messages at C5 (or at nothing).

        The counterpart of :meth:`set_resolution_handler`, and just as
        load-bearing: without this call every steer is answered ``no_handler``
        and the whole chat path is dead while the suite stays green.  C5 makes
        the call itself, in ``install``/``uninstall`` -- the other direction
        would have this module import ``inbound``, inverting the layering
        that module documents (``inbound.py:33-36``).
        """
        self._steer_handler = handler

    def adopt_token_for_test(self, token: Optional[str]) -> None:
        """Force the cached token, reproducing a rotation we have not seen.

        The state this creates -- a live session holding a token the broker no
        longer accepts -- is otherwise only reachable by racing a real
        re-election, and the healing path out of it is production code that
        needs a genuinely stale token to heal from.
        """
        with self._lock:
            self._token = token

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Open the inbound socket and start beating.  Idempotent, immediate."""
        self._inbound.start()
        with self._lock:
            if self._heartbeat_thread is not None:
                return
            self._heartbeat_thread = threading.Thread(
                target=self._beat, name="cp_discord-client", daemon=True
            )
            thread = self._heartbeat_thread
        thread.start()

    def stop(self) -> None:
        """Stand down.  Never raises: teardown has to reach every layer."""
        self._stop.set()
        with self._lock:
            heartbeat, self._heartbeat_thread = self._heartbeat_thread, None
        self._inbound.stop()
        if heartbeat is not None and heartbeat.is_alive():
            heartbeat.join(2.0)

    # -- the sink C3 delivers into --------------------------------------

    def sink(self, event: Any) -> None:
        """Take one mailbox event to the broker.  NEVER raises (INV-C1).

        This is C3's delivery path, so it runs on the reporter's worker
        thread -- never on a core hook -- but it is still the boundary where
        a broker problem would otherwise become the agent's problem.
        """
        try:
            frame = _frame_for(event)
            if frame is None:
                logger.debug("cp_discord: ignoring an unknown event %r", event)
                return
            method, params = frame
            self._send(method, params)
        except Exception:
            logger.debug("cp_discord: delivering %r failed", event, exc_info=True)

    # -- gates: the HINWEG (§3.2b) --------------------------------------

    def submit_gate(
        self,
        gate_id: str,
        title: str,
        body: str,
        *,
        preview: Optional[str] = None,
        remote_resolvable: bool = True,
    ) -> bool:
        """Put a gate into this session's thread.  Returns whether it landed.

        C4 calls THIS, never the socket: the client stays the only thing that
        knows the wire format, so there is one place where a frame is built.

        ``False`` means "this gate cannot be answered from the phone" -- no
        broker, no thread, Discord down.  It is emphatically not an error
        (INV-C1, AC-92): the terminal prompt runs regardless, and a failed
        HINWEG is not a branch winner.

        *remote_resolvable* travels because only C4 knows it, and the broker
        needs it to decide whether to attach buttons at all (INV-C23, AC-91).
        """
        return self._send_gate(
            gate_id,
            {
                "status": GATE_OPEN,
                "title": title,
                "body": body,
                "preview": preview,
                "remote_resolvable": bool(remote_resolvable),
            },
        )

    def close_gate(self, gate_id: str, outcome: str, *, title: str = "") -> bool:
        """Tell the thread the gate is decided (AC-37, AC-39).

        Same method, different ``status``: the two belong to one message, and
        a separate method would let a gate be opened without ever being
        closed -- leaving live buttons under a decision that already happened.
        """
        return self._send_gate(
            gate_id, {"status": GATE_CLOSED, "outcome": outcome, "title": title}
        )

    def _send_gate(self, gate_id: str, params: Dict[str, Any]) -> bool:
        """One gate frame.  NEVER raises: this sits on the approval path."""
        try:
            return self._send(M_GATE, {"gate_id": gate_id, **params})
        except Exception:
            logger.debug(
                "cp_discord: submitting gate %s failed", gate_id, exc_info=True
            )
            return False

    # -- registration (§3.1) --------------------------------------------

    def register_now(self, *, attempts: int = ELECTION_ATTEMPTS) -> bool:
        """Find a broker and register with it.  Returns whether it worked.

        §3.1 steps 1-4: read the portfile, register, back off, and after
        *attempts* rounds give up -- QUIETLY.  Giving up is the normal outcome
        for a machine with no Discord configured, and the session goes on
        exactly as before (AC-2).
        """
        for attempt in range(max(1, attempts)):
            if self._register_once():
                return True
            if attempt + 1 < attempts:
                time.sleep(ELECTION_RETRY_DELAY)
        logger.info(
            "cp_discord: no broker answered; this session continues without Discord"
        )
        return False

    def _register_once(self) -> bool:
        address = election.read_portfile()
        if address is None:
            return False
        with self._lock:
            self._address = address
            self._token = address.token
            self._registered = False
        self._inbound.start()
        params = {
            "title": self._title,
            "pid": self._pid,
            "started_at": self._started_at,
            "inbound_port": self.inbound_port,
        }
        return self._send(M_REGISTER, params, heal_unknown=False)

    def tick(self, *, attempts: int = ELECTION_ATTEMPTS) -> None:
        """One supervision round: refresh the token, then beat (§3.1a, §7).

        The refresh comes FIRST and unconditionally: it is the upper bound on
        healing after a rotation, and a heartbeat sent with a stale token
        would be refused and heal only as a side effect.
        """
        self._refresh_token_now()
        if not self.registered:
            self.register_now(attempts=attempts)
            return
        self._send(M_HEARTBEAT, {})

    def _beat(self) -> None:
        self.register_now()
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            try:
                self.tick()
            except Exception:
                logger.debug("cp_discord: a heartbeat round failed", exc_info=True)

    # -- sending (§3.2) --------------------------------------------------

    def _send(
        self,
        method: str,
        params: Dict[str, Any],
        *,
        heal_unknown: bool = True,
        heal_token: bool = True,
    ) -> bool:
        """One frame; transport retries, then at most one healed retry.

        The transport retry (three attempts, 50 ms apart) sends the IDENTICAL
        envelope: renumbering it would defeat the receiver's ``seq`` check and
        let an already-applied state edge apply twice (AC-8).

        A healed retry is a NEW envelope, because it carries a new token or a
        new registration, and it is allowed exactly once per cause -- looping
        would turn a misconfigured broker into a busy wait on the reporter's
        thread.
        """
        with self._lock:
            address, token = self._address, self._token
        if address is None or token is None:
            return False

        envelope = {
            "token": token,
            "method": method,
            "session_id": self.session_id,
            "seq": self._next_seq(),
            "params": params,
        }
        payload = (json.dumps(envelope) + "\n").encode("utf-8")

        for attempt in range(SEND_ATTEMPTS):
            answer = _round_trip(address.host, address.port, payload)
            if answer is not None:
                return self._apply_answer(
                    answer,
                    method,
                    params,
                    heal_unknown=heal_unknown,
                    heal_token=heal_token,
                )
            if attempt + 1 < SEND_ATTEMPTS:
                time.sleep(SEND_BACKOFF)

        logger.debug(
            "cp_discord: %s undelivered after %d attempts", method, SEND_ATTEMPTS
        )
        with self._lock:
            self._registered = False
        return False

    def _apply_answer(
        self,
        answer: Dict[str, Any],
        method: str,
        params: Dict[str, Any],
        *,
        heal_unknown: bool,
        heal_token: bool,
    ) -> bool:
        """Turn the broker's reply into the next move.

        Three replies matter.  ``ok`` (including ``duplicate``) is done.
        ``unauthorized`` means the token rotated under us -- re-read and send
        once more (AC-85c(a)); it is emphatically NOT a dead broker, somebody
        answered.  ``unknown_session`` means a broker with a fresh register is
        serving: register again and resend, or the session stays mute until
        the next tick.

        Each cause disarms ITSELF for the retry (``heal_token`` /
        ``heal_unknown``) rather than disarming both: a resend that is refused
        for the OTHER reason is a different, real failure and deserves its own
        single attempt.
        """
        if answer.get("ok"):
            if method == M_REGISTER:
                with self._lock:
                    self._registered = True
            return True

        error = answer.get("error")
        if error == ERR_UNAUTHORIZED and heal_token and self._refresh_token_now():
            return self._send(
                method, params, heal_unknown=heal_unknown, heal_token=False
            )
        if error == ERR_UNKNOWN_SESSION and heal_unknown:
            with self._lock:
                self._registered = False
            if self._register_once():
                return self._send(
                    method, params, heal_unknown=False, heal_token=heal_token
                )
        logger.debug("cp_discord: the broker refused %s (%s)", method, error)
        return False

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _refresh_token_now(self) -> bool:
        """Re-read the portfile (AC-85c).  Returns whether anything changed.

        Goes through C1's ``refresh_token_from_portfile``: a second reader of
        the portfile would be a second place for the token rules to drift.
        """
        try:
            token = self._refresh_token()
        except Exception:
            logger.debug("cp_discord: re-reading the portfile failed", exc_info=True)
            return False
        if not token:
            return False
        address = election.read_portfile()
        with self._lock:
            changed = token != self._token or (
                address is not None and address != self._address
            )
            self._token = token
            if address is not None:
                self._address = address
        return changed

    def _authorized(self, token: Any) -> bool:
        """Whether an inbound frame carries OUR token (INV-C18)."""
        with self._lock:
            expected = self._token
        return client_inbound.token_matches(expected, token)


def _frame_for(event: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
    """The wire frame for one mailbox event, or ``None`` if it is not one."""
    if isinstance(event, reporter.StateEvent):
        return M_STATE, {
            "state": event.state,
            "message": event.message,
            "remote_resolvable": event.remote_resolvable,
        }
    if isinstance(event, reporter.ReportEvent):
        return M_REPORT, {"chunks": list(event.chunks)}
    if isinstance(event, reporter.ReleaseEvent):
        return M_RELEASE, {}
    return None


def _round_trip(host: str, port: int, payload: bytes) -> Optional[Dict[str, Any]]:
    """One request/response on loopback, or ``None`` if nobody answered.

    ``None`` means TRANSPORT failure (refused, timed out, closed) and is the
    only case worth retrying with the same envelope.  An answer -- even a
    refusal -- proves somebody is there, and repeating an identical envelope
    at them would simply be refused again.
    """
    try:
        with socket.create_connection((host, port), timeout=SOCKET_TIMEOUT) as sock:
            sock.settimeout(SOCKET_TIMEOUT)
            sock.sendall(payload)
            with sock.makefile("rb") as stream:
                line = read_frame(stream)
    except OSError:
        return None
    if not line:
        return None
    try:
        answer = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return answer if isinstance(answer, dict) else None


# --------------------------------------------------------------------------- #
# Plugin surface (C6 drives this)
# --------------------------------------------------------------------------- #

_client: Optional[SessionClient] = None


def active_client() -> Optional[SessionClient]:
    """The session's client, or ``None`` if C2 is not installed."""
    return _client


def install(config: Any) -> None:
    """Bring C2 up.  Returns at once (INV-C1).

    The one handover that makes this wave add up: ``reporter.set_sink``.
    Without it C3 is a correct, fully tested and completely SILENT no-op --
    it fills its mailbox, drains it, and delivers into nothing.
    """
    global _client

    if _client is not None:
        uninstall()

    title = broker_threads.derive_title(os.getcwd(), _session_name(config))
    instance = SessionClient(title=title)
    _client = instance
    reporter.set_sink(instance.sink)
    instance.start()
    logger.debug("cp_discord: C2 client installed (%s)", instance.session_id)


def uninstall() -> None:
    """Take C2 down.  Never raises: teardown must reach every layer."""
    global _client

    instance, _client = _client, None
    reporter.set_sink(None)
    if instance is None:
        return
    try:
        instance.stop()
    except Exception:
        logger.debug("cp_discord: stopping the client failed", exc_info=True)


def _session_name(config: Any) -> Optional[str]:
    """``--session-name``, from the config or from C6 (AC-12)."""
    override = getattr(config, "session_name", None)
    if override:
        return str(override)
    try:
        from . import register_callbacks

        return register_callbacks.session_name_override()
    except Exception:
        logger.debug("cp_discord: no session name available", exc_info=True)
        return None


__all__: Sequence[str] = (
    "ERR_NO_HANDLER",
    "HEARTBEAT_INTERVAL",
    "SEND_ATTEMPTS",
    "SEND_BACKOFF",
    "InboundListener",
    "SessionClient",
    "active_client",
    "install",
    "uninstall",
)
