"""L1 — the wire itself: framing and the accept loop, shared by both ends.

The broker (:mod:`.broker_server`) and the session's return channel
(:mod:`.client_inbound`) are opposite ends of the SAME protocol -- TCP on
``127.0.0.1``, newline-delimited UTF-8 JSON, one connection per message.  Both
therefore grew the same three moves: read a length-bounded line, write an
answer, and run an accept loop that survives a transient failure.  They were
copies, and a copy is where two ends of one protocol drift apart: ``6a85ff6``
is the bug where one loop treated a transient ``accept`` failure as teardown
and the other did not.

**This module lives FLAT, next to its callers on purpose** (§3.1).
``deploy.ps1:234`` copies top-level ``*.py`` only; a sub-package would deploy
as silence, which is the failure mode :mod:`.broker_activation` warns about.

**It interprets nothing** (INV-1).  :func:`read_frame` answers "a line, or
nothing" -- not "a valid frame".  The JSON parse stays with the caller,
because what an unreadable reply MEANS differs per caller: for a session it is
a reason to retry, for the broker it is the difference between a refusal and a
dead session that gets its Discord thread archived (§3.2a).  A helper that
decided this for both would have to be wrong for one of them.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any, BinaryIO, Callable, Dict, Optional, Protocol, Sequence, Tuple

#: A frame longer than this is not a frame (defence against a local process
#: feeding an endless line to either end).
MAX_FRAME_BYTES = 1024 * 1024


class Acceptor(Protocol):
    """The ONLY thing :func:`serve_accept_loop` may need from a socket.

    Deliberately narrow: everything else -- ``settimeout``, ``close``, the
    ``None`` check on a broker's server socket -- stays with the caller, who
    owns the socket's lifetime.  The loop borrows it for one call.
    """

    def accept(self) -> Tuple[Any, Any]: ...


def read_frame(stream: BinaryIO) -> Optional[bytes]:
    """One frame off *stream*, or ``None`` when the peer said nothing.

    Length-bounded and nothing else: no decoding, no parse, no verdict on
    whether silence was a transport failure or a refusal (INV-1).
    """
    return stream.readline(MAX_FRAME_BYTES) or None


def write_frame(stream: BinaryIO, frame: Dict[str, Any]) -> None:
    """Encode *frame* and put it on the wire, flushed.

    Flushing is part of the contract, not a detail: the peer is blocked on a
    ``readline`` and a buffered answer reads exactly like no answer at all.
    """
    stream.write((json.dumps(frame) + "\n").encode("utf-8"))
    stream.flush()


def serve_accept_loop(
    sock: Acceptor,
    stop: threading.Event,
    handle: Callable[[Any], None],
    logger: Any,
) -> None:
    """Accept until *stop*, handing each connection to *handle*.

    *logger* is the CALLER's logger, not one of ours: the record has to say
    which end of the wire it came from, and a session's test raises the level
    for its own logger by name.
    """
    while not stop.is_set():
        try:
            connection, _peer = sock.accept()
        except (TimeoutError, socket.timeout):
            # Idle, not broken.  Kept ahead of ``OSError`` AND kept silent:
            # ``socket.timeout`` IS ``TimeoutError`` and a subclass of
            # ``OSError``, so folding the two together would file a traceback
            # twice a second and drown the failure below in its own noise.
            continue
        except OSError:
            # Closed underneath us (stop) or a transient accept failure.
            # Ending the loop on a transient one would leave the socket
            # bound and the port registered while nothing drains it: the
            # broker keeps delivering, reads a transport failure, and
            # finally archives the thread of a live session (AC-15).
            if stop.is_set():
                return
            logger.debug("cp_discord: an accept failed", exc_info=True)
            continue
        try:
            handle(connection)
        except Exception:
            logger.debug("cp_discord: a connection failed", exc_info=True)
        finally:
            try:
                connection.close()
            except OSError:
                pass


__all__: Sequence[str] = (
    "MAX_FRAME_BYTES",
    "Acceptor",
    "read_frame",
    "serve_accept_loop",
    "write_frame",
)
