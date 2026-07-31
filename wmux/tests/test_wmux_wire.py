"""Wire MECHANISM: path validation, reply bounds, reply classification.

``wire.py`` is the mechanism half of the protocol and ``client.py`` the
policy half (SPEC R-13), so the mechanism gets its own suite: everything
here is asserted against ``wire`` directly, with no client, no worker thread
and no lanes in the way.

Covers AC-46 / AC-46b (G1), AC-47 (G2), AC-48b (G3), AC-49 / AC-49b (G4),
AC-50 and AC-51.
"""

from __future__ import annotations

import ctypes
import sys
import time

import pytest
from conftest import ACK, PipeServer, _payload

from wmux import wire as wire_mod
from wmux.diagnostics import MAX_LOG_DETAIL
from wmux.wire import (
    DELIVERED,
    REJECTED,
    UNDELIVERED,
    classify_reply,
    is_pipe_path,
)

windows_only = pytest.mark.skipif(
    not sys.platform.startswith("win"), reason="wmux is a Windows named-pipe protocol"
)


# --- G1: only the LOCAL NPFS device path is a pipe path (AC-46) ------------


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(r"\\.\pipe\wmux", id="default"),
        pytest.param(r"\\.\pipe\wmux-test-abc123", id="instance-suffixed"),
        pytest.param(r"\\.\pipe\a", id="one-char-name"),
        pytest.param("\\\\.\\pipe\\" + "a" * 255, id="name-255-chars"),
        pytest.param(r"\\.\pipe\wmux.sock", id="dotted-name"),
        pytest.param(r"\\.\pipe\wmux_1", id="underscored-name"),
    ],
)
def test_ac46_the_local_pipe_device_path_is_accepted(path):
    """The forms wmux itself emits must keep working."""
    assert is_pipe_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        # --- G1 proper: the host must be the LITERAL '.' -------------------
        pytest.param(r"\\evil.example.com\pipe\wmux", id="remote-dns-host"),
        pytest.param(r"\\10.0.0.5\pipe\x", id="remote-rfc1918-host"),
        pytest.param(r"\\1.2.3.4\pipe\wmux", id="remote-public-ip"),
        pytest.param(r"\\localhost\pipe\wmux", id="localhost"),
        pytest.param(r"\\127.0.0.1\pipe\wmux", id="loopback-ip"),
        pytest.param("\\\\.\\pipe\\wmux\n", id="trailing-newline"),
        pytest.param("\\\\.\\pipe\\wmux\r\n", id="trailing-crlf"),
        pytest.param("\\\\.\\pipe\\wmux\nx", id="embedded-newline"),
        # --- the round-1 rejections, which must not regress ----------------
        pytest.param(r"C:\Users\me\victim.txt", id="regular-file"),
        pytest.param(r"\\.\pipe\..\..\C:\Users\me\victim.txt", id="traversal"),
        pytest.param(r"\\server\share\secrets.txt", id="unc-share"),
        pytest.param(r"\\.\C:\Users\me\victim.txt", id="device-path"),
        pytest.param("/tmp/victim", id="posix"),
        pytest.param(r"\\.\pipe\a\b", id="nested-segment"),
        pytest.param("", id="empty"),
        # --- shape and charset ---------------------------------------------
        pytest.param(r"\\.\pipe\ ", id="space-name"),
        pytest.param(r"\\.\pipe\wmux ", id="trailing-space"),
        pytest.param(r"\\.\pipe\wmux.", id="trailing-dot"),
        pytest.param(r"\\.\PIPE\wmux", id="uppercase-pipe"),
        pytest.param(r"\\.\pipe\\", id="empty-name"),
        pytest.param(r"\\.\pipe", id="no-name-segment"),
        pytest.param("\\\\.\\pipe\\" + "a" * 256, id="name-256-chars"),
        pytest.param("\\\\.\\pipe\\wmux\x00", id="nul-byte"),
        pytest.param("\\\\.\\pipe\\wmux\x1b[2J", id="ansi-escape"),
        pytest.param("\\\\.\\pipe\\wmuх", id="cyrillic-lookalike"),
        pytest.param(r"//./pipe/wmux", id="forward-slashes"),
    ],
)
def test_ac46_anything_but_the_local_device_path_is_rejected(path):
    """SECURITY (G1): a REMOTE UNC host authenticates IMPLICITLY.

    Opening ``\\\\<host>\\pipe\\x`` goes through the SMB redirector, which
    performs implicit NTLM authentication -- so a hostile ``WMUX_PIPE``
    ships the developer's NetNTLMv2 response off-box AND hands the pipe
    token over in cleartext. That is strictly worse than the on-box
    arbitrary-file write this guard originally replaced, so ``localhost``
    and ``127.0.0.1`` are rejected too: only the literal ``.`` names the
    local NPFS device without a redirector.
    """
    assert is_pipe_path(path) is False


def test_ac46_the_pattern_is_anchored_with_capital_z_not_dollar():
    r"""``$`` matches before a trailing ``\n`` -- ``\Z`` does not.

    Asserted on the PATTERN as well as through :func:`is_pipe_path`, because
    a future edit that swaps the anchors back is invisible at every other
    call site.
    """
    pattern = wire_mod._PIPE_PATH_RE.pattern
    assert pattern.startswith("\\A") and pattern.endswith("\\Z")
    assert "^" not in pattern and "$" not in pattern
    assert wire_mod._PIPE_PATH_RE.match("\\\\.\\pipe\\wmux\n") is None


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(r"\\.\pipe\..", id="name-is-dotdot"),
        pytest.param(r"\\.\pipe\.", id="name-is-a-single-dot"),
        pytest.param(r"\\.\pipe\wmux.", id="trailing-dot"),
        pytest.param(r"\\.\pipe\a..b", id="dotdot-inside-the-name"),
    ],
)
def test_ac46b_the_dot_guards_are_load_bearing_not_redundant(path):
    r"""The ``..`` / trailing-``.`` guards do work the PATTERN does not.

    Measured (fix round 3, M8): ``.`` is a member of the name charset
    ``[A-Za-z0-9._-]``, so every path below matches ``_PIPE_PATH_RE`` on its
    own -- ``\\.\pipe\..`` and ``\\.\pipe\wmux.`` included. The guards are
    the ONLY thing rejecting them, so deleting them as "belt and braces"
    (as the docstring used to claim they were) silently widens the guard.

    Asserted on the PATTERN as well as through :func:`is_pipe_path`: without
    the pattern half, this test would still pass if somebody tightened the
    charset AND dropped the guards, and would then no longer pin the guards
    at all.
    """
    assert wire_mod._PIPE_PATH_RE.match(path) is not None, (
        "precondition: the pattern alone must ACCEPT this, or the guards "
        "really would be redundant and this test proves nothing"
    )
    assert is_pipe_path(path) is False


# --- G2: a hostile reply cannot escape the classifier (AC-47) --------------


def test_ac47_a_deeply_nested_reply_is_undelivered_not_an_exception():
    """SECURITY (G2): ``RecursionError`` is NOT a ``ValueError``.

    Measured: ``json.loads(b"[" * 100000)`` raises ``RecursionError``, whose
    MRO is ``[RecursionError, RuntimeError, Exception, BaseException,
    object]`` -- so ``except (ValueError, UnicodeDecodeError)`` misses it and
    it escapes ``classify_reply`` entirely. That hands a pipe squatter a
    one-line primitive to silently discard any single state report.

    UNDELIVERED, not DELIVERED: an unparseable-because-hostile reply is
    exactly the case a retry answers.
    """
    verdict, detail = classify_reply(b"[" * 100_000)
    assert verdict == UNDELIVERED
    assert detail


def test_ac47_a_nested_object_reply_is_also_contained():
    """The object form of the same primitive (``{"a":{"a":{...}}}``)."""
    verdict, _ = classify_reply(b'{"a":' * 100_000 + b"1" + b"}" * 100_000)
    assert verdict == UNDELIVERED


def test_ac47_ordinary_garbage_stays_delivered():
    """The control: G2 must not turn every unparseable reply into a retry.

    Without this, "classify everything unparseable as UNDELIVERED" would
    satisfy AC-47 while retrying every reply the server ever reshapes.
    """
    assert classify_reply(b"not json at all")[0] == DELIVERED
    assert classify_reply(b"\xff\xfe not utf-8")[0] == DELIVERED


# --- G3: the WHOLE detail is sanitized, both halves (AC-48b) ---------------


def test_ac48b_a_hostile_error_code_is_sanitized_like_the_message():
    r"""SECURITY (G3): ``code`` is as attacker-controlled as ``message``.

    Measured (fix round 3, H1): the round-2 fix sanitized ``message`` but
    handed ``code`` straight through, and ``code`` is just
    ``error.get("code")`` off the reply JSON -- so
    ``{"error":{"code":"\u001b[2J\u001b[HFAKE-CODE","message":"m"}}``
    produced the detail ``'code \x1b[2J\x1b[HFAKE-CODE: m'`` with a RAW
    escape, and the verifier drove that through a real pipe server until the
    developer's screen cleared and attacker text was painted on it. The
    exact G3 threat model, still live through the sibling field.
    """
    verdict, detail = classify_reply(
        b'{"error":{"code":"\\u001b[2J\\u001b[HFAKE-CODE","message":"m"}}'
    )
    assert verdict == REJECTED
    assert "\x1b" not in detail, "an ANSI escape must never reach the terminal"
    assert "\\x1b" in detail, "and it must be ESCAPED, not silently dropped"


def test_ac48b_a_hostile_error_code_cannot_bypass_the_length_cap():
    """A 200000-char ``code`` used to produce a 200008-char detail.

    ``sanitize_for_log`` caps at :data:`MAX_LOG_DETAIL`; sanitizing only the
    message left the cap trivially bypassable through the sibling field.
    """
    reply = b'{"error":{"code":"' + b"C" * 200_000 + b'","message":"m"}}'
    verdict, detail = classify_reply(reply)
    assert verdict == REJECTED
    assert len(detail) <= MAX_LOG_DETAIL + 10, (
        f"the cap must bound the WHOLE detail; got {len(detail)} chars"
    )


def test_ac48b_a_hostile_message_is_still_escaped():
    r"""The control that must not regress: the round-2 fix still works.

    Without this, "sanitize the code and stop sanitizing the message" would
    satisfy the two assertions above while reopening G3 through the very
    field it was opened on.
    """
    verdict, detail = classify_reply(
        b'{"error":{"code":-32001,"message":"\\u001b[2Jevil"}}'
    )
    assert verdict == REJECTED
    assert detail == "code -32001: \\x1b[2Jevil"


def test_ac48b_the_error_code_leads_so_it_survives_truncation():
    """The CODE is the only actionable part, so it must outlive the cut.

    Measured (fix round 3, M7): moving the code to the TAIL of the detail
    passed 199/199 -- the property was asserted nowhere, so a squatter could
    push the one field an operator can act on past the truncation point with
    a long enough message, exactly the bug the round-2 ordering fixed.

    The message is deliberately longer than :data:`MAX_LOG_DETAIL` so that
    truncation is FORCED: with a short message a tail-positioned code would
    survive too, and the test could not fail.
    """
    reply = b'{"error":{"code":-32001,"message":"' + b"m" * 5000 + b'"}}'
    verdict, detail = classify_reply(reply)
    assert verdict == REJECTED
    assert len(detail) > MAX_LOG_DETAIL - 20, "precondition: truncation happened"
    assert detail.endswith("..."), "precondition: the detail really was cut"
    assert "-32001" in detail, "the code must survive truncation"
    assert detail.startswith("code -32001"), "...which it only does by LEADING"


# --- AC-51: an unrecognised shape is greppable -----------------------------


def test_ac51_an_unrecognised_shape_has_its_own_detail():
    """A wmux contract drift must be greppable, not indistinguishable.

    Both are DELIVERED -- the server answered, so the request provably
    landed -- but a detail of "ok" for BOTH means a protocol change looks
    exactly like success in the log that reports it.
    """
    ok_verdict, ok_detail = classify_reply(b'{"result":{"accepted":true}}')
    drift_verdict, drift_detail = classify_reply(b'{"result":{}}')
    assert ok_verdict == drift_verdict == DELIVERED
    assert ok_detail != drift_detail
    assert "unrecognised" in drift_detail


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(b'{"result":{"accepted":true,"state":"idle"}}', id="accepted"),
        pytest.param(b'{"result":{"accepted":false,"state":"idle"}}', id="dedupe"),
        pytest.param(b'{"result":{"released":true}}', id="released"),
        pytest.param(b'{"result":{"ok":true}}', id="activity"),
    ],
)
def test_ac51_every_measured_success_shape_still_reads_as_ok(reply):
    """The control for AC-51: the four MEASURED shapes are not "drift"."""
    assert classify_reply(reply) == (DELIVERED, "ok")


# --- G4: the reply accumulator is bounded (AC-49) --------------------------


@windows_only
def test_ac49_an_endless_reply_is_abandoned_long_before_the_deadline(monkeypatch):
    """SECURITY (G4): the deadline bounds TIME, not BYTES.

    A squatter that never sends ``\\n`` can stream at named-pipe throughput
    for the whole timeout, three attempts deep, per report. The timeout is
    set FAR above the time this takes, so a missing cap shows up as a
    six-second test rather than a passing one.
    """
    monkeypatch.setattr(wire_mod, "_MAX_REPLY_BYTES", 4096)
    with PipeServer(script=[b"A" * 4096] * 8) as server:
        started = time.monotonic()
        reply = wire_mod.transport(server.path, _payload(), timeout_s=6.0)
        elapsed = time.monotonic() - started
    assert reply is None, "an unterminated flood must read as 'no reply'"
    assert elapsed < 2.0, (
        f"the accumulator must be abandoned on SIZE, not waited out on time "
        f"(took {elapsed:.2f}s of a 6.0s deadline)"
    )


@windows_only
def test_ac49_a_reply_below_the_cap_is_still_returned_whole():
    """The control: the cap must not truncate a real reply.

    The largest MEASURED real reply is an all-surface ``pane.agent_state``
    at 371 bytes; the cap is several KB. Without this test a cap of 10 bytes
    would satisfy AC-49 and silently break every reply.
    """
    body = b'{"result":{"accepted":true,"pad":"' + b"p" * 3000 + b'"}}'
    assert len(body) < wire_mod._MAX_REPLY_BYTES
    with PipeServer(script=[body + b"\n"]) as server:
        reply = wire_mod.transport(server.path, _payload(), timeout_s=5.0)
    assert reply == body
    assert classify_reply(reply)[0] == DELIVERED


def _framed(size: int) -> bytes:
    """A well-formed success reply of EXACTLY ``size`` bytes, no newline."""
    head, tail = b'{"result":{"accepted":true,"pad":"', b'"}}'
    return head + b"p" * (size - len(head) - len(tail)) + tail


@windows_only
def test_ac49b_an_over_cap_reply_is_rejected_even_when_it_is_terminated():
    r"""SECURITY (G4/H2): the cap must bound TERMINATED replies too.

    Measured (fix round 3, H2): the round-2 cap sat AFTER the newline
    early-return, so it only ever bounded UNTERMINATED replies --
    ``if newline >= 0: return bytes(buf[:newline])`` returned a line of ANY
    size, and the ``len(buf) > _MAX_REPLY_BYTES`` branch below it was
    unreachable for them. The verifier measured a 100000-byte reply ending
    in ``\n`` returned WHOLE as "delivered", and chained it with H1 to dump
    90 KB into the terminal.

    **The framing is what makes this deterministic, and it is not
    decoration.** A naive "write 100 KB then a newline" test PASSES against
    the unfixed code: the reader breaches the cap somewhere in the middle of
    the flood, while no newline is in the buffer yet, and returns ``None``
    down the UNTERMINATED branch -- proving nothing about the branch under
    test (measured: that first draft passed pre-fix). Here the newline is
    the byte that breaks the cap, so the buffer can only ever exceed the cap
    WITH a newline already in it. That is the H2 branch and only the H2
    branch, whatever chunk sizes the pipe happens to hand back.

    Run against the REAL ``_MAX_REPLY_BYTES``, never a patched-down one.
    """
    # Checked BEFORE the body is built: mutant M6 raises the cap 16000x, and
    # allocating a gigabyte to discover that is not a test failure anybody
    # can read. The largest MEASURED real reply is 371 bytes, so a megabyte
    # is already three orders of magnitude of headroom -- past that the
    # constant has stopped being a cap.
    assert wire_mod._MAX_REPLY_BYTES <= 1024 * 1024, (
        "the cap must stay small enough to bound what a squatter can dump "
        "into a terminal; a raised cap is exactly mutant M6"
    )
    body = _framed(wire_mod._MAX_REPLY_BYTES)
    with PipeServer(script=[body, b"\n"]) as server:
        reply = wire_mod.transport(server.path, _payload(), timeout_s=6.0)
    assert reply is None, (
        "an over-cap reply must read as 'no reply' and be retried, whether "
        "or not the squatter bothered to terminate it"
    )


@windows_only
def test_ac49b_a_large_but_under_cap_reply_still_parses():
    """The control for AC-49b: only OVER-cap replies may be dropped.

    60 KB is under the 64 KB cap. Without this, "return None whenever the
    buffer is large" -- or a cap dropped to a few bytes -- would satisfy
    AC-49b while silently discarding every legitimate reply.
    """
    body = _framed(60 * 1024)
    assert len(body) < wire_mod._MAX_REPLY_BYTES
    with PipeServer(script=[body + b"\n"]) as server:
        reply = wire_mod.transport(server.path, _payload(), timeout_s=6.0)
    assert reply == body
    assert classify_reply(reply) == (DELIVERED, "ok")


@windows_only
def test_ac49b_a_small_reply_is_untouched_by_the_cap():
    """The narrowest control: the ordinary 50-byte case still round-trips.

    The two tests above both work in tens of kilobytes; this is the size
    every REAL reply actually is (371 bytes at the largest measured), so an
    off-by-one in the new bound cannot hide behind "it only affects floods".
    """
    with PipeServer(script=[ACK]) as server:
        reply = wire_mod.transport(server.path, _payload(), timeout_s=5.0)
    assert reply == ACK.rstrip(b"\n")
    assert classify_reply(reply) == (DELIVERED, "ok")


# --- AC-50: a failed peek says WHY ----------------------------------------


@windows_only
def test_ac50_a_failing_peek_logs_the_last_error(monkeypatch, caplog):
    """``use_last_error=True`` is pointless if nobody ever reads it.

    Every ``PeekNamedPipe`` failure is otherwise indistinguishable from
    every other one, which is the hardest class of pipe bug to diagnose.
    """

    class FailingPeek:
        def PeekNamedPipe(self, *_args):
            ctypes.set_last_error(231)  # ERROR_PIPE_BUSY
            return 0

    monkeypatch.setattr(wire_mod, "_kernel32", FailingPeek)
    with caplog.at_level("DEBUG", logger="wmux.wire"):
        with PipeServer(script=[]) as server:
            reply = wire_mod.transport(server.path, _payload(), timeout_s=0.4)
    assert reply is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("231" in m for m in messages), (
        f"the Windows error code must reach the log; got {messages}"
    )
