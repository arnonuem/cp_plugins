"""Client transport, lanes, sweep tick, generation, release and activation.

Two levels, deliberately:

* **policy** -- envelope, seq, retry, lanes and the release budget go through
  the monkeypatched ``_transport`` seam (SPEC R-13);
* **mechanism** -- the reply DEADLINE lives inside ``_transport``, so a stub
  cannot test it. It is exercised against a REAL local named-pipe server
  built with ``_winapi.CreateNamedPipe`` + ``ConnectNamedPipe`` +
  ``WriteFile``.

Everything asserting client-internal structure (the critical slot, the
generation high-water mark, ``_closing``, ``_cond``) lives here rather than
in the reporter suite, whose FakeClient has none of them.

Covers AC-0, AC-1..AC-9b, AC-14k..AC-14n, AC-14l, AC-17, AC-30..AC-32b,
AC-33c and AC-37..AC-40.
"""

from __future__ import annotations

import ctypes
import json
import os.path
import sys
import threading
import time
import uuid
from typing import List

import pytest
from conftest import (
    ACK,
    DEDUPE,
    UNAUTHORIZED,
    PipeServer,
    Wire,
    _payload,
    acquirable_from_another_thread,
    spin,
)

from wmux import client as cl
from wmux import wire as wire_mod
from wmux.client import DEFAULT_PIPE, WmuxClient

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"), reason="wmux is a Windows named-pipe protocol"
)


# --- activation (AC-1..AC-3b, AC-40) ---------------------------------------


def test_ac1_no_wmux_env_means_no_client_and_no_callbacks(monkeypatch):
    for var in ("WMUX", "WMUX_SURFACE_ID", "WMUX_PIPE", "WMUX_PIPE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    client = WmuxClient()
    assert client.active is False
    assert client._worker is None

    import importlib

    from code_puppy import callbacks

    import wmux.register_callbacks as rc

    def counts():
        return {
            phase: len(handlers) for phase, handlers in callbacks._callbacks.items()
        }

    before = counts()
    importlib.reload(rc)
    assert rc._reporter.active is False
    assert counts() == before, "an inactive plugin must register ZERO callbacks"


def test_ac2_full_env_activates(wmux_env):
    client = WmuxClient()
    assert client.active is True
    assert client._worker is not None and client._worker.daemon


@pytest.mark.parametrize("missing", ["WMUX", "WMUX_SURFACE_ID"])
def test_ac3_either_var_missing_deactivates(wmux_env, monkeypatch, missing):
    monkeypatch.delenv(missing, raising=False)
    assert WmuxClient().active is False


def test_ac40_pipe_var_is_exempt_and_defaults(wmux_env, monkeypatch):
    monkeypatch.delenv("WMUX_PIPE", raising=False)
    client = WmuxClient()
    assert client.active is True
    assert client._pipe == DEFAULT_PIPE


def test_ac3b_inactive_client_is_inert(monkeypatch, wire):
    monkeypatch.delenv("WMUX", raising=False)
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    client.report_session("s")
    client.report_metadata(model="m", tokens="1k/2k", context_pct=5)
    client.report_activity(tool="t")
    client.release_and_close(timeout_s=0.1)
    assert wire.sent == []


# --- envelope / seq / retry (AC-4..AC-9b) ----------------------------------


def test_ac4_envelope_is_well_formed(wmux_env, wire):
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 2}, 1)
    assert wire.wait_for(1)
    envelope = wire.sent[0]
    assert envelope["method"] == "pane.report_agent"
    assert envelope["id"] == 1
    assert envelope["token"] == "tok-test"
    assert envelope["params"]["surfaceId"] == "surf-test"
    assert isinstance(envelope["params"]["seq"], int)
    assert envelope["params"]["runDepth"] == 2
    assert wire.raw[0].endswith(b"\n")


def test_ac5_seq_strictly_increases(wmux_env, wire):
    client = WmuxClient()
    for gen in range(1, 6):
        client.report_state({"awaitingHuman": False, "runDepth": gen}, gen)
        assert wire.wait_for(gen)
    seqs = [e["params"]["seq"] for e in wire.sent]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_ac5b_seq_seed_is_wall_clock_derived(wmux_env, wire):
    floor = int(time.time() * 1000) * 1000
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert wire.wait_for(1)
    assert wire.params(0)["seq"] > floor


def test_ac5c_seq_is_stamped_at_wire_time_not_enqueue(wmux_env, wire):
    wire.gate = threading.Event()
    client = WmuxClient()
    # Park the worker inside a gated send so both lanes fill while it waits.
    client.report_metadata(tokens="1k/2k")
    assert wire.entered.wait(timeout=3.0)
    client.report_metadata(tokens="2k/4k")  # decorative, enqueued FIRST
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)  # critical
    wire.gate.set()
    assert wire.wait_for(3)
    methods = wire.methods()[1:]
    assert methods == ["pane.report_agent", "pane.report_metadata"]
    seqs = [e["params"]["seq"] for e in wire.sent[1:]]
    assert seqs == sorted(seqs), "decorative report must carry the HIGHER seq"


def test_ac6_no_reply_retries_the_identical_envelope(wmux_env, monkeypatch):
    monkeypatch.setattr(cl, "_SEND_BACKOFF_S", 0.001)
    wire = Wire(replies=[None, None, ACK])
    monkeypatch.setattr(cl, "_transport", wire)
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert wire.wait_for(3)
    assert len({payload for payload in wire.raw}) == 1


def test_ac7_accepted_false_is_definitive_and_not_retried(wmux_env, monkeypatch):
    monkeypatch.setattr(cl, "_SEND_BACKOFF_S", 0.001)
    wire = Wire(replies=[DEDUPE])
    monkeypatch.setattr(cl, "_transport", wire)
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert wire.wait_for(1)
    time.sleep(0.1)
    assert len(wire.sent) == 1


# --- F3: the reply is PARSED (AC-41..AC-41d) -------------------------------
#
# Every reply shape below was MEASURED against the live wmux server from
# inside a real pane on 2026-07-31 -- none is invented. See the SPEC R-4
# table for the full capture.


def test_ac41_an_error_reply_is_a_failure_and_is_retried(wmux_env, monkeypatch):
    """An ``error`` reply must NOT count as delivered.

    Measured: a wrong or empty token answers
    ``{"error":{"code":-32001,"message":"Unauthorized: ..."}}``. Treating any
    reply line as success made that indistinguishable from an ack -- the
    plugin reports into the void while the pane shows the LAST state it
    managed to set, confidently and wrongly.
    """
    monkeypatch.setattr(cl, "_SEND_BACKOFF_S", 0.001)
    wire = Wire(replies=[UNAUTHORIZED, UNAUTHORIZED, UNAUTHORIZED])
    monkeypatch.setattr(cl, "_transport", wire)
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert wire.wait_for(cl._SEND_ATTEMPTS)
    time.sleep(0.05)
    assert len(wire.sent) == cl._SEND_ATTEMPTS, (
        "an error reply must be retried like a missing reply, not accepted"
    )


def test_ac41b_unauthorized_warns_exactly_once_per_process(
    wmux_env, monkeypatch, caplog
):
    """Structural death gets ONE warning -- a debug log would be invisible.

    Measured: core installs no logging config, so lastResort applies at a
    fixed WARNING and every ``logger.debug`` in this plugin is discarded.
    A stale-but-present token sails past the empty-token guard, so this is
    the ONLY observable that the plugin is dead.
    """
    monkeypatch.setattr(cl, "_SEND_BACKOFF_S", 0.001)
    wire = Wire(replies=[UNAUTHORIZED] * 12)
    monkeypatch.setattr(cl, "_transport", wire)
    with caplog.at_level("DEBUG"):
        client = WmuxClient()
        client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
        assert wire.wait_for(cl._SEND_ATTEMPTS)
        # A SECOND report: the warning must not repeat, or a persistently
        # bad token spams the user's terminal for the life of the process.
        client.report_state({"awaitingHuman": False, "runDepth": 2}, 2)
        assert wire.wait_for(cl._SEND_ATTEMPTS * 2)
        time.sleep(0.05)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    rejected = [r for r in warnings if "rejected" in r.getMessage().lower()]
    assert len(rejected) == 1, f"expected exactly one warning, got {len(rejected)}"
    assert "unauthorized" in rejected[0].getMessage().lower()


def test_ac48_a_hostile_server_message_is_sanitized_before_it_is_logged(
    wmux_env, monkeypatch, caplog
):
    r"""SECURITY (G3): the server's free text reaches a TRUSTED terminal.

    Measured: a reply of
    ``{"error":{"message":"\u001b[2J\u001b[HFAKE OUTPUT","code":1}}``
    produced the detail ``'\x1b[2J\x1b[HFAKE OUTPUT (code 1)'``, and a
    5000-char message passed through at full length. So a pipe squatter can
    clear the developer's screen or repaint fake output in a terminal they
    trust. ``warn_once`` caps the COUNT at one, not the SIZE -- and a
    terminal escape needs exactly one shot.

    The CODE is the actionable part; the free text is decoration.
    """
    monkeypatch.setattr(cl, "_SEND_BACKOFF_S", 0.001)
    hostile = (
        b'{"error":{"message":"\\u001b[2J\\u001b[HFAKE OUTPUT'
        + b"A" * 5000
        + b'","code":1},"id":1}\n'
    )
    wire = Wire(replies=[hostile] * 6)
    monkeypatch.setattr(cl, "_transport", wire)
    with caplog.at_level("DEBUG"):
        client = WmuxClient()
        client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
        assert wire.wait_for(cl._SEND_ATTEMPTS)
        time.sleep(0.05)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged, "precondition: the rejection must be logged at all"
    assert "\x1b" not in logged, "an ANSI escape must never reach the terminal"
    assert "FAKE OUTPUT" not in logged or "\x1b" not in logged
    assert "A" * 200 not in logged, "a 5000-char message must be truncated"
    # The code survives sanitising: it is the part an operator can act on.
    assert "code 1" in logged


@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param("plain text", "plain text", id="passthrough"),
        pytest.param("\x1b[2Jwiped", "\\x1b[2Jwiped", id="ansi-escaped-not-dropped"),
        pytest.param("a\nb", "a\\x0ab", id="newline-cannot-forge-a-log-line"),
        pytest.param("a\x00b", "a\\x00b", id="nul"),
        pytest.param("a\rb", "a\\x0db", id="carriage-return-cannot-overwrite"),
        pytest.param("", "", id="empty"),
    ],
)
def test_ac48_sanitize_escapes_rather_than_silently_dropping(raw, expected):
    """Non-printables are ESCAPED, not deleted.

    Deleting them would let ``\\x1b[2J`` read back as the innocent ``[2J``
    and hide that anything hostile ever arrived. ``\\r`` and ``\\n`` are in
    here specifically: either one alone lets a squatter forge what looks
    like a separate, trustworthy log line.
    """
    from wmux.diagnostics import sanitize_for_log

    assert sanitize_for_log(raw) == expected


def test_ac48_sanitize_truncates_and_says_so():
    """Truncation is VISIBLE, so a reader knows text was removed."""
    from wmux.diagnostics import MAX_LOG_DETAIL, sanitize_for_log

    out = sanitize_for_log("x" * 5000)
    assert len(out) <= MAX_LOG_DETAIL + 3
    assert out.endswith("...")


def test_ac41c_seq_dedupe_stays_definitive_and_never_warns(
    wmux_env, monkeypatch, caplog
):
    """``accepted:false`` from the seq gate is SUCCESS, not failure.

    Measured against the server source: a report losing the seq dedupe race
    answers ``{accepted:false}`` "rather than erroring -- a client retry must
    be a harmless no-op". Retrying it would be wrong, and warning about it
    would fire on every ordinary retry.
    """
    monkeypatch.setattr(cl, "_SEND_BACKOFF_S", 0.001)
    wire = Wire(replies=[DEDUPE, DEDUPE, DEDUPE])
    monkeypatch.setattr(cl, "_transport", wire)
    with caplog.at_level("WARNING"):
        client = WmuxClient()
        client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
        assert wire.wait_for(1)
        time.sleep(0.1)
    assert len(wire.sent) == 1, "a deduped report must not be retried"
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(b"not json at all\n", id="garbage"),
        pytest.param(b"\n", id="empty-line"),
        pytest.param(b'{"result":{}}\n', id="result-without-accepted"),
        pytest.param(b'"a bare string"\n', id="non-object"),
    ],
)
def test_ac41d_an_unparseable_reply_is_definitive_not_a_crash(
    wmux_env, monkeypatch, reply
):
    """An unrecognised reply is treated as delivered, and never raises.

    The server answered SOMETHING, so the request provably arrived; retrying
    a request that landed is worse than trusting it. What must never happen
    is an exception on the worker thread -- that kills delivery entirely.
    """
    monkeypatch.setattr(cl, "_SEND_BACKOFF_S", 0.001)
    wire = Wire(replies=[reply, reply, reply])
    monkeypatch.setattr(cl, "_transport", wire)
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert wire.wait_for(1)
    time.sleep(0.1)
    assert len(wire.sent) == 1
    assert client._worker.is_alive()


def test_ac41e_release_and_activity_replies_are_accepted(wmux_env, monkeypatch):
    """The non-``accepted`` success shapes must not be read as failures.

    Measured: ``pane.release_agent`` answers ``{"result":{"released":true}}``
    and ``agent.activity`` answers ``{"result":{"ok":true}}`` -- neither
    carries an ``accepted`` key at all. A parser keying only on ``accepted``
    would retry both three times on every single call.
    """
    monkeypatch.setattr(cl, "_SEND_BACKOFF_S", 0.001)
    wire = Wire(
        replies=[b'{"result":{"ok":true},"id":1}\n']
        + [b'{"result":{"released":true},"id":1}\n']
    )
    monkeypatch.setattr(cl, "_transport", wire)
    client = WmuxClient()
    client.report_activity(tool="probe")
    assert wire.wait_for(1)
    client.release_and_close(timeout_s=2.0)
    assert wire.methods() == ["agent.activity", "pane.release_agent"], (
        "a success shape without an 'accepted' key must not be retried"
    )


def test_ac8_transport_raising_never_propagates(wmux_env, monkeypatch):
    monkeypatch.setattr(cl, "_SEND_BACKOFF_S", 0.001)
    calls: List[int] = []

    def boom(pipe_path, payload, timeout_s):
        calls.append(1)
        raise OSError("pipe gone")

    monkeypatch.setattr(cl, "_transport", boom)
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert spin(lambda: len(calls) >= cl._SEND_ATTEMPTS, 3.0)
    # The worker survived and still delivers.
    assert client._worker.is_alive()


def test_ac8b_timeout_stub_triggers_the_retry_path(wmux_env, monkeypatch):
    monkeypatch.setattr(cl, "_SEND_BACKOFF_S", 0.001)
    wire = Wire(replies=[None, None, None])
    monkeypatch.setattr(cl, "_transport", wire)
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert wire.wait_for(cl._SEND_ATTEMPTS)
    time.sleep(0.05)
    assert len(wire.sent) == cl._SEND_ATTEMPTS


def test_ac9_pipe_io_happens_on_the_worker_thread(wmux_env, wire):
    client = WmuxClient()
    caller = threading.get_ident()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert wire.wait_for(1)
    assert wire.threads[0] != caller
    assert wire.threads[0] == client._worker.ident


def test_ac9b_run_depth_is_absolute_and_run_delta_never_sent(wmux_env, wire):
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 3}, 1)
    client.report_session("s")
    client.report_metadata(tokens="1k/2k", context_pct=4)
    assert wire.wait_for(3)
    assert wire.sent[0]["params"]["runDepth"] == 3
    assert all("runDelta" not in e["params"] for e in wire.sent)


def test_activity_is_sent_without_a_seq_and_without_a_message(wmux_env, wire):
    client = WmuxClient()
    client.report_activity(tool="read_file")
    assert wire.wait_for(1)
    # Drained before the next one, because the decorative slot is
    # latest-wins by design: a superseded activity is meant to be dropped.
    client.report_activity(done=True)
    assert wire.wait_for(2)
    assert wire.methods() == ["agent.activity", "agent.activity"]
    assert wire.sent[0]["params"] == {"surfaceId": "surf-test", "tool": "read_file"}
    assert wire.sent[1]["params"] == {"surfaceId": "surf-test", "done": True}
    assert all("message" not in e["params"] for e in wire.sent)


def test_decorative_activity_coalesces_latest_wins(wmux_env, wire):
    wire.gate = threading.Event()
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)  # occupies worker
    assert wire.entered.wait(timeout=3.0)
    client.report_activity(tool="read_file")
    client.report_activity(done=True)
    assert client._activity == {"done": True}
    wire.gate.set()
    assert wire.wait_for(2)
    time.sleep(0.05)
    assert wire.methods() == ["pane.report_agent", "agent.activity"]


def test_ac0_startup_claim_reaches_the_wire(wmux_env, wire):
    """The startup claim reaches the WIRE even from a SETTLED idle state.

    Driving the reporter to a settled idle payload first is load-bearing:
    against a pristine reporter ``_last_payload`` is ``None``, so the
    ``payload == self._last_payload`` short-circuit can never fire and
    ``force=True`` is a no-op the test cannot see. Mutant M8 (delete the
    ``force`` argument entirely) survived the earlier version of this test.
    """
    from wmux.reporter import WmuxReporter

    client = WmuxClient()
    reporter = WmuxReporter(client)
    # Settle on idle THROUGH the state machine, so _last_payload holds the
    # very payload the startup claim will recompute. Each edge is drained
    # before the next is fired: the critical slot is a coalescing
    # latest-wins slot BY DESIGN, so a synchronous burst may legitimately
    # collapse the two edges into one send.
    reporter.on_run_start("g-settle")
    assert wire.wait_for(1)
    reporter.on_run_terminal("g-settle")
    assert wire.wait_for(2)
    assert wire.params(-1)["runDepth"] == 0
    settled = len(wire.sent)

    reporter.on_startup()

    assert wire.wait_for(settled + 1), (
        "the startup claim must reach the wire even though the local state "
        "is ALREADY idle -- otherwise a crashed predecessor's ghost stands"
    )
    assert wire.params(-1)["runDepth"] == 0
    assert wire.params(-1)["awaitingHuman"] is False


# --- F1: PeekNamedPipe is argtyped and ArgumentError is caught (AC-42) -----


def test_ac42_an_oversized_handle_never_escapes_as_argument_error(monkeypatch):
    """``ctypes.ArgumentError`` must not escape ``_transport``.

    Measured: its MRO is ``[ArgumentError, Exception, BaseException,
    object]`` -- it is NOT an ``OSError`` nor a ``ValueError``, so the
    original ``except (OSError, ValueError)`` did not catch it. One
    oversized handle would make EVERY send raise, degrade to "no reply",
    retry three times and die into a discarded debug log: the plugin 100%
    dead with zero symptom.
    """
    import msvcrt

    monkeypatch.setattr(msvcrt, "get_osfhandle", lambda _fd: 2**64 + 7)
    with PipeServer(script=[]) as server:
        # Must return None (the documented "no definitive answer"), never
        # raise, and never hang.
        started = time.monotonic()
        reply = cl._transport(server.path, _payload(), timeout_s=0.4)
        elapsed = time.monotonic() - started
    assert reply is None
    assert elapsed < 2.0


def test_ac42_argument_error_is_caught_by_the_except_tuple(monkeypatch):
    """``ctypes.ArgumentError`` is caught, not merely made unreachable.

    AC-42 proves the argtypes stop the CURRENT trigger; this proves the
    except tuple would still contain the failure if any other marshalling
    mismatch reached it. Both halves are needed: with only the argtypes
    test, deleting ``ctypes.ArgumentError`` from the except tuple changes
    nothing observable and the mutant survives (measured: M1 did).

    ArgumentError is raised DIRECTLY rather than provoked, because the
    argtypes now prevent the natural provocation -- which is the point.
    """

    class Exploding:
        def PeekNamedPipe(self, *_args):
            raise ctypes.ArgumentError("argument 1: OverflowError: int too long")

    monkeypatch.setattr(wire_mod, "_kernel32", Exploding)
    with PipeServer(script=[]) as server:
        reply = cl._transport(server.path, _payload(), timeout_s=0.4)
    assert reply is None, "ArgumentError must degrade to 'no reply', not raise"


def test_ac42b_peek_named_pipe_is_argtyped_on_a_private_handle():
    """The argtypes live on a PRIVATE WinDLL, not the shared cache.

    ``ctypes.windll.kernel32`` is a process-wide cached object. Setting
    ``argtypes`` on it would silently reconfigure the SAME function object
    that ``code_puppy/tools/command_runner.py:60`` holds, so this plugin
    must own its own handle.
    """
    peek = wire_mod._kernel32().PeekNamedPipe
    assert peek.argtypes is not None, "PeekNamedPipe must be argtyped"
    assert len(peek.argtypes) == 6
    assert getattr(ctypes.windll.kernel32.PeekNamedPipe, "argtypes", None) is None, (
        "the SHARED windll cache must not be mutated by this plugin"
    )


# --- F2: the pipe path and instance name are validated (AC-43..AC-43c) -----


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(r"C:\Users\me\victim.txt", id="regular-file"),
        pytest.param(r"\\.\pipe\..\..\C:\Users\me\victim.txt", id="traversal"),
        pytest.param(r"\\server\share\secrets.txt", id="unc-share"),
        pytest.param(r"\\.\C:\Users\me\victim.txt", id="device-path"),
        pytest.param("/tmp/victim", id="posix"),
        pytest.param(r"\\.\pipe\a\b", id="nested-segment"),
    ],
)
def test_ac43_a_non_pipe_wmux_pipe_deactivates_the_plugin(monkeypatch, wire, path):
    """A SECURITY fix: the envelope carries the auth token.

    Measured: ``open(path, "r+b")`` on a REGULAR FILE succeeds and writes in
    place -- the victim file's content was destroyed and
    ``"token":"SECRET-TOKEN-abc123"`` was left on disk in cleartext. So an
    unvalidated ``WMUX_PIPE`` writes a live credential to an
    attacker-chosen path. Reject, and DEACTIVATE rather than open it.
    """
    monkeypatch.setenv("WMUX", "1")
    monkeypatch.setenv("WMUX_SURFACE_ID", "surf-test")
    monkeypatch.setenv("WMUX_PIPE_TOKEN", "SECRET-TOKEN-abc123")
    monkeypatch.setenv("WMUX_PIPE", path)
    client = WmuxClient()
    assert client.active is False, f"{path!r} must not activate the plugin"
    assert client._worker is None
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    time.sleep(0.05)
    assert wire.sent == [], "a rejected pipe path must never be written to"


def test_ac43_an_empty_pipe_var_defaults_rather_than_deactivating(monkeypatch, wire):
    """``WMUX_PIPE=""`` is UNSET, not hostile -- it must default (AC-40).

    Deactivating here would be a regression: an empty env var is
    indistinguishable from an absent one, and SPEC R-1 makes ``WMUX_PIPE``
    explicitly exempt from the activation guard precisely so a pane with
    ``WMUX=1`` but no explicit pipe var is not left dead.
    """
    monkeypatch.setenv("WMUX", "1")
    monkeypatch.setenv("WMUX_SURFACE_ID", "surf-test")
    monkeypatch.setenv("WMUX_PIPE_TOKEN", "tok")
    monkeypatch.setenv("WMUX_PIPE", "")
    client = WmuxClient()
    assert client.active is True
    assert client._pipe == DEFAULT_PIPE


def test_ac43_a_rejected_pipe_path_warns_exactly_once(monkeypatch, caplog):
    monkeypatch.setenv("WMUX", "1")
    monkeypatch.setenv("WMUX_SURFACE_ID", "surf-test")
    monkeypatch.setenv("WMUX_PIPE_TOKEN", "SECRET-TOKEN-abc123")
    monkeypatch.setenv("WMUX_PIPE", r"C:\Users\me\victim.txt")
    with caplog.at_level("WARNING"):
        WmuxClient()
        WmuxClient()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    # The token must never appear in the diagnostic that reports its danger.
    assert "SECRET-TOKEN-abc123" not in warnings[0].getMessage()


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(r"\\.\pipe\wmux", id="default"),
        pytest.param(r"\\.\pipe\wmux-test-abc123", id="instance-suffixed"),
    ],
)
def test_ac43b_a_real_pipe_path_still_activates(monkeypatch, wire, path):
    """The guard must not break the paths wmux actually injects.

    NARROWED in fix round 2 (G1): a ``\\\\<host>\\pipe\\wmux`` case used to
    live here, on the assumption that a remote pipe was a legitimate wmux
    form. It is not one -- wmux emits only the local device path -- and
    accepting it meant an implicit NTLM handshake with whoever answered.
    The remote forms are now asserted REJECTED in
    ``test_wmux_wire.py::test_ac46_anything_but_the_local_device_path_is_rejected``.
    """
    monkeypatch.setenv("WMUX", "1")
    monkeypatch.setenv("WMUX_SURFACE_ID", "surf-test")
    monkeypatch.setenv("WMUX_PIPE_TOKEN", "tok")
    monkeypatch.setenv("WMUX_PIPE", path)
    client = WmuxClient()
    assert client.active is True
    assert client._pipe == path


@pytest.mark.parametrize(
    "instance",
    [
        pytest.param("../../../x", id="posix-traversal"),
        pytest.param(r"..\..\x", id="windows-traversal"),
        pytest.param("C:", id="drive-letter"),
        pytest.param(r"C:\Users\me", id="absolute"),
        pytest.param("a/b", id="forward-slash"),
        pytest.param("..", id="dotdot"),
    ],
)
def test_ac43c_a_hostile_wmux_instance_never_escapes_appdata(
    monkeypatch, tmp_path, instance
):
    """``WMUX_INSTANCE`` is interpolated into a path and must be sanitized.

    ``os.path.join(base, f"wmux-{instance}", "pipe-token")`` with
    ``instance='../../../x'`` escapes ``%APPDATA%`` entirely -- and an
    absolute value (``C:\\...``) makes ``join`` DISCARD the base outright.
    """
    monkeypatch.delenv("WMUX_PIPE_TOKEN", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("WMUX_INSTANCE", instance)
    token, from_file = cl.resolve_token()
    assert (token, from_file) == ("", False), (
        "a hostile WMUX_INSTANCE must resolve no token at all"
    )


def test_ac43c_a_hostile_instance_cannot_read_an_escaped_file(monkeypatch, tmp_path):
    """Falsifiable end: PLANT the file the traversal would actually reach.

    Without a real file at the escaped location the AC-43c assertion passes
    for the wrong reason -- ``resolve_token`` returns ``("", False)`` on a
    plain ``FileNotFoundError`` too. Removing the sanitiser then changes
    nothing observable, and the mutant survives (measured: M2b did).

    The traversal depth is MEASURED, not assumed: the instance is
    CONCATENATED into the directory name (``wmux-<instance>``), which costs
    one extra level, so THREE ``..`` segments are needed to leave
    ``%APPDATA%`` -- two land back inside it.
    """
    appdata = tmp_path / "a" / "b" / "roaming"
    (appdata / "wmux").mkdir(parents=True)

    monkeypatch.delenv("WMUX_PIPE_TOKEN", raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))

    # Build the file AT the location the traversal actually resolves to,
    # rather than where it looks like it should: `wmux-..` consumes one of
    # the `..` segments itself, so this lands beside %APPDATA%, not above
    # its grandparent. Derived from the path, never assumed.
    escape = os.path.join("..", "..", "..", "stolen")
    reached = os.path.join(str(appdata), f"wmux-{escape}", "pipe-token")
    resolved = os.path.normpath(reached)
    assert not resolved.lower().startswith(str(appdata).lower() + os.sep), (
        f"precondition: {resolved} must lie OUTSIDE %APPDATA%"
    )
    os.makedirs(os.path.dirname(resolved))
    with open(resolved, "w", encoding="utf-8") as handle:
        handle.write("STOLEN-TOKEN")

    # The traversal provably REACHES a real, readable file: Windows resolves
    # the `..` segments lexically, so the non-existent `wmux-..` directory
    # does not save us. Without the sanitiser this is what would be opened.
    with open(reached, "r", encoding="utf-8") as handle:
        assert handle.read() == "STOLEN-TOKEN", "probe misconfigured"

    monkeypatch.setenv("WMUX_INSTANCE", escape)
    assert cl.resolve_token() == ("", False), "the escaped token file must NOT be read"

    # And a legitimate instance name still resolves normally, so the guard
    # is not simply breaking the feature.
    (appdata / "wmux-dev").mkdir()
    (appdata / "wmux-dev" / "pipe-token").write_text("ok-token", encoding="utf-8")
    monkeypatch.setenv("WMUX_INSTANCE", "dev")
    assert cl.resolve_token() == ("ok-token", True)


def test_ac52_a_non_utf8_token_file_never_raises_out_of_resolve_token(
    monkeypatch, tmp_path
):
    """A corrupt token FILE is a fail-soft, not an import-time crash.

    ``resolve_token`` caught only ``OSError``, but ``open(..., \"r\",
    encoding=\"utf-8\")`` raises ``UnicodeDecodeError`` on undecodable
    bytes -- and that is NOT an ``OSError`` (its MRO is
    ``[UnicodeDecodeError, UnicodeError, ValueError, ...]``). It is read
    during ``WmuxClient()`` construction, i.e. at plugin import, so it took
    the whole plugin down rather than degrading to "no token".
    """
    monkeypatch.delenv("WMUX_PIPE_TOKEN", raising=False)
    monkeypatch.delenv("WMUX_INSTANCE", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    (tmp_path / "wmux").mkdir()
    # Lone UTF-16 surrogate bytes: undecodable as UTF-8 under `strict`.
    (tmp_path / "wmux" / "pipe-token").write_bytes(b"\xff\xfe t \x00 o \x00 k")

    assert cl.resolve_token() == ("", False)

    # And the client that reads it still constructs, warning rather than
    # dying -- the whole point of the fail-soft.
    monkeypatch.setenv("WMUX", "1")
    monkeypatch.setenv("WMUX_SURFACE_ID", "surf-test")
    monkeypatch.setenv("WMUX_PIPE", r"\\.\pipe\wmux-test")
    assert WmuxClient().active is True


# --- F7: the worker's _send is guarded too (AC-44) -------------------------


def test_ac44_worker_survives_a_raising_send(wmux_env, monkeypatch, wire):
    """A raising ``_send`` must not kill the worker.

    The idle hook eight lines above WAS guarded, with a comment naming the
    exact consequence; ``_send`` was not. A dead worker is unrecoverable and
    invisible: later reports queue with no consumer and even the release
    never goes out.
    """
    client = WmuxClient()
    calls: List[int] = []
    real_send = client._send

    def exploding_send(method, params):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("send exploded")
        return real_send(method, params)

    client._send = exploding_send
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert spin(lambda: calls, 3.0)
    time.sleep(0.05)
    assert client._worker.is_alive(), "a raising _send must not kill the worker"
    # And the client still delivers afterwards.
    client.report_state({"awaitingHuman": False, "runDepth": 2}, 2)
    assert wire.wait_for(1)


def test_ac44b_a_raising_send_still_releases(wmux_env, monkeypatch, wire):
    """The release must go out even after a send raised.

    This is the half that makes the failure UNRECOVERABLE rather than merely
    lossy: without the guard the worker is gone, so ``release_and_close``
    waits out its whole timeout and the pane keeps a ghost forever.
    """
    client = WmuxClient()
    real_send = client._send
    exploded = threading.Event()

    def exploding_send(method, params):
        if method == "pane.report_agent" and not exploded.is_set():
            exploded.set()
            raise RuntimeError("send exploded")
        return real_send(method, params)

    client._send = exploding_send
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert exploded.wait(timeout=3.0)
    client.release_and_close(timeout_s=2.0)
    assert client._released.is_set()
    assert wire.methods().count("pane.release_agent") == 1


def test_ac44c_a_raising_send_warns_exactly_once_per_process(
    wmux_env, monkeypatch, wire, caplog
):
    """The lost report must be VISIBLE -- SPEC R-6b names the key.

    AC-44/AC-44b prove the worker survives; neither proves the user is ever
    told. A debug log here is discarded outright (R-6b), so a silently
    dropped report is exactly the zero-symptom failure this plugin exists to
    eliminate. Asserted across TWO raising sends so a per-report warning
    (which would bury the terminal) fails the test just as a missing one
    does. Mutant M11 (delete the ``warn_once``) survived the whole suite
    until this AC existed.
    """
    client = WmuxClient()
    real_send = client._send
    raised: List[int] = []

    def exploding_send(method, params):
        if len(raised) < 2:
            raised.append(1)
            raise RuntimeError("send exploded")
        return real_send(method, params)

    client._send = exploding_send
    with caplog.at_level("DEBUG", logger="wmux.diagnostics"):
        client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
        assert spin(lambda: len(raised) >= 1, 3.0)
        client.report_state({"awaitingHuman": False, "runDepth": 2}, 2)
        assert spin(lambda: len(raised) >= 2, 3.0)
        time.sleep(0.05)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    dropped = [r for r in warnings if "dropped" in r.getMessage()]
    assert len(dropped) == 1, (
        f"a lost report must warn EXACTLY once per process, got {len(dropped)}"
    )


# --- generation high-water mark (AC-14l) -----------------------------------


def test_ac14l_a_stale_write_into_an_undrained_slot_is_rejected(wmux_env, wire):
    wire.gate = threading.Event()
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 0}, 1)  # occupies worker
    assert wire.entered.wait(timeout=3.0)
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 6)  # newer
    client.report_state({"awaitingHuman": False, "runDepth": 0}, 5)  # stale
    assert client._state["runDepth"] == 1
    wire.gate.set()
    assert wire.wait_for(2)
    time.sleep(0.05)
    assert wire.sent[-1]["params"]["runDepth"] == 1


def test_ac14l_a_stale_write_into_a_DRAINED_slot_is_rejected(wmux_env, wire):
    # The reachable variant: the slot is emptied on drain, so a
    # contents-based comparison degrades to "accept anything" right here.
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 6)
    assert wire.wait_for(1)
    assert spin(lambda: client._state is None, 1.0)
    client.report_state({"awaitingHuman": False, "runDepth": 0}, 5)
    time.sleep(0.1)
    assert client._state is None
    assert len(wire.sent) == 1
    assert wire.sent[-1]["params"]["runDepth"] == 1


# --- idle hook / sweep tick (AC-14k, AC-14m, AC-14n, AC-33c) ---------------


def test_ac14n_a_none_idle_hook_is_a_no_op(wmux_env, monkeypatch, wire):
    monkeypatch.setattr(cl, "_SWEEP_S", 0.01)
    client = WmuxClient()  # worker starts before any hook is wired
    time.sleep(0.1)
    assert client._worker.is_alive()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert wire.wait_for(1)


def test_ac14k_and_ac33c_hook_is_ticked_with_cond_released(wmux_env, monkeypatch, wire):
    monkeypatch.setattr(cl, "_SWEEP_S", 0.02)
    client = WmuxClient()
    ticked = threading.Event()
    observed: List[bool] = []

    def hook():
        # The probe MUST run on a DIFFERENT thread: threading.Condition wraps
        # an RLock, so a re-entrant acquire from the worker itself returns
        # True unconditionally and the assertion could never fail.
        observed.append(acquirable_from_another_thread(client._cond))
        ticked.set()

    client.set_idle_hook(hook)
    assert ticked.wait(timeout=3.0)
    assert observed and all(observed)


def test_cond_probe_can_actually_fail(wmux_env, wire):
    """The AC-14k/AC-33c probe is falsifiable -- proven, not assumed."""
    client = WmuxClient()
    with client._cond:
        assert acquirable_from_another_thread(client._cond) is False


def test_ac14m_worker_survives_a_raising_hook(wmux_env, monkeypatch, wire):
    monkeypatch.setattr(cl, "_SWEEP_S", 0.01)
    client = WmuxClient()
    raised = threading.Event()

    def hook():
        raised.set()
        raise RuntimeError("sweep exploded")

    client.set_idle_hook(hook)
    assert raised.wait(timeout=3.0)
    time.sleep(0.05)
    assert client._worker.is_alive()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert wire.wait_for(1), "a report after a raising hook must still deliver"


def test_sweep_tick_publishes_through_the_real_client(wmux_env, monkeypatch, wire):
    """The two halves wired together: reporter decides, client ticks."""
    from wmux import reporter as rp
    from wmux.reporter import WmuxReporter

    monkeypatch.setattr(cl, "_SWEEP_S", 0.02)
    monkeypatch.setattr(rp, "_RUN_TTL_S", 0.01)
    client = WmuxClient()
    reporter = WmuxReporter(client)
    client.set_idle_hook(reporter.sweep_once)
    reporter.on_run_start("leaked")
    assert wire.wait_for(1)
    assert wire.params(0)["runDepth"] == 1
    assert wire.wait_for(2, timeout=5.0)
    assert wire.params(1)["runDepth"] == 0


# --- release (AC-17, AC-30..AC-32b) ----------------------------------------


def test_ac30_session_end_releases_exactly_once(wmux_env, wire):
    client = WmuxClient()
    client.release_and_close(timeout_s=2.0)
    assert wire.methods().count("pane.release_agent") == 1


def test_ac31_shutdown_after_session_end_is_idempotent(wmux_env, wire):
    client = WmuxClient()
    client.release_and_close(timeout_s=2.0)
    client.release_and_close(timeout_s=0.2)
    assert wire.methods().count("pane.release_agent") == 1


def test_ac32_release_is_bounded_when_the_pipe_is_unavailable(wmux_env, monkeypatch):
    def always_timeout(pipe_path, payload, timeout_s):
        time.sleep(timeout_s)
        return None

    monkeypatch.setattr(cl, "_transport", always_timeout)
    monkeypatch.setattr(cl, "_RELEASE_TIMEOUT_S", 0.3)
    client = WmuxClient()
    started = time.monotonic()
    client.release_and_close(timeout_s=2.0)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"release took {elapsed:.2f}s; budget was 0.3s"


def test_ac32b_release_budget_still_permits_retries(wmux_env, monkeypatch):
    monkeypatch.setattr(cl, "_SEND_BACKOFF_S", 0.001)
    monkeypatch.setattr(cl, "_RELEASE_TIMEOUT_S", 1.0)
    wire = Wire(replies=[None, b'{"result":{"released":true}}\n'])
    monkeypatch.setattr(cl, "_transport", wire)
    client = WmuxClient()
    client.release_and_close(timeout_s=2.0)
    assert len(wire.sent) > 1, "a single-attempt release would also pass AC-32"
    assert wire.methods() == ["pane.release_agent", "pane.release_agent"]


def test_ac17_release_drops_decorative_work_and_refuses_new_work(wmux_env, wire):
    wire.gate = threading.Event()
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)  # occupies worker
    assert wire.entered.wait(timeout=3.0)
    client.report_metadata(tokens="1k/2k")
    client.report_activity(tool="read_file")
    client.release_and_close(timeout_s=0.0)  # schedules, does not wait
    assert client._metadata is None and client._activity is None
    # New work is refused once a release is scheduled.
    client.report_metadata(tokens="9k/9k")
    client.report_state({"awaitingHuman": True, "runDepth": 9}, 99)
    client.report_session("late")
    assert client._metadata is None and client._state is None
    assert client._session is None
    wire.gate.set()
    assert client._released.wait(timeout=3.0)
    assert wire.methods() == ["pane.report_agent", "pane.release_agent"]


def test_release_flushes_pending_critical_work_first(wmux_env, wire):
    wire.gate = threading.Event()
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 1}, 1)
    assert wire.entered.wait(timeout=3.0)
    client.report_session("sess")
    client.release_and_close(timeout_s=0.0)
    wire.gate.set()
    assert client._released.wait(timeout=3.0)
    assert wire.methods() == [
        "pane.report_agent",
        "pane.report_agent_session",
        "pane.release_agent",
    ]


# --- token resolution (AC-37..AC-39) ---------------------------------------


def test_ac37_env_token_is_used_verbatim(wmux_env, wire):
    client = WmuxClient()
    client.report_state({"awaitingHuman": False, "runDepth": 0}, 1)
    assert wire.wait_for(1)
    assert wire.sent[0]["token"] == "tok-test"


def test_ac38_token_falls_back_to_the_appdata_file(wmux_env, monkeypatch, tmp_path):
    monkeypatch.delenv("WMUX_PIPE_TOKEN", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    (tmp_path / "wmux").mkdir()
    (tmp_path / "wmux" / "pipe-token").write_text("file-token\n", encoding="utf-8")
    assert cl.resolve_token() == ("file-token", True)

    (tmp_path / "wmux-foo").mkdir()
    (tmp_path / "wmux-foo" / "pipe-token").write_text("foo-token", encoding="utf-8")
    monkeypatch.setenv("WMUX_INSTANCE", "foo")
    assert cl.resolve_token() == ("foo-token", True)


def test_ac39_no_token_still_activates_and_warns_exactly_once(
    wmux_env, monkeypatch, tmp_path, caplog
):
    monkeypatch.delenv("WMUX_PIPE_TOKEN", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))  # no pipe-token file
    with caplog.at_level("WARNING", logger="wmux.client"):
        client = WmuxClient()
    assert client.active is True
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "unauthenticated" in warnings[0].getMessage()


# --- mechanism: _transport against a REAL pipe server (AC-8c) --------------


def _assert_server_was_healthy(server) -> None:
    """Fail loudly if the HARNESS died instead of the transport timing out.

    A dead server closes the pipe, which makes ``_transport`` return ``None``
    immediately -- indistinguishable from a correct timeout unless checked.
    """
    assert server.connected.is_set(), "server never read the request"
    assert server.error is None, f"server thread failed: {server.error!r}"


def test_ac8c_a_silent_server_times_out_and_never_hangs():
    with PipeServer(script=[]) as server:
        started = time.monotonic()
        reply = cl._transport(server.path, _payload(), timeout_s=0.4)
        elapsed = time.monotonic() - started
        _assert_server_was_healthy(server)
    assert reply is None
    assert 0.3 < elapsed < 2.0


def test_ac8c_a_partial_line_times_out_and_never_hangs():
    # The exact case a peek-then-readline() implementation hangs on: peek
    # reports 17 BYTES available, but there is no complete LINE.
    with PipeServer(script=[b'{"result":{"accep']) as server:
        started = time.monotonic()
        reply = cl._transport(server.path, _payload(), timeout_s=0.4)
        elapsed = time.monotonic() - started
        _assert_server_was_healthy(server)
    assert reply is None
    assert 0.3 < elapsed < 2.0


def test_ac8c_a_chunked_reply_is_reassembled():
    script = [b'{"result":{"ac', b'cepted":true}}\n']
    with PipeServer(script=script, gap_s=0.15) as server:
        reply = cl._transport(server.path, _payload(), timeout_s=2.0)
        _assert_server_was_healthy(server)
    assert reply is not None
    assert json.loads(reply.decode("utf-8")) == {"result": {"accepted": True}}


def test_transport_returns_none_when_the_pipe_does_not_exist():
    missing = rf"\\.\pipe\wmux-absent-{uuid.uuid4().hex}"
    assert cl._transport(missing, _payload(), timeout_s=0.2) is None


# --- hygiene (AC-35) -------------------------------------------------------


def test_ac35_import_has_no_module_scope_side_effects(monkeypatch):
    """Importing the plugin must not mint a session or load an agent."""
    import importlib
    from unittest.mock import patch

    for var in ("WMUX", "WMUX_SURFACE_ID"):
        monkeypatch.delenv(var, raising=False)
    with (
        patch("code_puppy.config.get_current_session_name") as mint,
        patch("code_puppy.agents.agent_manager.get_current_agent") as agent,
    ):
        for name in ("wmux.client", "wmux.reporter", "wmux.sources"):
            importlib.reload(sys.modules[name])
        import wmux.register_callbacks as rc

        importlib.reload(rc)
    assert mint.call_count == 0
    assert agent.call_count == 0
