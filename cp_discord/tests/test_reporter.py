"""C3 — the state reporter: AC-18..25, AC-76a, and INV-C4/INV-C23.

Everything here runs against a FAKE SINK.  There is no broker and no Discord
in wave 1, and that is the point: the reporter's job is to *derive* and
*coalesce* state, and all of that is provable without a delivery path.  The
arrival in a thread is AC-26b/27b/81b and belongs to W1.
"""

from __future__ import annotations

import threading
import time
from typing import Any, List

import pytest

from cp_discord import reporter as reporter_module
from cp_discord.reporter import (
    IDLE,
    BLOCKED,
    WORKING,
    ACTIVITY_CODING,
    LOCAL_ONLY_MARKER,
    Mailbox,
    ReleaseEvent,
    ReportEvent,
    StateEvent,
    StateReporter,
    derive_state,
)


class _Sink:
    """Collects delivered events.  Stands in for C2's send path."""

    def __init__(self) -> None:
        self.events: List[Any] = []

    def __call__(self, event: Any) -> None:
        self.events.append(event)

    @property
    def states(self) -> List[StateEvent]:
        return [e for e in self.events if isinstance(e, StateEvent)]


class _Clock:
    """A hand-cranked monotonic clock, so throttling is not a race."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def sink() -> _Sink:
    return _Sink()


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def mailbox(sink, clock) -> Mailbox:
    box = Mailbox(sink, clock=clock, min_interval=0.0)
    yield box
    box.close()


def _reporter(mailbox: Mailbox, *, mode: str = "report") -> StateReporter:
    return StateReporter(mailbox, mode=mode)


def _settle(mailbox: Mailbox) -> None:
    """Deliver everything a worker thread would have delivered by now."""
    mailbox.drain_now()


# --------------------------------------------------------------------------- #
# AC-18 — the state model, all combinations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("awaiting", "run_depth", "expected"),
    [
        (False, 0, IDLE),
        (False, 1, WORKING),
        (False, 7, WORKING),
        (True, 0, BLOCKED),
        (True, 1, BLOCKED),
        (True, 7, BLOCKED),
    ],
)
def test_ac18_state_is_a_pure_function_of_awaiting_and_depth(
    awaiting, run_depth, expected
):
    assert derive_state(awaiting, run_depth) == expected


def test_ac18_the_reporter_reports_those_states(mailbox, sink):
    state = _reporter(mailbox)

    state.on_run_start()
    _settle(mailbox)
    assert sink.states[-1].state == WORKING

    state.gate_opened()
    _settle(mailbox)
    assert sink.states[-1].state == BLOCKED

    state.gate_closed()
    state.on_run_end()
    _settle(mailbox)
    assert sink.states[-1].state == IDLE


# --------------------------------------------------------------------------- #
# AC-19 — refcount, not a bool
# --------------------------------------------------------------------------- #


def test_ac19_a_finished_subagent_does_not_make_the_session_idle(mailbox, sink):
    """With a bool this would be IDLE — the defect the refcount exists for."""
    state = _reporter(mailbox)

    state.on_run_start()  # root agent
    state.on_run_start()  # sub-agent, same hooks
    state.on_run_end()  # sub-agent finishes
    _settle(mailbox)

    assert state.state == WORKING
    assert sink.states[-1].state == WORKING

    state.on_run_end()  # root agent finishes
    _settle(mailbox)

    assert sink.states[-1].state == IDLE


def test_ac19_the_depth_never_goes_negative(mailbox):
    state = _reporter(mailbox)

    state.on_run_end()
    state.on_run_end()
    state.on_run_start()

    assert state.state == WORKING


# --------------------------------------------------------------------------- #
# AC-20 — BLOCKED beats WORKING
# --------------------------------------------------------------------------- #


def test_ac20_an_approval_mid_run_reports_blocked_not_working(mailbox, sink):
    state = _reporter(mailbox)
    state.on_run_start()
    _settle(mailbox)

    state.gate_opened()
    _settle(mailbox)

    assert state.state == BLOCKED
    assert sink.states[-1].state == BLOCKED


def test_ac20_resolving_the_gate_returns_to_working(mailbox, sink):
    state = _reporter(mailbox)
    state.on_run_start()
    state.gate_opened()
    _settle(mailbox)

    state.gate_closed()
    _settle(mailbox)

    assert sink.states[-1].state == WORKING


# --------------------------------------------------------------------------- #
# AC-21 — INV-C4: the awaiting_user_input handler NEVER blocks
# --------------------------------------------------------------------------- #


def test_ac21_the_handler_returns_immediately_behind_a_slow_sink():
    """The hook is sync and on a hot path: a slow sink must not reach it."""
    entered = threading.Event()
    release = threading.Event()

    def slow_sink(_event: Any) -> None:
        entered.set()
        release.wait(5.0)

    box = Mailbox(slow_sink, min_interval=0.0, autostart=True)
    try:
        state = StateReporter(box, mode="report")
        state.on_run_start()  # gets the worker into the slow sink
        assert entered.wait(2.0), "the worker never reached the sink"

        started = time.monotonic()
        state.on_awaiting_user_input(True)
        elapsed = time.monotonic() - started

        assert elapsed < 0.05, f"the handler blocked for {elapsed:.3f}s"
    finally:
        release.set()
        box.close(timeout=2.0)


def test_ac21_no_handler_blocks_behind_a_slow_sink():
    """The same guarantee for every other hot-path handler."""
    release = threading.Event()

    def slow_sink(_event: Any) -> None:
        release.wait(5.0)

    box = Mailbox(slow_sink, min_interval=0.0, autostart=True)
    try:
        state = StateReporter(box, mode="stream")
        started = time.monotonic()
        for _ in range(200):
            state.on_run_start()
            state.on_tool_start("run_shell_command")
            state.on_tool_end()
            state.on_awaiting_user_input(True)
            state.on_awaiting_user_input(False)
            state.gate_opened()
            state.gate_closed()
            state.on_run_end()
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"the handlers blocked for {elapsed:.3f}s"
    finally:
        release.set()
        box.close(timeout=2.0)


# --------------------------------------------------------------------------- #
# AC-22 — notify=False (a user-initiated menu) does not say "waiting for you"
# --------------------------------------------------------------------------- #


def test_ac22_a_menu_with_notify_false_reports_nothing(mailbox, sink):
    state = _reporter(mailbox)
    state.on_awaiting_user_input(True, notify=False)
    _settle(mailbox)

    assert sink.states == []


def test_ac22_closing_that_menu_does_not_emit_a_late_edge(mailbox, sink):
    state = _reporter(mailbox)
    state.on_awaiting_user_input(True, notify=False)
    _settle(mailbox)
    state.on_awaiting_user_input(False, notify=False)
    _settle(mailbox)

    assert sink.states == []


def test_ac22_an_agent_initiated_wait_still_reports(mailbox, sink):
    state = _reporter(mailbox)
    state.on_awaiting_user_input(True)
    _settle(mailbox)

    assert sink.states[-1].state == BLOCKED


# --------------------------------------------------------------------------- #
# INV-C23 — a BLOCKED that the phone cannot answer says so
# --------------------------------------------------------------------------- #


def test_invc23_a_hook_blocked_is_marked_as_pc_only(mailbox, sink):
    state = _reporter(mailbox)
    state.on_awaiting_user_input(True)
    _settle(mailbox)

    event = sink.states[-1]
    assert event.remote_resolvable is False
    assert LOCAL_ONLY_MARKER in event.message


def test_invc23_a_gate_blocked_is_answerable_from_the_phone(mailbox, sink):
    state = _reporter(mailbox)
    state.gate_opened()
    _settle(mailbox)

    event = sink.states[-1]
    assert event.remote_resolvable is True
    assert LOCAL_ONLY_MARKER not in (event.message or "")


# --------------------------------------------------------------------------- #
# AC-23 — coalescing: 1000 tool calls are NOT 1000 messages
# --------------------------------------------------------------------------- #


def test_ac23_a_thousand_decorative_posts_collapse_into_one(mailbox, sink):
    for index in range(1000):
        mailbox.post_activity(StateEvent(WORKING, f"running tool {index}", False))

    _settle(mailbox)

    assert len(sink.events) == 1
    assert sink.events[0].message == "running tool 999", "latest-wins, not oldest"


def test_ac23_a_thousand_tool_calls_do_not_produce_a_thousand_events(mailbox, sink):
    state = _reporter(mailbox, mode="stream")
    state.on_run_start()
    _settle(mailbox)
    sink.events.clear()

    for index in range(1000):
        state.on_tool_start(f"tool_{index}")
        state.on_tool_end()

    _settle(mailbox)

    assert len(sink.events) <= 2, f"{len(sink.events)} messages for 1000 tool calls"


def test_ac23_the_throttle_holds_decorative_traffic_back(sink, clock):
    box = Mailbox(sink, clock=clock, min_interval=2.0)
    try:
        box.post_activity(StateEvent(WORKING, "first", False))
        box.drain_now()
        assert len(sink.events) == 1

        box.post_activity(StateEvent(WORKING, "second", False))
        box.drain_now()
        assert len(sink.events) == 1, "the throttle must hold it back"

        clock.advance(2.0)
        box.drain_now()
        assert len(sink.events) == 2
        assert sink.events[-1].message == "second"
    finally:
        box.close()


# --------------------------------------------------------------------------- #
# AC-24 — critical overtakes decorative; release is the last event
# --------------------------------------------------------------------------- #


def test_ac24_critical_work_overtakes_decorative_work(mailbox, sink):
    mailbox.post_activity(StateEvent(WORKING, "decorative", False))
    mailbox.post_state(StateEvent(BLOCKED, "critical", True))

    _settle(mailbox)

    assert [e.message for e in sink.events] == ["critical", "decorative"]


def test_ac24_a_report_precedes_the_state_edge_it_belongs_to(mailbox, sink):
    """§8b: at the wait point the phone gets the report AND the gate."""
    mailbox.post_state(StateEvent(BLOCKED, "gate", True))
    mailbox.post_report(ReportEvent(("the report",)))

    _settle(mailbox)

    assert isinstance(sink.events[0], ReportEvent)
    assert isinstance(sink.events[1], StateEvent)


def test_ac24_release_is_the_last_event_and_decorative_work_is_dropped(sink, clock):
    box = Mailbox(sink, clock=clock, min_interval=0.0)
    box.post_activity(StateEvent(WORKING, "decorative", False))
    box.post_state(StateEvent(IDLE, None, False))

    box.close()

    assert isinstance(sink.events[-1], ReleaseEvent)
    assert [type(e) for e in sink.events] == [StateEvent, ReleaseEvent], (
        "decorative work must be discarded on shutdown, critical work flushed"
    )


def test_ac24_closing_twice_releases_once(sink, clock):
    box = Mailbox(sink, clock=clock, min_interval=0.0)
    box.close()
    box.close()

    assert sum(isinstance(e, ReleaseEvent) for e in sink.events) == 1


# --------------------------------------------------------------------------- #
# AC-25 — only genuine (state, message) changes go on the wire
# --------------------------------------------------------------------------- #


def test_ac25_an_unchanged_state_is_not_resent(mailbox, sink):
    state = _reporter(mailbox)

    state.on_run_start()
    _settle(mailbox)
    state.on_run_start()  # sub-agent: still WORKING, same message
    _settle(mailbox)
    state.on_run_end()
    _settle(mailbox)

    assert [e.state for e in sink.states] == [WORKING]


def test_ac25_a_message_only_change_still_goes_out(mailbox, sink):
    state = _reporter(mailbox, mode="stream")
    state.on_run_start()
    _settle(mailbox)

    state.on_tool_start("read_file")
    _settle(mailbox)

    assert len(sink.states) == 2
    assert sink.states[-1].state == WORKING
    assert sink.states[-1].message != sink.states[0].message


def test_ac25_in_report_mode_the_status_line_never_changes(mailbox, sink):
    """§8b: during work, report mode shows the status line and nothing else."""
    state = _reporter(mailbox, mode="report")
    state.on_run_start()
    _settle(mailbox)

    for name in ("read_file", "run_shell_command", "edit_file"):
        state.on_tool_start(name)
        state.on_tool_end()
        _settle(mailbox)

    assert [e.message for e in sink.states] == [ACTIVITY_CODING]


# --------------------------------------------------------------------------- #
# AC-76a — dedup: an open gate silences the concurrent hook (INV-C27)
# --------------------------------------------------------------------------- #


def test_ac76a_a_hook_edge_during_an_open_gate_is_ignored(mailbox, sink):
    state = _reporter(mailbox)
    state.on_run_start()
    _settle(mailbox)
    sink.events.clear()

    state.gate_opened()  # INV-C24: the backend reports it itself
    state.on_awaiting_user_input(True)  # INV-C27: and the flag fires too
    _settle(mailbox)

    assert len(sink.states) == 1
    assert sink.states[0].state == BLOCKED


def test_ac76a_the_gate_closing_restores_the_state_without_a_hook_edge(mailbox, sink):
    """The dedup must not depend on the hook's False ever arriving."""
    state = _reporter(mailbox)
    state.on_run_start()
    state.gate_opened()
    state.on_awaiting_user_input(True)
    _settle(mailbox)
    sink.events.clear()

    state.gate_closed()
    _settle(mailbox)

    assert sink.states[-1].state == WORKING


def test_ac76a_a_late_hook_false_after_the_gate_is_harmless(mailbox, sink):
    state = _reporter(mailbox)
    state.on_run_start()
    state.gate_opened()
    state.on_awaiting_user_input(True)
    state.gate_closed()
    state.on_awaiting_user_input(False)
    _settle(mailbox)

    assert sink.states[-1].state == WORKING


def test_ac76a_two_gates_need_two_closes(mailbox, sink):
    """Gates are refcounted for the same reason runs are (AC-19)."""
    state = _reporter(mailbox)
    state.on_run_start()
    state.gate_opened()
    state.gate_opened()
    state.gate_closed()
    _settle(mailbox)

    assert state.state == BLOCKED

    state.gate_closed()
    _settle(mailbox)

    assert state.state == WORKING


def test_ac76a_gate_closed_never_goes_negative(mailbox):
    state = _reporter(mailbox)
    state.gate_closed()
    state.gate_closed()
    state.on_run_start()
    state.gate_opened()

    assert state.state == BLOCKED

    state.gate_closed()

    assert state.state == WORKING


# --------------------------------------------------------------------------- #
# Wait points — the seam C7 hangs on
# --------------------------------------------------------------------------- #


def test_a_wait_point_fires_when_work_stops_for_a_human(mailbox):
    seen: List[str] = []
    state = _reporter(mailbox)
    state.add_wait_point_observer(lambda: seen.append("fired"))

    state.on_run_start()
    assert seen == []

    state.gate_opened()
    assert seen == ["fired"]


def test_a_wait_point_fires_when_the_run_ends(mailbox):
    seen: List[str] = []
    state = _reporter(mailbox)
    state.add_wait_point_observer(lambda: seen.append("fired"))

    state.on_run_start()
    state.on_run_end()

    assert seen == ["fired"]


def test_a_failing_observer_never_reaches_the_agent(mailbox, sink):
    state = _reporter(mailbox)

    def explode() -> None:
        raise RuntimeError("observer is broken")

    state.add_wait_point_observer(explode)

    state.on_run_start()
    state.on_run_end()
    _settle(mailbox)

    assert sink.states[-1].state == IDLE


def test_a_failing_sink_never_reaches_the_agent(clock):
    def explode(_event: Any) -> None:
        raise RuntimeError("the broker is gone")

    box = Mailbox(explode, clock=clock, min_interval=0.0)
    try:
        state = StateReporter(box, mode="report")
        state.on_run_start()
        box.drain_now()
    finally:
        box.close()


# --------------------------------------------------------------------------- #
# Cancellation and turn boundaries
# --------------------------------------------------------------------------- #


def test_a_cancelled_run_falls_back_to_idle(mailbox, sink):
    state = _reporter(mailbox)
    state.on_run_start()
    state.on_run_start()
    _settle(mailbox)

    state.on_run_cancel()
    _settle(mailbox)

    assert sink.states[-1].state == IDLE


def test_a_cancelled_run_does_NOT_clear_a_gate_the_backend_owns(mailbox, sink):
    """Both gate edges belong to C4 (INV-C24) — C3 must not forge the second.

    A cancel does not take a terminal approval prompt off the screen.  If C3
    cleared the gate here, the phone would show IDLE while a prompt is still
    waiting, and C4's later ``gate_closed()`` would clamp to a no-op — the
    wait point would be invisible on exactly the path INV-C24 exists for.
    """
    state = _reporter(mailbox)
    state.on_run_start()
    state.gate_opened()
    _settle(mailbox)

    state.on_run_cancel()
    _settle(mailbox)

    assert sink.states[-1].state == BLOCKED

    state.gate_closed()  # the backend answers, as it always does
    _settle(mailbox)

    assert sink.states[-1].state == IDLE


def test_a_finished_turn_falls_back_to_idle(mailbox, sink):
    state = _reporter(mailbox)
    state.on_run_start()
    _settle(mailbox)

    state.on_turn_end()
    _settle(mailbox)

    assert sink.states[-1].state == IDLE


# --------------------------------------------------------------------------- #
# The plugin surface C6 drives
# --------------------------------------------------------------------------- #


class _Config:
    def __init__(self, mode: str = "report") -> None:
        self.mode = mode


def test_install_registers_the_hot_path_hooks_and_uninstall_removes_them():
    from code_puppy.callbacks import get_callbacks

    reporter_module.install(_Config())
    try:
        assert reporter_module._on_awaiting_user_input in get_callbacks(
            "awaiting_user_input"
        )
        assert reporter_module._on_run_start in get_callbacks("agent_run_start")
        assert reporter_module.active_reporter() is not None
    finally:
        reporter_module.uninstall()

    assert reporter_module._on_awaiting_user_input not in get_callbacks(
        "awaiting_user_input"
    )
    assert reporter_module.active_reporter() is None


def test_the_sink_can_be_wired_before_or_after_install():
    """C2 installs BEFORE C3 (COMPONENTS order), so order must not matter."""
    delivered: List[Any] = []
    reporter_module.set_sink(delivered.append)
    try:
        reporter_module.install(_Config())
        try:
            reporter_module.active_reporter().on_run_start()
            reporter_module.active_mailbox().drain_now()
        finally:
            reporter_module.uninstall()
    finally:
        reporter_module.set_sink(None)

    assert [e.state for e in delivered if isinstance(e, StateEvent)] == [WORKING]


def test_without_a_sink_the_reporter_is_a_harmless_no_op():
    reporter_module.set_sink(None)
    reporter_module.install(_Config())
    try:
        reporter_module.active_reporter().on_run_start()
        reporter_module.active_mailbox().drain_now()
    finally:
        reporter_module.uninstall()
