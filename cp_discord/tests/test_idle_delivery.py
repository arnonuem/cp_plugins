"""C8 — idle delivery: AC-1 … AC-27 of the ``leerlauf_zustellung`` spec.

**What is real here and what is not.**  The delivery path runs against the
REAL ``PauseController`` singleton, a REAL ``asyncio.Queue`` and a REAL event
loop; where the spec says "from a foreign thread", a real ``threading.Thread``
is what fires the steer.  Mocking any of those would prove only that the mock
was called -- this component's whole job is to move a string between two core
queues.

The ONE thing that is a stand-in is the ``(N pending)`` display: AC-8/AC-9 use
a hand-built listener that records every ``count`` it is handed, not the real
``steer_queue`` plugin.  It reproduces the only surface C8 shares with it
(``listener(count)``); whether the real plugin behaves identically is a
question for the live check, not for this file.

``run_ui``'s module state is written directly (``_persistent``,
``_run_active``, ``_idle_queue``, ``_loop``) -- that is what a running
persistent UI would set, and starting one needs a TTY.

**AC-14 (line budget) has no test here on purpose:** it is already measured
over every source file by
``test_gate_wire.py::test_ac47_every_source_file_stays_under_600_lines``,
which globs ``*.py`` rather than a list somebody has to remember to grow.  A
second copy would be a duplicate, not a check.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, List, Optional

import pytest

from code_puppy.messaging import pause_controller as pause_module
from code_puppy.messaging import run_ui

from cp_discord import idle_delivery

#: The private core names C8 leans on (SPEC R9).  AC-19 removes each in turn.
CORE_INTERNALS = (
    "_lock",
    "_loop",
    "_get_loop",
    "_persistent",
    "_run_active",
    "_idle_queue",
    "_push_idle",
)

#: Introduced by the core fix ``9e1926ac``.  C8 must not touch any of them
#: (SPEC R8) -- otherwise it measures the core fix instead of itself.
CORE_FIX_NAMES = ("wake_idle_waiter", "_WAKE", "_register_steer_wakeup")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_state():
    """Reset all THREE leaking levels around every test.

    One is not enough.  ``run_ui``'s globals are module state; the
    ``PauseController`` is a process-wide lazy singleton
    (``pause_controller.py:469-479``) and is where C8's listener actually
    hangs; and C8 itself remembers whether it is installed.  AC-16 ends with a
    registered listener -- without this fixture that listener fires against
    the NEXT test's ``run_ui`` state.
    """
    saved = {name: getattr(run_ui, name, None) for name in CORE_INTERNALS}

    pause_module.reset_pause_controller()
    idle_delivery.reset_state()
    run_ui._persistent = False
    run_ui._run_active = False
    run_ui._idle_queue = None
    run_ui._loop = None

    yield

    idle_delivery.reset_state()
    pause_module.reset_pause_controller()
    for name, value in saved.items():
        setattr(run_ui, name, value)


@pytest.fixture
def loop():
    """A real, open event loop -- closed again whatever the test does."""
    new_loop = asyncio.new_event_loop()
    yield new_loop
    new_loop.close()


@pytest.fixture
def idle_ui(loop):
    """The state a persistent UI parked at the prompt would leave behind."""
    run_ui._loop = loop
    run_ui._idle_queue = asyncio.Queue()
    run_ui._persistent = True
    run_ui._run_active = False
    return loop


@pytest.fixture
def controller():
    return pause_module.get_pause_controller()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _steer_from_thread(text: str, mode: str = "queue") -> None:
    """Fire a steer from a REAL foreign thread, the way the broker does."""
    thread = threading.Thread(
        target=lambda: pause_module.get_pause_controller().request_steer(text, mode),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "the steer thread hung"


def _settle(loop: asyncio.AbstractEventLoop, rounds: int = 6) -> None:
    """Run whatever ``call_soon_threadsafe`` scheduled, including re-entries.

    One round is not enough: ``_deliver`` schedules a follow-up round of its
    own (the pop re-fires the listeners), and that one only runs on the next
    loop iteration.
    """
    for _ in range(rounds):
        loop.run_until_complete(asyncio.sleep(0))


def _drain(queue: Optional[asyncio.Queue]) -> List[Any]:
    """Everything sitting in the idle queue right now, without waiting."""
    items: List[Any] = []
    if queue is None:
        return items
    while True:
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return items


class _Display:
    """Stand-in for the ``steer_queue`` plugin's ``(N pending)`` tag."""

    def __init__(self) -> None:
        self.counts: List[int] = []

    def __call__(self, count: int) -> None:
        self.counts.append(count)


# --------------------------------------------------------------------------- #
# R1 — delivery at idle (AC-1, AC-2, AC-7)
# --------------------------------------------------------------------------- #


def test_ac1_an_idle_steer_is_delivered_without_a_keystroke():
    """The end-to-end shape: real loop, real queue, real foreign thread."""

    async def scenario() -> str:
        run_ui._loop = asyncio.get_running_loop()
        run_ui._idle_queue = asyncio.Queue()
        run_ui._persistent = True
        run_ui._run_active = False
        idle_delivery.install()

        threading.Thread(
            target=lambda: pause_module.get_pause_controller().request_steer(
                "vom handy", "queue"
            ),
            daemon=True,
        ).start()

        return await asyncio.wait_for(run_ui.wait_for_idle_submission(), timeout=5.0)

    assert asyncio.run(scenario()) == "vom handy"


def test_ac2_the_delivered_text_leaves_the_steer_queue(idle_ui, controller):
    idle_delivery.install()

    _steer_from_thread("vom handy")
    _settle(idle_ui)

    assert controller.peek_pending_steer_queued() == []
    assert _drain(run_ui._idle_queue) == ["vom handy"]


def test_ac7_two_idle_messages_arrive_in_order(idle_ui, controller):
    idle_delivery.install()

    _steer_from_thread("erste")
    _steer_from_thread("zweite")
    _settle(idle_ui)

    assert _drain(run_ui._idle_queue) == ["erste", "zweite"]
    assert controller.peek_pending_steer_queued() == []


# --------------------------------------------------------------------------- #
# R2 — during a run nothing changes (AC-3, AC-10)
# --------------------------------------------------------------------------- #


def test_ac3_a_run_in_flight_keeps_the_text_in_the_steer_queue(idle_ui, controller):
    run_ui._run_active = True
    idle_delivery.install()

    _steer_from_thread("waehrend des laufs")
    _settle(idle_ui)

    assert controller.peek_pending_steer_queued() == ["waehrend des laufs"]
    assert _drain(run_ui._idle_queue) == []


def test_ac10_the_queue_menu_path_is_untouched_during_a_run(idle_ui, controller):
    """``peek`` + ``replace`` -- the two operations ``/queue`` is built on."""
    run_ui._run_active = True
    idle_delivery.install()

    _steer_from_thread("eins")
    _steer_from_thread("zwei")
    _settle(idle_ui)

    seen = controller.peek_pending_steer_queued()
    assert seen == ["eins", "zwei"]

    controller.replace_pending_steer_queued(seen)
    _settle(idle_ui)

    assert controller.peek_pending_steer_queued() == ["eins", "zwei"]
    assert _drain(run_ui._idle_queue) == []


# --------------------------------------------------------------------------- #
# R3 — nothing is lost while C8 has not touched it (AC-4, AC-5, AC-6, AC-23)
# --------------------------------------------------------------------------- #


def test_ac4_the_classic_prompt_path_keeps_its_text(idle_ui, controller):
    run_ui._persistent = False
    idle_delivery.install()

    _steer_from_thread("klassischer pfad")
    _settle(idle_ui)

    assert controller.peek_pending_steer_queued() == ["klassischer pfad"]


def test_ac5_a_missing_idle_queue_keeps_the_text(idle_ui, controller):
    """The teardown window: persistent is still set, the queue is already gone."""
    run_ui._idle_queue = None
    idle_delivery.install()

    _steer_from_thread("teardown")
    _settle(idle_ui)

    assert controller.peek_pending_steer_queued() == ["teardown"]


def test_ac6_a_closed_loop_keeps_the_text_and_does_not_crash(loop, controller):
    run_ui._loop = loop
    run_ui._idle_queue = asyncio.Queue()
    run_ui._persistent = True
    run_ui._run_active = False
    idle_delivery.install()
    loop.close()

    _steer_from_thread("geschlossener loop")

    assert controller.peek_pending_steer_queued() == ["geschlossener loop"]


@pytest.mark.parametrize("loop_state", ["missing", "closed"])
def test_ac23_no_loop_at_listener_time_means_no_pop(loop, controller, loop_state):
    """SPEC §3.5: the loop is read fresh per listener call, never captured."""
    run_ui._idle_queue = asyncio.Queue()
    run_ui._persistent = True
    run_ui._run_active = False
    if loop_state == "missing":
        run_ui._loop = None
    else:
        run_ui._loop = loop
        loop.close()
    idle_delivery.install()

    _steer_from_thread("kein loop")

    assert controller.peek_pending_steer_queued() == ["kein loop"]
    assert _drain(run_ui._idle_queue) == []


# --------------------------------------------------------------------------- #
# R4 — the display stays correct (AC-8, AC-9)
# --------------------------------------------------------------------------- #


def test_ac8_the_display_is_correct_when_it_registered_first(idle_ui, controller):
    display = _Display()
    controller.add_steer_queue_listener(display)
    idle_delivery.install()

    _steer_from_thread("anzeige")
    _settle(idle_ui)

    assert display.counts[-1] == len(controller.peek_pending_steer_queued()) == 0


def test_ac9_the_display_is_correct_when_it_registered_last(idle_ui, controller):
    """The ghost number from the prelude: C8 first, display second."""
    idle_delivery.install()
    display = _Display()
    controller.add_steer_queue_listener(display)

    _steer_from_thread("anzeige")
    _settle(idle_ui)

    assert display.counts[-1] == len(controller.peek_pending_steer_queued()) == 0


# --------------------------------------------------------------------------- #
# R5 — C8's own failures stay visible (AC-15)
# --------------------------------------------------------------------------- #


def test_ac15_a_throwing_listener_is_caught_and_logged(idle_ui, monkeypatch, caplog):
    """``_fire_steer_queue_listeners`` swallows everything (``:195-198``)."""

    def boom() -> None:
        raise RuntimeError("kaputt")

    monkeypatch.setattr(run_ui, "_get_loop", boom)
    idle_delivery.install()

    with caplog.at_level("DEBUG", logger=idle_delivery.logger.name):
        idle_delivery._on_steer_queued(1)

    assert any(
        "kaputt" in record.getMessage() or record.exc_info for record in caplog.records
    )


# --------------------------------------------------------------------------- #
# R6 — teardown (AC-16, AC-17, AC-18)
# --------------------------------------------------------------------------- #


def test_ac16_installing_twice_registers_one_listener(idle_ui, controller):
    idle_delivery.install()
    idle_delivery.install()

    registered = controller._steer_queue_listeners

    assert registered.count(idle_delivery._on_steer_queued) == 1


def test_ac17_uninstall_without_install_is_a_no_op():
    idle_delivery.uninstall()
    idle_delivery.uninstall()

    assert idle_delivery.is_installed() is False


def test_ac18_after_uninstall_nothing_is_delivered(idle_ui, controller):
    idle_delivery.install()
    idle_delivery.uninstall()

    _steer_from_thread("nach dem abbau")
    _settle(idle_ui)

    assert controller.peek_pending_steer_queued() == ["nach dem abbau"]
    assert _drain(run_ui._idle_queue) == []


# --------------------------------------------------------------------------- #
# R7 — re-entrancy (AC-12, AC-20, AC-21)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not hasattr(run_ui, "wake_idle_waiter"),
    reason="core fix 9e1926ac not installed -- skipping is the EXPECTED state "
    "once it has been rolled back",
)
def test_ac12_coexisting_with_the_core_fix_executes_the_text_once(idle_ui, controller):
    """``_register_steer_wakeup()`` directly: ``start_persistent_ui`` needs a TTY.

    Without it the core listener would never be registered and this test would
    pass while measuring nothing.  The ``_WAKE`` nudges are deliberately NOT
    counted -- how many the core sends is its business.
    """
    run_ui._register_steer_wakeup()
    idle_delivery.install()

    _steer_from_thread("genau einmal")
    _settle(idle_ui)

    delivered = _drain(run_ui._idle_queue)

    assert delivered.count("genau einmal") == 1
    assert controller.peek_pending_steer_queued() == []


def test_ac20_the_pop_refires_the_listeners_without_recursing(
    idle_ui, controller, monkeypatch
):
    """``pop_next_steer_queued`` fires the listeners again (``:318``)."""
    depth = {"current": 0, "max": 0}
    real_deliver = idle_delivery._deliver

    def traced() -> None:
        depth["current"] += 1
        depth["max"] = max(depth["max"], depth["current"])
        try:
            real_deliver()
        finally:
            depth["current"] -= 1

    monkeypatch.setattr(idle_delivery, "_deliver", traced)
    idle_delivery.install()

    _steer_from_thread("reentranz")
    _settle(idle_ui)

    assert depth["max"] == 1, "the delivery must not nest inside itself"
    assert _drain(run_ui._idle_queue) == ["reentranz"]


def test_ac21_the_second_deliver_never_pushes_none(idle_ui, controller, monkeypatch):
    """SPEC §3.4: an empty second round happens on EVERY delivery.

    ``wait_for_idle_submission`` returns its item unchanged (``:332``), so a
    ``None`` would come back as user text and be executed as a turn.
    """
    pushed: List[Any] = []
    real_push = run_ui._push_idle

    def recording(item: Any) -> None:
        pushed.append(item)
        real_push(item)

    monkeypatch.setattr(run_ui, "_push_idle", recording)
    idle_delivery.install()

    _steer_from_thread("einmal")
    _settle(idle_ui)

    assert pushed == ["einmal"], "the empty round must not push anything"
    assert _drain(run_ui._idle_queue) == ["einmal"]


# --------------------------------------------------------------------------- #
# R8 — no dependency on the core fix (AC-11)
# --------------------------------------------------------------------------- #


def test_ac11_c8_uses_no_symbol_the_core_fix_introduced():
    source = (Path(idle_delivery.__file__)).read_text(encoding="utf-8")

    found = [name for name in CORE_FIX_NAMES if name in source]

    assert found == [], f"C8 must run against pure upstream, found: {found}"


# --------------------------------------------------------------------------- #
# R9 — defensive access to core internals (AC-19)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("missing", CORE_INTERNALS)
def test_ac19_a_missing_core_internal_disables_c8_without_breaking_startup(
    idle_ui, controller, monkeypatch, caplog, missing
):
    """A raising ``install()`` rolls the WHOLE bridge back (``:432-433``)."""
    monkeypatch.delattr(run_ui, missing, raising=False)

    with caplog.at_level("DEBUG", logger=idle_delivery.logger.name):
        idle_delivery.install()

    assert idle_delivery.is_installed() is False
    assert any(missing in record.getMessage() for record in caplog.records)
    assert idle_delivery._on_steer_queued not in controller._steer_queue_listeners


# --------------------------------------------------------------------------- #
# Wiring — C8 is really reachable through the lifecycle (AC-25's other half)
# --------------------------------------------------------------------------- #


def test_c8_is_wired_into_components_and_resolves_to_this_module():
    """CREATE -> WIRE -> VERIFY: the entry alone proves nothing.

    ``test_lifecycle.py`` drives startup against FAKE component modules, so a
    typo in the module name would stay green there.  This resolves the entry
    through the real importer the bridge uses.
    """
    from cp_discord import register_callbacks

    entries = [
        component
        for component in register_callbacks.COMPONENTS
        if component.layer == "C8"
    ]

    assert [component.module for component in entries] == ["idle_delivery"]
    assert register_callbacks.COMPONENTS[-1] is entries[0], (
        "C8 must be LAST so teardown reaches it FIRST"
    )
    assert register_callbacks._import_component(entries[0]) is idle_delivery


# --------------------------------------------------------------------------- #
# §3.3 — the lock is held for the snapshot only (AC-22)
# --------------------------------------------------------------------------- #


def test_ac22_deliver_does_not_deadlock_on_the_core_lock(
    idle_ui, controller, monkeypatch
):
    """Runs against an ISOLATED lock, on purpose.

    ``join(timeout)`` does not kill the thread -- it only stops waiting.  A
    hung ``_deliver`` would hold ``run_ui._lock`` forever and freeze every
    later test that touches it.  Swapping the lock object keeps the poison
    inside this test (TEST_PLAN, M8, route 2).
    """
    monkeypatch.setattr(run_ui, "_lock", threading.Lock())
    controller.request_steer("deadlock", "queue")

    thread = threading.Thread(target=idle_delivery._deliver, daemon=True)
    thread.start()
    thread.join(timeout=2.0)

    assert not thread.is_alive(), "_deliver held the lock across pop/push"


# --------------------------------------------------------------------------- #
# Characterisation tests — known CORE limits (AC-24, AC-26, AC-27)
# --------------------------------------------------------------------------- #


def test_ac24_teardown_between_snapshot_and_push_loses_the_text_but_warns(
    idle_ui, controller, monkeypatch
):
    """Charakterisierungstest. Wird er rot, wurde die Einschraenkung im Kern
    behoben — dann diesen Test loeschen, nicht das Plugin anpassen.

    The window from SPEC R3: the lock MUST fall after the snapshot, so the UI
    can disappear before the push lands.  Monkeypatching the pop is the only
    deterministic way to hit it.
    """
    warned: List[str] = []
    monkeypatch.setattr(run_ui, "_warn_command_dropped", warned.append)
    controller.request_steer("verloren", "queue")

    real_pop = controller.pop_next_steer_queued

    def popping_then_teardown() -> Optional[str]:
        text = real_pop()
        run_ui._idle_queue = None
        return text

    monkeypatch.setattr(controller, "pop_next_steer_queued", popping_then_teardown)

    idle_delivery._deliver()

    assert controller.peek_pending_steer_queued() == []
    assert warned == ["verloren"], "the loss must not be silent"


def test_ac26_a_run_starting_after_the_snapshot_reroutes_the_text(
    idle_ui, controller, monkeypatch
):
    """Charakterisierungstest. Wird er rot, wurde die Einschraenkung im Kern
    behoben — dann diesen Test loeschen, nicht das Plugin anpassen.

    The window from SPEC R2: nothing is LOST, but the text changes lane -- it
    goes through the idle queue and runs as a fresh turn afterwards instead of
    being fed into the run in flight.
    """
    controller.request_steer("umgeleitet", "queue")
    real_pop = controller.pop_next_steer_queued

    def run_starts_then_pops() -> Optional[str]:
        run_ui._run_active = True
        return real_pop()

    monkeypatch.setattr(controller, "pop_next_steer_queued", run_starts_then_pops)

    idle_delivery._deliver()
    _settle(idle_ui)

    assert controller.peek_pending_steer_queued() == []
    assert _drain(run_ui._idle_queue) == ["umgeleitet"]


def test_ac27_a_pushed_text_can_die_with_the_ui_without_any_warning(
    idle_ui, controller, monkeypatch
):
    """Charakterisierungstest. Wird er rot, wurde die Einschraenkung im Kern
    behoben — dann diesen Test loeschen, nicht das Plugin anpassen.

    The SECOND, wider window from SPEC R3: the push SUCCEEDS and the text then
    waits in ``_idle_queue`` until the REPL ends.  ``_warn_command_dropped``
    only runs in the discard branch (``:421-422``), so this one is silent.

    ``_idle_queue`` is cleared directly rather than through
    ``stop_persistent_ui()``: that would drag in the editor, the bottom bar,
    the PauseController and a thread join for the single effect needed here.
    """
    warned: List[str] = []
    monkeypatch.setattr(run_ui, "_warn_command_dropped", warned.append)
    idle_delivery.install()

    _steer_from_thread("still verloren")
    _settle(idle_ui)

    orphaned = run_ui._idle_queue
    run_ui._idle_queue = None

    assert _drain(orphaned) == ["still verloren"]
    assert warned == [], "the second window is silent -- that is the point"
    with pytest.raises(EOFError):
        idle_ui.run_until_complete(run_ui.wait_for_idle_submission())


# --------------------------------------------------------------------------- #
# Review round 1 -- gaps the reviewers proved were untested
# --------------------------------------------------------------------------- #


def test_a_dead_loop_in_deliver_leaves_the_text_in_the_steer_queue(idle_ui, controller):
    """The loop guard INSIDE _deliver, measured on its own.

    AC-6 and AC-23 close the loop BEFORE the steer, so ``_on_steer_queued``
    bails out first and ``_deliver`` never reaches its own guard -- a
    review-round mutation removed those two lines and the whole suite stayed
    green.  Calling ``_deliver`` directly is the only way the guard is reached.
    """
    idle_delivery.install()
    controller.request_steer("loop stirbt dazwischen", mode="queue")

    run_ui._loop = None  # the UI went away between listener and delivery

    idle_delivery._deliver()

    assert controller.peek_pending_steer_queued() == ["loop stirbt dazwischen"]
    assert _drain(run_ui._idle_queue) == []


def test_install_survives_a_steer_queue_that_refuses_listeners(idle_ui):
    """A throwing registration must not take the whole bridge down.

    ``_install_components`` answers an exception with ``_uninstall_components()``
    plus ``return False`` (``register_callbacks.py:432-433``): every layer C1-C5
    goes with it.  So the cost of an unguarded raise here is not "no idle
    delivery", it is "Discord is dark".
    """

    class _Refuses:
        def add_steer_queue_listener(self, callback):
            raise RuntimeError("no listeners today")

        def remove_steer_queue_listener(self, callback):
            pass

    original = idle_delivery._controller
    idle_delivery._controller = lambda: _Refuses()
    try:
        idle_delivery.install()  # must not raise
    finally:
        idle_delivery._controller = original

    assert idle_delivery.is_installed() is False


def test_a_second_install_does_not_stack_a_second_listener(idle_ui):
    """install() twice must go through uninstall(), observably.

    AC-16 only checks the end state, and the core already guarantees that by
    comparing listener identity (``pause_controller.py:173``) -- so the reentry
    guard in ``install()`` carried no test of its own.  This watches the
    teardown happen instead of trusting the result.
    """
    removed: List[Any] = []
    real = pause_module.get_pause_controller()

    class _Watching:
        def add_steer_queue_listener(self, callback):
            real.add_steer_queue_listener(callback)

        def remove_steer_queue_listener(self, callback):
            removed.append(callback)
            real.remove_steer_queue_listener(callback)

    original = idle_delivery._controller
    idle_delivery._controller = lambda: _Watching()
    try:
        idle_delivery.install()
        assert removed == [], "nothing to tear down on the first install"
        idle_delivery.install()
    finally:
        idle_delivery._controller = original

    assert removed == [idle_delivery._on_steer_queued], (
        "the second install has to remove the first listener, not stack one"
    )


# --------------------------------------------------------------------------- #
# Review round 2 -- the except branch, and what /queue does when idle
# --------------------------------------------------------------------------- #


def test_a_failure_after_the_pop_says_the_message_is_gone(idle_ui, controller, caplog):
    """The loss window reports itself as a loss.

    Between pop and push the text belongs to nobody.  Both reviewers found
    this branch untested -- an injected raise after the push left the whole
    suite green.
    """
    idle_delivery.install()
    controller.request_steer("verloren", mode="queue")

    def _boom(item):
        raise RuntimeError("push exploded")

    original = run_ui._push_idle
    run_ui._push_idle = _boom
    try:
        with caplog.at_level("WARNING"):
            idle_delivery._deliver()  # must not raise
    finally:
        run_ui._push_idle = original

    assert controller.peek_pending_steer_queued() == [], "the pop did happen"
    assert any(
        "lost a queued message" in record.message for record in caplog.records
    ), "a loss has to be reported AS a loss"


def test_a_failure_before_the_pop_says_the_message_is_safe(idle_ui, controller, caplog):
    """The other half: nothing was popped, so nothing was lost.

    One message for both cases would send the operator hunting in the wrong
    window -- the text is still in the steer queue here.
    """
    idle_delivery.install()
    controller.request_steer("noch da", mode="queue")

    original = idle_delivery._pop_next_queued_steer

    def _boom():
        raise RuntimeError("pop exploded")

    idle_delivery._pop_next_queued_steer = _boom
    try:
        with caplog.at_level("WARNING"):
            idle_delivery._deliver()  # must not raise
    finally:
        idle_delivery._pop_next_queued_steer = original

    assert controller.peek_pending_steer_queued() == ["noch da"], "still safe"
    assert any(
        "still in the steer queue" in record.message for record in caplog.records
    ), "a non-loss must not be reported as a loss"


def test_editing_the_queue_while_idle_runs_the_entries(idle_ui, controller):
    """Characterisation: /queue edits while idle deliver immediately.

    ``replace_pending_steer_queued`` fires the listeners (pause_controller.py
    :336), so C8 picks the entries up -- editing ONE entry starts every entry
    in the list.  That follows from what the user asked for ("idle means run
    it"); a queue entry does not become less wanted by being edited, and C8
    cannot tell an edit from an arrival because the core reports both as the
    same event.  ``/queue`` has no is_run_active() guard of its own, unlike
    ``/steer`` (steer_queue/register_callbacks.py:51).

    Pinned so the behaviour is a decision on record, not a surprise.
    """
    run_ui._run_active = True
    controller.request_steer("erster", mode="queue")
    controller.request_steer("zweiter", mode="queue")
    run_ui._run_active = False

    idle_delivery.install()
    _settle(idle_ui)
    assert _drain(run_ui._idle_queue) == [], "installing alone delivers nothing"

    controller.replace_pending_steer_queued(["erster-editiert", "zweiter"])
    _settle(idle_ui)

    assert _drain(run_ui._idle_queue) == ["erster-editiert", "zweiter"]
    assert controller.peek_pending_steer_queued() == []


def test_the_env_isolation_covers_every_module_that_reads_one():
    """The derived env list must not silently shrink.

    It hangs on the ``*_ENV_VAR`` naming convention across three modules;
    a rename or a new module would drop out of it without a single test
    going red.  This is that test.
    """
    from cp_discord.tests import conftest

    names = set(conftest._env_var_names())

    assert "CP_DISCORD_TOOL_LOG" in names
    assert "DISCORD_BOT_TOKEN" in names
    assert "CP_DISCORD_DIR" in names, "broker_election's variable"
    assert "CODE_PUPPY_DISCORD_AUTHZ_DB" in names, "bindings' variable"
    assert len(names) >= 10, f"expected at least 10 env vars, found {len(names)}"
