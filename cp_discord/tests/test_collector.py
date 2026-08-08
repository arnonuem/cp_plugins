"""C7 — the report collector: AC-26a, AC-27a, AC-81a, AC-82.

Against a FAKE SINK, like the reporter's suite.  The arrival in a Discord
thread is AC-26b/27b/81b and belongs to W1 (wave 2).

The one thing these tests exist to prevent is the EMPTY REPORT: AC-26a alone
("during work only the status line, one report at the end") is green with a
report that says nothing, which is exactly the shell §8b introduced C7 to
avoid.  AC-81a therefore asserts the content, not the count.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from cp_discord import collector as collector_module
from cp_discord.chunking import DISCORD_LIMIT
from cp_discord.collector import (
    MAX_ENTRIES,
    MAX_BYTES,
    OVERFLOW_TEMPLATE,
    ReportCollector,
)
from cp_discord.reporter import (
    ACTIVITY_CODING,
    Mailbox,
    ReportEvent,
    StateEvent,
    StateReporter,
)


class _Delta:
    """Stands in for pydantic-ai's ``TextPartDelta``."""

    def __init__(self, content_delta: str) -> None:
        self.content_delta = content_delta


class _Part:
    """Stands in for pydantic-ai's ``TextPart``."""

    def __init__(self, content: str) -> None:
        self.content = content


def _part_start(content: str, part_type: str = "TextPart") -> tuple:
    return "part_start", {"index": 0, "part_type": part_type, "part": _Part(content)}


def _part_delta(content: str, delta_type: str = "TextPartDelta") -> tuple:
    return "part_delta", {
        "index": 0,
        "delta_type": delta_type,
        "delta": _Delta(content),
    }


class _Sink:
    def __init__(self) -> None:
        self.events: List[Any] = []

    def __call__(self, event: Any) -> None:
        self.events.append(event)

    @property
    def reports(self) -> List[ReportEvent]:
        return [e for e in self.events if isinstance(e, ReportEvent)]

    @property
    def states(self) -> List[StateEvent]:
        return [e for e in self.events if isinstance(e, StateEvent)]


class _Clock:
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


def _wire(mailbox: Mailbox, mode: str = "report"):
    """A reporter and a collector, wired the way ``install()`` wires them."""
    state = StateReporter(mailbox, mode=mode)
    report = ReportCollector(mailbox, state, mode=mode)
    return state, report


def _settle(mailbox: Mailbox) -> None:
    mailbox.drain_now()


def _text_of(event: ReportEvent) -> str:
    return "\n".join(event.chunks)


# --------------------------------------------------------------------------- #
# AC-81a — the report CONTENT.  An empty report is a FAILURE.
# --------------------------------------------------------------------------- #


def test_ac81a_the_report_carries_the_last_assistant_message(mailbox, sink):
    state, report = _wire(mailbox)
    state.on_run_start()
    report.on_stream_event(*_part_start("Here is what "))
    report.on_stream_event(*_part_delta("I found."))
    state.on_run_end()
    _settle(mailbox)

    assert sink.reports, "a run that produced output must produce a report"
    body = _text_of(sink.reports[-1])
    assert "Here is what I found." in body


def test_ac81a_the_report_lists_the_tools_since_the_last_wait_point(mailbox, sink):
    state, report = _wire(mailbox)
    state.on_run_start()
    report.on_pre_tool_call("read_file", {"file_path": "a.py"})
    report.on_post_tool_call("read_file", {"file_path": "a.py"}, None, 12.0)
    report.on_pre_tool_call("run_shell_command", {"command": "pytest -q"})
    report.on_post_tool_call("run_shell_command", {"command": "pytest -q"}, None, 900.0)
    report.on_stream_event(*_part_start("Done."))
    state.on_run_end()
    _settle(mailbox)

    body = _text_of(sink.reports[-1])
    assert "read_file" in body
    assert "a.py" in body
    assert "run_shell_command" in body
    assert "pytest -q" in body


def test_the_tool_log_can_be_switched_off_without_losing_the_answer(
    mailbox, sink, monkeypatch
):
    """``cp_discord_tool_log = 0`` drops the inventory, nothing else.

    The ``-> tool`` / ``<- tool (n ms)`` list is the bulk of a report and
    reads as noise to somebody who only wants to know what the agent SAID.
    Switching it off must not cost the answer -- otherwise the report is
    empty and never posted at all (see the test below).
    """
    monkeypatch.setattr(collector_module, "_tool_log_enabled", lambda: False)

    state, report = _wire(mailbox)
    state.on_run_start()
    report.on_pre_tool_call("read_file", {"file_path": "a.py"})
    report.on_post_tool_call("read_file", {"file_path": "a.py"}, None, 12.0)
    report.on_stream_event(*_part_start("Here is what I found."))
    state.on_run_end()
    _settle(mailbox)

    body = _text_of(sink.reports[-1])
    assert "Here is what I found." in body, "the answer must survive"
    assert "read_file" not in body
    assert "a.py" not in body
    assert "ms)" not in body


def test_the_tool_log_defaults_to_on(mailbox, sink):
    """Unconfigured means ON -- the inventory is why most people read it."""
    from cp_discord import register_callbacks

    assert register_callbacks.tool_log_enabled() is True


def test_ac81a_an_empty_report_is_never_emitted(mailbox, sink):
    """Nothing happened -> nothing to report.  The counterpart of the above."""
    state, _report = _wire(mailbox)
    state.on_run_start()
    state.on_run_end()
    _settle(mailbox)

    assert sink.reports == []


def test_ac81a_a_run_with_only_tool_calls_still_reports(mailbox, sink):
    """A model that answers with a tool call and no prose is the normal case."""
    state, report = _wire(mailbox)
    state.on_run_start()
    report.on_pre_tool_call("edit_file", {"file_path": "b.py"})
    report.on_post_tool_call("edit_file", {"file_path": "b.py"}, None, 5.0)
    state.on_run_end()
    _settle(mailbox)

    assert sink.reports
    assert "edit_file" in _text_of(sink.reports[-1])


def test_ac81a_the_next_report_starts_after_the_last_wait_point(mailbox, sink):
    state, report = _wire(mailbox)

    state.on_run_start()
    report.on_pre_tool_call("read_file", {"file_path": "first.py"})
    report.on_stream_event(*_part_start("First answer."))
    state.on_run_end()
    _settle(mailbox)

    state.on_run_start()
    report.on_pre_tool_call("read_file", {"file_path": "second.py"})
    report.on_stream_event(*_part_start("Second answer."))
    state.on_run_end()
    _settle(mailbox)

    second = _text_of(sink.reports[-1])
    assert "second.py" in second
    assert "first.py" not in second, "the buffer must reset at every wait point"
    assert "Second answer." in second
    assert "First answer." not in second


def test_ac81a_thinking_parts_are_not_the_assistant_message(mailbox, sink):
    state, report = _wire(mailbox)
    state.on_run_start()
    report.on_stream_event(*_part_start("secret plan", part_type="ThinkingPart"))
    report.on_stream_event(*_part_delta("more secrets", delta_type="ThinkingPartDelta"))
    report.on_pre_tool_call("read_file", {"file_path": "a.py"})
    state.on_run_end()
    _settle(mailbox)

    body = _text_of(sink.reports[-1])
    assert "secret plan" not in body
    assert "more secrets" not in body


def test_ac81a_only_the_LAST_assistant_message_is_reported(mailbox, sink):
    """Two turns in one run: the phone wants the conclusion, not the draft."""
    state, report = _wire(mailbox)
    state.on_run_start()
    report.on_stream_event(*_part_start("Let me check."))
    report.on_pre_tool_call("read_file", {"file_path": "a.py"})
    report.on_post_tool_call("read_file", {"file_path": "a.py"}, None, 3.0)
    report.on_stream_event(*_part_start("It is fine."))
    state.on_run_end()
    _settle(mailbox)

    body = _text_of(sink.reports[-1])
    assert "It is fine." in body
    assert "Let me check." not in body


def test_ac81a_the_report_is_chunked_for_discord(mailbox, sink):
    state, report = _wire(mailbox)
    state.on_run_start()
    report.on_stream_event(*_part_start("x" * (DISCORD_LIMIT * 2)))
    state.on_run_end()
    _settle(mailbox)

    event = sink.reports[-1]
    assert len(event.chunks) > 1
    assert all(len(chunk) <= DISCORD_LIMIT for chunk in event.chunks)


def test_ac81a_a_gate_is_a_wait_point_too(mailbox, sink):
    """§8b: at the wait point, the report AND the gate — a gate is one."""
    state, report = _wire(mailbox)
    state.on_run_start()
    report.on_pre_tool_call("run_shell_command", {"command": "rm -rf build"})
    state.gate_opened()
    _settle(mailbox)

    assert sink.reports, "the report must arrive with the gate, not after it"
    assert "rm -rf build" in _text_of(sink.reports[-1])


# --------------------------------------------------------------------------- #
# AC-82 — buffer bounds and a VISIBLE overflow marker
# --------------------------------------------------------------------------- #


def test_ac82_the_entry_limit_holds_and_the_overflow_is_visible(mailbox, sink):
    state, report = _wire(mailbox)
    state.on_run_start()
    for index in range(MAX_ENTRIES + 25):
        report.on_pre_tool_call("read_file", {"file_path": f"file_{index}.py"})
    state.on_run_end()
    _settle(mailbox)

    body = _text_of(sink.reports[-1])
    assert OVERFLOW_TEMPLATE.format(count=25) in body
    assert "file_0.py" not in body, "the oldest entries are the ones dropped"
    assert f"file_{MAX_ENTRIES + 24}.py" in body, "the newest are kept"


def test_ac82_nothing_is_dropped_silently(mailbox, sink):
    state, report = _wire(mailbox)
    state.on_run_start()
    for index in range(MAX_ENTRIES):
        report.on_pre_tool_call("read_file", {"file_path": f"file_{index}.py"})
    state.on_run_end()
    _settle(mailbox)

    body = _text_of(sink.reports[-1])
    assert "weitere" not in body, "exactly at the limit nothing overflowed"


def test_ac82_the_byte_limit_bites_before_the_entry_limit(mailbox, sink):
    """Whichever bites first — here the bytes, with only a handful of entries.

    A tool line is structurally small (``rendering.py:25`` caps the inlined
    argument at 120 characters), so entries alone cannot fill 8 KB.  A long
    assistant answer can, and the budget then has to come out of the entries
    even though there are far fewer than 50 of them.
    """
    state, report = _wire(mailbox)
    state.on_run_start()
    for index in range(10):
        report.on_pre_tool_call("read_file", {"file_path": f"file_{index}.py"})
    report.on_stream_event(*_part_start("z" * MAX_BYTES))

    # Measured BEFORE the wait point: flushing resets the buffer, so asserting
    # afterwards would only prove that zero is under the limit.
    assert report.buffered_entries < 10, "the byte budget must evict entries"
    assert report.buffered_bytes <= MAX_BYTES

    state.on_run_end()
    _settle(mailbox)

    assert "weitere" in _text_of(sink.reports[-1]), "and it has to say so"


def test_ac82_the_buffer_never_grows_without_bound(mailbox):
    state, report = _wire(mailbox)
    state.on_run_start()
    for index in range(5000):
        report.on_pre_tool_call("read_file", {"file_path": f"file_{index}.py"})

    assert report.buffered_entries <= MAX_ENTRIES
    assert report.buffered_bytes <= MAX_BYTES


def test_ac82_a_single_oversized_entry_does_not_empty_the_buffer(mailbox, sink):
    state, report = _wire(mailbox)
    state.on_run_start()
    report.on_pre_tool_call("run_shell_command", {"command": "z" * (MAX_BYTES * 2)})
    state.on_run_end()
    _settle(mailbox)

    assert sink.reports, "an oversized entry must still produce a report"
    assert report.buffered_bytes <= MAX_BYTES


def test_ac82_the_assistant_message_is_bounded_too(mailbox, sink):
    state, report = _wire(mailbox)
    state.on_run_start()
    for _ in range(500):
        report.on_stream_event(*_part_delta("w" * 100))
    assert report.buffered_bytes <= MAX_BYTES

    state.on_run_end()
    _settle(mailbox)

    assert sink.reports


# --------------------------------------------------------------------------- #
# AC-26a — report mode: status line during the work, ONE report at the end
# --------------------------------------------------------------------------- #


def test_ac26a_during_the_work_only_the_status_line_is_sent(mailbox, sink):
    state, report = _wire(mailbox, mode="report")
    state.on_run_start()
    for index in range(50):
        report.on_pre_tool_call("read_file", {"file_path": f"f{index}.py"})
        report.on_stream_event(*_part_delta(f"chunk {index} "))
        report.on_post_tool_call("read_file", {"file_path": f"f{index}.py"}, None, 1.0)
        _settle(mailbox)

    assert sink.reports == [], "report mode streams nothing while working"
    assert {e.message for e in sink.states} == {ACTIVITY_CODING}


def test_ac26a_exactly_one_report_event_at_the_wait_point(mailbox, sink):
    state, report = _wire(mailbox, mode="report")
    state.on_run_start()
    for index in range(50):
        report.on_pre_tool_call("read_file", {"file_path": f"f{index}.py"})
    report.on_stream_event(*_part_start("All done."))
    state.on_run_end()
    _settle(mailbox)

    assert len(sink.reports) == 1


def test_ac26a_a_second_wait_point_without_new_work_reports_nothing(mailbox, sink):
    state, report = _wire(mailbox, mode="report")
    state.on_run_start()
    report.on_stream_event(*_part_start("Done."))
    state.on_run_end()
    _settle(mailbox)

    state.on_run_start()
    state.on_run_end()
    _settle(mailbox)

    assert len(sink.reports) == 1


# --------------------------------------------------------------------------- #
# AC-27a — stream mode: deltas are produced, THROTTLED
# --------------------------------------------------------------------------- #


def test_ac27a_stream_mode_forwards_deltas_while_working(sink, clock):
    box = Mailbox(sink, clock=clock, min_interval=0.0)
    try:
        state, report = _wire(box, mode="stream")
        state.on_run_start()
        report.on_stream_event(*_part_start("live "))
        box.drain_now()

        assert any("live" in (e.message or "") for e in sink.states)
    finally:
        box.close()


def test_ac27a_a_thousand_deltas_are_throttled_not_streamed(sink, clock):
    box = Mailbox(sink, clock=clock, min_interval=2.0)
    try:
        state, report = _wire(box, mode="stream")
        state.on_run_start()
        box.drain_now()
        sink.events.clear()

        for index in range(1000):
            report.on_stream_event(*_part_delta(f"token{index} "))
            box.drain_now()

        assert len(sink.events) <= 1, f"{len(sink.events)} messages for 1000 deltas"

        clock.advance(2.0)
        box.drain_now()
        assert "token999" in sink.states[-1].message, "latest-wins, nothing queued"
    finally:
        box.close()


def test_ac27a_stream_mode_still_produces_the_report_at_the_wait_point(mailbox, sink):
    state, report = _wire(mailbox, mode="stream")
    state.on_run_start()
    report.on_stream_event(*_part_start("Streamed answer."))
    state.on_run_end()
    _settle(mailbox)

    assert "Streamed answer." in _text_of(sink.reports[-1])


# --------------------------------------------------------------------------- #
# INV-C4/INV-C1 — the collector never disturbs the agent
# --------------------------------------------------------------------------- #


def test_the_hooks_return_none_so_no_tool_is_blocked(mailbox):
    _state, report = _wire(mailbox)

    assert report.on_pre_tool_call("read_file", {"file_path": "a.py"}) is None
    assert report.on_post_tool_call("read_file", {}, None, 1.0) is None
    assert report.on_stream_event(*_part_start("x")) is None


def test_a_hostile_event_payload_is_survived(mailbox, sink):
    _state, report = _wire(mailbox)

    report.on_stream_event("part_start", None)
    report.on_stream_event("part_delta", {"delta": object()})
    report.on_stream_event("something_else", {})
    report.on_pre_tool_call(None, None)
    report.on_post_tool_call(None, None, None, None)

    _settle(mailbox)


# --------------------------------------------------------------------------- #
# The plugin surface C6 drives
# --------------------------------------------------------------------------- #


class _Config:
    def __init__(self, mode: str = "report") -> None:
        self.mode = mode


def test_install_registers_the_three_hooks_and_uninstall_removes_them():
    from code_puppy.callbacks import get_callbacks
    from cp_discord import reporter as reporter_module

    reporter_module.install(_Config())
    collector_module.install(_Config())
    try:
        assert collector_module._on_stream_event in get_callbacks("stream_event")
        assert collector_module._on_pre_tool_call in get_callbacks("pre_tool_call")
        assert collector_module._on_post_tool_call in get_callbacks("post_tool_call")
    finally:
        collector_module.uninstall()
        reporter_module.uninstall()

    assert collector_module._on_stream_event not in get_callbacks("stream_event")
    assert collector_module.active_collector() is None


def test_install_without_the_reporter_refuses_loudly():
    """C3 installs before C7 (COMPONENTS order); a silent no-op would ship an
    empty report shell, which is what AC-81a exists to prevent."""
    from cp_discord import reporter as reporter_module

    reporter_module.uninstall()

    with pytest.raises(RuntimeError):
        collector_module.install(_Config())


def test_the_layers_run_off_the_REAL_core_hooks_end_to_end():
    """Drive both layers through the core's own dispatch, not the fakes.

    Every other test in this file calls the handlers directly, so a handler
    registered under a wrong phase name -- or one whose signature does not
    match what the core passes -- would stay invisible: green units, dead
    plugin.  This is the one test that would notice.
    """
    import asyncio

    from code_puppy.callbacks import (
        on_agent_run_end,
        on_agent_run_start,
        on_awaiting_user_input,
        on_post_tool_call,
        on_pre_tool_call,
        on_stream_event,
    )
    from cp_discord import reporter as reporter_module

    seen: List[Any] = []
    reporter_module.set_sink(seen.append)

    async def drive() -> None:
        await on_agent_run_start("agent", "model", "cp_discord:abc")
        await on_pre_tool_call("run_shell_command", {"command": "pytest -q"})
        await on_post_tool_call(
            "run_shell_command", {"command": "pytest -q"}, None, 42.0
        )
        await on_stream_event(*_part_start("All green."))
        await on_agent_run_end("agent", "model", "cp_discord:abc")

    try:
        reporter_module.install(_Config())  # COMPONENTS order: C3 then C7
        collector_module.install(_Config())
        try:
            asyncio.run(drive())
            on_awaiting_user_input(True)  # as command_runner:337 fires it
            reporter_module.active_mailbox().drain_now()
        finally:
            collector_module.uninstall()
            reporter_module.uninstall()
    finally:
        reporter_module.set_sink(None)

    reports = [e for e in seen if isinstance(e, ReportEvent)]
    states = [e for e in seen if isinstance(e, StateEvent)]

    assert len(reports) == 1
    body = _text_of(reports[0])
    assert "All green." in body and "pytest -q" in body
    assert [e.state for e in states] == ["working", "idle", "blocked"]
    # The report belongs to the wait point, so it lands before that edge.
    assert seen[seen.index(reports[0]) + 1] is states[1]
    # INV-C23: nothing reported a gate, so this BLOCKED is PC-only.
    assert states[-1].remote_resolvable is False
