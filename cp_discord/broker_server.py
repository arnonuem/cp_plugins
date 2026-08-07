"""C1b — the broker itself: a TCP server on loopback.

The broker is a DAEMON THREAD inside one running session, not a process of its
own (§3.1, decided in R1).  A separate process would need spawning, detaching,
restart semantics and its own logging; a thread costs none of that.  The price
is that the broker dies with its tab -- which is why the re-election in
:mod:`.broker_activation` is mandatory rather than a refinement.

:class:`Broker` serves sessions: registration, heartbeats, state edges,
reports, releases -- and the RETURN channel, which pushes a gate resolution
into a session's own listener rather than waiting for it to call in.  A
session parked on an approval sends nothing, and that is precisely when it
needs to hear from us (§3.2a).

WHO runs the broker is a different question and lives next door
(:mod:`.broker_activation`).  ``install``/``uninstall`` stay here because C6
addresses this layer by module name (``register_callbacks.py:112``).

**The wire protocol is language-neutral** (§3.2): TCP on ``127.0.0.1``,
newline-delimited JSON, UTF-8, one connection per message, any non-empty reply
is an ack.  No ``pickle``, no Python-specific encoding: the broker must not
assume its clients are Python, and a bearer-token socket that unpickles what
it is sent would be a remote-code-execution hole rather than a protocol.

**Nothing here may break a terminal session** (INV-C1).  Every handler catches
everything, every Discord call is allowed to fail, and a broker that cannot
start is simply a session without Discord.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional, Sequence, Set, Tuple

from . import broker_election as election
from . import broker_gates, broker_threads
from .broker_election import LOOPBACK, BrokerAddress

#: Re-exported so that :mod:`.broker_server` stays the single import site for
#: the wire, exactly as it already is for ``client`` and ``client_inbound``.
#: Each value lives in the lowest layer using it -- the frame limit in
#: :mod:`.wire`, the rest next door; binding them by NAME keeps them aligned.
from .broker_gates import (  # noqa: F401
    GATE_CLOSED,
    GATE_OPEN,
    M_RESOLVE,
    RETRY_ATTEMPTS,
    RETRY_DELAY,
    SOCKET_TIMEOUT,
    UNDELIVERABLE,
)
from .reporter import BLOCKED, LOCAL_ONLY_MARKER
from .wire import MAX_FRAME_BYTES, read_frame, serve_accept_loop, write_frame

logger = logging.getLogger(__name__)

#: Wire methods.  Named, not numbered, because a human reads these in a
#: packet capture when something goes wrong.
M_REGISTER = "register"
M_HEARTBEAT = "heartbeat"
M_STATE = "state"
M_REPORT = "report"
M_RELEASE = "release"

#: The sixth method, and the only one that carries a WIDGET rather than text
#: (§3.2b).  Named like the rest -- a human reads these in a packet capture.
M_GATE = "gate"

ERR_UNAUTHORIZED = "unauthorized"
ERR_UNKNOWN_SESSION = "unknown_session"
ERR_BAD_REQUEST = "bad_request"

#: §3.1 step 3/4: ten attempts, 200 ms apart, then give up for this round.
ELECTION_ATTEMPTS = 10
ELECTION_RETRY_DELAY = 0.2

#: §3.1a: how often a session re-checks the broker and its own broker thread.
SUPERVISION_INTERVAL = 30.0


class Broker:
    """The TCP server, the session register and the Discord side.

    *gateway* is anything with ``adopt``, ``open_thread``, ``post``,
    ``post_channel`` and ``archive``.  Keeping it behind that interface is
    what makes the whole transport testable without a bot token -- and it is
    a real seam, not a test affordance: the Discord client runs its own event
    loop, so the calls have to be handed over rather than awaited here.
    """

    UNAUTHORIZED = ERR_UNAUTHORIZED

    def __init__(
        self,
        gateway: Any,
        *,
        token: str,
        notices: Sequence[str] = (),
    ) -> None:
        self._gateway = gateway
        self.token = token
        self._notices = tuple(notices)
        self._notices_announced = False
        self.registry = broker_threads.SessionRegistry()
        # The gateway learns a thread's id when it CREATES one; the registry is
        # all that still knows it once this broker is gone.  A callable, not
        # the register itself, so the dependency keeps pointing one way.
        recorder = getattr(gateway, "set_thread_recorder", None)
        if recorder is not None:
            self._safely(recorder, self.registry.set_thread_id)  # INV-C14
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._address: Optional[BrokerAddress] = None
        # Sessions whose listener refused a CONNECTION.  Not a death sentence
        # on its own -- see :meth:`sweep_dead_sessions`.
        self._unreachable: Set[str] = set()

    # -- lifecycle ------------------------------------------------------

    @property
    def address(self) -> BrokerAddress:
        if self._address is None:
            raise RuntimeError("the broker is not listening")
        return self._address

    def bound_host(self) -> str:
        """The host actually bound.  Read from the socket, not from intent."""
        return (
            self.address.host if self._server is None else self._server.getsockname()[0]
        )

    def start(self) -> None:
        """Bind loopback and start serving.  Idempotent."""
        if self._thread is not None:
            return
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # No SO_REUSEADDR: a port already in use means a broker may still be
        # there, and stealing it would produce exactly the two-broker state
        # the election exists to prevent.
        server.bind((LOOPBACK, 0))
        server.listen(64)
        server.settimeout(SOCKET_TIMEOUT)
        self._server = server
        self._address = BrokerAddress(port=server.getsockname()[1], token=self.token)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, name="cp_discord-broker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop serving.  Never raises; teardown has to reach every layer."""
        self._stop.set()
        thread, self._thread = self._thread, None
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(2.0)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def kill_thread_for_test(self) -> None:
        """Stop the serving thread while leaving the broker 'installed'.

        This reproduces INV-C22's second death case -- broker thread gone,
        process alive -- which is otherwise only reachable by injecting an
        exception into a running thread.  It is on the production class
        because the supervisor's recovery path is production code and needs a
        real dead thread to recover from, not a mocked ``is_alive``.
        """
        self.stop()

    # -- serving --------------------------------------------------------

    def _serve(self) -> None:
        server = self._server
        if server is None:
            return
        serve_accept_loop(server, self._stop, self._handle_connection, logger)

    def _handle_connection(self, connection: socket.socket) -> None:
        connection.settimeout(SOCKET_TIMEOUT)
        with connection.makefile("rwb") as stream:
            line = read_frame(stream)
            if not line:
                return
            response = self._dispatch(line)
            write_frame(stream, response)

    def _dispatch(self, line: bytes) -> Dict[str, Any]:
        """Answer one frame.  ALWAYS answers: silence would trigger a retry."""
        try:
            payload = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"ok": False, "error": ERR_BAD_REQUEST}
        if not isinstance(payload, dict):
            return {"ok": False, "error": ERR_BAD_REQUEST}

        if not self._authorized(payload.get("token")):
            return {"ok": False, "error": ERR_UNAUTHORIZED}

        session_id = payload.get("session_id")
        method = payload.get("method")
        if not isinstance(session_id, str) or not isinstance(method, str):
            return {"ok": False, "error": ERR_BAD_REQUEST}
        params = payload.get("params")
        params = params if isinstance(params, dict) else {}

        try:
            return self._handle(method, session_id, payload.get("seq"), params)
        except Exception:
            logger.debug("cp_discord: handling %s failed", method, exc_info=True)
            return {"ok": False, "error": ERR_BAD_REQUEST}

    def _authorized(self, token: Any) -> bool:
        """Constant-time token comparison (INV-C2)."""
        import hmac

        return isinstance(token, str) and hmac.compare_digest(token, self.token)

    def _handle(
        self, method: str, session_id: str, seq: Any, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        if method == M_REGISTER:
            return self._on_register(session_id, params)

        if self.registry.get(session_id) is None:
            return {"ok": False, "error": ERR_UNKNOWN_SESSION}

        if isinstance(seq, int) and not isinstance(seq, bool):
            if not self.registry.accept_seq(session_id, seq):
                # ACKED on purpose: a retry must stop retrying (AC-8).  The
                # flag lets a caller tell "applied" from "already had it".
                return {"ok": True, "duplicate": True}

        self.registry.touch(session_id)
        self._unreachable.discard(session_id)

        if method == M_HEARTBEAT:
            return {"ok": True}
        if method == M_STATE:
            return self._on_state(session_id, params)
        if method == M_REPORT:
            return self._on_report(session_id, params)
        if method == M_GATE:
            return self._on_gate(session_id, params)
        if method == M_RELEASE:
            return self._on_release(session_id)
        return {"ok": False, "error": ERR_BAD_REQUEST}

    # -- handlers -------------------------------------------------------

    def _on_register(self, session_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Register a session and give it a thread, or adopt its existing one.

        Re-registration is normal (a re-election makes every session call in
        again) and must NOT rebuild the thread -- INV-C14: the history has to
        survive a tab switch.

        The question asked is "does it HAVE a thread?", never "is it new?" --
        identical until a registration gets through while Discord does not
        (see :meth:`SessionRegistry.claim_thread`).
        """
        pid = params.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            return {"ok": False, "error": ERR_BAD_REQUEST}

        known = self.registry.get(session_id)
        title = str(params.get("title") or session_id)
        record = broker_threads.SessionRecord(
            session_id=session_id,
            title=known.title if known is not None else title,
            pid=pid,
            started_at=broker_threads.as_optional_float(params.get("started_at")),
            inbound_port=broker_threads.as_optional_int(params.get("inbound_port")),
            thread_id=known.thread_id if known is not None else None,
            last_seen=time.time(),
            last_seq=known.last_seq if known is not None else 0,
        )
        self.registry.upsert(record)
        self._unreachable.discard(session_id)
        # A claim is a REQUEST: if the thread exists and only the record lost
        # it, the gateway hands back that one rather than opening a second.
        if self.registry.claim_thread(session_id):
            self._safely(self._gateway.open_thread, session_id, record.title)
        return {"ok": True, "session_id": session_id}

    def _on_state(self, session_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Post a state edge, labelled if the phone cannot answer it (AC-69)."""
        message = params.get("message")
        text = str(message) if message else str(params.get("state") or "")
        if not text:
            return {"ok": True}
        if params.get("state") == BLOCKED and not params.get(
            "remote_resolvable", False
        ):
            text = _mark_local_only(text)
        self._safely(self._gateway.post, session_id, text)
        return {"ok": True}

    def _on_report(self, session_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Post a report, chunk by chunk, in order (AC-81b).

        C7 has already split it to Discord's limit, so the chunks are posted
        as they are -- re-joining and re-splitting here would move a boundary
        into the middle of a code fence.
        """
        chunks = params.get("chunks")
        if not isinstance(chunks, list):
            return {"ok": False, "error": ERR_BAD_REQUEST}
        for chunk in chunks:
            if isinstance(chunk, str) and chunk:
                self._safely(self._gateway.post, session_id, chunk)
        return {"ok": True}

    def _on_gate(self, session_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Put a gate into the thread, or finish one (§3.2b, AC-90/91).

        Built like :meth:`_on_state` -- token, ``seq`` and session lookup have
        already happened in :meth:`_handle` -- and it answers the same way: an
        ack, no result.  The decision travels back over the return channel
        later (§3.2a).

        The Discord work goes through the same ``_safely`` as every other
        handler: a gate that cannot be posted is a gate the phone cannot
        answer, never an error for the session (INV-C1).
        """
        try:
            well_formed = broker_gates.handle_gate(
                session_id,
                params,
                gateway=self._gateway,
                send_resolution=self.deliver_click,
                board=self._gateway.gate_board(),
            )
        except Exception:
            logger.debug("cp_discord: posting a gate failed", exc_info=True)
            return {"ok": True}
        return {"ok": True} if well_formed else {"ok": False, "error": ERR_BAD_REQUEST}

    def _on_release(self, session_id: str) -> Dict[str, Any]:
        """A clean session end: archive the thread, forget the session."""
        self.registry.remove(session_id)
        self._unreachable.discard(session_id)
        self._safely(self._gateway.archive, session_id)
        return {"ok": True}

    # -- the return channel (§3.2a) -------------------------------------

    def deliver_resolution(
        self,
        session_id: str,
        gate_id: str,
        decision: str,
        discord_user_id: Any,
    ) -> bool:
        """Push a gate resolution into the session's own listener.

        Whether it ARRIVED.  Whether the session then ACCEPTED the click is a
        different question with a different audience -- see
        :meth:`deliver_click`, which the buttons use.

        The transport lives in :mod:`.broker_gates`; what stays here is the
        one judgement only the broker can make -- whether a failed delivery
        means the session is GONE.  A token refusal proves the opposite
        (somebody answered), so it must never mark the session dead: that
        would archive a live session's thread (INV-C14, AC-15, AC-85d).
        """
        return self._push_resolution(session_id, gate_id, decision, discord_user_id)[0]

    def deliver_click(
        self,
        session_id: str,
        gate_id: str,
        decision: str,
        discord_user_id: Any,
    ) -> Optional[str]:
        """Deliver a BUTTON PRESS and answer the person who pressed it.

        ``None`` means the session took the decision; a string is what the
        clicker is told.  The distinction from :meth:`deliver_resolution` is
        the point: a frame can ARRIVE and still resolve nothing -- an
        outsider's press, or a gate the terminal already answered.  Reporting
        that as a delivery failure would blame the transport for a refusal the
        session deliberately made (AC-39, AC-74).
        """
        delivered, refusal = self._push_resolution(
            session_id, gate_id, decision, discord_user_id
        )
        if not delivered:
            return UNDELIVERABLE
        return refusal

    def _push_resolution(
        self, session_id: str, gate_id: str, decision: str, discord_user_id: Any
    ) -> Tuple[bool, Optional[str]]:
        """One delivery: did it arrive, and what did the session say about it."""
        record = self.registry.get(session_id)
        if record is None or not record.inbound_port:
            return False, None
        frame = broker_gates.resolution_frame(
            self.token, session_id, gate_id, decision, discord_user_id
        )
        outcome, answer = broker_gates.push(record.inbound_port, frame)
        if outcome is broker_gates.DELIVERED:
            self._unreachable.discard(session_id)
            refusal = answer.get("refusal") if isinstance(answer, dict) else None
            return True, str(refusal) if refusal else None
        if outcome is broker_gates.TRANSPORT_FAILED:
            # Nobody answered at all -- §3.2a's "session gone" case.
            self._unreachable.add(session_id)
        self._safely(self._gateway.post, session_id, UNDELIVERABLE)
        return False, None

    def is_marked_dead(self, session_id: str) -> bool:
        """Whether the session's listener could not be reached at all."""
        return session_id in self._unreachable

    # -- housekeeping ---------------------------------------------------

    def sweep_dead_sessions(self) -> None:
        """Archive the threads of sessions that are really gone (§7).

        An unreachable listener only makes a session a CANDIDATE; the PID has
        the last word (INV-C13).  A session can be momentarily unreachable --
        listener restarting, port briefly closed -- and archiving on that
        alone would take a working session's thread with it (AC-15).
        """
        candidates = set(self.registry.dead_sessions())
        for session_id in sorted(self._unreachable):
            record = self.registry.get(session_id)
            if record is not None and not election.process_matches(
                record.pid, record.started_at
            ):
                candidates.add(session_id)
        for session_id in sorted(candidates):
            self.registry.remove(session_id)
            self._unreachable.discard(session_id)
            self._safely(self._gateway.archive, session_id)

    def adopt_registered_sessions(self) -> None:
        """Hand the loaded register to the gateway (INV-C14, AC-53)."""
        records = self.registry.records()
        if records:
            self._safely(self._gateway.adopt, records)

    def announce_notices(self) -> None:
        """Post the activation warnings into the channel (AC-60b, AC-71b).

        Once: a re-election must not replay every old warning.
        """
        if self._notices_announced or not self._notices:
            return
        self._notices_announced = True
        for notice in self._notices:
            self._safely(self._gateway.post_channel, notice)

    def _safely(self, call: Callable[..., Any], *args: Any) -> None:
        """Call into Discord without letting it reach the caller (INV-C1)."""
        try:
            call(*args)
        except Exception:
            logger.debug(
                "cp_discord: %s failed", getattr(call, "__name__", call), exc_info=True
            )


def broker_is_reachable(address: Optional[BrokerAddress]) -> bool:
    """Whether a broker is actually ANSWERING at *address* (SS3.1 step 2).

    The published address must be probed, not merely read.  When the holding
    tab dies, its portfile stays behind: treating the file's existence as
    "somebody is serving" would turn it into a tombstone that blocks every
    future election, and Discord would stay dead until the next cold start --
    precisely the failure the re-election exists to fix (AC-52).

    A bare TCP connect is enough and is deliberately all that happens: it
    needs no token, so a session whose token went stale after a rotation
    still gets a truthful answer instead of concluding the broker is gone and
    starting a pointless election.
    """
    if address is None:
        return False
    try:
        with socket.create_connection(
            (address.host, address.port), timeout=SOCKET_TIMEOUT
        ):
            return True
    except OSError:
        return False


def _mark_local_only(text: str) -> str:
    """Label a wait the phone cannot resolve (INV-C23, AC-69)."""
    if LOCAL_ONLY_MARKER in text:
        return text
    return f"{text} — {LOCAL_ONLY_MARKER}"


# --------------------------------------------------------------------------- #
# Plugin surface (C6 drives this)
# --------------------------------------------------------------------------- #
#
# The election, the supervision and the Discord login live in
# :mod:`.broker_activation`.  These names stay HERE because
# ``register_callbacks.COMPONENTS`` addresses this layer as ``broker_server``
# (``register_callbacks.py:112``) and calls ``install()`` on whatever it
# imports -- so the entry point has to carry that name.  The import is
# deferred because ``broker_activation`` imports :class:`Broker` from here.

#: W2's seam (AC-85c): re-read the token after a rejection.  Bound by NAME so
#: there is exactly one reader of the portfile.
refresh_token_from_portfile = election.refresh_token_from_portfile


def install(config: Any) -> None:
    """Bring C1 up.  Returns at once (INV-C1)."""
    from . import broker_activation

    broker_activation.install(config)


def uninstall() -> None:
    """Take C1 down.  Never raises."""
    from . import broker_activation

    broker_activation.uninstall()


def active_supervisor() -> Optional[Any]:
    from . import broker_activation

    return broker_activation.active_supervisor()


def active_gateway() -> Optional[broker_threads.DiscordGateway]:
    from . import broker_activation

    return broker_activation.active_gateway()


__all__: Sequence[str] = (
    "ELECTION_ATTEMPTS",
    "ELECTION_RETRY_DELAY",
    "ERR_BAD_REQUEST",
    "ERR_UNAUTHORIZED",
    "ERR_UNKNOWN_SESSION",
    "LOCAL_ONLY_MARKER",
    "MAX_FRAME_BYTES",
    "GATE_CLOSED",
    "GATE_OPEN",
    "M_GATE",
    "M_HEARTBEAT",
    "M_REGISTER",
    "M_RELEASE",
    "M_REPORT",
    "M_RESOLVE",
    "M_STATE",
    "RETRY_ATTEMPTS",
    "RETRY_DELAY",
    "SOCKET_TIMEOUT",
    "SUPERVISION_INTERVAL",
    "UNDELIVERABLE",
    "Broker",
    "active_gateway",
    "broker_is_reachable",
    "active_supervisor",
    "install",
    "refresh_token_from_portfile",
    "uninstall",
)
