"""Wire mechanics for the wmux named pipe: open it, read a reply, judge it.

This module is the MECHANISM half of the protocol -- how to talk to the pipe
without hanging, without opening the wrong thing, and how to tell whether the
server actually applied what we sent. ``client.py`` is the POLICY half: which
report to send, on which lane, when, and how often to retry.

The split follows the seam SPEC R-13 already named: everything here sits at
or below :func:`transport`, the single function the rest of the plugin is
tested through. Nothing here knows what a run, a lane or a report is.

Four properties are load-bearing, each measured against the real server
before being written down:

* **The reply MUST be read with a deadline, and never with ``readline()``.**
  A plain ``readline()`` on a silent pipe blocks forever, and so does
  peek-then-``readline()`` -- ``PeekNamedPipe`` reports BYTES available, not
  a complete LINE. :func:`transport` therefore reads EXACTLY the number of
  bytes the peek announced, which cannot block, and reassembles the line
  itself.
* **``PeekNamedPipe`` must be ARGTYPED, and its ``ArgumentError`` caught.**
  See :func:`_kernel32`.
* **``WMUX_PIPE`` is VALIDATED before it is opened.** See
  :func:`is_pipe_path`.
* **The reply is PARSED, never merely counted.** See :func:`classify_reply`.
"""

from __future__ import annotations

import ctypes
import json
import logging
import re
import sys
import time
from typing import Any, Optional, Tuple

from wmux.diagnostics import sanitize_for_log

logger = logging.getLogger(__name__)

#: Poll interval inside the reply deadline loop.
_POLL_S = 0.01

#: Hard cap on the reply accumulator, enforced on EVERY reply -- terminated
#: or not (fix round 3, H2). The DEADLINE bounds TIME, not BYTES: a squatter
#: can stream at named-pipe throughput for the full timeout, times
#: ``_SEND_ATTEMPTS``, per report, and a single ``\n`` at the end of that
#: flood must not buy it an unbounded line. Generous by two orders of
#: magnitude -- the largest MEASURED real reply is an all-surface
#: ``pane.agent_state`` at 371 bytes. Overflow is "no reply", which the
#: caller retries.
_MAX_REPLY_BYTES = 64 * 1024

_IS_WINDOWS = sys.platform.startswith("win")

#: The local NPFS device path, and nothing else. The host is the
#: LITERAL ``.``: any other host (including ``localhost`` and
#: ``127.0.0.1``) is resolved by the SMB redirector, which
#: authenticates IMPLICITLY -- shipping the developer's NetNTLMv2
#: response to whoever answers. \A/\Z, never ^/$: ``$`` matches before
#: a trailing newline. The name is a POSITIVE allowlist, so control
#: characters, NUL and unicode lookalikes cannot ride along.
_PIPE_PATH_RE = re.compile(r"\A\\\\\.\\pipe\\[A-Za-z0-9._-]{1,255}\Z")

#: ``WMUX_INSTANCE`` is interpolated into a filesystem path, so it may not
#: contain a separator, a drive letter, or a traversal.
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# --- reply verdicts ---------------------------------------------------------

#: The keys the MEASURED success replies carry. A reply carrying none of
#: them is still DELIVERED (the server answered), but is reported with a
#: DIFFERENT detail so a wmux contract drift can be grepped for rather than
#: reading exactly like success.
_KNOWN_RESULT_KEYS = frozenset({"accepted", "released", "ok"})

#: Applied, or definitively declined. Stop.
DELIVERED = "delivered"
#: No definitive answer. Retry.
UNDELIVERED = "undelivered"
#: The server refused us. Retry AND warn once.
REJECTED = "rejected"


def is_pipe_path(path: str) -> bool:
    r"""Return whether ``path`` is a named-pipe path safe to open.

    **This is a security guard, not a tidiness check**, and it guards TWO
    distinct disasters:

    * A REGULAR FILE. ``open(path, "r+b")`` on one succeeds and writes IN
      PLACE -- measured: the victim file's content was destroyed and
      ``"token":"SECRET-TOKEN-abc123"`` was left on disk in cleartext,
      because the token rides in every envelope.
    * A REMOTE UNC HOST, which is strictly WORSE, because it leaves the
      box. Measured (fix round 2, G1): the earlier ``\\<host>\pipe\<name>``
      pattern accepted ``\\evil.example.com\pipe\wmux``,
      ``\\10.0.0.5\pipe\x`` and ``\\1.2.3.4\pipe\wmux``. Opening any of
      them goes through the SMB redirector, which performs IMPLICIT NTLM
      authentication -- so a hostile ``WMUX_PIPE`` ships the developer's
      NetNTLMv2 response to whoever answers, PLUS the pipe token in
      cleartext. Hence the host must be the literal ``.``: ``localhost``
      and ``127.0.0.1`` are REJECTED too, since both still go through the
      redirector, and the plugin only ever needs the form wmux itself
      emits.

    The old pattern also anchored with ``$``, which matches BEFORE a
    trailing newline -- so ``\\.\pipe\wmux\n`` passed (measured). ``\Z``
    does not.

    The ``..`` and trailing-``.`` checks are LOAD-BEARING, not
    belt-and-braces -- this docstring claimed the opposite until it was
    measured (fix round 3, M8). ``.`` is a member of the name charset
    ``[A-Za-z0-9._-]``, so ``\\.\pipe\..``, ``\\.\pipe\.`` and
    ``\\.\pipe\wmux.`` all MATCH the pattern on their own; the two checks
    are the only thing rejecting them. Deleting either one as redundant
    silently widens the guard.
    """
    return (
        bool(path)
        and ".." not in path
        and not path.endswith(".")
        and _PIPE_PATH_RE.match(path) is not None
    )


def is_safe_instance(value: str) -> bool:
    """Return whether ``WMUX_INSTANCE`` is safe to interpolate into a path.

    The value is CONCATENATED into the directory NAME
    (``wmux-<instance>``), so ``..\\..\\..\\x`` walks out of ``%APPDATA%``
    entirely. Measured, because the arithmetic is off by one from the naive
    reading: concatenation costs an extra level, so ``..\\..\\x`` still lands
    INSIDE ``%APPDATA%``, and an absolute value does NOT make
    ``os.path.join`` discard the base (it is never a separate segment). An
    allowlist rather than a count, precisely so that arithmetic does not have
    to be re-derived correctly by the next reader.
    """
    return ".." not in value and _INSTANCE_RE.match(value) is not None


def _kernel32() -> Any:
    """Return this module's OWN kernel32 handle, with ``PeekNamedPipe`` typed.

    Two separate reasons this is not ``ctypes.windll.kernel32``:

    * **argtypes are required.** Without them ctypes guesses the marshalling
      and an oversized handle raises ``ctypes.ArgumentError: argument 1:
      OverflowError: int too long to convert``. Measured; declaring the
      handle as ``wintypes.HANDLE`` makes the same call return normally.
    * **``ctypes.windll`` is a process-wide CACHE.** Setting ``argtypes`` on
      it would silently reconfigure the very same function object that
      ``code_puppy/tools/command_runner.py:60`` holds. A plugin must not
      reach into core's marshalling, so it owns a private ``WinDLL``.

    Built once and memoised on the function, because ``WinDLL`` re-opens the
    library on every construction and this sits on the per-message path.
    """
    lib = getattr(_kernel32, "_lib", None)
    if lib is None:
        from ctypes import wintypes

        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        lib.PeekNamedPipe.argtypes = [
            wintypes.HANDLE,  # hNamedPipe
            wintypes.LPVOID,  # lpBuffer (NULL: do not read)
            wintypes.DWORD,  # nBufferSize
            ctypes.POINTER(wintypes.DWORD),  # lpBytesRead
            ctypes.POINTER(wintypes.DWORD),  # lpTotalBytesAvail
            ctypes.POINTER(wintypes.DWORD),  # lpBytesLeftThisMessage
        ]
        lib.PeekNamedPipe.restype = wintypes.BOOL
        _kernel32._lib = lib
    return lib


def transport(pipe_path: str, payload: bytes, timeout_s: float) -> Optional[bytes]:
    """Send one framed request and return the reply LINE, or ``None``.

    ``None`` means "no definitive answer" -- a connect failure, a timeout, or
    a partial line still unfinished at the deadline. All three are retried by
    the caller. This is the single seam the rest of the plugin is tested
    through (SPEC R-13).
    """
    if not _IS_WINDOWS:
        return None
    try:
        import msvcrt
        from ctypes import wintypes

        kernel32 = _kernel32()
        with open(pipe_path, "r+b", buffering=0) as pipe:
            pipe.write(payload)
            handle = msvcrt.get_osfhandle(pipe.fileno())
            deadline = time.monotonic() + timeout_s
            buf = bytearray()
            available = wintypes.DWORD(0)
            while time.monotonic() < deadline:
                ok = kernel32.PeekNamedPipe(
                    handle, None, 0, None, ctypes.byref(available), None
                )
                if not ok:
                    # use_last_error=True is pointless unless somebody reads
                    # it: without this, every PeekNamedPipe failure is
                    # indistinguishable from every other one, which is the
                    # hardest class of pipe bug to diagnose. Debug is the
                    # right level -- a failed peek is a "no reply", which
                    # the caller already retries.
                    logger.debug(
                        "wmux: PeekNamedPipe failed (WinError %s)",
                        ctypes.get_last_error(),
                    )
                    return None
                count = available.value
                if count:
                    # Reading EXACTLY what the peek announced cannot block --
                    # which is the whole reason readline() is banned here.
                    buf += pipe.read(count)
                    # The cap is checked BEFORE the newline, and the order is
                    # the whole guard (fix round 3, H2). Behind the
                    # early-return it only ever bounded UNTERMINATED replies:
                    # a squatter that ends its flood with "\n" took the
                    # `return bytes(buf[:newline])` path at ANY size, and the
                    # cap below it was unreachable. Measured against a real
                    # server: a 100000-byte terminated reply came back whole
                    # and DELIVERED, which chained with G3 to paint 90 KB of
                    # attacker text onto the developer's terminal.
                    if len(buf) > _MAX_REPLY_BYTES:
                        # Abandon it on SIZE rather than waiting the deadline
                        # out, and treat it as "no reply" like every other
                        # unusable line.
                        logger.debug("wmux: reply exceeded %d bytes", _MAX_REPLY_BYTES)
                        return None
                    newline = buf.find(b"\n")
                    if newline >= 0:
                        return bytes(buf[:newline])
                else:
                    time.sleep(_POLL_S)
            return None
    except (OSError, ValueError, ctypes.ArgumentError):
        # A busy single-instance pipe fails immediately rather than queueing.
        # That is a "no reply", never a crash.
        #
        # ctypes.ArgumentError is listed EXPLICITLY because it is NOT an
        # OSError and NOT a ValueError -- its MRO is [ArgumentError,
        # Exception, BaseException, object] (measured). Omitting it let a
        # single bad handle escape as a hard exception on every send: three
        # retries, then death into a log nobody can see. In-tree precedent:
        # tools/command_runner.py:98 catches it the same way.
        return None


def classify_reply(reply: Optional[bytes]) -> Tuple[str, str]:
    """Classify one reply line as ``(verdict, detail)``.

    Every shape below was MEASURED against the live wmux server from inside
    a real pane, and cross-checked against the server's own dispatcher
    (``dist/main/agent-state-v2.js``):

    ===========================================  ===========  ==============
    reply                                        verdict      cause
    ===========================================  ===========  ==============
    ``{"result":{"accepted":true,"state":..}}``  DELIVERED    applied
    ``{"result":{"released":true}}``             DELIVERED    release
    ``{"result":{"ok":true}}``                   DELIVERED    activity
    ``{"result":{"accepted":false,"state":..}}`` DELIVERED    seq dedupe
    ``{"error":{"code":-32001,...}}``            REJECTED     bad token
    ``{"error":{"code":-32602,...}}``            REJECTED     bad params
    ``{"error":{"code":-32601,...}}``            REJECTED     bad method
    ``None``                                     UNDELIVERED  timeout
    ===========================================  ===========  ==============

    **``accepted:false`` is DELIVERED, not a failure.** The server's own
    comment states a report losing the seq dedupe race "answers
    ``{ accepted: false }`` rather than erroring -- a client retry must be a
    harmless no-op, not a failure that invites another retry." Retrying it
    would replay an already-applied state; warning about it would fire on
    every ordinary retry.

    **An unrecognised shape is DELIVERED.** The server answered something,
    so the request provably arrived and was processed; retrying a request
    that landed is worse than trusting it. Guessing "failure" here would
    triple every send the day wmux adds a field. It gets its OWN detail
    rather than the plain ``ok``, so a wmux contract drift is greppable
    instead of indistinguishable from success.

    **A reply that DEFEATS the parser is UNDELIVERED, not delivered.**
    Measured (fix round 2, G2): ``json.loads(b"[" * 100000)`` raises
    ``RecursionError``, whose MRO is ``[RecursionError, RuntimeError,
    Exception, BaseException, object]`` -- it is NOT a ``ValueError``, so
    ``except (ValueError, UnicodeDecodeError)`` missed it and it escaped
    this function entirely, landing in ``_run``'s broad except. That handed
    a pipe squatter a one-line primitive to silently discard any single
    state report while the warning blamed an internal bug. Retry is the
    right answer to an unparseable-BECAUSE-HOSTILE reply -- as distinct
    from ordinary garbage, which stays DELIVERED because the server
    provably answered.
    """
    if reply is None:
        return UNDELIVERED, "no reply"
    try:
        parsed = json.loads(reply.decode("utf-8"))
    except RecursionError:
        return UNDELIVERED, "reply too deeply nested to parse"
    except (ValueError, UnicodeDecodeError):
        return DELIVERED, "unparseable reply"
    if not isinstance(parsed, dict):
        return DELIVERED, "non-object reply"
    error = parsed.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        # The CODE LEADS, and the WHOLE line is sanitized as one string.
        # Both halves are load-bearing, and each was a measured bug:
        #
        # * The code leads (fix round 2, G3) because it is the only
        #   actionable part. Putting it last let a 5000-char message push it
        #   past the truncation point, so a squatter could erase the one
        #   field an operator can act on.
        # * The WHOLE line is sanitized (fix round 3, H1) because `code` is
        #   `error["code"]` off the reply JSON -- every bit as
        #   attacker-controlled as `message`, and not an integer just
        #   because the real server sends one. Sanitizing only `message`
        #   left a raw `\x1b[2J` reaching the developer's terminal through
        #   the sibling field (measured end-to-end: the screen cleared),
        #   and let a 200000-char code walk straight past the length cap.
        #   Sanitizing the composed string rather than each part also keeps
        #   ONE cap over the whole line instead of one per field.
        return REJECTED, sanitize_for_log(f"code {code}: {message or 'error'}")
    if error:
        return REJECTED, sanitize_for_log(str(error))
    result = parsed.get("result")
    if isinstance(result, dict) and _KNOWN_RESULT_KEYS.isdisjoint(result):
        return DELIVERED, "unrecognised result shape"
    if not isinstance(result, dict):
        return DELIVERED, "unrecognised reply shape"
    return DELIVERED, "ok"


__all__ = [
    "DELIVERED",
    "REJECTED",
    "UNDELIVERED",
    "classify_reply",
    "is_pipe_path",
    "is_safe_instance",
    "transport",
]
