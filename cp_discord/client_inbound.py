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
from typing import Any, Callable, Dict, Optional, Sequence

from .broker_election import LOOPBACK
from .broker_server import (
    ERR_BAD_REQUEST,
    ERR_UNAUTHORIZED,
    M_RESOLVE,
    MAX_FRAME_BYTES,
    SOCKET_TIMEOUT,
)

logger = logging.getLogger(__name__)

#: Refused because nothing is there to take the resolution.  Deliberately NOT
#: an ack: an ack means "taken", and acking with nowhere to put it would lose
#: the click while telling the broker it landed.
ERR_NO_HANDLER = "no_handler"

#: The handler signature C4 implements (§3.2a).  ``discord_user_id`` travels
#: with the decision because the APPROVER check happens in the SESSION, which
#: owns the authorization database -- not in the broker.
ResolutionHandler = Callable[..., Any]


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
    session with a rotated token look at the portfile again), and
    *handler_provider* is consulted per frame because C4 installs its handler
    after the socket is already up.
    """

    def __init__(
        self,
        *,
        authorize: Callable[[Any], bool],
        on_refused: Callable[[], None],
        handler_provider: Callable[[], Optional[ResolutionHandler]],
    ) -> None:
        self._authorize = authorize
        self._on_refused = on_refused
        self._handler_provider = handler_provider
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
        while not self._stop.is_set():
            try:
                connection, _peer = sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            try:
                self._handle_connection(connection)
            except Exception:
                logger.debug("cp_discord: an inbound frame failed", exc_info=True)
            finally:
                try:
                    connection.close()
                except OSError:
                    pass

    def _handle_connection(self, connection: socket.socket) -> None:
        connection.settimeout(SOCKET_TIMEOUT)
        with connection.makefile("rwb") as stream:
            line = stream.readline(MAX_FRAME_BYTES)
            if not line:
                return
            response = self.dispatch(line)
            stream.write((json.dumps(response) + "\n").encode("utf-8"))
            stream.flush()

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

        if payload.get("method") != M_RESOLVE:
            return {"ok": False, "error": ERR_BAD_REQUEST}
        params = payload.get("params")
        params = params if isinstance(params, dict) else {}

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

    def _refuse(self) -> None:
        try:
            self._on_refused()
        except Exception:
            logger.debug("cp_discord: the refusal hook failed", exc_info=True)


__all__: Sequence[str] = (
    "ERR_NO_HANDLER",
    "InboundListener",
    "ResolutionHandler",
    "token_matches",
)
