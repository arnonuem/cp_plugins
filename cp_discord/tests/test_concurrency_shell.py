"""L1 shell path — AC-43, AC-49, AC-51, AC-57, AC-58, AC-59.

Shell output is produced three thread-hops away from the channel that asked
for it::

    gateway loop            <- session_scope(sid) is entered HERE
      -> _SHELL_EXECUTOR pool thread          (command_runner.py:1490-1492)
           -> reader threads read_stdout/read_stderr   (command_runner.py:981-982)

A ContextVar survives neither hop, so patch E bridges the first
(``copy_context``/``ctx.run``) and patches G+F bridge the second (a stamp on
the thread object).  These tests drive the REAL ``_run_command_inner`` with a
real subprocess, because that is the only way to prove the whole chain.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import code_puppy.tools.command_runner as command_runner
from code_puppy.messaging.bus import MessageBus
from code_puppy.plugins.cp_discord import concurrency

ECHO = "echo cp-discord-line"


@pytest.fixture(autouse=True)
def _clean_state():
    concurrency.uninstall()
    concurrency.set_enabled(True)
    concurrency._SESSION_ID.set(None)
    yield
    concurrency.uninstall()
    concurrency.set_enabled(True)
    concurrency._SESSION_ID.set(None)


@pytest.fixture
def installed():
    concurrency.install()
    yield
    concurrency.uninstall()


@pytest.fixture
def probe_bus(monkeypatch):
    """Route the global bus into a throwaway instance we can drain."""
    from code_puppy.messaging import bus as bus_module

    fresh = MessageBus()
    monkeypatch.setattr(bus_module, "_global_bus", fresh)
    return fresh


@pytest.fixture
def single_worker(monkeypatch):
    """One pool worker so "the same pool thread" is guaranteed, not hoped for.

    Resolved through the module at call time by both the patched and the
    original ``_run_command_inner`` (``command_runner.py:1491``), which is why
    E must not capture the executor at install time.
    """
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test_shell_")
    monkeypatch.setattr(command_runner, "_SHELL_EXECUTOR", executor)
    yield executor
    executor.shutdown(wait=True)


@pytest.fixture
def pool_probe(monkeypatch):
    """Record the pool thread each command runs on, plus its stamp on entry."""
    seen: list[tuple[threading.Thread, object]] = []
    real = command_runner._run_command_sync

    def _probe(*args, **kwargs):
        thread = threading.current_thread()
        seen.append((thread, getattr(thread, concurrency.THREAD_SID_ATTR, "<unset>")))
        return real(*args, **kwargs)

    monkeypatch.setattr(command_runner, "_run_command_sync", _probe)
    return seen


@pytest.fixture
def markers(monkeypatch):
    """Capture every per-call marker so tests can reach ``marker.readers``.

    AC-49 demands the reader thread OBJECTS themselves: an invalidation loop
    over an empty list would otherwise look green while doing nothing.
    """
    captured: list = []
    real = concurrency._ShellMarker

    def _record(sid):
        marker = real(sid)
        captured.append(marker)
        return marker

    monkeypatch.setattr(concurrency, "_ShellMarker", _record)
    return captured


async def run_command(command: str = ECHO, timeout: int = 30):
    return await command_runner._run_command_inner(
        command, None, timeout, "grp", silent=False
    )


def lines(bus: MessageBus) -> list:
    return [
        m for m in bus.get_buffered_messages() if type(m).__name__ == "ShellLineMessage"
    ]


def outputs(bus: MessageBus) -> list:
    return [
        m
        for m in bus.get_buffered_messages()
        if type(m).__name__ == "ShellOutputMessage"
    ]


# ---------------------------------------------------------------------------
# AC-43 / AC-58 — the produced messages carry the session
# ---------------------------------------------------------------------------
async def test_ac43_baseline_shell_messages_are_unattributed(probe_bus, single_worker):
    """A/B part 1: unpatched, reader-thread output carries no session at all."""
    with concurrency.session_scope("discord:43"):
        result = await run_command()

    assert result.success is True
    assert [m.line for m in lines(probe_bus)] == ["cp-discord-line"]
    assert all(m.session_id is None for m in lines(probe_bus))
    assert all(m.session_id is None for m in outputs(probe_bus))


async def test_ac43_shell_line_and_shell_output_carry_the_session(
    installed, probe_bus, single_worker
):
    with concurrency.session_scope("discord:43"):
        result = await run_command()

    assert result.success is True
    emitted = lines(probe_bus)
    assert [m.line for m in emitted] == ["cp-discord-line"]
    assert [m.session_id for m in emitted] == ["discord:43"]

    summary = outputs(probe_bus)
    assert len(summary) == 1, (
        "ShellOutputMessage is the run's single most useful artefact"
    )
    assert summary[0].session_id == "discord:43"
    assert summary[0].exit_code == 0


async def test_ac58_the_very_first_line_already_carries_the_session(
    installed, probe_bus, single_worker
):
    """``Thread.start()`` returns while ``run()`` is already going — the stamp
    must be set BEFORE ``original(self)`` or the first line races it."""
    with concurrency.session_scope("discord:58"):
        await run_command()

    emitted = lines(probe_bus)
    assert emitted, "no shell line was emitted at all"
    assert emitted[0].session_id == "discord:58"


# ---------------------------------------------------------------------------
# AC-49 / AC-57 — what gets marked, and for how long
# ---------------------------------------------------------------------------
async def test_ac49_marking_lives_exactly_as_long_as_the_command(
    installed, probe_bus, single_worker, markers, monkeypatch
):
    during: list = []
    original_emit = command_runner.emit_shell_line

    def _sample(line, stream="stdout"):
        thread = threading.current_thread()
        during.append((thread, getattr(thread, concurrency.THREAD_SID_ATTR, None)))
        return original_emit(line, stream=stream)

    monkeypatch.setattr(command_runner, "emit_shell_line", _sample)

    with concurrency.session_scope("discord:49"):
        await run_command()

    assert len(markers) == 1
    readers = markers[0].readers
    assert len(readers) == 2, f"expected stdout+stderr readers, got {len(readers)}"

    # ... during: the emitting thread is one of the marked readers, and stamped
    assert during, "no line was emitted, so 'during' proves nothing"
    for thread, sid in during:
        assert thread in readers, "the emitting thread was never marked"
        assert sid == "discord:49"

    # ... after: every reader object we handed out is unstamped again
    assert [getattr(t, concurrency.THREAD_SID_ATTR, "<unset>") for t in readers] == [
        None,
        None,
    ]


async def test_ac57_the_pool_thread_is_never_marked(
    installed, probe_bus, single_worker, pool_probe, markers
):
    """``Executor.submit()`` starts pool threads synchronously in the CALLING
    thread; a marker sitting on the loop would have registered them."""
    with concurrency.session_scope("discord:57"):
        await run_command()

    assert len(pool_probe) == 1
    pool_thread, stamp_on_entry = pool_probe[0]
    assert stamp_on_entry == "<unset>", "the pool thread was marked before it ran"
    assert getattr(pool_thread, concurrency.THREAD_SID_ATTR, None) is None
    assert pool_thread not in markers[0].readers


# ---------------------------------------------------------------------------
# AC-59 — G is stdlib and process-wide: inert without a marker, never raises
# ---------------------------------------------------------------------------
def test_ac59_threads_outside_the_window_are_not_marked(installed):
    """py-cord, httpx, MCP and the gateway loop all start threads too."""
    started: list[threading.Thread] = []
    for _ in range(3):
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        started.append(t)

    assert all(getattr(t, concurrency.THREAD_SID_ATTR, None) is None for t in started)


def test_ac59_thread_start_never_raises_out(installed, monkeypatch):
    """A propagated error would kill thread creation in the WHOLE process."""

    def _boom():
        raise RuntimeError("plugin logic exploded")

    monkeypatch.setattr(concurrency, "_enabled", _boom)

    token = concurrency._SHELL_MARKER.set(concurrency._ShellMarker("discord:59"))
    try:
        ran = threading.Event()
        t = threading.Thread(target=ran.set)
        t.start()
        t.join(timeout=5)
    finally:
        concurrency._SHELL_MARKER.reset(token)

    assert ran.is_set(), "the thread did not run"
    assert getattr(t, concurrency.THREAD_SID_ATTR, None) is None


def test_ac59_janitor_thread_inside_the_window_is_harmless(installed):
    """``detach_to_background`` starts its janitor IN the pool thread, so it is
    marked too (``command_runner.py:944-946``).  It emits nothing
    (``shell_backgrounding.py``: wait/write_line/close), so the effect is nil —
    an absolutely-worded test would be falsely red here."""
    marker = concurrency._ShellMarker("discord:59")
    token = concurrency._SHELL_MARKER.set(marker)
    try:
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
    finally:
        concurrency._SHELL_MARKER.reset(token)

    assert getattr(t, concurrency.THREAD_SID_ATTR, None) == "discord:59"
    assert t in marker.readers  # and therefore reachable for invalidation
    concurrency._invalidate_readers(marker)
    assert getattr(t, concurrency.THREAD_SID_ATTR, None) is None


# ---------------------------------------------------------------------------
# AC-51 — thread reuse must never mis-attribute
# ---------------------------------------------------------------------------
async def test_ac51a_two_sessions_over_one_pool_thread(
    installed, probe_bus, single_worker, pool_probe
):
    with concurrency.session_scope("discord:A"):
        await run_command("echo from-A")
    with concurrency.session_scope("discord:B"):
        await run_command("echo from-B")

    assert len(pool_probe) == 2
    assert pool_probe[0][0] is pool_probe[1][0], "not the same pool thread"

    tagged = {(m.line, m.session_id) for m in lines(probe_bus)}
    assert tagged == {("from-A", "discord:A"), ("from-B", "discord:B")}


async def test_ac51c_a_fallback_command_does_not_inherit_the_old_session(
    installed, probe_bus, single_worker, pool_probe
):
    """The marker is a ContextVar in E's COPIED context, so it cannot leak into
    the next command over the same worker (Finding R6-W5)."""
    with concurrency.session_scope("discord:A"):
        await run_command("echo from-A")

    await run_command("echo no-session")  # fallback: E delegates to the original

    concurrency.set_enabled(False)
    with concurrency.session_scope("discord:A"):
        await run_command("echo disabled")  # fallback: runtime toggle off
    concurrency.set_enabled(True)

    assert len({probe[0] for probe in pool_probe}) == 1, "not the same pool thread"
    tagged = {(m.line, m.session_id) for m in lines(probe_bus)}
    assert tagged == {
        ("from-A", "discord:A"),
        ("no-session", None),
        ("disabled", None),
    }


async def test_ac51b_a_zombie_reader_emits_to_the_system_channel(
    installed, probe_bus, single_worker, markers, monkeypatch
):
    """Timeout kills the command while a reader lives on.

    ``cleanup_process_and_threads`` joins with ``timeout=3`` and returns even
    with readers still alive (``command_runner.py:888-906``).  The zombie's
    late lines must carry no session — the system channel — never the next
    channel's.  A stand-in for ``_run_command_sync`` makes the surviving reader
    deterministic; everything else (E, G, F, D) is the real chain.
    """
    release = threading.Event()
    emitted_late = threading.Event()
    real_sync = command_runner._run_command_sync

    def _zombie_reader():
        release.wait(timeout=10)
        command_runner.emit_shell_line("late-zombie-line", stream="stdout")
        emitted_late.set()

    def _abandoning_sync(command, cwd, timeout, group_id, silent=False):
        if command != "zombie":
            return real_sync(command, cwd, timeout, group_id, silent)
        reader = threading.Thread(target=_zombie_reader, daemon=True)
        reader.start()  # marked by G, appended to marker.readers
        command_runner.emit_shell_line("in-time-line", stream="stdout")
        return command_runner.ShellCommandOutput(
            success=False,
            command=command,
            error="Command timed out",
            stdout="",
            stderr="",
            exit_code=-9,
            execution_time=0.0,
            timeout=True,
        )

    monkeypatch.setattr(command_runner, "_run_command_sync", _abandoning_sync)

    with concurrency.session_scope("discord:A"):
        await run_command("zombie")

    with concurrency.session_scope("discord:B"):
        await run_command("echo from-B")

    release.set()
    assert emitted_late.wait(timeout=10), "the zombie reader never emitted"

    tagged = {(m.line, m.session_id) for m in lines(probe_bus)}
    assert ("in-time-line", "discord:A") in tagged
    assert ("from-B", "discord:B") in tagged
    assert ("late-zombie-line", None) in tagged, (
        f"the zombie was mis-attributed: {tagged}"
    )
    assert all(
        getattr(t, concurrency.THREAD_SID_ATTR, "<unset>") is None
        for t in markers[0].readers
    )
