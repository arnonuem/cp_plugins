"""Delivery policy for wmux's declared-agent-state protocol.

wmux (a Windows terminal multiplexer) injects ``WMUX=1``,
``WMUX_SURFACE_ID``, ``WMUX_PIPE`` and ``WMUX_PIPE_TOKEN`` into the shells it
spawns. This module speaks the newline-delimited JSON protocol on that pipe
far enough to call ``pane.report_agent`` / ``pane.report_agent_session`` /
``pane.report_metadata`` / ``pane.release_agent`` / ``agent.activity``.

This is the POLICY half: which report goes on which lane, when it is stamped,
how often it is retried, and what happens when the server refuses it. The
MECHANISM half -- opening the pipe, reading a reply under a deadline, judging
the reply -- lives in :mod:`wmux.wire`, at and below the ``_transport`` seam
SPEC R-13 named.

Two properties are load-bearing here, each measured against the real server
before being written down:

* **``seq`` is stamped at WIRE time, after lane selection.** The critical
  lane overtakes the decorative one, so an enqueue-time stamp would put the
  lower ``seq`` on the wire last -- and wmux drops any report at or below the
  last one it saw.
* **The seq seed is wall-clock derived.** wmux keeps ``last_seq`` per surface
  across client restarts, so a process seeding from 0 after a crash would be
  silently rejected forever.

Delivery runs on one owned daemon worker thread fed by coalescing
latest-wins mailbox slots: a critical lane (state edges, session references,
the release) that always overtakes a decorative lane (metadata, activity).
Nothing here ever raises into the caller -- reporting state must not be able
to disturb the agent.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from wmux.diagnostics import warn_once
from wmux.wire import (
    DELIVERED,
    REJECTED,
    classify_reply,
    is_pipe_path,
    is_safe_instance,
)
from wmux.wire import transport as _transport

logger = logging.getLogger(__name__)

#: wmux's own default when ``WMUX_PIPE`` is unset (mirrors ``wmux.js:13``).
DEFAULT_PIPE = r"\\.\pipe\wmux"

# --- tuning constants -------------------------------------------------------

#: Deadline for one reply. The real pipe answers in 0-16 ms; ~125x headroom.
_REPLY_TIMEOUT_S = 2.0
#: Attempts per report. A retry replays the IDENTICAL envelope; wmux dedupes
#: on ``seq``, so a report that already applied is harmlessly ignored.
_SEND_ATTEMPTS = 3
_SEND_BACKOFF_S = 0.05
#: TOTAL wall budget for the release path, so an unavailable wmux can never
#: add seconds to process exit (3 x 2.0 s would).
_RELEASE_TIMEOUT_S = 1.0
#: Idle-wait timeout that ticks the sweep hook. A worker-loop parameter, so
#: it lives here; what the tick DECIDES is none of this module's business.
_SWEEP_S = 60.0
#: Metadata TTL (24 h) so an abruptly-killed process cannot leave stale
#: token/model numbers on the sidebar forever.
_METADATA_TTL_MS = 86_400_000

# --- wmux methods -----------------------------------------------------------

_M_STATE = "pane.report_agent"
_M_SESSION = "pane.report_agent_session"
_M_METADATA = "pane.report_metadata"
_M_RELEASE = "pane.release_agent"
_M_ACTIVITY = "agent.activity"

#: Methods that carry a monotonic ``seq``. ``agent.activity`` deliberately
#: does not -- the CLI does not send one either (measured: ``{"ok": true}``),
#: and undocumented protocol noise is still noise.
_SEQ_METHODS = frozenset({_M_STATE, _M_SESSION, _M_METADATA, _M_RELEASE})


def resolve_token() -> Tuple[str, bool]:
    """Resolve the pipe token the way the wmux CLI does (``wmux.js:38-52``).

    Returns ``(token, from_fallback_file)``. An empty token still activates
    the plugin -- but the caller warns once, because rejected reports look
    exactly like success at the transport level.

    ``WMUX_INSTANCE`` is SANITIZED before it reaches the path: it is
    CONCATENATED into the directory NAME (``wmux-<instance>``), so
    ``..\\..\\..\\x`` walks out of ``%APPDATA%`` entirely. Measured, because
    the arithmetic is off by one from the naive reading: concatenation costs
    an extra level, so ``..\\..\\x`` still lands INSIDE ``%APPDATA%``, and an
    absolute value does NOT make ``os.path.join`` discard the base (it is
    never a separate segment). The guard is an allowlist rather than a count
    precisely so that arithmetic does not have to be re-derived correctly by
    the next reader. A rejected instance resolves
    NO token rather than falling back to the un-suffixed file: the two name
    different wmux instances, and silently reading the wrong instance's
    token would produce exactly the every-report-rejected death that F3
    exists to surface.
    """
    from_env = (os.environ.get("WMUX_PIPE_TOKEN") or "").strip()
    if from_env:
        return from_env, False
    instance = (os.environ.get("WMUX_INSTANCE") or "").strip()
    if instance and not is_safe_instance(instance):
        warn_once(
            "bad-instance",
            "wmux: WMUX_INSTANCE is not a plain instance name, so the "
            "pipe-token file was NOT read; agent-state reports will be "
            "unauthenticated and rejected",
        )
        return "", False
    try:
        suffix = f"-{instance}" if instance else ""
        base = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming"
        )
        path = os.path.join(base, f"wmux{suffix}", "pipe-token")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip(), True
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is listed EXPLICITLY because it is NOT an
        # OSError (MRO: [UnicodeDecodeError, UnicodeError, ValueError, ...]).
        # A corrupt or non-UTF-8 token file would otherwise raise straight
        # out of WmuxClient() construction -- i.e. out of plugin IMPORT --
        # instead of degrading to "no token" like every other read failure.
        return "", False


class WmuxClient:
    """Coalescing two-lane reporter drained on one daemon worker thread."""

    def __init__(
        self,
        pipe_path: Optional[str] = None,
        surface_id: Optional[str] = None,
        token: Optional[str] = None,
    ) -> None:
        self._surface_id = surface_id or os.environ.get("WMUX_SURFACE_ID") or ""
        self._pipe = pipe_path or os.environ.get("WMUX_PIPE") or DEFAULT_PIPE
        self._active = bool(os.environ.get("WMUX") == "1" and self._surface_id)

        if self._active and not is_pipe_path(self._pipe):
            # DEACTIVATE rather than open it. Opening loses either way: the
            # envelope carries the auth token, so writing to a regular file
            # destroys that file AND leaks a live credential in cleartext
            # (both measured). A dead plugin is the strictly better failure.
            #
            # The path is NOT echoed into the warning: it is attacker-
            # controlled text, and the operator can read their own env var.
            self._active = False
            warn_once(
                "bad-pipe",
                "wmux: WMUX_PIPE is not a named-pipe path "
                "(expected \\\\.\\pipe\\<name>); the plugin is disabled "
                "rather than write the pipe token to an arbitrary file",
            )

        if token is None and self._active:
            token, _ = resolve_token()
            if not token:
                # The ONLY observable symptom of an unauthenticated plugin:
                # everything else in this module logs at debug level.
                logger.warning(
                    "wmux: no pipe token (WMUX_PIPE_TOKEN unset and no "
                    "pipe-token file); agent-state reports are "
                    "unauthenticated and will likely be rejected"
                )
        self._token = token or ""

        # One condition guards every slot and every lifecycle flag.
        self._cond = threading.Condition()
        self._state: Optional[Dict[str, Any]] = None
        self._session: Optional[Dict[str, Any]] = None
        self._metadata: Optional[Dict[str, Any]] = None
        self._activity: Optional[Dict[str, Any]] = None
        self._release: Optional[Dict[str, Any]] = None

        # Persistent high-water mark of the highest state generation ever
        # ADMITTED. Compared against instead of the slot's contents, because
        # the slot is EMPTIED on drain -- a contents-based check degrades to
        # "accept anything" exactly in the window that matters.
        self._state_gen_hwm = 0

        self._closing = False
        self._release_scheduled = False
        self._released = threading.Event()
        self._idle_hook: Optional[Callable[[], None]] = None

        self._seq_lock = threading.Lock()
        self._seq = int(time.time() * 1000) * 1000
        self._worker: Optional[threading.Thread] = None
        if self._active:
            self._worker = threading.Thread(
                target=self._run, name="wmux-reporter", daemon=True
            )
            self._worker.start()

    @property
    def active(self) -> bool:
        return self._active

    def set_idle_hook(self, hook: Optional[Callable[[], None]]) -> None:
        """Install the callable ticked from the worker's idle wait.

        The client never learns what the hook does; it only guarantees the
        cadence, that ``_cond`` is RELEASED at call time, and that a raising
        hook cannot kill the worker.
        """
        with self._cond:
            self._idle_hook = hook

    # -- public API ----------------------------------------------------

    def report_state(self, params: Dict[str, Any], generation: int) -> None:
        """Enqueue a state report on the critical lane, newest generation wins.

        ``generation`` is client-internal metadata: it gates the slot and
        never reaches the wire.
        """
        if not self._active:
            return
        with self._cond:
            if self._closing:
                return
            if generation <= self._state_gen_hwm:
                logger.debug(
                    "wmux: dropped stale state gen %s (high-water mark %s)",
                    generation,
                    self._state_gen_hwm,
                )
                return
            self._state_gen_hwm = generation
            self._state = dict(params)
            self._cond.notify()

    def report_session(self, session_id: Optional[str]) -> None:
        """Enqueue a durable session reference on the critical lane."""
        self._put("_session", {"sessionId": session_id})

    def report_metadata(
        self,
        model: Optional[str] = None,
        tokens: Optional[str] = None,
        context_pct: Optional[int] = None,
    ) -> None:
        """Enqueue pane metadata on the decorative lane."""
        params: Dict[str, Any] = {"ttlMs": _METADATA_TTL_MS}
        if model:
            params["model"] = model
        if tokens:
            params["tokens"] = tokens
        if context_pct is not None:
            params["contextPct"] = int(context_pct)
        self._put("_metadata", params)

    def report_activity(self, tool: Optional[str] = None, done: bool = False) -> None:
        """Enqueue an activity update on the decorative lane.

        ``agent.activity`` is a SEPARATE wmux method: ``pane.report_agent``
        has no ``message`` field.
        """
        params: Dict[str, Any] = {}
        if tool:
            params["tool"] = tool
        if done:
            params["done"] = True
        self._put("_activity", params)

    def release_and_close(self, timeout_s: float = _RELEASE_TIMEOUT_S) -> None:
        """Schedule exactly one ``pane.release_agent`` and wait, bounded."""
        if not self._active:
            return
        self._schedule_release()
        self._released.wait(timeout=timeout_s)

    def _put(self, slot: str, params: Dict[str, Any]) -> None:
        if not self._active:
            return
        with self._cond:
            if self._closing:
                return
            setattr(self, slot, params)
            self._cond.notify()

    def _schedule_release(self) -> None:
        with self._cond:
            if self._release_scheduled:
                return
            self._release_scheduled = True
            self._closing = True
            # Decorative work is not worth delaying shutdown for.
            self._metadata = None
            self._activity = None
            self._release = {}
            self._cond.notify()

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    # -- worker --------------------------------------------------------

    def _take_next_locked(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Pop the next job by priority. Caller holds ``self._cond``."""
        if self._state is not None:
            params, self._state = self._state, None
            return _M_STATE, params
        if self._session is not None:
            params, self._session = self._session, None
            return _M_SESSION, params
        if not self._closing:
            if self._metadata is not None:
                params, self._metadata = self._metadata, None
                return _M_METADATA, params
            if self._activity is not None:
                params, self._activity = self._activity, None
                return _M_ACTIVITY, params
        if self._closing and self._release is not None:
            params, self._release = self._release, None
            return _M_RELEASE, params
        return None

    def _run(self) -> None:
        while True:
            hook: Optional[Callable[[], None]] = None
            with self._cond:
                job = self._take_next_locked()
                while job is None:
                    if self._closing:
                        self._released.set()
                        return
                    signalled = self._cond.wait(_SWEEP_S)
                    job = self._take_next_locked()
                    if job is None and not signalled:
                        # Idle tick. Break out so the hook runs with _cond
                        # RELEASED (SPEC R-12 direction 2).
                        hook = self._idle_hook
                        break
            if hook is not None:
                # A raising hook would otherwise kill the worker: later
                # reports would queue with no consumer and even the release
                # would never go out -- a permanent ghost with zero symptom.
                try:
                    hook()
                except Exception:
                    logger.debug("wmux: idle hook failed", exc_info=True)
                continue
            if job is None:
                continue
            method, params = job
            try:
                self._send(method, params)
            except Exception:
                # Guarded for the SAME reason as the idle hook eight lines
                # above, and with the same consequence: an escaping
                # exception kills the daemon worker, after which every later
                # _put queues with no consumer, the pane freezes on its last
                # state, and even the release below never goes out. That is
                # unrecoverable AND invisible -- release_and_close would
                # simply wait out its timeout and return.
                #
                # _send already swallows transport failures internally; this
                # catches the ones it cannot foresee (a JSON-unserialisable
                # param, an OOM in json.dumps) so an unforeseen bug degrades
                # to one lost report instead of a permanent ghost.
                warn_once(
                    "send-raised",
                    "wmux: a report failed unexpectedly and was dropped; "
                    "pane state may lag until the next transition",
                )
                logger.debug("wmux: _send raised for %s", method, exc_info=True)
            if method == _M_RELEASE:
                self._released.set()
                return

    def _send(self, method: str, params: Dict[str, Any]) -> None:
        wire: Dict[str, Any] = {"surfaceId": self._surface_id}
        if method in _SEQ_METHODS:
            # Stamped here, AFTER lane selection, so wire order stays
            # monotonic even when critical work overtook decorative work.
            wire["seq"] = self._next_seq()
        wire.update(params)
        envelope = {"method": method, "params": wire, "id": 1, "token": self._token}
        payload = (json.dumps(envelope) + "\n").encode("utf-8")

        deadline = (
            time.monotonic() + _RELEASE_TIMEOUT_S if method == _M_RELEASE else None
        )
        # Seeded, because the release path can exhaust its wall budget before
        # the first attempt runs at all -- leaving `detail` unbound.
        detail = "no attempt within the release budget"
        for attempt in range(_SEND_ATTEMPTS):
            timeout_s = _REPLY_TIMEOUT_S
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                timeout_s = min(_REPLY_TIMEOUT_S, remaining)
            try:
                reply = _transport(self._pipe, payload, timeout_s)
            except Exception:
                logger.debug("wmux: transport raised for %s", method, exc_info=True)
                reply = None
            # `detail` may embed the SERVER's own free text, and it lands in
            # a warning on a terminal the developer TRUSTS -- so it arrives
            # ALREADY sanitized and length-capped from `classify_reply`,
            # which is the single point where untrusted bytes become text.
            verdict, detail = classify_reply(reply)
            if verdict == DELIVERED:
                return
            if verdict == REJECTED:
                # The server refused us. This is the failure mode that
                # produces a CONFIDENTLY WRONG pane: a stale-but-present
                # token passes the empty-token guard at construction, so
                # every report is refused server-side while the pane keeps
                # displaying the last state it managed to set.
                #
                # It warns ONCE per process rather than per report: the
                # cause is persistent (a bad token stays bad), so warning
                # every time would bury the terminal. And it must warn at
                # all, because a debug log here is discarded outright --
                # see wmux/diagnostics.py.
                warn_once(
                    "rejected",
                    "wmux: the pane rejected an agent-state report (%s); "
                    "the pane will keep showing a STALE state. Check "
                    "WMUX_PIPE_TOKEN / the pipe-token file.",
                    detail,
                )
            if attempt + 1 >= _SEND_ATTEMPTS:
                break
            if deadline is not None and time.monotonic() + _SEND_BACKOFF_S >= deadline:
                break
            time.sleep(_SEND_BACKOFF_S)
        logger.debug(
            "wmux: %s undelivered after %d attempts (%s)",
            method,
            _SEND_ATTEMPTS,
            detail,
        )


__all__ = ["WmuxClient", "DEFAULT_PIPE", "resolve_token"]
