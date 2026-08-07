"""L1 — the shared wire: framing, the accept loop, and the two ends' contract.

A new file rather than an addition to ``test_broker.py``/``test_client.py``:
Batch A's whole point is that the 517 existing tests stay UNTOUCHED, and a
test one edits to make a refactor pass is precisely the safety net the
refactor needed.

Three of these cover ground nothing covered before -- the frame limit had zero
references in the suite, and two of ``push_once``'s four exits were unreached
because the only session double in the tree always answers.
"""

from __future__ import annotations

import errno
import logging
import socket
import threading
import time
from pathlib import Path

import pytest

import cp_discord
from cp_discord import broker_gates, broker_server, client_inbound, wire


# --------------------------------------------------------------------------- #
# Framing (AC-A3)
# --------------------------------------------------------------------------- #


def test_a_frame_is_read_whole_and_an_oversized_one_is_cut(tmp_path):
    """The limit is a defence, not a hint: a local process can feed forever.

    Untested until now -- ``MAX_FRAME_BYTES`` had no reference anywhere in the
    suite, because no legitimate peer ever sends an oversized line.
    """
    path = tmp_path / "frames"
    path.write_bytes(b"x" * (wire.MAX_FRAME_BYTES + 512) + b"\n")

    with path.open("rb") as stream:
        oversized = wire.read_frame(stream)

    assert oversized is not None
    assert len(oversized) == wire.MAX_FRAME_BYTES

    path.write_bytes(b'{"ok": true}\n')
    with path.open("rb") as stream:
        assert wire.read_frame(stream) == b'{"ok": true}\n'
        assert wire.read_frame(stream) is None


# --------------------------------------------------------------------------- #
# The two unreached ``push_once`` exits (AC-A5 a+b)
# --------------------------------------------------------------------------- #


class OneShotListener:
    """A session listener that answers exactly *reply*, or nothing at all.

    It ALWAYS reads the request line to its end before closing.  That order is
    load-bearing: unread bytes in the receive buffer make ``close`` send an
    RST (the normal case on Windows), ``sendall`` then fails, and the caller
    lands on ``broker_gates.py:248`` -- a DIFFERENT exit that happens to
    return the same outcome, which would leave the branch under test unproven.
    """

    def __init__(self, reply: bytes | None) -> None:
        self._reply = reply
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self._server.settimeout(0.2)
        self.port = self._server.getsockname()[1]
        self.read_lines: list[bytes] = []
        self._stop = threading.Event()
        self._accepting = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        assert self._accepting.wait(5), "the listener thread never reached accept"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                self._accepting.set()
                connection, _peer = self._server.accept()
            except OSError:
                continue
            with connection, connection.makefile("rwb") as stream:
                self.read_lines.append(stream.readline())
                if self._reply is not None:
                    stream.write(self._reply)
                    stream.flush()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self._server.close()


@pytest.fixture
def one_shot():
    made = []

    def build(reply):
        listener = OneShotListener(reply)
        made.append(listener)
        return listener

    yield build
    for listener in made:
        listener.close()


def test_a_session_that_answers_nothing_is_a_transport_failure(one_shot):
    """Silence is the ONE outcome that may declare a session unreachable.

    ``TRANSPORT_FAILED`` feeds ``_unreachable`` and archives a Discord thread,
    so it must stay reachable only from real silence -- never from a reply we
    merely failed to understand.

    Both the arrival check and the CLOCK are load-bearing.  ``push_once`` has
    a second exit returning the identical outcome -- the ``OSError`` handler
    one line above -- and a starved listener reaches it by letting the read
    run out its ``SOCKET_TIMEOUT``.  The frame has arrived by then, so the
    arrival check alone does not tell the two apart, and unmutated code
    returns the same value either way: the test would pass while proving
    nothing.  A clean close answers at once; a timeout cannot.
    """
    listener = one_shot(None)

    started = time.monotonic()
    outcome, answer = broker_gates.push_once(listener.port, {"method": "resolve"})
    elapsed = time.monotonic() - started

    assert listener.read_lines and listener.read_lines[0], "the frame never arrived"
    assert elapsed < broker_gates.SOCKET_TIMEOUT, "the read timed out; wrong exit"
    assert outcome is broker_gates.TRANSPORT_FAILED
    assert answer is None


def test_a_session_that_answers_garbage_is_only_refused(one_shot):
    """An unreadable reply still PROVES somebody is there.

    Collapsing this into a transport failure would archive the thread of a
    live session; the reverse would leave a dead one undetected forever.
    """
    listener = one_shot(b"this is not json\n")

    outcome, answer = broker_gates.push_once(listener.port, {"method": "resolve"})

    assert listener.read_lines and listener.read_lines[0], "the frame never arrived"
    assert outcome is broker_gates.REFUSED
    assert answer is None


# --------------------------------------------------------------------------- #
# The accept loop (AC-A8, AC-A8c)
# --------------------------------------------------------------------------- #


class AbortsOnceThenCloses:
    """``accept`` fails transiently, then loses the race against ``stop``."""

    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop
        self.calls = 0

    def accept(self):
        self.calls += 1
        if self.calls == 1:
            raise OSError(errno.ECONNABORTED, "software caused connection abort")
        self._stop.set()  # what really happens: teardown, mid-accept
        raise OSError(errno.EBADF, "bad file descriptor")


class AlwaysTimesOut:
    """An idle listener: every ``accept`` runs out its 0.5 s budget."""

    def __init__(self, stop: threading.Event, rounds: int) -> None:
        self._stop = stop
        self._rounds = rounds
        self.calls = 0

    def accept(self):
        self.calls += 1
        if self.calls >= self._rounds:
            self._stop.set()
        raise socket.timeout("timed out")


@pytest.mark.parametrize(
    "module", [broker_server, client_inbound], ids=["broker", "inbound"]
)
def test_a_transient_accept_failure_is_logged_by_the_calling_module(module, caplog):
    """Both ends log it, and the record names the end it came from.

    The broker had no test for this at all -- ``6a85ff6`` aligned the two
    loops' LOGIC and left their diagnostics apart, so one half went dark.
    """
    stop = threading.Event()
    sock = AbortsOnceThenCloses(stop)

    with caplog.at_level(logging.DEBUG, logger=module.logger.name):
        wire.serve_accept_loop(sock, stop, lambda connection: None, module.logger)

    records = [record for record in caplog.records if record.name == module.__name__]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.DEBUG
    # NOT ``is OSError``: PEP 3151 turns ECONNABORTED into
    # ``ConnectionAbortedError``.  NOT ``issubclass(..., OSError)`` either --
    # that is satisfied by a timeout and would re-open the hole below.
    assert record.exc_info[0] is not None and not issubclass(
        record.exc_info[0], TimeoutError
    )


def test_an_idle_accept_loop_stays_silent(caplog):
    """A timeout is idleness, not a fault.

    ``socket.timeout`` IS ``TimeoutError`` and a subclass of ``OSError``, so a
    single merged handler would file a traceback twice a second at
    ``SOCKET_TIMEOUT = 0.5`` -- and a timeout record satisfies every condition
    the transient-failure assertion checks, disarming it in silence.
    """
    stop = threading.Event()
    sock = AlwaysTimesOut(stop, rounds=3)

    with caplog.at_level(logging.DEBUG, logger=client_inbound.logger.name):
        wire.serve_accept_loop(
            sock, stop, lambda connection: None, client_inbound.logger
        )

    assert sock.calls == 3
    assert [r for r in caplog.records if r.name.startswith("cp_discord")] == []


# --------------------------------------------------------------------------- #
# Placement and re-export (AC-A9b, AC-A10)
# --------------------------------------------------------------------------- #


def test_the_frame_limit_is_still_reachable_through_broker_server():
    """``broker_server`` stays the single import site for the wire.

    It lists ``MAX_FRAME_BYTES`` in its ``__all__``; moving the value without
    re-binding the name would leave that entry pointing at nothing.
    """
    assert broker_server.MAX_FRAME_BYTES is wire.MAX_FRAME_BYTES
    assert broker_gates.MAX_FRAME_BYTES is wire.MAX_FRAME_BYTES


def test_the_wire_module_lives_flat_in_the_plugin_root():
    """``deploy.ps1:234`` copies top-level ``*.py`` only.

    A sub-package would pass every test here and deploy as an ImportError on
    the target machine -- the failure mode ``broker_activation`` warns about.
    """
    assert (
        Path(wire.__file__).resolve().parent
        == Path(cp_discord.__file__).resolve().parent
    )
