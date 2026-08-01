"""Effectiveness probes for the concurrency adapter (AC-9).

The adapter couples to private core internals (``_get_approval_async_lock``,
``_run_command_inner``), so a core refactor can rename a target and leave the
plugin *installed but inert*.  Checking that the symbols merely exist would not
notice: the B-1 finding survived a whole review round precisely because a
``hasattr`` test cannot see it.

Every probe therefore exercises the patch and asserts its EFFECT, one probe per
target.  ``concurrency.selftest()`` runs them at plugin start and fails loudly.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional

from . import concurrency as _c

_PROBE_LINE = "__cp_discord_selftest_line__"
"""Marks the one line :func:`probe_dfg` emits, so it can be told apart from
anything another thread emits while the global bus is borrowed."""


@contextmanager
def _borrowed_bus() -> Iterator[Any]:
    """Route the global bus into a throwaway instance for the duration.

    Driving the REAL patched ``emit_shell_line`` is the only way to prove F
    works, and that function resolves ``get_message_bus()`` at call time with
    no other seam — so the probe has to own the global for a moment.  Anything
    another thread emits meanwhile is forwarded on afterwards rather than
    dropped: a probe must not cost the product a message.
    """
    from code_puppy.messaging import bus as bus_module

    probe = bus_module.MessageBus()
    with bus_module._bus_lock:
        previous = bus_module._global_bus
        bus_module._global_bus = probe
    try:
        yield probe
    finally:
        with bus_module._bus_lock:
            bus_module._global_bus = previous
        target = bus_module.get_message_bus()
        for message in probe.get_buffered_messages():
            if getattr(message, "line", None) != _PROBE_LINE:
                target.emit(message)


def probe_a() -> Optional[str]:
    """All five session symbols read and write the plugin's ContextVar."""
    import code_puppy.messaging as messaging
    import code_puppy.tools.agent_tools as agent_tools
    import code_puppy.tools.subagent_invocation as sai

    with _c.session_scope("__cp_discord_selftest__"):
        for module in (messaging, sai, agent_tools):
            if module.get_session_context() != "__cp_discord_selftest__":
                return f"A: {module.__name__}.get_session_context ignored the session"
        for module in (sai, agent_tools):
            module.set_session_context("__cp_discord_selftest_2__")
            if _c.current_session_id() != "__cp_discord_selftest_2__":
                return f"A: {module.__name__}.set_session_context did not mirror"
            module.set_session_context("__cp_discord_selftest__")
    return None


def probe_b() -> Optional[str]:
    """Approval locks are keyed per session, not process-wide."""
    import code_puppy.tools.common as common

    try:
        with _c.session_scope("__cp_discord_selftest_a__"):
            lock_a = common._get_approval_async_lock()
            same = common._get_approval_async_lock()
        with _c.session_scope("__cp_discord_selftest_b__"):
            lock_b = common._get_approval_async_lock()
    finally:
        _c.release_session("__cp_discord_selftest_a__")
        _c.release_session("__cp_discord_selftest_b__")
    if lock_a is not same:
        return "B: the same session got two different locks"
    if lock_a is lock_b:
        return "B: two sessions share one lock (no per-session keying)"
    return None


def probe_c() -> Optional[str]:
    """The backend must SEE the session id — being called is not enough.

    Also checks INV-7's None pass-through: were it broken, every stdin
    approval in the product would break the moment the plugin loads.
    """
    import code_puppy.tools.common as common

    seen: List[Any] = []

    def _probe(sid, title, message, preview=None):
        seen.append(sid)
        return False, None

    setattr(_probe, _c.SENTINEL, True)
    previous = common._APPROVAL_BACKEND  # touch the slot directly, restore below
    try:
        common._APPROVAL_BACKEND = _probe
        with _c.session_scope("__cp_discord_selftest__"):
            bound = common.get_approval_backend()
            if bound is None:
                return "C: the backend vanished behind the wrapper"
            bound("title", "message", None)
        if seen != ["__cp_discord_selftest__"]:
            return f"C: the backend saw {seen!r} instead of the session id"
        common._APPROVAL_BACKEND = None
        if common.get_approval_backend() is not None:
            return "C: None is not passed through (stdin approvals would break)"
    finally:
        common._APPROVAL_BACKEND = previous
    return None


def probe_e() -> Optional[str]:
    """E must really cross the executor boundary — chaining here is inert.

    A delegating wrapper installs cleanly, keeps AC-1 green and does nothing,
    so this probe drives the patched ``_run_command_inner`` for real and looks
    for the artefacts only the Form-2 body can produce: a per-call marker, at
    least one reader stamped inside the pool thread, and every stamp cleared
    afterwards.

    ``silent=True`` keeps it invisible: it suppresses both ``emit_shell_line``
    and the ``ShellOutputMessage`` (``command_runner.py:1035-1044``), so no
    probe output can reach a channel.  The command produces no output and
    exits immediately.
    """
    import code_puppy.tools.command_runner as command_runner

    quiet_command = "cd ." if _c._is_windows() else "true"
    observed: List[Any] = []
    outcome: List[Any] = []

    def _run() -> None:
        async def _drive():
            with _c.session_scope("__cp_discord_selftest__"):
                _c._MARKER_OBSERVER.set(observed)
                return await command_runner._run_command_inner(
                    quiet_command, None, 30, "__cp_discord_selftest__", silent=True
                )

        try:
            outcome.append(asyncio.run(_drive()))
        except Exception as e:
            outcome.append(e)

    # Own thread + own loop: selftest() is sync and may be called from a
    # context that already has a running loop.
    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=30)
    if worker.is_alive():
        return "E: the probe command did not finish within 30s"
    if outcome and isinstance(outcome[0], Exception):
        return f"E: the probe command raised {outcome[0]!r}"
    if not observed:
        return "E: no per-call marker was created (chaining wrapper? patch is inert)"
    marker = observed[0]
    if not marker.readers:
        return "E: no reader thread was marked (the context never reached the pool)"
    stale = [
        t for t in marker.readers if getattr(t, _c.THREAD_SID_ATTR, None) is not None
    ]
    if stale:
        return f"E: {len(stale)} reader(s) kept their marking after the call"
    return None


def probe_dfg() -> Optional[str]:
    """One line, produced the way a real shell line is: pool thread -> reader.

    The line goes through the REAL ``command_runner.emit_shell_line`` — the
    patch target itself — not through F's ``_reader_session`` helper.  Calling
    the helper only proves the helper works: F refactored into a plain
    delegation installs cleanly, keeps its sentinel, and silently lets shell
    lines carry a foreign session, which such a probe cannot see (measured).
    The stray line is kept off the live bus by borrowing the global bus rather
    than by bypassing the patch.
    """
    import code_puppy.tools.command_runner as command_runner

    marker = _c._ShellMarker("__cp_discord_selftest__")
    result: List[Any] = []

    def _in_reader_thread():
        # No session scope here on purpose: a reader thread starts with an
        # empty context, so ONLY F's adoption of G's stamp can attribute this.
        command_runner.emit_shell_line(_PROBE_LINE, stream="stdout")

    def _in_pool_thread():
        reader = threading.Thread(target=_in_reader_thread)
        reader.start()
        reader.join(timeout=5)
        result.append(getattr(reader, _c.THREAD_SID_ATTR, None))

    def _pool_body():
        ctx = contextvars.copy_context()
        ctx.run(_c._marked_call, marker, _in_pool_thread)

    with _borrowed_bus() as probe_bus:
        with _c.session_scope("__cp_discord_selftest__"):
            pool = threading.Thread(target=_pool_body)
            pool.start()
            pool.join(timeout=5)
            if getattr(pool, _c.THREAD_SID_ATTR, None) is not None:
                return "G: the pool thread was marked (only readers may be)"
        emitted = [
            m
            for m in probe_bus.get_buffered_messages()
            if getattr(m, "line", None) == _PROBE_LINE
        ]

    if result != ["__cp_discord_selftest__"]:
        return f"G: the reader thread carried {result!r}"
    if len(emitted) != 1:
        return f"F: the reader thread's line reached the bus {len(emitted)} times"
    if emitted[0].session_id != "__cp_discord_selftest__":
        return (
            "D+F: the reader thread's line was tagged "
            f"{emitted[0].session_id!r} instead of the session"
        )

    _c._invalidate_readers(marker)
    if any(getattr(t, _c.THREAD_SID_ATTR, None) is not None for t in marker.readers):
        return "E: the reader marking was not invalidated"
    return None


PROBES = (probe_a, probe_b, probe_c, probe_e, probe_dfg)
