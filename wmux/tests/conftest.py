"""Shared test harness for the wmux plugin.

Two jobs:

1. Make the plugin importable as a namespace package. It ships without an
   ``__init__.py`` because Code Puppy's user-tier loader imports
   ``register_callbacks.py`` directly by file location after putting the
   plugins directory on ``sys.path`` (``code_puppy/plugins/__init__.py``
   ``:124-127``, ``:288-292``). Tests reproduce that layout by putting the
   plugin's PARENT directory on ``sys.path``.
2. Hold the harness both suites need -- the ``_transport`` recorder, the
   real named-pipe server, and the foreign-thread lock probe.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_PLUGIN_PARENT = str(Path(__file__).resolve().parents[2])

if _PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, _PLUGIN_PARENT)


#: The MEASURED success reply for ``pane.report_agent`` against the live
#: wmux server (probed 2026-07-31 from inside a real pane). The ``state``
#: field is the server's own resolution and is echoed back on every report.
ACK = b'{"result":{"accepted":true,"state":"idle"},"id":1}\n'

#: The MEASURED seq-dedupe reply: a report at or below the last seq seen.
#: Definitive, and NOT a failure -- a retry must be a harmless no-op.
DEDUPE = b'{"result":{"accepted":false,"state":"idle"},"id":1}\n'

#: The MEASURED reply to a wrong or empty token. This is the reply that
#: makes the plugin structurally dead: every report is rejected, forever.
UNAUTHORIZED = (
    b'{"error":{"code":-32001,"message":"Unauthorized: missing or invalid '
    b'token"},"id":1}\n'
)


@pytest.fixture(autouse=True)
def _reset_warn_once_dedupe():
    """Re-arm the one-shot warnings around every test.

    The dedupe set is a module global by design (once per PROCESS, never
    once per report), so without this the FIRST test to trigger a warning
    would silence it for every later test -- and "exactly one warning"
    assertions would pass for the wrong reason.
    """
    from wmux.diagnostics import reset_warn_once

    reset_warn_once()
    yield
    reset_warn_once()


@pytest.fixture
def wmux_env(monkeypatch):
    """A minimal active-pane environment."""
    monkeypatch.setenv("WMUX", "1")
    monkeypatch.setenv("WMUX_SURFACE_ID", "surf-test")
    monkeypatch.setenv("WMUX_PIPE", r"\\.\pipe\wmux-test")
    monkeypatch.setenv("WMUX_PIPE_TOKEN", "tok-test")
    monkeypatch.delenv("WMUX_INSTANCE", raising=False)


class Wire:
    """Records every envelope a stubbed ``_transport`` was handed."""

    def __init__(self, replies: Optional[List[Optional[bytes]]] = None) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.raw: List[bytes] = []
        self.threads: List[int] = []
        self._replies = list(replies) if replies is not None else None
        self.gate: Optional[threading.Event] = None
        #: Set once the worker is parked inside a gated send, so a test can
        #: fill both lanes while the worker provably cannot drain them.
        self.entered = threading.Event()

    def __call__(self, pipe_path, payload, timeout_s):
        if self.gate is not None:
            self.entered.set()
            self.gate.wait(timeout=5.0)
        self.raw.append(payload)
        self.sent.append(json.loads(payload.decode("utf-8")))
        self.threads.append(threading.get_ident())
        if not self._replies:
            return ACK
        return self._replies.pop(0)

    def wait_for(self, count: int, timeout: float = 3.0) -> bool:
        return spin(lambda: len(self.sent) >= count, timeout)

    def params(self, index: int = -1) -> Dict[str, Any]:
        return self.sent[index]["params"]

    def methods(self) -> List[str]:
        return [e["method"] for e in self.sent]


@pytest.fixture
def wire(monkeypatch) -> Wire:
    from wmux import client as cl

    recorder = Wire()
    monkeypatch.setattr(cl, "_transport", recorder)
    return recorder


class PipeServer:
    """A real newline-framed named-pipe server on a daemon thread.

    The reply deadline lives INSIDE ``_transport``, so a stub could only
    prove the stub works -- the deadline has to face a real server.
    """

    def __init__(self, script: List[bytes], gap_s: float = 0.0) -> None:
        import _winapi

        self.path = rf"\\.\pipe\wmux-test-{uuid.uuid4().hex}"
        self._script = script
        self._gap_s = gap_s
        self._winapi = _winapi
        self.ready = threading.Event()
        #: Set once the client is actually connected and its request read.
        self.connected = threading.Event()
        self.error: Optional[BaseException] = None
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        w = self._winapi
        # Byte-type and byte-readmode are both 0 in the Win32 headers and
        # _winapi does not name them; PIPE_WAIT (also 0) spells the intent.
        handle = w.CreateNamedPipe(
            self.path,
            w.PIPE_ACCESS_DUPLEX,
            w.PIPE_WAIT,
            1,
            65536,
            65536,
            0,
            w.NULL,
        )
        self.ready.set()
        try:
            try:
                w.ConnectNamedPipe(handle, w.NULL)
            except OSError as exc:
                # ERROR_PIPE_CONNECTED means the client won the race between
                # CreateNamedPipe and ConnectNamedPipe. That is a documented
                # SUCCESS, not a failure -- treating it as one killed this
                # thread, closed the pipe, and made the client return early,
                # which looked exactly like a flaky deadline test.
                if exc.winerror != w.ERROR_PIPE_CONNECTED:
                    raise
            w.ReadFile(handle, 65536)
            self.connected.set()
            for chunk in self._script:
                if self._gap_s:
                    time.sleep(self._gap_s)
                w.WriteFile(handle, chunk)
            # Hold the pipe open so a silent or partial server really is
            # silent, rather than signalling EOF and rescuing a bad reader.
            time.sleep(5.0)
        except OSError as exc:
            self.error = exc
        finally:
            w.CloseHandle(handle)

    def __enter__(self) -> "PipeServer":
        self.thread.start()
        assert self.ready.wait(timeout=5.0)
        return self

    def __exit__(self, *_exc) -> None:
        return None


def _payload() -> bytes:
    """One framed request, for the tests that drive ``transport`` directly.

    Shared rather than duplicated: both the client suite (AC-8c, AC-42) and
    the wire suite (AC-49, AC-50) need a request the real pipe server can
    read before it replies.
    """
    return json.dumps({"method": "pane.agent_state", "params": {}, "id": 1}).encode()


def spin(predicate, timeout: float) -> bool:
    """Poll ``predicate`` until it holds or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def acquirable_from_another_thread(lock, timeout: float = 0.2) -> bool:
    """Return whether ``lock`` can be acquired from a FOREIGN thread.

    The foreign thread is not a detail -- it is the whole point.
    ``threading.Condition`` wraps an RLock, and a re-entrant acquire from
    the holding thread returns ``True`` unconditionally, so a self-check is
    an assertion that cannot fail. Works for ``Lock`` and ``Condition``
    alike: both expose ``acquire(blocking, timeout)``.
    """
    result: List[bool] = []

    def probe():
        acquired = lock.acquire(blocking=True, timeout=timeout)
        result.append(acquired)
        if acquired:
            lock.release()

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(timeout=2.0)
    assert result, "lock probe thread did not finish"
    return result[0]
