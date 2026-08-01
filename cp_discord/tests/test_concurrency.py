"""L1 concurrency adapter — AC-1..11, 37, 38, 42..45, 49, 51, 56..61.

The adapter replaces ELEVEN symbols across NINE patch targets so that several
Discord channels can run agents in parallel without stealing each other's
output or serialising each other's approval gates.  These tests check the
EFFECT of every patch, not merely its presence: a wrapper that installs
cleanly and does nothing is the exact failure mode this layer was built to
avoid.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import code_puppy.messaging as messaging
import code_puppy.tools.agent_tools as agent_tools
import code_puppy.tools.command_runner as command_runner
import code_puppy.tools.common as common
import code_puppy.tools.subagent_invocation as sai
from code_puppy.messaging.bus import MessageBus
from code_puppy.plugins.cp_discord import concurrency

# The eleven patched symbols, as (owner, attribute) pairs.  AC-1/AC-7 are
# count-dependent, so the list is spelled out instead of derived from the
# module under test.
PATCHED_SYMBOLS = [
    ("A1", messaging, "get_session_context"),
    ("A2-get", sai, "get_session_context"),
    ("A2-set", sai, "set_session_context"),
    ("A3-get", agent_tools, "get_session_context"),
    ("A3-set", agent_tools, "set_session_context"),
    ("B", common, "_get_approval_async_lock"),
    ("C", common, "get_approval_backend"),
    ("D", MessageBus, "emit"),
    ("E", command_runner, "_run_command_inner"),
    ("F", command_runner, "emit_shell_line"),
    ("G", threading.Thread, "start"),
]

#: The pristine symbols, captured at import time -- before anything can have
#: installed.  Needed because several tests deliberately patch OVER a target,
#: which ``uninstall`` then rightly refuses to evict; ``monkeypatch`` undoing
#: that afterwards restores OUR wrapper as the module's own symbol, and
#: ``install()`` skips every sentinel-marked target as already-ours
#: (``concurrency.py:504-505``).  The next test would then silently run with
#: fewer than eleven patches.  Measured with ``--setup-show``: monkeypatch
#: tears down AFTER the fixtures, so this has to be repaired at SETUP.
PRISTINE_SYMBOLS = [
    (owner, attr, getattr(owner, attr)) for _n, owner, attr in PATCHED_SYMBOLS
]


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts and ends with the adapter uninstalled.

    The ContextVar is reset explicitly: the A2/A3 setters mirror into it
    WITHOUT a token, by design — ``subagent_invocation.py:310`` sets a child's
    id and restores the parent's at ``:655``.  pytest runs every test in one
    and the same context, so a bare ``set_session_context`` would otherwise
    leak into the next test.
    """
    concurrency.uninstall()
    for owner, attr, pristine in PRISTINE_SYMBOLS:
        if getattr(getattr(owner, attr, None), concurrency.SENTINEL, False):
            setattr(owner, attr, pristine)
    concurrency.set_enabled(True)
    concurrency._SESSION_ID.set(None)
    saved_backend = common.get_approval_backend()
    saved_async_lock = common._APPROVAL_ASYNC_LOCK
    # A cached asyncio.Lock from a previous test is bound to that test's
    # (now closed) loop and would raise on await here.
    common._APPROVAL_ASYNC_LOCK = None
    yield
    concurrency.uninstall()
    concurrency.set_enabled(True)
    concurrency._SESSION_ID.set(None)
    common.set_approval_backend(saved_backend)
    common._APPROVAL_ASYNC_LOCK = saved_async_lock
    messaging.get_message_bus().set_session_context(None)


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


def drain(bus: MessageBus, kind: str) -> list:
    """All buffered messages of the given class name (no renderer attached)."""
    return [m for m in bus.get_buffered_messages() if type(m).__name__ == kind]


# ---------------------------------------------------------------------------
# Install / uninstall lifecycle
# ---------------------------------------------------------------------------
def test_ac1_install_replaces_all_eleven_symbols(installed):
    assert concurrency.is_installed() is True
    for name, owner, attr in PATCHED_SYMBOLS:
        assert getattr(getattr(owner, attr), "_cp_discord", False) is True, name


def test_ac2_second_install_is_a_no_op():
    concurrency.install()
    first = {name: getattr(owner, attr) for name, owner, attr in PATCHED_SYMBOLS}
    concurrency.install()
    for name, owner, attr in PATCHED_SYMBOLS:
        assert getattr(owner, attr) is first[name], name


def test_ac7_uninstall_restores_every_symbol_and_is_idempotent():
    before = {name: getattr(owner, attr) for name, owner, attr in PATCHED_SYMBOLS}
    concurrency.install()
    concurrency.uninstall()
    for name, owner, attr in PATCHED_SYMBOLS:
        assert getattr(owner, attr) is before[name], name
    assert concurrency.is_installed() is False
    concurrency.uninstall()  # idempotent
    for name, owner, attr in PATCHED_SYMBOLS:
        assert getattr(owner, attr) is before[name], name


def test_ac7_leftover_thread_start_wrapper_is_inert(monkeypatch):
    """A foreign patch on top of G blocks the rollback — what stays must be dead."""
    original_start = threading.Thread.start
    concurrency.install()
    ours = threading.Thread.start

    def _foreign(self):
        return ours(self)

    monkeypatch.setattr(threading.Thread, "start", _foreign)
    concurrency.uninstall()
    assert threading.Thread.start is _foreign  # AC-8: not rolled back

    # Our wrapper is still in the chain. Even WITH a marker set it must not mark.
    token = concurrency._SHELL_MARKER.set(concurrency._ShellMarker("discord:1"))
    try:
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
    finally:
        concurrency._SHELL_MARKER.reset(token)
    assert getattr(t, "_cp_discord_sid", None) is None
    monkeypatch.setattr(threading.Thread, "start", original_start)


def test_ac8_uninstall_leaves_foreign_patch_alone(monkeypatch):
    concurrency.install()
    ours = messaging.get_session_context

    def _foreign():
        return ours()

    monkeypatch.setattr(messaging, "get_session_context", _foreign)
    concurrency.uninstall()
    assert messaging.get_session_context is _foreign


def test_ac44_coexists_with_subagent_panel_patch(monkeypatch):
    """subagent_panel patches sai.set_session_context first; we chain over it."""
    seen = []
    panel_original = sai.set_session_context

    def _panel(session_id):
        seen.append(session_id)
        return panel_original(session_id)

    _panel._subagent_panel = True
    monkeypatch.setattr(sai, "set_session_context", _panel)

    concurrency.install()
    try:
        assert getattr(sai.set_session_context, "_cp_discord", False) is True
        sai.set_session_context("discord:44")
        assert seen == ["discord:44"], "subagent_panel wrapper was bypassed"
        assert concurrency.current_session_id() == "discord:44"
    finally:
        concurrency.uninstall()
    assert sai.set_session_context is _panel
    assert getattr(sai.set_session_context, "_subagent_panel", False) is True


# ---------------------------------------------------------------------------
# Patch A — session attribution (AC-42, AC-3)
# ---------------------------------------------------------------------------
def test_ac42_all_five_session_symbols_see_the_contextvar(installed):
    with concurrency.session_scope("discord:42"):
        assert messaging.get_session_context() == "discord:42"
        assert sai.get_session_context() == "discord:42"
        assert agent_tools.get_session_context() == "discord:42"

        # The setters must feed the SAME ContextVar (sub-agents swap the id
        # mid-run and restore it afterwards).
        sai.set_session_context("discord:42:child")
        assert messaging.get_session_context() == "discord:42:child"
        agent_tools.set_session_context("discord:42:grandchild")
        assert sai.get_session_context() == "discord:42:grandchild"
        sai.set_session_context("discord:42")
        assert agent_tools.get_session_context() == "discord:42"


def test_ac42_without_session_falls_back_to_the_core_global(installed):
    messaging.get_message_bus().set_session_context("core-owned")
    try:
        assert concurrency.current_session_id() is None
        assert messaging.get_session_context() == "core-owned"
    finally:
        messaging.get_message_bus().set_session_context(None)


async def test_ac3_baseline_cross_tags_two_concurrent_runs():
    """A/B part 1: unpatched, the single global loses one of the two sessions."""

    async def run(sid, delay):
        messaging.set_session_context(sid)
        await asyncio.sleep(delay)
        return messaging.get_session_context()

    a, b = await asyncio.gather(run("A", 0.06), run("B", 0.01))
    messaging.set_session_context(None)
    assert (a, b) == ("B", "B"), (
        "baseline should cross-tag — otherwise AC-3 proves nothing"
    )


async def test_ac3_patched_keeps_stream_events_apart(installed):
    """A/B part 2: with the patch each task keeps its own id in stream_event."""
    from code_puppy import callbacks
    from code_puppy.agents import event_stream_handler

    captured: list[tuple[str, str | None]] = []

    async def _cb(event_type, event_data, agent_session_id=None):
        captured.append((event_data, agent_session_id))

    callbacks.register_callback("stream_event", _cb)
    try:

        async def run(sid, delay):
            with concurrency.session_scope(sid):
                await asyncio.sleep(delay)
                event_stream_handler._fire_stream_event("part_delta", sid)
                await asyncio.sleep(0.05)
                return messaging.get_session_context()

        a, b = await asyncio.gather(run("discord:A", 0.06), run("discord:B", 0.01))
    finally:
        callbacks.unregister_callback("stream_event", _cb)

    assert (a, b) == ("discord:A", "discord:B")
    assert sorted(captured) == [
        ("discord:A", "discord:A"),
        ("discord:B", "discord:B"),
    ]


# ---------------------------------------------------------------------------
# Patch B — per-session approval lock (AC-4, AC-5, AC-6, AC-11, AC-45)
# ---------------------------------------------------------------------------
def _sleeping_backend(seconds: float, calls: list):
    def backend(sid, title, message, preview=None):
        calls.append(sid)
        time.sleep(seconds)
        return True, None

    backend._cp_discord = True
    return backend


def _sleeping_backend_3arg(seconds: float, calls: list):
    def backend(title, message, preview=None):
        calls.append(title)
        time.sleep(seconds)
        return True, None

    return backend


async def _approve(title="Shell Command"):
    return await common.get_user_approval_async(title=title, content="please")


async def test_ac4_baseline_serialises_two_sessions():
    """A/B part 1: unpatched, two channels queue behind one process-wide lock."""
    calls: list = []
    common.set_approval_backend(_sleeping_backend_3arg(0.2, calls))

    start = time.perf_counter()
    await asyncio.gather(_approve("A"), _approve("B"))
    elapsed = time.perf_counter() - start

    assert len(calls) == 2
    assert elapsed >= 0.38, f"baseline was not serial ({elapsed:.2f}s)"


async def test_ac4_patched_runs_two_sessions_in_parallel(installed):
    """A/B part 2: with per-session locks the same work takes half as long."""
    calls: list = []
    common.set_approval_backend(_sleeping_backend(0.2, calls))

    async def one(sid):
        with concurrency.session_scope(sid):
            return await _approve(sid)

    start = time.perf_counter()
    await asyncio.gather(one("discord:A"), one("discord:B"))
    elapsed = time.perf_counter() - start

    assert sorted(calls) == ["discord:A", "discord:B"]
    assert elapsed < 0.35, f"approvals did not overlap ({elapsed:.2f}s)"


async def test_ac5_same_session_stays_serialised(installed):
    calls: list = []
    common.set_approval_backend(_sleeping_backend(0.2, calls))

    async def one():
        with concurrency.session_scope("discord:same"):
            return await _approve()

    start = time.perf_counter()
    await asyncio.gather(one(), one())
    elapsed = time.perf_counter() - start

    assert calls == ["discord:same", "discord:same"]
    assert elapsed >= 0.38, f"same-session approvals overlapped ({elapsed:.2f}s)"


async def test_ac6_sessionless_callers_share_the_default_lock(installed):
    first = common._get_approval_async_lock()
    second = common._get_approval_async_lock()
    assert first is second
    assert "__default__" in concurrency._APPROVAL_LOCKS


async def test_ac11_release_session_drops_the_lock(installed):
    with concurrency.session_scope("discord:11"):
        common._get_approval_async_lock()
    assert "discord:11" in concurrency._APPROVAL_LOCKS
    size = len(concurrency._APPROVAL_LOCKS)
    concurrency.release_session("discord:11")
    assert "discord:11" not in concurrency._APPROVAL_LOCKS
    assert len(concurrency._APPROVAL_LOCKS) == size - 1
    concurrency.release_session("discord:11")  # idempotent


def test_ac45_foreign_event_loop_gets_an_uncached_lock(installed):
    """asyncio.run() inside a sync callback must not await another loop's lock."""
    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:

        async def grab():
            with concurrency.session_scope("discord:45"):
                return common._get_approval_async_lock()

        lock_a = loop_a.run_until_complete(grab())
        lock_a_again = loop_a.run_until_complete(grab())
        lock_b = loop_b.run_until_complete(grab())

        assert lock_a is lock_a_again, "same loop must reuse the cached lock"
        assert lock_b is not lock_a, "foreign loop must get a fresh lock"
    finally:
        loop_a.close()
        loop_b.close()


# ---------------------------------------------------------------------------
# Patch C — session-bound approval backend (INV-7)
# ---------------------------------------------------------------------------
async def test_ac37_backend_sees_the_session_id(installed):
    seen: list = []

    def backend(sid, title, message, preview=None):
        seen.append(sid)
        return True, None

    backend._cp_discord = True
    common.set_approval_backend(backend)

    with concurrency.session_scope("discord:37"):
        approved, _ = await _approve()

    assert approved is True
    assert seen == ["discord:37"], "the backend never saw the session id"


async def test_ac37_missing_session_binds_none_not_default(installed):
    seen: list = []

    def backend(sid, title, message, preview=None):
        seen.append(sid)
        return False, None

    backend._cp_discord = True
    common.set_approval_backend(backend)

    await _approve()
    assert seen == [None]


def test_ac56_none_passes_through_and_l4_can_install(installed):
    common.set_approval_backend(None)
    assert common.get_approval_backend() is None, (
        "a non-None wrapper would break stdin approvals product-wide"
    )

    def backend(sid=None, title="", message="", preview=None):
        return True, None

    backend._cp_discord = True
    common.set_approval_backend(backend)
    bound = common.get_approval_backend()
    assert bound is not None
    assert getattr(bound, "_cp_discord", False) is True, (
        "L4 must recognise its own backend in the slot"
    )


def test_ac61_foreign_backend_is_passed_through_unwrapped(installed):
    def foreign(title, message, preview=None):
        return True, "acp"

    common.set_approval_backend(foreign)
    assert common.get_approval_backend() is foreign
    assert foreign("t", "m", None) == (True, "acp")


async def test_ac60_teardown_in_the_wrong_order_rejects(installed):
    """L1 rolled back before L4 deregistered: reject, never a TypeError."""
    seen: list = []

    def backend(sid=None, title="", message="", preview=None):
        seen.append(sid)
        if sid not in ("discord:60",):  # cannot resolve -> fail closed
            return False, None
        return True, None

    backend._cp_discord = True
    common.set_approval_backend(backend)

    concurrency.uninstall()  # wrong order on purpose

    approved, feedback = await _approve(title="Shell Command")
    assert approved is False
    assert seen == ["Shell Command"], "core calls positionally — sid is misbound"


async def test_ac38_sync_approval_path_is_not_reached(installed, monkeypatch):
    """AC-38 pins the §4a assumption: the async path is the only one used."""
    acquisitions = []
    real_lock = common._APPROVAL_SYNC_LOCK

    class CountingLock:
        def __enter__(self):
            acquisitions.append(1)
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    monkeypatch.setattr(common, "_APPROVAL_SYNC_LOCK", CountingLock())

    def backend(sid, title, message, preview=None):
        return True, None

    backend._cp_discord = True
    common.set_approval_backend(backend)

    with concurrency.session_scope("discord:38"):
        await _approve()

    assert acquisitions == [], "a sync approval leaked into the Discord path"


# ---------------------------------------------------------------------------
# Defensive discipline (AC-10) and selftest (AC-9)
# ---------------------------------------------------------------------------
def test_ac10_plugin_errors_fall_back_to_original_behaviour(
    installed, monkeypatch, probe_bus
):
    def _boom():
        raise RuntimeError("plugin logic exploded")

    monkeypatch.setattr(concurrency, "_current_sid", _boom)

    messaging.get_message_bus().set_session_context("core-owned")
    try:
        assert messaging.get_session_context() == "core-owned"
    finally:
        messaging.get_message_bus().set_session_context(None)

    from code_puppy.messaging.messages import ShellLineMessage

    probe_bus.emit(ShellLineMessage(line="still works", stream="stdout"))
    assert len(drain(probe_bus, "ShellLineMessage")) == 1

    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()


def test_ac9_selftest_passes_when_installed(installed):
    ok, detail = concurrency.selftest()
    assert ok is True, detail


def test_ac9_selftest_fails_when_not_installed():
    ok, detail = concurrency.selftest()
    assert ok is False
    assert "not installed" in detail.lower()


def test_ac9_selftest_detects_a_core_refactor(installed):
    """P6: a renamed/replaced target must fail loudly, not degrade silently.

    Restored by hand rather than with ``monkeypatch``: ``uninstall`` rightly
    refuses to evict a foreign patch, so monkeypatch's undo -- which runs AFTER
    every other fixture teardown (measured with ``--setup-show``) -- would put
    OUR wrapper back as ``common``'s own symbol.  ``install()`` then skips it
    as already-ours (``concurrency.py:504-505``) and every later test would
    silently run with ten patches instead of eleven.
    """
    ours = common._get_approval_async_lock

    def _foreign():
        return asyncio.Lock()

    common._get_approval_async_lock = _foreign
    try:
        ok, detail = concurrency.selftest()
    finally:
        assert common._get_approval_async_lock is _foreign
        common._get_approval_async_lock = ours

    assert ok is False
    assert "B" in detail


def test_ac9_selftest_leaves_the_backend_slot_untouched(installed):
    def foreign(title, message, preview=None):
        return True, None

    common.set_approval_backend(foreign)
    concurrency.selftest()
    assert common.get_approval_backend() is foreign


@pytest.mark.parametrize("dropped", [name for name, _o, _a in PATCHED_SYMBOLS])
def test_ac9_selftest_fails_when_any_single_target_is_missing(dropped, monkeypatch):
    """Ten of eleven installed must never read as healthy.

    A core refactor that removes ONE target leaves the other ten working, so
    ``install()`` reports no error and every remaining probe passes.  The count
    is therefore checked against the expected names, not against whatever
    happened to install.
    """
    full = concurrency._patch_targets()
    monkeypatch.setattr(
        concurrency, "_patch_targets", lambda: [t for t in full if t[0] != dropped]
    )
    concurrency.install()
    try:
        assert len(concurrency._PATCHES) == 10, "the drop did not take effect"
        ok, detail = concurrency.selftest()
    finally:
        concurrency.uninstall()

    assert ok is False, f"{dropped} missing went unnoticed"
    assert dropped in detail


def test_ac9_selftest_detects_an_installed_but_ineffective_f(installed):
    """F refactored into a plain delegation: installed, sentinel-marked, inert.

    F's whole job is to adopt the reader thread's stamp before delegating
    (``concurrency.py:359-370``).  A wrapper that only delegates passes every
    existence and sentinel check while shell lines silently carry a foreign
    session — the exact failure AC-9 exists to catch.  A probe that drives F's
    helper instead of ``command_runner.emit_shell_line`` cannot see it.

    Restored by hand, for the reason spelled out in the core-refactor test.
    """
    from code_puppy.messaging.bus import emit_shell_line as core_emit_shell_line

    def _inert(line, stream="stdout"):
        return core_emit_shell_line(line, stream)

    setattr(_inert, concurrency.SENTINEL, True)
    ours = command_runner.emit_shell_line
    command_runner.emit_shell_line = _inert
    try:
        ok, detail = concurrency.selftest()
    finally:
        command_runner.emit_shell_line = ours

    assert ok is False, "an inert F passed the selftest"
    assert "F" in detail


def test_ac9_selftest_does_not_leak_a_line_onto_the_live_bus(installed, probe_bus):
    """The probes emit for real now — nothing of that may reach the real bus."""
    ok, detail = concurrency.selftest()
    assert ok is True, detail
    assert drain(probe_bus, "ShellLineMessage") == []


def test_ac9_selftest_restores_the_global_bus(installed):
    from code_puppy.messaging import bus as bus_module

    before = bus_module._global_bus
    concurrency.selftest()
    assert bus_module._global_bus is before
