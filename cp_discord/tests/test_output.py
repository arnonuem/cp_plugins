"""L5 output — AC-29..33, AC-53, AC-54.

The output layer is the only place where a channel actually SEES anything, so
these tests drive the real routing surface (callbacks, bus messages, outcome
sink) against fake Discord objects rather than mocking the router itself.

Four properties are load-bearing and each has its own test:

* attribution — three concurrent sessions must not bleed into each other;
* chunking — Discord's 2000-character limit, without breaking a code fence;
* throttling — edits are coalesced, and a superseded intermediate state is
  DROPPED rather than sent late;
* the system channel — a message nobody can attribute still has to land
  somewhere, and a failing Discord call must never reach the agent run.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

import pytest

from code_puppy.messaging.messages import (
    DiffMessage,
    MessageLevel,
    ShellLineMessage,
    ShellOutputMessage,
    TextMessage,
)
from code_puppy.plugins.cp_discord import chunking, gateway, output

# ---------------------------------------------------------------------------
# Fake Discord surface
# ---------------------------------------------------------------------------


class FakeMessage:
    """A sent Discord message that records every edit it receives."""

    def __init__(self, channel: "FakeChannel", content: str) -> None:
        self.channel = channel
        self.content = content
        self.edits: List[str] = []

    async def edit(self, content: str) -> None:
        self.content = content
        self.edits.append(content)
        self.channel.edited.append(content)


class FakeChannel:
    def __init__(self, channel_id: int, fail: bool = False) -> None:
        self.id = channel_id
        self.fail = fail
        self.sent: List[str] = []
        self.edited: List[str] = []
        self.messages: List[FakeMessage] = []
        #: Every ``send`` kwarg, so the mention policy stays observable.
        self.send_kwargs: List[dict] = []

    async def send(self, content: str, **kwargs) -> FakeMessage:
        # Everything routed here is text the bot did not author, so a missing
        # suppression would let an @everyone in agent output ping the server.
        assert "allowed_mentions" in kwargs, "output must suppress mentions"
        if self.fail:
            raise RuntimeError(f"discord is down for channel {self.id}")
        self.send_kwargs.append(dict(kwargs))
        self.sent.append(content)
        message = FakeMessage(self, content)
        self.messages.append(message)
        return message

    def text(self) -> str:
        """Everything currently visible in this channel, in order."""
        return "\n".join(m.content for m in self.messages)


class FakeClient:
    def __init__(self, *channels: FakeChannel) -> None:
        self.channels = {c.id: c for c in channels}

    def get_channel(self, channel_id: int) -> Optional[FakeChannel]:
        return self.channels.get(channel_id)


class FakeClock:
    """Monotonic clock the test moves by hand, so no test ever sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(autouse=True)
def _clean_state():
    output.uninstall()
    gateway.reset_state()
    yield
    output.uninstall()
    gateway.reset_state()


@pytest.fixture
def wired(clock):
    """Install the router with a system channel and a fake client attached."""

    def _wire(*channels: FakeChannel, system: Optional[FakeChannel] = None):
        all_channels = list(channels) + ([system] if system is not None else [])
        gateway.set_connection(FakeClient(*all_channels), asyncio.get_event_loop())
        output.install(
            system_channel_id=system.id if system is not None else None,
            clock=clock,
            start_tasks=False,
        )

    return _wire


def _delta(content: str) -> dict:
    """A ``part_delta`` payload shaped like pydantic-ai's TextPartDelta."""

    class _TextPartDelta:
        def __init__(self, text: str) -> None:
            self.content_delta = text

    return {"index": 0, "delta_type": "TextPartDelta", "delta": _TextPartDelta(content)}


def _start(content: str = "") -> dict:
    class _TextPart:
        def __init__(self, text: str) -> None:
            self.content = text

    return {"index": 0, "part_type": "TextPart", "part": _TextPart(content)}


# ===========================================================================
# AC-30 — chunking (pure text, no Discord involved)
# ===========================================================================


def test_ac30_short_text_is_one_chunk():
    assert chunking.chunk_message("hello") == ["hello"]


def test_ac30_empty_text_produces_nothing():
    assert chunking.chunk_message("") == []
    assert chunking.chunk_message("   \n  ") == []


def test_ac30_long_text_is_split_below_the_limit_at_line_boundaries():
    text = "\n".join(f"line {i:04d}" for i in range(500))
    chunks = chunking.chunk_message(text)

    assert len(chunks) > 1
    assert all(len(c) <= chunking.DISCORD_LIMIT for c in chunks)
    # Nothing was lost and no line was cut in half.
    assert "\n".join(chunks).split() == text.split()
    for chunk in chunks:
        for line in chunk.splitlines():
            assert line == "" or line.startswith("line ")


def test_ac30_code_fence_is_never_broken_across_a_chunk():
    body = "\n".join(f"    payload row {i:04d}" for i in range(400))
    text = f"```python\n{body}\n```"

    chunks = chunking.chunk_message(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= chunking.DISCORD_LIMIT
        # An odd number of fence markers means the block bleeds into the next
        # message and Discord renders the rest of the channel as code.
        assert chunk.count("```") % 2 == 0, "chunk ends inside an open code fence"
        assert chunk.startswith("```")
        assert chunk.rstrip().endswith("```")


def test_ac30_unterminated_fence_is_closed_in_every_chunk():
    """A live stream is mid-block by definition — it must still render."""
    text = "```\n" + "\n".join(f"row {i}" for i in range(400))

    chunks = chunking.chunk_message(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0


def test_ac30_single_overlong_line_is_hard_split():
    text = "x" * (chunking.DISCORD_LIMIT * 2 + 17)

    chunks = chunking.chunk_message(text)

    assert all(len(c) <= chunking.DISCORD_LIMIT for c in chunks)
    assert "".join(chunks) == text


def test_ac30_overlong_line_inside_a_fence_stays_fenced():
    text = "```\n" + "y" * (chunking.DISCORD_LIMIT * 2)

    chunks = chunking.chunk_message(text)

    for chunk in chunks:
        assert len(chunk) <= chunking.DISCORD_LIMIT
        assert chunk.count("```") % 2 == 0


# ===========================================================================
# AC-29 — deltas land in the right channel, with three runs in flight
# ===========================================================================


async def test_ac29_three_concurrent_sessions_never_cross_channels(wired):
    channels = [FakeChannel(101), FakeChannel(102), FakeChannel(103)]
    wired(*channels, system=FakeChannel(999))

    async def run(channel_id: int, marker: str) -> None:
        from code_puppy.plugins.cp_discord import concurrency

        with concurrency.session_scope(gateway.session_id_for(channel_id)):
            for i in range(5):
                await output.on_stream_event("part_delta", _delta(f"{marker}{i} "))
                await asyncio.sleep(0)

    await asyncio.gather(
        run(101, "A"),
        run(102, "B"),
        run(103, "C"),
    )
    await output.flush_due(force=True)

    for channel, marker in zip(channels, "ABC"):
        body = channel.text()
        assert body, f"channel {channel.id} received nothing"
        assert marker in body
        for other in set("ABC") - {marker}:
            assert other not in body, f"channel {channel.id} leaked {other}"


async def test_ac29_agent_session_id_attributes_when_the_contextvar_is_absent():
    """The callback's own session argument is the documented second source."""
    channel = FakeChannel(201)
    gateway.set_connection(FakeClient(channel), asyncio.get_event_loop())
    output.install(system_channel_id=None, start_tasks=False)

    await output.on_stream_event(
        "part_delta", _delta("from-callback"), gateway.session_id_for(201)
    )
    await output.flush_due(force=True)

    assert "from-callback" in channel.text()


# ===========================================================================
# AC-31 — throttled edits, stale intermediates dropped
# ===========================================================================


async def test_ac31_many_deltas_produce_one_message_not_one_per_delta(wired, clock):
    channel = FakeChannel(301)
    wired(channel, system=FakeChannel(999))
    sid = gateway.session_id_for(301)

    from code_puppy.plugins.cp_discord import concurrency

    with concurrency.session_scope(sid):
        for i in range(20):
            await output.on_stream_event("part_delta", _delta(f"d{i} "))
            await output.flush_due()  # the pump ticks, the throttle holds

    assert channel.sent == [], "nothing may be sent before the throttle window"

    clock.advance(output.EDIT_INTERVAL_S + 0.01)
    await output.flush_due()

    assert len(channel.sent) == 1
    assert channel.messages[0].content.startswith("d0 ")
    assert "d19" in channel.messages[0].content


async def test_ac31_superseded_intermediate_states_are_dropped_not_sent_late(
    wired, clock
):
    channel = FakeChannel(302)
    wired(channel, system=FakeChannel(999))

    from code_puppy.plugins.cp_discord import concurrency

    with concurrency.session_scope(gateway.session_id_for(302)):
        for i in range(30):
            await output.on_stream_event("part_delta", _delta(f"{i}."))
            await output.flush_due()
            clock.advance(0.1)
            if i % 10 == 9:
                clock.advance(output.EDIT_INTERVAL_S)
                await output.flush_due()

    clock.advance(output.EDIT_INTERVAL_S)
    await output.flush_due(force=True)

    writes = channel.sent + channel.edited
    assert len(writes) <= 5, f"edits were not coalesced: {len(writes)} writes"
    # Every write is a strict prefix-extension of the previous one: no write
    # ever repeats a state that a later write already superseded.
    assert writes == sorted(writes, key=len)
    assert writes[-1].endswith("29.")


async def test_ac31_flush_before_the_window_is_a_no_op(wired, clock):
    channel = FakeChannel(303)
    wired(channel, system=FakeChannel(999))

    from code_puppy.plugins.cp_discord import concurrency

    with concurrency.session_scope(gateway.session_id_for(303)):
        await output.on_stream_event("part_delta", _delta("first"))

    clock.advance(output.EDIT_INTERVAL_S + 0.01)
    await output.flush_due()
    assert len(channel.sent) == 1

    with concurrency.session_scope(gateway.session_id_for(303)):
        await output.on_stream_event("part_delta", _delta(" second"))
    await output.flush_due()

    assert channel.edited == [], "the second write jumped the throttle window"

    clock.advance(output.EDIT_INTERVAL_S + 0.01)
    await output.flush_due()
    assert channel.edited == ["first second"]


# ===========================================================================
# AC-54 — ShellOutputMessage, the single most useful artefact of a run
# ===========================================================================


async def test_ac54_shell_output_message_is_routed_with_its_exit_code(wired):
    channel = FakeChannel(401)
    wired(channel, system=FakeChannel(999))

    output.route_bus_message(
        ShellOutputMessage(
            command="pytest -q",
            stdout="3 passed",
            stderr="",
            exit_code=0,
            duration_seconds=1.25,
            session_id=gateway.session_id_for(401),
        )
    )
    await output.flush_due(force=True)

    body = channel.text()
    assert "pytest -q" in body
    assert "3 passed" in body
    assert "exit 0" in body


async def test_ac54_failing_shell_output_shows_the_nonzero_exit_code(wired):
    channel = FakeChannel(402)
    wired(channel, system=FakeChannel(999))

    output.route_bus_message(
        ShellOutputMessage(
            command="false",
            stdout="",
            stderr="boom",
            exit_code=7,
            duration_seconds=0.1,
            session_id=gateway.session_id_for(402),
        )
    )
    await output.flush_due(force=True)

    body = channel.text()
    assert "exit 7" in body
    assert "boom" in body


async def test_ac54_shell_lines_and_diffs_reach_the_channel(wired):
    channel = FakeChannel(403)
    wired(channel, system=FakeChannel(999))
    sid = gateway.session_id_for(403)

    output.route_bus_message(
        ShellLineMessage(line="compiling...", stream="stdout", session_id=sid)
    )
    output.route_bus_message(
        ShellLineMessage(line="warning: slow", stream="stderr", session_id=sid)
    )
    output.route_bus_message(
        DiffMessage(path="a/b.py", operation="modify", session_id=sid)
    )
    await output.flush_due(force=True)

    body = channel.text()
    assert "compiling..." in body
    assert "warning: slow" in body
    assert "a/b.py" in body


async def test_ac54_bus_pump_drains_the_real_message_bus(wired, monkeypatch):
    """The router is a real bus consumer, not just a function tests can call."""
    from code_puppy.messaging import bus as bus_module

    fresh = bus_module.MessageBus()
    monkeypatch.setattr(bus_module, "_global_bus", fresh)

    channel = FakeChannel(404)
    wired(channel, system=FakeChannel(999))

    fresh.emit(
        ShellOutputMessage(
            command="echo hi",
            stdout="hi",
            stderr="",
            exit_code=0,
            duration_seconds=0.01,
            session_id=gateway.session_id_for(404),
        )
    )
    await output.drain_bus()
    await output.flush_due(force=True)

    assert "echo hi" in channel.text()


# ===========================================================================
# AC-53 — the system channel catches what nobody can attribute
# ===========================================================================


async def test_ac53_bus_message_without_a_session_lands_in_the_system_channel(wired):
    channel = FakeChannel(501)
    system = FakeChannel(999)
    wired(channel, system=system)

    output.route_bus_message(
        ShellLineMessage(line="zombie reader line", stream="stdout", session_id=None)
    )
    await output.flush_due(force=True)

    assert "zombie reader line" in system.text()
    assert channel.text() == "", "an unattributed line must not pick a channel"


async def test_ac53_unknown_session_id_lands_in_the_system_channel(wired):
    channel = FakeChannel(502)
    system = FakeChannel(999)
    wired(channel, system=system)

    output.route_bus_message(
        ShellLineMessage(line="stray", stream="stdout", session_id="acp:whatever")
    )
    await output.flush_due(force=True)

    assert "stray" in system.text()
    assert channel.text() == ""


async def test_ac53_legacy_emit_info_lands_in_the_system_channel(wired):
    system = FakeChannel(999)
    wired(FakeChannel(503), system=system)

    from code_puppy.messaging import message_queue

    output.route_legacy_message(
        message_queue.UIMessage(
            type=message_queue.MessageType.WARNING, content="legacy warning"
        )
    )
    await output.flush_due(force=True)

    assert "legacy warning" in system.text()


async def test_ac53_bus_text_message_without_a_session_lands_in_the_system_channel(
    wired,
):
    system = FakeChannel(999)
    wired(FakeChannel(504), system=system)

    output.route_bus_message(
        TextMessage(level=MessageLevel.WARNING, text="unattributed notice")
    )
    await output.flush_due(force=True)

    assert "unattributed notice" in system.text()


async def test_ac53_without_a_configured_system_channel_nothing_is_dropped(wired):
    """No Discord system channel is still not permission to discard."""
    wired(FakeChannel(505))  # no system channel at all

    output.route_bus_message(
        ShellLineMessage(line="orphan line", stream="stdout", session_id=None)
    )
    await output.flush_due(force=True)

    assert any("orphan line" in entry for entry in output.undelivered())


async def test_ac53_legacy_listener_is_actually_registered(wired):
    """Registration, not just the handler, is what makes AC-53 real."""
    from code_puppy.messaging import message_queue

    system = FakeChannel(999)
    wired(FakeChannel(506), system=system)

    queue = message_queue.get_global_queue()
    assert output.route_legacy_message in queue._listeners

    output.uninstall()
    assert output.route_legacy_message not in queue._listeners


# ===========================================================================
# AC-32 — a broken Discord send never breaks the agent run
# ===========================================================================


async def test_ac32_send_failure_does_not_propagate_out_of_a_callback(wired):
    broken = FakeChannel(601, fail=True)
    wired(broken, system=FakeChannel(999))

    from code_puppy.plugins.cp_discord import concurrency

    with concurrency.session_scope(gateway.session_id_for(601)):
        await output.on_stream_event("part_delta", _delta("doomed"))

    await output.flush_due(force=True)  # must not raise


async def test_ac32_a_broken_channel_does_not_stall_a_healthy_one(wired):
    broken = FakeChannel(602, fail=True)
    healthy = FakeChannel(603)
    wired(broken, healthy, system=FakeChannel(999))

    output.route_bus_message(
        ShellLineMessage(
            line="lost", stream="stdout", session_id=gateway.session_id_for(602)
        )
    )
    output.route_bus_message(
        ShellLineMessage(
            line="delivered", stream="stdout", session_id=gateway.session_id_for(603)
        )
    )
    await output.flush_due(force=True)

    assert "delivered" in healthy.text()


async def test_ac32_a_failing_sink_does_not_fail_the_turn(wired, monkeypatch):
    """The whole run, end to end, with Discord refusing every write."""
    broken = FakeChannel(604, fail=True)
    wired(broken, system=FakeChannel(999))

    class _Result:
        output = "done anyway"

        def all_messages(self):
            return ["m"]

    class _Agent:
        def set_message_history(self, history):
            self.history = list(history)

        async def run_with_mcp(self, prompt, **_):
            await output.on_stream_event("part_delta", _delta("streamed"))
            return _Result()

    monkeypatch.setattr(gateway, "_new_agent", _Agent)
    gateway.set_authorizer(lambda message: "principal-1")
    gateway.set_outcome_sink(output.on_outcome)

    outcome = await gateway.handle_message(
        gateway.IncomingMessage(channel_id=604, author_id=1, content="hi")
    )

    assert outcome.status is gateway.TurnStatus.COMPLETED


# ===========================================================================
# AC-33 — no streaming, no problem: the final result still shows up
# ===========================================================================


async def test_ac33_final_result_is_posted_when_nothing_streamed(wired):
    channel = FakeChannel(701)
    wired(channel, system=FakeChannel(999))

    class _Result:
        output = "the whole answer, unstreamed"

    await output.on_outcome(
        gateway.TurnOutcome(
            status=gateway.TurnStatus.COMPLETED,
            session_id=gateway.session_id_for(701),
            principal="p",
            result=_Result(),
        )
    )
    await output.flush_due(force=True)

    assert "the whole answer, unstreamed" in channel.text()


async def test_ac33_streamed_result_is_not_posted_twice(wired):
    channel = FakeChannel(702)
    wired(channel, system=FakeChannel(999))
    sid = gateway.session_id_for(702)

    from code_puppy.plugins.cp_discord import concurrency

    class _Result:
        output = "hello world"

    with concurrency.session_scope(sid):
        await output.on_stream_event("part_delta", _delta("hello world"))

    await output.on_outcome(
        gateway.TurnOutcome(
            status=gateway.TurnStatus.COMPLETED,
            session_id=sid,
            principal="p",
            result=_Result(),
        )
    )
    await output.flush_due(force=True)

    assert channel.text().count("hello world") == 1


async def test_ac33_the_next_turn_can_fall_back_again(wired):
    """The streamed-flag is per turn; leaking it would mute the next turn."""
    channel = FakeChannel(703)
    wired(channel, system=FakeChannel(999))
    sid = gateway.session_id_for(703)

    from code_puppy.plugins.cp_discord import concurrency

    class _Result:
        def __init__(self, text: str) -> None:
            self.output = text

    with concurrency.session_scope(sid):
        await output.on_stream_event("part_delta", _delta("turn one"))
    await output.on_outcome(
        gateway.TurnOutcome(
            status=gateway.TurnStatus.COMPLETED,
            session_id=sid,
            principal="p",
            result=_Result("turn one"),
        )
    )
    await output.on_outcome(
        gateway.TurnOutcome(
            status=gateway.TurnStatus.COMPLETED,
            session_id=sid,
            principal="p",
            result=_Result("turn two"),
        )
    )
    await output.flush_due(force=True)

    assert "turn two" in channel.text()


async def test_ac33_failed_and_cancelled_turns_are_visible(wired):
    channel = FakeChannel(704)
    wired(channel, system=FakeChannel(999))
    sid = gateway.session_id_for(704)

    await output.on_outcome(
        gateway.TurnOutcome(
            status=gateway.TurnStatus.FAILED,
            session_id=sid,
            principal="p",
            detail="model exploded",
        )
    )
    await output.on_outcome(
        gateway.TurnOutcome(
            status=gateway.TurnStatus.CANCELLED, session_id=sid, principal="p"
        )
    )
    await output.flush_due(force=True)

    body = channel.text()
    assert "model exploded" in body
    assert "cancel" in body.lower()


async def test_ac33_denied_turn_is_audited_in_the_system_channel_only(wired):
    """A refused sender gets silence in-channel (L3/R2) but leaves a trace."""
    channel = FakeChannel(705)
    system = FakeChannel(999)
    wired(channel, system=system)

    await output.on_outcome(
        gateway.TurnOutcome(
            status=gateway.TurnStatus.DENIED,
            session_id=gateway.session_id_for(705),
            detail="sender is not authorized",
        )
    )
    await output.flush_due(force=True)

    assert "not authorized" in system.text()
    assert channel.text() == ""


# ===========================================================================
# Tool activity + lifecycle
# ===========================================================================


async def test_tool_calls_are_announced_in_the_channel(wired):
    channel = FakeChannel(801)
    wired(channel, system=FakeChannel(999))

    from code_puppy.plugins.cp_discord import concurrency

    with concurrency.session_scope(gateway.session_id_for(801)):
        assert await output.on_pre_tool_call("read_file", {"path": "x.py"}) is None
        await output.on_post_tool_call("read_file", {"path": "x.py"}, "ok", 12.0)

    await output.flush_due(force=True)

    body = channel.text()
    assert "read_file" in body


async def test_thinking_deltas_are_not_forwarded(wired):
    channel = FakeChannel(802)
    wired(channel, system=FakeChannel(999))

    class _ThinkingDelta:
        content_delta = "secret ruminations"

    from code_puppy.plugins.cp_discord import concurrency

    with concurrency.session_scope(gateway.session_id_for(802)):
        await output.on_stream_event(
            "part_delta",
            {"index": 0, "delta_type": "ThinkingPartDelta", "delta": _ThinkingDelta()},
        )
    await output.flush_due(force=True)

    assert "secret ruminations" not in channel.text()


async def test_part_start_content_is_forwarded(wired):
    """pydantic-ai often front-loads the first token into part_start."""
    channel = FakeChannel(803)
    wired(channel, system=FakeChannel(999))

    from code_puppy.plugins.cp_discord import concurrency

    with concurrency.session_scope(gateway.session_id_for(803)):
        await output.on_stream_event("part_start", _start("opening words"))
        await output.on_stream_event("part_delta", _delta(" and more"))
    await output.flush_due(force=True)

    assert "opening words and more" in channel.text()


async def test_install_is_idempotent_and_uninstall_unregisters(wired):
    channel = FakeChannel(804)
    wired(channel, system=FakeChannel(999))

    from code_puppy import callbacks

    output.install(system_channel_id=999, start_tasks=False)  # second call
    assert callbacks._callbacks["stream_event"].count(output.on_stream_event) == 1

    output.uninstall()
    assert output.on_stream_event not in callbacks._callbacks["stream_event"]
    assert gateway._OUTCOME_SINK is None
    assert not output.is_installed()


async def test_routing_without_an_installed_router_is_inert():
    """A late bus message after teardown must not explode."""
    output.route_bus_message(
        ShellLineMessage(line="after teardown", stream="stdout", session_id=None)
    )
    await output.flush_due(force=True)


async def test_no_client_connected_is_recorded_not_dropped(clock):
    output.install(system_channel_id=999, clock=clock, start_tasks=False)
    gateway.set_connection(None, None)

    output.route_bus_message(
        ShellLineMessage(line="nowhere to go", stream="stdout", session_id=None)
    )
    await output.flush_due(force=True)

    assert any("nowhere to go" in entry for entry in output.undelivered())
