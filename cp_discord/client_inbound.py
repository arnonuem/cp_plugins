"""C2b — the session's own listening socket (§3.2a, the RETURN channel).

Split out of :mod:`.client`, which answers the opposite question.  That module
SENDS: it finds a broker, registers, beats and reports.  This one SERVES: it
owns a socket, an accept loop and the framing of what arrives, so that a gate
resolved on a phone reaches the session that is parked on it.

**Why the session listens at all.**  A session waiting on an approval sends
nothing -- and that is precisely the moment it needs to hear from Discord.
Piggybacking the answer on the next heartbeat would put a 30-second floor
under every phone click, and INV-C17 budgets 100 ms.

**Same security rules as the broker's socket** (INV-C18): ``127.0.0.1`` only,
a token on every frame, constant-time comparison, and anything else dropped.
A return channel without authentication would be a remote control for every
local process, on a session that can approve shell commands.

The three decisions this listener does NOT make are injected, so nothing here
reaches back into the client: whether a token is valid, what to do when one is
not (the AC-85c(b) heal), and who takes a resolution.  The dependency points
one way, and the socket half is testable without a broker.
"""

from __future__ import annotations

import hmac
import json
import logging
import socket
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional, Sequence

from . import constants
from .broker_election import LOOPBACK
from .broker_server import (
    ERR_BAD_REQUEST,
    ERR_UNAUTHORIZED,
    M_RESOLVE,
    SOCKET_TIMEOUT,
)

# Straight from its own module, not through :mod:`.broker_server` like the
# names above: that file sits one line under the project's cap, and the two
# lines a re-export costs there are the two it does not have (SPEC §5a).
from .broker_steer import M_STEER
from .wire import read_frame, serve_accept_loop, write_frame

logger = logging.getLogger(__name__)

#: Refused because nothing is there to take the resolution.  Deliberately NOT
#: an ack: an ack means "taken", and acking with nowhere to put it would lose
#: the click while telling the broker it landed.
ERR_NO_HANDLER = "no_handler"

#: The handler signature C4 implements (§3.2a).  ``discord_user_id`` travels
#: with the decision because the APPROVER check happens in the SESSION, which
#: owns the authorization database -- not in the broker.
ResolutionHandler = Callable[..., Any]

#: The handler signature C5 implements (§4.3c): called with ``external_id``
#: and ``text``, answering a flat ``dict`` of ``accepted``/``steer``/``mode``.
#: A ``dict`` rather than C5's own ``Delivery`` -- importing that type here
#: would turn the dependency between the two modules around
#: (``inbound.py:33-36``).
SteerHandler = Callable[..., Any]

#: How many delivered messages the duplicate window remembers (§4.4a).
#:
#: The only source of repeats is the broker's own retry -- three attempts, one
#: second apart -- so this is roomy by an order of magnitude.  It is a WINDOW,
#: not a store: it needs no clearing because it evicts its own oldest entry,
#: and it holds ids, never text.
STEER_WINDOW = 32


def token_matches(expected: Optional[str], offered: Any) -> bool:
    """Constant-time token comparison; ``False`` for anything unusable.

    Shared by both ends of the session's auth so the two cannot drift: the
    broker applies the identical rule to what we send it.
    """
    if not expected or not isinstance(offered, str):
        return False
    return hmac.compare_digest(offered, expected)


class InboundListener:
    """A loopback socket that turns gate resolutions into handler calls.

    *authorize* answers whether a frame's token is ours, *on_refused* fires
    when it is not (that refusal is the only thing that would ever make a
    session with a rotated token look at the portfile again), and both
    providers are consulted per frame because C4 and C5 install their handlers
    after the socket is already up.

    *steer_provider* is OPTIONAL and stays optional: a listener without one
    simply answers ``no_handler`` to a steer, which is what a session that has
    not brought C5 up should say.
    """

    def __init__(
        self,
        *,
        authorize: Callable[[Any], bool],
        on_refused: Callable[[], None],
        handler_provider: Callable[[], Optional[ResolutionHandler]],
        steer_provider: Optional[Callable[[], Optional[SteerHandler]]] = None,
    ) -> None:
        self._authorize = authorize
        self._on_refused = on_refused
        self._handler_provider = handler_provider
        self._steer_provider = steer_provider or (lambda: None)
        # Per INSTANCE, not module-global: tests build their own listeners and
        # have to stay isolated from one another.  No lock -- this listener
        # serves on exactly one thread and works its connections through
        # strictly in sequence, so a second concurrent ``dispatch`` does not
        # arise in production.
        self._seen_steers: Deque[Any] = deque(maxlen=STEER_WINDOW)
        self._lock = threading.Lock()
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- observation ----------------------------------------------------

    @property
    def port(self) -> int:
        """The port the broker delivers to.  ``0`` when not listening."""
        sock = self._socket
        return sock.getsockname()[1] if sock is not None else 0

    @property
    def host(self) -> str:
        sock = self._socket
        return sock.getsockname()[0] if sock is not None else LOOPBACK

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Bind ``127.0.0.1:0`` and serve.  Idempotent; never raises.

        A session that cannot open this socket is one that cannot be answered
        from a phone -- degraded, but not broken (INV-C1), so the failure is a
        log line and the session goes on.
        """
        with self._lock:
            if self._socket is not None:
                return
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind((LOOPBACK, 0))
                sock.listen(16)
                sock.settimeout(SOCKET_TIMEOUT)
            except OSError:
                logger.debug("cp_discord: no inbound socket", exc_info=True)
                return
            self._stop.clear()
            self._socket = sock
            self._thread = threading.Thread(
                target=self._serve, args=(sock,), name="cp_discord-inbound", daemon=True
            )
            thread = self._thread
        thread.start()

    def stop(self) -> None:
        """Close the socket and join the thread.  Safe to call twice."""
        self._stop.set()
        with self._lock:
            sock, self._socket = self._socket, None
            thread, self._thread = self._thread, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(2.0)

    # -- serving --------------------------------------------------------

    def _serve(self, sock: socket.socket) -> None:
        serve_accept_loop(sock, self._stop, self._handle_connection, logger)

    def _handle_connection(self, connection: socket.socket) -> None:
        connection.settimeout(SOCKET_TIMEOUT)
        with connection.makefile("rwb") as stream:
            line = read_frame(stream)
            if not line:
                return
            response = self.dispatch(line)
            write_frame(stream, response)

    def dispatch(self, line: bytes) -> Dict[str, Any]:
        """Answer one frame.  ALWAYS answers -- silence would mean a retry."""
        try:
            payload = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"ok": False, "error": ERR_BAD_REQUEST}
        if not isinstance(payload, dict):
            return {"ok": False, "error": ERR_BAD_REQUEST}

        if not self._authorize(payload.get("token")):
            # AC-85c(b).  After a rotation the resolution carries the NEW
            # token and we still hold the old one; this refusal is the only
            # event that would ever make us re-read the portfile.  The broker
            # retries three times, so attempt two meets the healed token.
            self._refuse()
            return {"ok": False, "error": ERR_UNAUTHORIZED}

        # AFTER the token, never before (INV-3): the healing above depends on
        # a bad token being refused whatever verb it claims to carry.
        method = payload.get("method")
        params = payload.get("params")
        params = params if isinstance(params, dict) else {}
        if method == M_STEER:
            return self._on_steer(params)
        if method != M_RESOLVE:
            return {"ok": False, "error": ERR_BAD_REQUEST}

        handler = self._handler_provider()
        if handler is None:
            return {"ok": False, "error": ERR_NO_HANDLER}
        try:
            refusal = handler(
                gate_id=params.get("gate_id"),
                decision=params.get("decision"),
                discord_user_id=params.get("discord_user_id"),
            )
        except Exception:
            logger.debug("cp_discord: resolving a gate failed", exc_info=True)
            return {"ok": False, "error": ERR_BAD_REQUEST}
        answer: Dict[str, Any] = {"ok": True}
        if isinstance(refusal, str) and refusal:
            # The frame was TAKEN (hence ``ok``), but the click resolved
            # nothing -- an outsider, or a gate somebody already answered.
            # Carrying the reason back is what lets the clicker be told
            # (AC-39/74); ``ok: False`` would instead make the broker retry a
            # decision that has already been made, and finally declare the
            # session unreachable (AC-85d).
            answer["refusal"] = refusal
        return answer

    def _on_steer(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """One chat message on its way into this session (§4.3a).

        Six answers, and only ONE of them is ``ok: False``.  A frame that was
        TAKEN is acked even when it changed nothing -- the same rule
        ``M_RESOLVE`` follows below.  ``ok: False`` makes the broker retry for
        three seconds and then announce in the thread that the session is not
        answering: for a stranger's message that would be three wasted seconds,
        an untrue notice, and confirmation that the session is alive (INV-6).
        Two answers earn it: a missing handler (transient -- C5 may still be
        wiring up) and a handler that RAISED.  The second is retried too, and
        that is deliberate: we did not evaluate the message, so we must not
        ack it.  Acking would drop it silently; refusing costs three seconds
        on a path that only opens when ``steer_message`` itself is broken.
        """
        handler = self._steer_provider()
        if handler is None:
            return {"ok": False, "error": ERR_NO_HANDLER}

        message_id = params.get("message_id")
        if message_id is not None and message_id in self._seen_steers:
            # Delivered before; the answer was lost on the way back and the
            # broker is retrying.  Acknowledged like a delivery (§4.4b) --
            # the message DID arrive, and saying otherwise would be a lie.
            return {"ok": True, "steer": constants.STEER_DUPLICATE}

        try:
            result = handler(
                external_id=params.get("external_id"), text=params.get("text")
            )
        except Exception:
            # No text and no sender in the record: this may be an
            # unauthorized message, and its content must not reach a log
            # (``inbound.py:201``).
            logger.debug("cp_discord: steering a message failed", exc_info=True)
            return {"ok": False, "error": ERR_BAD_REQUEST}

        result = result if isinstance(result, dict) else {}
        answer: Dict[str, Any] = {
            "ok": True,
            "steer": str(result.get("steer") or constants.STEER_UNDELIVERED),
        }
        if not result.get("accepted"):
            # Not accepted, so nothing was steered: the id stays OUT of the
            # window.  Entering it before the verdict would answer a
            # stranger's retry with ``duplicate`` instead of ``refused`` --
            # and the broker reacts to ``duplicate``.  INV-6 would fall
            # through the deduplication.
            return answer
        if message_id is not None:
            self._seen_steers.append(message_id)
        if result.get("mode"):
            answer["mode"] = result["mode"]
        return answer

    def _refuse(self) -> None:
        try:
            self._on_refused()
        except Exception:
            logger.debug("cp_discord: the refusal hook failed", exc_info=True)


__all__: Sequence[str] = (
    "ERR_NO_HANDLER",
    "STEER_WINDOW",
    "InboundListener",
    "ResolutionHandler",
    "SteerHandler",
    "token_matches",
)
