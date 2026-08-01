"""Regression tests for review round 9 — three blockers, five warnings.

Every test here exists because a defect walked past the 264 tests that were
already green.  Each one is written to FAIL against the pre-fix code, so the
test itself is the evidence that the hole was real:

* **C1** — ``CancelledError`` was booked as a timeout, so ``/cancel`` on an
  open gate did not stop the run: the gate returned "denied", the run
  continued calling tools, and the channel was told the gate had EXPIRED;
* **Q2** — ``_new_agent`` was patched out by every single test, so a mutant
  swapping ``load_agent`` for ``get_current_agent`` kept all four AC-15 tests
  green while every channel silently shared ONE conversation;
* **Q3** — the shell hook had no top-level guard, and the callback dispatcher
  swallows exceptions into ``None``, which ``command_runner`` reads as
  "allowed": any raise inside the hook was an ungated command;
* **Q4** — P1 (the cross-channel stall) was only ever tested at the lock
  level with a *sleeping* backend that always returned, never with a gate
  that really hangs unanswered while a second channel runs a full turn;
* **Q6** — AC-9 proved ``selftest()`` returns ``False``, but nothing proved
  the boot actually REFUSES on it (P6's second half);
* **C2** — ``concurrency`` was the only module without a logger, so its 13
  silent handlers made a systematic fallback undiagnosable;
* **C3** — a missing system channel silently dropped the rejection audit
  trail into a 200-entry deque nobody reads.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import pytest

from code_puppy.plugins.cp_discord import (
    approvals,
    authz,
    bindings,
    concurrency,
    gateway,
    output,
    register_callbacks,
)

ALICE = "alice"
BOB = "bob"
CHANNEL_A = 4242
CHANNEL_B = 5353
SYSTEM_CHANNEL = 7777
ALICE_DISCORD_ID = "1111"
BOB_DISCORD_ID = "2222"


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class FakeMessage:
    def __init__(self, channel: "FakeChannel", content: str, view: Any) -> None:
        self.channel = channel
        self.content = content
        self.posted = content
        self.view = view
        self.edits: List[str] = []

    async def edit(self, content: str = None, view: Any = None, **_: Any) -> None:
        if content is not None:
            self.content = content
            self.edits.append(content)
        if view is not None:
            self.view = view


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: List[FakeMessage] = []

    async def send(self, content: str = None, view: Any = None, **_: Any):
        message = FakeMessage(self, content or "", view)
        self.sent.append(message)
        return message


class FakeClient:
    def __init__(self, *channel_ids: int) -> None:
        self.channels = {cid: FakeChannel(cid) for cid in channel_ids}

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        channel = self.channels.get(channel_id)
        if channel is None:
            raise RuntimeError(f"unknown channel {channel_id}")
        return channel


class FakeResponse:
    async def defer(self, *_: Any, **__: Any) -> None:
        return None

    async def send_message(self, content: str = None, **_: Any) -> None:
        return None


class FakeInteraction:
    def __init__(self, user_id: str) -> None:
        self.response = FakeResponse()
        self.user = type("U", (), {"id": int(user_id)})()


class FakeResult:
    def __init__(self, messages: List[Any]) -> None:
        self.output = "done"
        self._messages = messages

    def all_messages(self) -> List[Any]:
        return list(self._messages)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(bindings.DB_PATH_ENV, str(tmp_path / "db" / "authz.db"))
    bindings.forget_initialized_paths()
    authz.clear_state()
    approvals.reset_state()
    yield
    approvals.uninstall()
    approvals.reset_state()
    authz.clear_state()
    concurrency.uninstall()
    output.uninstall()
    gateway.reset_state()
    bindings.forget_initialized_paths()


@pytest.fixture
async def client():
    fake = FakeClient(CHANNEL_A, CHANNEL_B, SYSTEM_CHANNEL)
    gateway.set_connection(fake, asyncio.get_running_loop())
    return fake


def _approver(external_id: str, principal: str) -> None:
    bindings.bind(gateway.SESSION_PREFIX, external_id, principal)
    bindings.grant(principal, bindings.Role.TALKER)
    bindings.grant(principal, bindings.Role.APPROVER)


def _incoming(channel_id: int, text: str, author_id: int = 1111):
    return gateway.IncomingMessage(
        channel_id=channel_id, author_id=author_id, content=text
    )


async def _wait_for_gate(channel: FakeChannel, index: int = 0) -> FakeMessage:
    for _ in range(600):
        if len(channel.sent) > index:
            return channel.sent[index]
        await asyncio.sleep(0.005)
    raise AssertionError(f"no gate posted in channel {channel.id}")


async def _click(message: FakeMessage, label: str, user_id: str) -> None:
    button = next(c for c in message.view.children if c.label == label)
    await button.callback(FakeInteraction(user_id))


# =========================================================================== #
# BLOCKER 1 (C1) — a cancel is a cancel, not a timeout
# =========================================================================== #


class GatedAgent:
    """Asks for a shell gate, then records whether it was allowed to go on.

    The second entry in :attr:`tool_calls` is the whole point: it can only
    appear if the gate *returned* instead of propagating the cancellation, i.e.
    if the run survived its own ``/cancel``.
    """

    def __init__(self) -> None:
        self.tool_calls: List[str] = []

    def get_message_history(self) -> List[Any]:
        return []

    def set_message_history(self, history: List[Any]) -> None:
        return None

    async def run_with_mcp(self, prompt: str, **_: Any):
        self.tool_calls.append("shell:rm -rf /")
        verdict = await approvals.on_run_shell_command(None, "rm -rf /")
        self.tool_calls.append(f"continued:{verdict is None}")
        return FakeResult(["user:" + prompt, "assistant:done"])


async def _cancel_on_open_gate(client: FakeClient, monkeypatch) -> tuple:
    """Run a turn that opens a gate, cancel it, and return (agent, outcome)."""
    _approver(ALICE_DISCORD_ID, ALICE)
    approvals.install()
    gateway.set_authorizer(lambda message: ALICE)

    agent = GatedAgent()
    monkeypatch.setattr(gateway, "_new_agent", lambda: agent)

    task = asyncio.ensure_future(gateway.handle_message(_incoming(CHANNEL_A, "go")))
    message = await _wait_for_gate(client.channels[CHANNEL_A])

    assert gateway.cancel_channel(CHANNEL_A) is True

    outcome = await asyncio.wait_for(task, timeout=10)
    return agent, outcome, message


async def test_c1_cancelling_an_open_gate_stops_the_run(client, monkeypatch):
    """``/cancel`` on an open gate must end the turn, not merely deny it.

    Pre-fix, ``CancelledError`` shared a branch with ``TimeoutError``: it was
    swallowed, the gate returned ``False``, and with no cancellation pending
    any more the run simply carried on — opening further gates and calling
    further tools, in exactly the situation (an open ``rm -rf`` gate) where the
    abort matters most.
    """
    agent, outcome, _message = await _cancel_on_open_gate(client, monkeypatch)

    assert outcome.status is gateway.TurnStatus.CANCELLED, (
        "a cancelled run must be reported as CANCELLED, not COMPLETED"
    )
    assert agent.tool_calls == ["shell:rm -rf /"], (
        f"the run continued past the cancel: {agent.tool_calls}"
    )


async def test_c1_the_channel_is_told_cancelled_not_expired(client, monkeypatch):
    """The gate message is an audit artefact; it must not state a falsehood.

    A gate that was cancelled after four seconds is not an expired one, and
    labelling it "EXPIRED — treated as DENIED" puts a wrong statement into the
    permanent channel record.
    """
    _agent, _outcome, message = await _cancel_on_open_gate(client, monkeypatch)

    assert message.edits, "the gate message was never closed out"
    final = message.edits[-1]
    assert "CANCELLED" in final, f"the outcome was not reported: {final!r}"
    assert "EXPIRED" not in final, f"a cancelled gate was booked as expired: {final!r}"


async def test_c1_a_real_timeout_is_still_reported_as_expired(client, monkeypatch):
    """Splitting the branch must not cost the timeout its own report (R3).

    The mirror image of the test above: separating the two states is only a fix
    if BOTH keep working, so this pins the branch the cancel case was taken out
    of.
    """
    _approver(ALICE_DISCORD_ID, ALICE)
    monkeypatch.setattr(approvals, "GATE_TIMEOUT_SECONDS", 0.05)
    approvals.install()

    session_id = gateway.session_id_for(CHANNEL_A)
    authz.bind_session_principal(session_id, ALICE)

    allowed = await approvals._request_approval(session_id, "Shell Command", "sleep")

    assert allowed is False
    message = client.channels[CHANNEL_A].sent[0]
    assert "EXPIRED" in message.edits[-1]


async def test_c1_cancelling_channel_a_leaves_channel_b_running(client, monkeypatch):
    """AC-55 still holds: the sharpened cancel must stay per-channel."""
    _approver(ALICE_DISCORD_ID, ALICE)
    _approver(BOB_DISCORD_ID, BOB)
    approvals.install()
    gateway.set_authorizer(lambda message: ALICE if message.author_id == 1111 else BOB)

    agents: Dict[int, GatedAgent] = {}

    def _factory() -> GatedAgent:
        agent = GatedAgent()
        agents[len(agents)] = agent
        return agent

    monkeypatch.setattr(gateway, "_new_agent", _factory)

    task_a = asyncio.ensure_future(gateway.handle_message(_incoming(CHANNEL_A, "a")))
    await _wait_for_gate(client.channels[CHANNEL_A])
    task_b = asyncio.ensure_future(
        gateway.handle_message(_incoming(CHANNEL_B, "b", author_id=2222))
    )
    message_b = await _wait_for_gate(client.channels[CHANNEL_B])

    assert gateway.cancel_channel(CHANNEL_A) is True
    outcome_a = await asyncio.wait_for(task_a, timeout=10)
    assert outcome_a.status is gateway.TurnStatus.CANCELLED

    await _click(message_b, approvals.APPROVE_LABEL, BOB_DISCORD_ID)
    outcome_b = await asyncio.wait_for(task_b, timeout=10)
    assert outcome_b.status is gateway.TurnStatus.COMPLETED


# =========================================================================== #
# BLOCKER 2 (Q2) — the ONE test that actually executes _new_agent
# =========================================================================== #


def test_q2_new_agent_really_builds_a_fresh_agent_per_call():
    """Runs the REAL ``_new_agent``; every other test replaces it.

    Eight test sites monkeypatch ``gateway._new_agent`` away, which left
    ``gateway.py:222-224`` unexecuted — including
    ``test_ac15_current_agent_singleton_is_never_touched``, which asserted the
    singleton stays untouched while the only function that would touch it was
    substituted.  A mutant swapping ``load_agent(get_current_agent_name())``
    for ``get_current_agent()`` therefore kept all four AC-15 tests green while
    every channel shared ONE conversation — requirement (1) silently dead.

    Two assertions, because the mutant breaks two different promises: it hands
    out the same object twice, and it populates the process-wide singleton.
    """
    from code_puppy.agents import agent_manager

    before = agent_manager._CURRENT_AGENT

    first = gateway._new_agent()
    second = gateway._new_agent()

    assert first is not None and second is not None
    assert first is not second, (
        "two channels received the SAME agent object — they would share one "
        "conversation (this is the get_current_agent() mutant)"
    )
    assert agent_manager._CURRENT_AGENT is before, (
        "_new_agent touched the process-wide _CURRENT_AGENT singleton"
    )


def test_q2_two_channels_really_get_two_agents_end_to_end(monkeypatch):
    """AC-15 once WITHOUT the ``agents`` fixture, so the real builder runs."""
    channel_a = gateway._channel_for(CHANNEL_A)
    channel_b = gateway._channel_for(CHANNEL_B)

    assert channel_a.agent is not channel_b.agent
    assert gateway._channel_for(CHANNEL_A).agent is channel_a.agent


# =========================================================================== #
# WARNING (Q3) — the shell hook must never raise into a fail-open dispatcher
# =========================================================================== #


async def test_q3_a_raising_shell_hook_blocks_instead_of_letting_it_run():
    """``_trigger_callbacks`` turns any exception into ``None`` = ALLOWED.

    ``callbacks.py:321-326`` catches every exception and appends ``None``, and
    ``command_runner.py:1099-1112`` treats anything that is not a ``blocked``
    dict as permission to execute.  So an unhandled raise anywhere in this hook
    is not a crash — it is an *ungated command*.  The hook therefore owns its
    own failure and answers "blocked" itself.
    """
    approvals.install()

    def _boom() -> Optional[str]:
        raise RuntimeError("session resolution exploded")

    original = approvals._current_session
    approvals._current_session = _boom
    try:
        result = await approvals.on_run_shell_command(None, "rm -rf /")
    finally:
        approvals._current_session = original

    assert result is not None, "a raising hook let the command through"
    assert result["blocked"] is True


async def test_q3_a_raising_gate_blocks_instead_of_letting_it_run(monkeypatch):
    """The same guard, one layer deeper: a broken gate is still a refusal."""
    approvals.install()

    async def _boom(*_: Any, **__: Any) -> bool:
        raise RuntimeError("the gate machinery exploded")

    monkeypatch.setattr(approvals, "_request_approval", _boom)
    monkeypatch.setattr(approvals, "_current_session", lambda: "discord:4242")

    result = await approvals.on_run_shell_command(None, "rm -rf /")

    assert result is not None and result["blocked"] is True


async def test_q3_a_raising_file_hook_denies_instead_of_abstaining(monkeypatch):
    """The same fail-open class on the neighbouring seam.

    ``_permission_denied`` denies only on an explicit ``False``
    (``file_modifications.py:42-48``), so the ``None`` the dispatcher appends
    for a raising callback means "no opinion" = the write proceeds.  And while
    yolo is on this callback is the ONLY gate on the file path, because the
    core short-circuits before the backend
    (``file_permission_handler/register_callbacks.py:466``).
    """
    approvals.install()
    monkeypatch.setattr(authz, "file_gate_callback_active", lambda: True)

    async def _boom(*_: Any, **__: Any) -> bool:
        raise RuntimeError("the gate machinery exploded")

    monkeypatch.setattr(approvals, "_request_approval", _boom)
    monkeypatch.setattr(approvals, "_current_session", lambda: "discord:4242")

    result = await approvals.on_file_permission(None, "/etc/passwd", "write")

    assert result is False, "a raising file hook abstained instead of denying"


async def test_q3_the_file_hook_still_abstains_while_yolo_is_off(monkeypatch):
    """AC-52 must survive the guard: abstaining is still the normal case."""
    approvals.install()
    monkeypatch.setattr(authz, "file_gate_callback_active", lambda: False)

    assert await approvals.on_file_permission(None, "/etc/passwd", "write") is None


async def test_q3_the_guard_still_lets_an_approved_command_through(client):
    """A blanket refusal is not a fix — the allow path must survive."""
    _approver(ALICE_DISCORD_ID, ALICE)
    approvals.install()
    session_id = gateway.session_id_for(CHANNEL_A)
    authz.bind_session_principal(session_id, ALICE)

    with concurrency.session_scope(session_id):
        hook = asyncio.ensure_future(approvals.on_run_shell_command(None, "ls"))
        message = await _wait_for_gate(client.channels[CHANNEL_A])
        await _click(message, approvals.APPROVE_LABEL, ALICE_DISCORD_ID)

    assert await asyncio.wait_for(hook, timeout=10) is None


async def test_q3_a_cancelled_shell_hook_does_not_degrade_into_a_block(
    client, monkeypatch
):
    """The Q3 guard must not re-swallow the C1 cancellation.

    ``except Exception`` would not catch ``CancelledError`` (it is a
    ``BaseException`` since 3.8) — but a careless ``except BaseException``
    would, and would reintroduce C1 through the back door.
    """
    _approver(ALICE_DISCORD_ID, ALICE)
    approvals.install()
    session_id = gateway.session_id_for(CHANNEL_A)
    authz.bind_session_principal(session_id, ALICE)

    async def _call() -> Any:
        with concurrency.session_scope(session_id):
            return await approvals.on_run_shell_command(None, "rm -rf /")

    task = asyncio.ensure_future(_call())
    await _wait_for_gate(client.channels[CHANNEL_A])
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)


# =========================================================================== #
# WARNING (Q4) — P1, the cross-channel stall, composed end to end
# =========================================================================== #


async def test_q4_an_unanswered_gate_in_a_does_not_stall_a_full_turn_in_b(
    client, monkeypatch
):
    """P1 — *the* core requirement — with a gate that really hangs.

    AC-4 only ever proved the LOCK is per session, using a backend that sleeps
    and then returns.  This composes the real thing: channel A's approval is
    parked on a gate nobody clicks, and channel B then drives a complete
    ``handle_message`` turn — through the same core approval path — while A is
    still waiting.  Unpatched, ``_APPROVAL_ASYNC_LOCK`` is process-wide
    (``tools/common.py:1422``) and B would never get past it.
    """
    from code_puppy.tools import common

    concurrency.install()
    approvals.install()
    _approver(ALICE_DISCORD_ID, ALICE)
    _approver(BOB_DISCORD_ID, BOB)
    gateway.set_authorizer(lambda message: ALICE if message.author_id == 1111 else BOB)

    session_a = gateway.session_id_for(CHANNEL_A)
    authz.bind_session_principal(session_a, ALICE)

    async def _park_channel_a() -> Any:
        # Through the CORE approval path, so A really holds the approval lock.
        with concurrency.session_scope(session_a):
            return await common.get_user_approval_async(
                title="Shell Command", content="rm -rf /"
            )

    parked = asyncio.ensure_future(_park_channel_a())
    await _wait_for_gate(client.channels[CHANNEL_A])  # posted, never clicked

    class ApprovingAgent:
        def get_message_history(self) -> List[Any]:
            return []

        def set_message_history(self, history: List[Any]) -> None:
            return None

        async def run_with_mcp(self, prompt: str, **_: Any):
            await common.get_user_approval_async(title="File Operation", content="w")
            return FakeResult(["user:" + prompt])

    monkeypatch.setattr(gateway, "_new_agent", ApprovingAgent)

    turn_b = asyncio.ensure_future(
        gateway.handle_message(_incoming(CHANNEL_B, "hi", author_id=2222))
    )
    message_b = await _wait_for_gate(client.channels[CHANNEL_B])
    await _click(message_b, approvals.APPROVE_LABEL, BOB_DISCORD_ID)

    outcome_b = await asyncio.wait_for(turn_b, timeout=15)

    assert outcome_b.status is gateway.TurnStatus.COMPLETED, (
        "channel B was stalled by channel A's unanswered gate"
    )
    assert not parked.done(), "channel A's gate must still be waiting"

    # Release A's gate properly: its approval is blocking a real executor
    # thread, and abandoning it would leave that thread parked for the full
    # gate timeout, slowing every test that follows.
    await _click(
        client.channels[CHANNEL_A].sent[0], approvals.APPROVE_LABEL, ALICE_DISCORD_ID
    )
    assert await asyncio.wait_for(parked, timeout=10) == (True, None)


# =========================================================================== #
# WARNING (Q6) — P6's second half: the boot must REFUSE, not just detect
# =========================================================================== #


async def test_q6_a_failing_selftest_refuses_to_boot(monkeypatch, capsys):
    """AC-9 proves ``selftest()`` says False; P6 demands the boot then stops.

    Detecting a broken patch and serving anyway would be worse than not
    checking: the operator sees a healthy bot that mixes channels.
    """
    served = False

    async def _fake_gateway(*_: Any, **__: Any) -> int:
        nonlocal served
        served = True
        return 0

    monkeypatch.setattr(
        register_callbacks,
        "_read_identity_lists",
        lambda: ([f"discord:{ALICE_DISCORD_ID}={ALICE}"], []),
    )
    monkeypatch.setattr(concurrency, "install", lambda: None)
    monkeypatch.setattr(concurrency, "uninstall", lambda: None)
    monkeypatch.setattr(
        concurrency, "selftest", lambda: (False, "patches no longer active: B")
    )
    monkeypatch.setattr(gateway, "run_gateway", _fake_gateway)

    assert await register_callbacks._serve(object(), "token") == 1
    assert served is False, "the gateway was served with a broken adapter"
    assert "B" in capsys.readouterr().err


async def test_q6_a_failing_selftest_rolls_the_adapter_back(monkeypatch):
    """A refused boot must not leave eleven monkey-patches on the core."""
    rolled_back = False

    def _uninstall() -> None:
        nonlocal rolled_back
        rolled_back = True

    monkeypatch.setattr(
        register_callbacks,
        "_read_identity_lists",
        lambda: ([f"discord:{ALICE_DISCORD_ID}={ALICE}"], []),
    )
    monkeypatch.setattr(concurrency, "install", lambda: None)
    monkeypatch.setattr(concurrency, "uninstall", _uninstall)
    monkeypatch.setattr(concurrency, "selftest", lambda: (False, "B missing"))

    async def _fake_gateway(*_: Any, **__: Any) -> int:
        raise AssertionError("must not serve after a failed selftest")

    monkeypatch.setattr(gateway, "run_gateway", _fake_gateway)

    assert await register_callbacks._serve(object(), "token") == 1
    assert rolled_back is True


# =========================================================================== #
# IMPORTANT (C2) — the silent module gets a voice
# =========================================================================== #


def test_c2_a_failing_session_lock_is_logged_not_just_swallowed(
    installed_adapter, caplog
):
    """Patch B falling back to the core lock is a FAILURE, not a fast path.

    If ``_session_lock`` throws systematically, every approval silently queues
    on the process-wide core lock again — precisely the serialisation patch B
    exists to remove.  The symptom is "Discord feels slow"; without a log line
    there is no diagnosis at all.
    """
    from code_puppy.tools import common

    def _boom() -> Optional[str]:
        raise RuntimeError("contextvar backend exploded")

    original = concurrency._current_sid
    concurrency._current_sid = _boom
    try:
        with caplog.at_level(
            logging.DEBUG, logger="code_puppy.plugins.cp_discord.concurrency"
        ):
            lock = common._get_approval_async_lock()
    finally:
        concurrency._current_sid = original

    assert lock is not None, "the fallback to the core lock must survive"
    assert any(
        record.levelno >= logging.DEBUG and "lock" in record.getMessage().lower()
        for record in caplog.records
    ), f"the fallback was silent: {[r.getMessage() for r in caplog.records]}"


def test_c2_a_patch_target_that_cannot_be_installed_is_a_warning(monkeypatch, caplog):
    """A skipped target is not a detail — it is a hole in the adapter.

    ``install()`` isolates each target so one core rename cannot take the other
    ten down.  That isolation is right, but silence about it is not: the
    selftest then reports a missing patch with no trace of WHY.
    """

    def _broken_targets():
        return [("BOGUS", object(), "does_not_exist", lambda original: original)]

    monkeypatch.setattr(concurrency, "_patch_targets", _broken_targets)

    with caplog.at_level(
        logging.WARNING, logger="code_puppy.plugins.cp_discord.concurrency"
    ):
        concurrency.install()

    assert any(record.levelno >= logging.WARNING for record in caplog.records), (
        "a target that could not be patched was installed silently"
    )


def test_c2_the_stdlib_thread_patch_stays_silent(installed_adapter, caplog):
    """Patch G is exempt, on purpose — logging there is not a free choice.

    ``threading.Thread.start`` is process-wide stdlib: py-cord, httpx, MCP and
    every ``ThreadPoolExecutor`` run through it.  A log call in that path fires
    on threads that have nothing to do with Discord, and can re-enter the
    logging machinery from a thread that is mid-start.  Silence is the correct
    rule there, and this test pins it so a later "consistency" pass does not
    quietly make G chatty.
    """
    import threading

    caplog.clear()
    with caplog.at_level(
        logging.DEBUG, logger="code_puppy.plugins.cp_discord.concurrency"
    ):
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()

    assert caplog.records == [], (
        f"patch G logged on an unrelated thread: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


@pytest.fixture
def installed_adapter():
    concurrency.install()
    yield
    concurrency.uninstall()


# =========================================================================== #
# IMPORTANT (C3) — a missing system channel is a total, silent loss
# =========================================================================== #


async def test_c3_boot_refuses_without_a_system_channel(monkeypatch, capsys):
    """SPEC-L5 §5.3: the system channel is BINDING and never silently dropped.

    Without it, everything unattributable lands in a 200-entry deque no
    production code reads: zombie reader lines, the whole legacy queue, and —
    the reason this is a refusal rather than a warning — the audit trail of
    REJECTED senders, which ``on_outcome`` deliberately routes there.  Losing
    the record of who was turned away is a security failure, and the module
    already treats its other unusable-configuration case (no identities) as a
    hard boot error with instructions.
    """
    monkeypatch.delenv(output.SYSTEM_CHANNEL_ENV_VAR, raising=False)
    monkeypatch.setattr(output, "resolve_system_channel_id", lambda: None)
    monkeypatch.setattr(
        register_callbacks,
        "_read_identity_lists",
        lambda: ([f"discord:{ALICE_DISCORD_ID}={ALICE}"], []),
    )
    monkeypatch.setattr(concurrency, "install", lambda: None)
    monkeypatch.setattr(concurrency, "uninstall", lambda: None)
    monkeypatch.setattr(concurrency, "selftest", lambda: (True, "ok"))

    served = False

    async def _fake_gateway(*_: Any, **__: Any) -> int:
        nonlocal served
        served = True
        return 0

    monkeypatch.setattr(gateway, "run_gateway", _fake_gateway)

    assert await register_callbacks._serve(object(), "token") == 1
    assert served is False
    error = capsys.readouterr().err
    assert output.SYSTEM_CHANNEL_ENV_VAR in error, (
        "the refusal must name the setting the operator has to fix"
    )
    assert output.SYSTEM_CHANNEL_CONFIG_KEY in error


async def test_c3_boot_proceeds_once_a_system_channel_is_configured(monkeypatch):
    """The refusal must be curable by configuration alone."""
    monkeypatch.setattr(output, "resolve_system_channel_id", lambda: SYSTEM_CHANNEL)
    monkeypatch.setattr(
        register_callbacks,
        "_read_identity_lists",
        lambda: ([f"discord:{ALICE_DISCORD_ID}={ALICE}"], []),
    )
    monkeypatch.setattr(concurrency, "install", lambda: None)
    monkeypatch.setattr(concurrency, "uninstall", lambda: None)
    monkeypatch.setattr(concurrency, "selftest", lambda: (True, "ok"))
    monkeypatch.setattr(output, "install", lambda **_: None)
    monkeypatch.setattr(output, "uninstall", lambda: None)

    async def _fake_gateway(*_: Any, **__: Any) -> int:
        return 0

    monkeypatch.setattr(gateway, "run_gateway", _fake_gateway)

    assert await register_callbacks._serve(object(), "token") == 0


def test_c3_the_env_var_is_a_supported_way_to_set_it(monkeypatch):
    """The refusal message points at two settings; both must actually work."""
    monkeypatch.setenv(output.SYSTEM_CHANNEL_ENV_VAR, str(SYSTEM_CHANNEL))
    assert output.resolve_system_channel_id() == SYSTEM_CHANNEL
