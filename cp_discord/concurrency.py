"""Concurrency adapter — per-session attribution and approval locks.

Code Puppy keeps two pieces of *process-wide* state that make several Discord
channels step on each other:

* ``MessageBus._current_session_id`` (``messaging/bus.py:93``) is a single
  global, so two channels streaming at the same time steal each other's output;
* ``_APPROVAL_ASYNC_LOCK`` (``tools/common.py:40``) wraps the whole approval
  path (``tools/common.py:1422``), so one unanswered gate in channel A blocks
  channel B's agent completely.

This module replaces ELEVEN symbols across NINE targets at plugin start and
rolls each one back at stop.  There is no core change.

Two patch forms, and the difference matters:

* **Form 1 — chaining.** The wrapper always delegates to the original
  (A1, A2, A3, B, C, D, F, G).
* **Form 2 — replacement with fallback.** Only ``E``.  The executor hand-off
  lives in the *body* of ``_run_command_inner`` (``command_runner.py:1490``),
  so a delegating wrapper would install cleanly and do nothing.

Three thread boundaries are crossed, none of which a ``ContextVar`` survives on
its own (INV-6): ``session_scope`` (gateway loop) -> ``_SHELL_EXECUTOR`` pool
thread (bridged by E via ``copy_context``/``ctx.run``) -> reader threads
(``command_runner.py:981``, which inherit *no* context at all; bridged by G
stamping ``thread._cp_discord_sid`` and F adopting it).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)
"""Every non-fast-path handler reports; patch G deliberately does not.

The handlers swallow to stay safe — a raising wrapper would take a core call
path down with it.  Swallowing SILENTLY is the different thing: if
``_session_lock`` fails systematically, every approval quietly queues on the
core's process-wide lock again — the serialisation patch B exists to remove —
and the symptom is "Discord feels slow" with nothing to diagnose it.  So:
recoverable fallbacks log DEBUG, ``install()`` logs WARNING (a patch that did
not install is an outage), and G is exempt per :func:`_wrap_thread_start`.
"""

SENTINEL = "_cp_discord"
"""Marks every wrapper we install (INV-5), so we never patch or roll back twice."""

EXPECTED_PATCHES = frozenset(
    {"A1", "A2-get", "A2-set", "A3-get", "A3-set", "B", "C", "D", "E", "F", "G"}
)
"""The eleven symbols that MUST be live.  Spelled out rather than derived from
``_patch_targets()``, so a target the core renamed away — which ``install()``
skips, since each target is isolated — is counted as missing instead of
shrinking the expectation to match reality (AC-9)."""

THREAD_SID_ATTR = "_cp_discord_sid"
"""Session id stamped on a reader thread by G and read back by F."""

DEFAULT_LOCK_KEY = "__default__"
"""Sessionless callers share one lock — exactly the pre-plugin behaviour."""

# The plugin's own session id.  Read first by every patched getter; when it is
# unset we delegate to the core's global, so the TUI keeps working unchanged.
_SESSION_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "cp_discord_session_id", default=None
)


@dataclass
class _ShellMarker:
    """Per-call marker that opens G's window inside the pool thread.

    ``readers`` is the ONLY handle anyone has on the reader threads: they are
    locals of ``run_shell_command_streaming`` (``command_runner.py:712-713``),
    never returned, so E cannot reach them.  G appends each thread it stamps;
    E invalidates exactly this list when the call ends.
    """

    sid: str
    readers: List[threading.Thread] = field(default_factory=list)


# Lives in the *copied* context of E's ``ctx.run`` body, so it disappears by
# itself when that context exits.  A ``threading.local`` or a pool-thread
# attribute would linger and make a later fallback command stamp its readers
# with the previous session (silent channel mixing).
_SHELL_MARKER: contextvars.ContextVar[Optional[_ShellMarker]] = contextvars.ContextVar(
    "cp_discord_shell_marker", default=None
)

# Lets the selftest observe the markers E creates for ITS OWN call only.  A
# ContextVar (rather than a module global that the probe swaps out) keeps a
# concurrent real command completely unaffected.
_MARKER_OBSERVER: contextvars.ContextVar[Optional[List[_ShellMarker]]] = (
    contextvars.ContextVar("cp_discord_marker_observer", default=None)
)

# session id -> (event loop the lock was created on, lock)
_APPROVAL_LOCKS: dict[
    str, Tuple[Optional[asyncio.AbstractEventLoop], asyncio.Lock]
] = {}
_LOCKS_GUARD = threading.Lock()

_PATCHES: List[
    Tuple[str, Any, str, Any, Any]
] = []  # (name, owner, attr, original, wrapper)
_INSTALL_GUARD = threading.Lock()
_ENABLED = True


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
def set_enabled(enabled: bool) -> None:
    """Toggle the wrappers' effect at runtime; the patches stay installed."""
    global _ENABLED
    _ENABLED = bool(enabled)


def _enabled() -> bool:
    return _ENABLED and bool(_PATCHES)


def _is_windows() -> bool:
    return sys.platform.startswith("win")


@contextmanager
def session_scope(session_id: str) -> Iterator[None]:
    """Bind *session_id* to the current context (and every task copied from it)."""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError(f"session_id must be a non-empty string, got {session_id!r}")
    token = _SESSION_ID.set(session_id)
    try:
        yield
    finally:
        _SESSION_ID.reset(token)


def current_session_id() -> Optional[str]:
    """The session id bound to this context, or ``None`` outside any scope."""
    return _SESSION_ID.get()


def release_session(session_id: str) -> None:
    """Drop the session's approval lock.  Without this the dict grows forever."""
    with _LOCKS_GUARD:
        _APPROVAL_LOCKS.pop(session_id, None)


def is_installed() -> bool:
    return bool(_PATCHES)


def _current_sid() -> Optional[str]:
    """The session id the wrappers act on, or ``None`` to defer to the core."""
    if not _ENABLED:
        return None
    return _SESSION_ID.get()


# ---------------------------------------------------------------------------
# Patch A — session attribution (A1, A2-get/set, A3-get/set)
# ---------------------------------------------------------------------------
def _wrap_get_session_context(original: Callable[[], Optional[str]]):
    def _get() -> Optional[str]:
        try:
            sid = _current_sid()
        except Exception:
            logger.debug("Discord: session lookup failed", exc_info=True)
            sid = None
        return sid if sid is not None else original()

    return _get


def _wrap_set_session_context(original: Callable[[Optional[str]], None]):
    def _set(session_id: Optional[str]):
        try:
            if _enabled():
                _SESSION_ID.set(session_id)
        except Exception:
            # The mirror failed, so a sub-agent's output may go unattributed.
            logger.debug("Discord: could not mirror the session", exc_info=True)
        return original(session_id)

    return _set


# ---------------------------------------------------------------------------
# Patch B — per-session approval lock
# ---------------------------------------------------------------------------
def _session_lock(key: str) -> asyncio.Lock:
    """One lock per session, rebuilt when the caller runs on a foreign loop.

    ``_trigger_callbacks_sync`` drives async callbacks through
    ``asyncio.run(...)`` (``callbacks.py:271-278``); awaiting a lock cached for
    another loop would break, so the loop is part of the cache identity.
    """
    try:
        loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    with _LOCKS_GUARD:
        cached = _APPROVAL_LOCKS.get(key)
        if cached is not None and cached[0] is loop:
            return cached[1]
        lock = asyncio.Lock()
        _APPROVAL_LOCKS[key] = (loop, lock)
        return lock


def _wrap_get_approval_async_lock(original: Callable[[], asyncio.Lock]):
    def _factory() -> asyncio.Lock:
        try:
            if _enabled():
                return _session_lock(_current_sid() or DEFAULT_LOCK_KEY)
        except Exception:
            # Back on the core's process-wide lock: every channel serialises
            # again, the exact condition patch B removes.
            logger.debug("Discord: per-session lock unavailable", exc_info=True)
        return original()

    return _factory


# ---------------------------------------------------------------------------
# Patch C — session-bound approval backend (INV-7)
# ---------------------------------------------------------------------------
def _wrap_get_approval_backend(original: Callable[[], Optional[Callable]]):
    """Bind the session id on the loop, where it is still visible.

    ``get_approval_backend()`` is resolved at ``common.py:1442``, immediately
    before ``run_in_executor`` hands the backend to a worker thread that would
    see no ContextVar at all.  Binding here means the returned closure carries
    the id by itself.
    """

    def _get() -> Optional[Callable]:
        backend = original()
        try:
            # INV-7 clause 2: ``None`` MUST stay ``None`` — otherwise
            # ``if backend is not None`` (common.py:1443, :1247) is permanently
            # true and stdin approvals break product-wide.
            # INV-7 clause 1: a foreign backend (e.g. ACP's 3-arg callable)
            # is passed through untouched; wrapping it would call it with four
            # arguments on every approval.
            if backend is None or not _enabled():
                return backend
            if not getattr(backend, SENTINEL, False):
                return backend
            sid = _current_sid()  # INV-7 clause 4: no session -> None, not __default__
        except Exception:
            logger.debug("Discord: could not bind the backend", exc_info=True)
            return backend

        def _bound(title, message, preview=None, _backend=backend, _sid=sid):
            return _backend(_sid, title, message, preview)

        setattr(_bound, SENTINEL, True)  # INV-7 clause 1: L4 must recognise its own
        return _bound

    return _get


# ---------------------------------------------------------------------------
# Patch D — MessageBus.emit
# ---------------------------------------------------------------------------
def _wrap_bus_emit(original: Callable):
    """Fill ``session_id`` before the bus tags from its single global.

    ``emit`` reads ``self._current_session_id`` directly (``bus.py:109-112``),
    so patch A cannot reach it.
    """

    def _emit(self, message):
        try:
            if _enabled() and getattr(message, "session_id", None) is None:
                sid = _current_sid()
                if sid is not None:
                    message.session_id = sid
        except Exception:
            # Unattributed output does not vanish; it lands in the system
            # channel instead of the one that produced it.
            logger.debug("Discord: could not attribute a bus message", exc_info=True)
        return original(self, message)  # unbound method: pass self through

    return _emit


# ---------------------------------------------------------------------------
# Patches E/F/G — the shell path across two thread boundaries
# ---------------------------------------------------------------------------
def _marked_call(marker: _ShellMarker, func: Callable, *args):
    """Body of E's ``ctx.run``: open G's window inside the POOL thread.

    Opening it on the loop instead would register the pool thread itself —
    ``Executor.submit()`` starts pool threads synchronously in the calling
    thread — which is exactly what the marking VETO forbids.
    """
    token = _SHELL_MARKER.set(marker)
    try:
        return func(*args)
    finally:
        _SHELL_MARKER.reset(token)


def _invalidate_readers(marker: _ShellMarker) -> None:
    """Unstamp every reader this call marked (zombies then emit as ``None``).

    ``cleanup_process_and_threads`` joins with ``timeout=3`` and returns even
    with readers still alive (``command_runner.py:888-906``); an unstamped
    zombie line lands in the system channel, never in a foreign one.
    """
    try:
        readers = list(marker.readers)
    except Exception:
        logger.debug("Discord: could not read the reader list", exc_info=True)
        return
    for thread in readers:
        try:
            setattr(thread, THREAD_SID_ATTR, None)
        except Exception:
            # A reader left stamped can still emit into this channel after its
            # command ended.
            logger.debug("Discord: could not unstamp reader", exc_info=True)


def _wrap_thread_start(original: Callable):
    """Stamp reader threads with the session id BEFORE they can run.

    ``threading.Thread.start`` is stdlib and process-wide (py-cord, httpx, MCP,
    every ``ThreadPoolExecutor``), so: fast path first, never raise, and stay
    inert once uninstalled.

    **The one handler that stays silent, on purpose.**  Every thread in the
    process passes through here, almost none of them Discord's; logging would
    drown the log and risk re-entering the logging machinery from a half-
    started thread.  What it swallows is cosmetic: an unstamped line loses its
    channel and F routes it to the system channel.
    """

    def _start(self):
        try:
            marker = _SHELL_MARKER.get()
        except Exception:
            marker = None
        if marker is not None:
            try:
                if _enabled():
                    # Set before ``original(self)``: ``start()`` returns while
                    # ``run()`` is already going, so stamping afterwards would
                    # race the first output line.
                    setattr(self, THREAD_SID_ATTR, marker.sid)
                    marker.readers.append(self)
            except Exception:
                pass
        return original(self)

    return _start


@contextmanager
def _reader_session() -> Iterator[Optional[str]]:
    """Adopt this thread's stamp into the ContextVar for the duration."""
    sid = None
    token = None
    try:
        if _enabled():
            sid = getattr(threading.current_thread(), THREAD_SID_ATTR, None)
            if sid is not None and _SESSION_ID.get() != sid:
                token = _SESSION_ID.set(sid)
    except Exception:
        # Unstamped, this line lands in the system channel instead.
        logger.debug("Discord: could not adopt the reader session", exc_info=True)
        token = None
    try:
        yield sid
    finally:
        if token is not None:
            try:
                _SESSION_ID.reset(token)
            except Exception:
                logger.debug("Discord: could not reset the reader", exc_info=True)


def _wrap_emit_shell_line(original: Callable):
    """Reader-thread entry point: adopt the stamp, then delegate.

    It deliberately does not build the message itself — patch D fills
    ``session_id`` from the ContextVar we just set.
    """

    def _emit_shell_line(line, stream: str = "stdout"):
        with _reader_session():
            return original(line, stream=stream)

    return _emit_shell_line


def _wrap_run_command_inner(original: Callable):
    """FORM 2 — reimplement the executor hand-off (chaining would be inert).

    ``_SHELL_EXECUTOR`` and ``_run_command_sync`` are resolved through the
    module at call time, so tests can shrink the pool to one worker and the
    reuse case stays observable.
    """

    async def _inner(command, cwd, timeout, group_id, silent: bool = False):
        from code_puppy.tools import command_runner

        try:
            sid = _current_sid() if _enabled() else None
        except Exception:
            logger.debug("Discord: no session for this shell run", exc_info=True)
            sid = None
        if sid is None:
            return await original(command, cwd, timeout, group_id, silent=silent)

        marker = _ShellMarker(sid)
        observer = _MARKER_OBSERVER.get()
        if observer is not None:
            observer.append(marker)
        try:
            loop = asyncio.get_running_loop()
            ctx = contextvars.copy_context()
            args = (command, cwd, timeout, group_id, silent)
            future = loop.run_in_executor(
                command_runner._SHELL_EXECUTOR,
                lambda: ctx.run(
                    _marked_call, marker, command_runner._run_command_sync, *args
                ),
            )
        except Exception:
            # Our own machinery failed: behave exactly like the unpatched core.
            # The command still runs; its output just loses this channel.
            logger.debug("Discord: shell hand-off failed", exc_info=True)
            return await original(command, cwd, timeout, group_id, silent=silent)

        try:
            return await future
        except Exception as e:
            # Mirrors command_runner.py:1494-1526.
            if not silent:
                import traceback

                command_runner.emit_error(
                    traceback.format_exc(), message_group=group_id
                )
            return command_runner.ShellCommandOutput(
                success=False,
                command=command,
                error=f"Error executing command {str(e)}",
                stdout=None,
                stderr=None,
                exit_code=-1,
                execution_time=None,
                timeout=False,
            )
        finally:
            _invalidate_readers(marker)

    return _inner


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------
def _patch_targets() -> List[Tuple[str, Any, str, Callable[[Any], Any]]]:
    """The nine targets / eleven symbols, each bound the way its callers are.

    A patch target must be a symbol resolved *at call time* by the code that
    uses it (SPEC-L1 §0).  A module-level ``from X import y`` copies the name
    at import time, so patching the package attribute would miss it — hence
    A2/A3 exist next to A1.
    """
    import code_puppy.messaging as messaging
    import code_puppy.tools.agent_tools as agent_tools
    import code_puppy.tools.command_runner as command_runner
    import code_puppy.tools.common as common
    import code_puppy.tools.subagent_invocation as sai
    from code_puppy.messaging.bus import MessageBus

    return [
        # A1 — function-local imports (event_stream_handler.py:49,
        # subagent_stream_handler.py:113) resolve the package attribute.
        ("A1", messaging, "get_session_context", _wrap_get_session_context),
        # A2/A3 — module-level imports (subagent_invocation.py:29-30,
        # agent_tools.py:22-23); A1 does NOT reach these copies.
        ("A2-get", sai, "get_session_context", _wrap_get_session_context),
        ("A2-set", sai, "set_session_context", _wrap_set_session_context),
        ("A3-get", agent_tools, "get_session_context", _wrap_get_session_context),
        ("A3-set", agent_tools, "set_session_context", _wrap_set_session_context),
        # B/C — resolved as module globals inside common.py (:1422, :1442).
        ("B", common, "_get_approval_async_lock", _wrap_get_approval_async_lock),
        ("C", common, "get_approval_backend", _wrap_get_approval_backend),
        # D — instance method lookup goes through the class (bus.py:146,178).
        ("D", MessageBus, "emit", _wrap_bus_emit),
        # E/F — module globals inside command_runner.py (:1353, :729).
        ("E", command_runner, "_run_command_inner", _wrap_run_command_inner),
        ("F", command_runner, "emit_shell_line", _wrap_emit_shell_line),
        # G — stdlib method, reached through the class on every Thread.
        ("G", threading.Thread, "start", _wrap_thread_start),
    ]


def install() -> None:
    """Install all eleven wrappers.  Idempotent; each target is isolated."""
    with _INSTALL_GUARD:
        if _PATCHES:
            return
        for name, owner, attr, factory in _patch_targets():
            try:
                original = getattr(owner, attr)
                if getattr(original, SENTINEL, False):
                    continue  # already ours (e.g. a reload) — never double-wrap
                wrapper = factory(original)
                setattr(wrapper, SENTINEL, True)
                setattr(owner, attr, wrapper)
                _PATCHES.append((name, owner, attr, original, wrapper))
            except Exception:
                # WARNING, not debug: isolation keeps one core rename from
                # taking the other ten down, but a target that did not install
                # is a hole in the adapter.  ``selftest()`` refuses the boot
                # over it; this line is the only thing saying WHICH and WHY.
                logger.warning(
                    "Discord: patch %s (%s) did not install; the selftest will "
                    "refuse to start",
                    name,
                    attr,
                    exc_info=True,
                )
                continue


def uninstall() -> None:
    """Roll back every wrapper we still own.  Idempotent.

    A target someone else patched on top of is left alone — restoring it would
    destroy the other patch.  ``G`` can therefore outlive us, which is why its
    wrapper is inert once ``_PATCHES`` is empty.
    """
    with _INSTALL_GUARD:
        while _PATCHES:
            name, owner, attr, original, wrapper = _PATCHES.pop()
            try:
                if getattr(owner, attr) is wrapper:
                    setattr(owner, attr, original)
            except Exception:
                logger.warning(
                    "Discord: patch %s could not be rolled back", name, exc_info=True
                )
    with _LOCKS_GUARD:
        _APPROVAL_LOCKS.clear()


# ---------------------------------------------------------------------------
# Selftest (AC-9)
# ---------------------------------------------------------------------------
def selftest() -> Tuple[bool, str]:
    """Prove every patch still WORKS — existence checks miss silent breakage.

    A core refactor that renames a target, or a wrapper that installs but has
    no effect, must fail loudly at plugin start instead of degrading into
    cross-channel mixing.  The probes live in ``concurrency_selftest``.
    """
    if not _PATCHES:
        return False, "concurrency adapter is not installed"
    if not _ENABLED:
        return False, "concurrency adapter is installed but disabled"

    live = {
        name
        for name, owner, attr, _original, _wrapper in _PATCHES
        if getattr(getattr(owner, attr, None), SENTINEL, False)
    }
    missing = EXPECTED_PATCHES - live
    if missing:
        return False, f"patches no longer active: {', '.join(sorted(missing))}"

    from . import concurrency_selftest

    for probe in concurrency_selftest.PROBES:
        try:
            failure = probe()
        except Exception as e:  # a raising probe is a failing probe
            return False, f"{probe.__name__} raised: {e!r}"
        if failure:
            return False, failure

    return True, f"all {len(EXPECTED_PATCHES)} patches verified"
