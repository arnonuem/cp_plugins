"""Regression tests for review round 8 — five blockers, four warnings.

Every test here exists because a defect walked past the 211 tests that were
already green.  Each one is written to FAIL against the pre-fix code, so the
test itself is the evidence that the hole was real:

* **S1** — ``universal_constructor`` reached ``Path.write_text`` and
  ``executor.submit`` without touching either approval seam;
* **S2** — a second talker rebound the channel's principal mid-run, so gates
  opened by the first talker's still-running turn carried the wrong requester;
* **P1** — ``flush_due`` was re-entrant and posted the same part twice;
* **P3** — ``sync_from_config`` had no production caller, so a fresh install
  refused every message with no supported way to grant access;
* **S3/S4/S5/P6** — mention escaping, gate-text escaping, database file mode,
  and one strict session-id parser shared by both halves of the plugin.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import stat
import sys
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


def _ui():
    """The Discord-presentation half of L4 (P2's split)."""
    from code_puppy.plugins.cp_discord import approvals_ui

    return approvals_ui


def _session_ids():
    """The single strict INV-1 parser (P6)."""
    from code_puppy.plugins.cp_discord import session_ids

    return session_ids


ALICE = "alice"
BOB = "bob"
CHANNEL_A = 4242
CHANNEL_B = 5353
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
        #: Every ``send`` kwarg set, so a missing ``allowed_mentions`` is visible.
        self.send_kwargs: List[Dict[str, Any]] = []

    async def send(self, content: str = None, **kwargs: Any):
        self.send_kwargs.append(dict(kwargs))
        message = FakeMessage(self, content or "", kwargs.get("view"))
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
    output.uninstall()
    gateway.reset_state()
    bindings.forget_initialized_paths()


@pytest.fixture
async def client():
    fake = FakeClient(CHANNEL_A, CHANNEL_B)
    gateway.set_connection(fake, asyncio.get_running_loop())
    return fake


def _approver(external_id: str, principal: str) -> None:
    bindings.bind(gateway.SESSION_PREFIX, external_id, principal)
    bindings.grant(principal, bindings.Role.TALKER)
    bindings.grant(principal, bindings.Role.APPROVER)


def _own(channel_id: int, principal: str) -> str:
    session_id = gateway.session_id_for(channel_id)
    authz.bind_session_principal(session_id, principal)
    return session_id


async def _wait_for_gate(channel: FakeChannel, index: int = 0) -> FakeMessage:
    for _ in range(400):
        if len(channel.sent) > index:
            return channel.sent[index]
        await asyncio.sleep(0.005)
    raise AssertionError(f"no gate posted in channel {channel.id}")


async def _click(message: FakeMessage, label: str, user_id: str) -> None:
    button = next(c for c in message.view.children if c.label == label)
    await button.callback(FakeInteraction(user_id))


# =========================================================================== #
# BLOCKER 1 (S1) — universal_constructor must not reach either seam
# =========================================================================== #


async def test_s1_universal_constructor_is_blocked_outright():
    """It writes model-chosen Python and executes it past BOTH gates.

    ``universal_constructor.py`` calls ``Path.write_text`` directly and runs
    the result through ``executor.submit`` — no ``on_file_permission``, no
    shell runner.  Blocked rather than gated: a gate carrying arbitrary Python
    is not something a human can audit from a chat message.
    """
    approvals.install()

    for action in ("create", "update", "call"):
        result = await approvals.on_pre_tool_call(
            "universal_constructor", {"action": action, "python_code": "import os"}
        )
        assert result is not None, f"action={action} was not blocked"
        assert result["blocked"] is True
        assert "[BLOCKED]" in result["error_message"]


async def test_s1_read_only_universal_constructor_actions_are_blocked_too():
    """``list``/``info`` ride the same tool, so they take the same answer.

    Allow-listing them would put an action string — model-chosen, unvalidated
    at this seam — in charge of whether code execution is gated.
    """
    approvals.install()

    for action in ("list", "info"):
        result = await approvals.on_pre_tool_call(
            "universal_constructor", {"action": action}
        )
        assert result is not None and result["blocked"] is True


async def test_s1_block_message_names_the_tool_so_the_model_can_react():
    approvals.install()
    result = await approvals.on_pre_tool_call("universal_constructor", {})

    text = result["error_message"].lower()
    assert "discord" in text
    assert "universal_constructor" in text


async def test_s1_ordinary_tools_are_still_allowed():
    """The block must be surgical — a blanket refusal is not a fix."""
    approvals.install()

    assert await approvals.on_pre_tool_call("read_file", {"file_path": "x"}) is None
    assert (
        await approvals.on_pre_tool_call("invoke_agent", {"agent_name": "qa"}) is None
    )


# =========================================================================== #
# BLOCKER 2 (S2) — the principal belongs to the TURN, not to the channel
# =========================================================================== #


def test_s2_rebinding_a_session_with_a_run_in_flight_is_refused():
    """R1: while Alice's turn runs, Bob must not become the channel's owner.

    If he does, every gate Alice's still-running turn opens afterwards carries
    ``requested_by_principal=bob`` — Bob may then release Alice's commands.
    """
    session_id = gateway.session_id_for(CHANNEL_A)
    authz.bind_session_principal(session_id, ALICE)

    with authz.session_turn(session_id):
        with pytest.raises(authz.AuthzError):
            authz.bind_session_principal(session_id, BOB)

        assert authz.session_principal(session_id) == ALICE


def test_s2_a_gate_opened_mid_run_still_belongs_to_the_original_principal():
    session_id = gateway.session_id_for(CHANNEL_A)
    authz.bind_session_principal(session_id, ALICE)

    with authz.session_turn(session_id):
        try:
            authz.bind_session_principal(session_id, BOB)
        except authz.AuthzError:
            pass
        gate = authz.open_gate(session_id, title="Shell Command")

    assert gate.requested_by_principal == ALICE


def test_s2_rebinding_is_allowed_once_the_turn_has_ended():
    """The channel is not frozen — only a run in flight is protected."""
    session_id = gateway.session_id_for(CHANNEL_A)
    authz.bind_session_principal(session_id, ALICE)

    with authz.session_turn(session_id):
        pass

    authz.bind_session_principal(session_id, BOB)
    assert authz.session_principal(session_id) == BOB


def test_s2_rebinding_the_same_principal_mid_run_is_a_no_op_not_an_error():
    """Alice sending a second message must not raise; she already owns it."""
    session_id = gateway.session_id_for(CHANNEL_A)
    authz.bind_session_principal(session_id, ALICE)

    with authz.session_turn(session_id):
        authz.bind_session_principal(session_id, ALICE)  # must not raise

    assert authz.session_principal(session_id) == ALICE


async def test_s2_gateway_takes_ownership_inside_the_channel_lock(monkeypatch):
    """The ordering bug itself: ownership was bound BEFORE the channel lock.

    Two talkers write while one turn is running.  Two things must hold, and
    only taking ownership INSIDE the lock gives both:

    * Alice's running turn keeps the channel — otherwise the gates it opens
      afterwards name Bob (R1);
    * Bob's message still gets served, after Alice's turn, in his own name.
      Refusing the rebind from outside the lock would satisfy the first point
      by silently DROPPING Bob's message instead of queueing it — fail-closed,
      but wrong: any talker could then deny another talker's messages.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    owners_seen: List[Optional[str]] = []

    class SlowAgent:
        def get_message_history(self) -> List[Any]:
            return []

        def set_message_history(self, history: List[Any]) -> None:
            return None

        async def run_with_mcp(self, prompt: str, **_: Any):
            started.set()
            await release.wait()
            owners_seen.append(
                authz.session_principal(gateway.session_id_for(CHANNEL_A))
            )
            return type("R", (), {"all_messages": lambda self: [], "output": "ok"})()

    monkeypatch.setattr(gateway, "_new_agent", SlowAgent)
    bindings.bind(gateway.SESSION_PREFIX, ALICE_DISCORD_ID, ALICE)
    bindings.grant(ALICE, bindings.Role.TALKER)
    bindings.bind(gateway.SESSION_PREFIX, BOB_DISCORD_ID, BOB)
    bindings.grant(BOB, bindings.Role.TALKER)
    gateway.set_authorizer(gateway.authz_authorizer)

    first = asyncio.ensure_future(
        gateway.handle_message(
            gateway.IncomingMessage(CHANNEL_A, int(ALICE_DISCORD_ID), "hi")
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)

    second = asyncio.ensure_future(
        gateway.handle_message(
            gateway.IncomingMessage(CHANNEL_A, int(BOB_DISCORD_ID), "steal")
        )
    )
    await asyncio.sleep(0.05)  # give Bob's message every chance to rebind

    # Alice's run is still in flight; the channel must still be hers.
    assert authz.session_principal(gateway.session_id_for(CHANNEL_A)) == ALICE

    release.set()
    first_outcome = await asyncio.wait_for(first, timeout=5)
    second_outcome = await asyncio.wait_for(second, timeout=5)

    assert owners_seen[0] == ALICE, "Alice's run saw a foreign owner"
    assert first_outcome.principal == ALICE

    # Bob queued behind the lock and was then served in his own name.
    assert second_outcome.status is gateway.TurnStatus.COMPLETED, (
        "Bob's message was dropped instead of queued"
    )
    assert second_outcome.principal == BOB
    assert owners_seen[1] == BOB, "Bob's own run did not own the channel"


async def test_s9_turn_teardown_releases_l3_session_ownership(monkeypatch):
    """S9: L1 and L4 state was released, L3's was not.

    ``gateway.py`` states its own rule — a principal left behind lets a later
    run inherit an owner nobody authorized.
    """

    class Agent:
        def get_message_history(self) -> List[Any]:
            return []

        def set_message_history(self, history: List[Any]) -> None:
            return None

        async def run_with_mcp(self, prompt: str, **_: Any):
            return type("R", (), {"all_messages": lambda self: [], "output": "ok"})()

    monkeypatch.setattr(gateway, "_new_agent", Agent)
    bindings.bind(gateway.SESSION_PREFIX, ALICE_DISCORD_ID, ALICE)
    bindings.grant(ALICE, bindings.Role.TALKER)
    gateway.set_authorizer(gateway.authz_authorizer)

    await gateway.handle_message(
        gateway.IncomingMessage(CHANNEL_A, int(ALICE_DISCORD_ID), "hi")
    )

    assert authz.session_principal(gateway.session_id_for(CHANNEL_A)) is None


# =========================================================================== #
# BLOCKER 3 (P1) — flush_due must not post the same part twice
# =========================================================================== #


class SlowMessage:
    def __init__(self, channel: "SlowChannel", content: str) -> None:
        self.channel = channel
        self.content = content

    async def edit(self, content: str = None, **_: Any) -> None:
        if content is not None:
            self.content = content
            self.channel.edited.append(content)


class SlowChannel:
    """A channel whose ``send`` blocks until the test lets it finish."""

    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: List[str] = []
        self.edited: List[str] = []
        self.in_send = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, content: str, **_: Any):
        self.in_send.set()
        await self.release.wait()
        self.sent.append(content)
        return SlowMessage(self, content)

    def visible(self) -> str:
        """What the channel currently shows, sends and edits combined."""
        return "\n".join(self.sent + self.edited)


async def test_p1_two_concurrent_flushes_post_a_part_only_once():
    """The re-entrancy: a part leaves ``buffer.parts`` only AFTER the await.

    A second flush entering during that await saw the same part and sent it
    again.  Reproduced against the real wiring, not a hand-made buffer.
    """
    channel = SlowChannel(CHANNEL_A)

    class Client:
        def get_channel(self, channel_id: int):
            return channel if channel_id == CHANNEL_A else None

    gateway.set_connection(Client(), asyncio.get_running_loop())
    output.install(system_channel_id=None, start_tasks=False)

    session_id = gateway.session_id_for(CHANNEL_A)
    with concurrency.session_scope(session_id):
        await output.on_stream_event("part_delta", _text_delta("agent answer"))

    first = asyncio.ensure_future(output.flush_due(force=True))
    await asyncio.wait_for(channel.in_send.wait(), timeout=5)
    second = asyncio.ensure_future(output.flush_due(force=True))
    await asyncio.sleep(0.05)

    channel.release.set()
    await asyncio.wait_for(first, timeout=5)
    await asyncio.wait_for(second, timeout=5)

    assert channel.sent == ["agent answer"], f"duplicated: {channel.sent}"


async def test_p1_a_second_flush_still_delivers_text_queued_afterwards():
    """The lock must serialise, not swallow: later text still gets out."""
    channel = SlowChannel(CHANNEL_A)

    class Client:
        def get_channel(self, channel_id: int):
            return channel if channel_id == CHANNEL_A else None

    gateway.set_connection(Client(), asyncio.get_running_loop())
    output.install(system_channel_id=None, start_tasks=False)

    session_id = gateway.session_id_for(CHANNEL_A)
    with concurrency.session_scope(session_id):
        await output.on_stream_event("part_delta", _text_delta("first"))

    first = asyncio.ensure_future(output.flush_due(force=True))
    await asyncio.wait_for(channel.in_send.wait(), timeout=5)
    channel.release.set()
    await asyncio.wait_for(first, timeout=5)

    with concurrency.session_scope(session_id):
        await output.on_stream_event("part_delta", _text_delta(" second"))
    await output.flush_due(force=True)

    # Streamed text extends the SAME message, so the follow-up arrives as an
    # edit rather than a second send -- either way the channel must see it.
    assert "second" in channel.visible()


def _text_delta(content: str) -> dict:
    class _TextPartDelta:
        def __init__(self, text: str) -> None:
            self.content_delta = text

    return {"index": 0, "delta_type": "TextPartDelta", "delta": _TextPartDelta(content)}


# =========================================================================== #
# BLOCKER 4 (P2) — approvals.py under the 600-line cap, split on a real seam
# =========================================================================== #


def test_p2_no_plugin_source_file_exceeds_the_600_line_cap():
    """AGENTS.md rule 3 is a hard project rule, not a style preference."""
    from pathlib import Path

    plugin_dir = Path(approvals.__file__).parent
    oversized = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in plugin_dir.glob("*.py")
        if len(path.read_text(encoding="utf-8").splitlines()) > 600
    }
    assert not oversized, f"over the 600-line cap: {oversized}"


def test_p2_the_discord_ui_half_lives_in_its_own_module():
    """The split is a cohesion seam: presentation vs. policy."""
    for name in ("build_view", "on_click", "finish_message", "gate_text"):
        assert hasattr(_ui(), name), f"approvals_ui is missing {name}"


# =========================================================================== #
# BLOCKER 5 (P3) — the config lists must actually reach the bindings database
# =========================================================================== #


async def test_p3_boot_syncs_the_configured_lists_before_serving(monkeypatch):
    """Without this, a fresh install refuses every message from everyone.

    INV-3 is fail-closed, so an empty database is a bot nobody can use — and
    ``allow_from``/``approvers`` in puppy.cfg are the specified way in.
    """
    monkeypatch.setattr(
        register_callbacks,
        "_read_identity_lists",
        lambda: (["discord:1111=alice"], ["discord:1111=alice"]),
    )
    monkeypatch.setattr(concurrency, "install", lambda: None)
    monkeypatch.setattr(concurrency, "uninstall", lambda: None)
    monkeypatch.setattr(concurrency, "selftest", lambda: (True, "ok"))
    monkeypatch.setattr(approvals, "install", lambda: None)
    monkeypatch.setattr(approvals, "uninstall", lambda: None)
    monkeypatch.setattr(output, "install", lambda **_: None)
    monkeypatch.setattr(output, "uninstall", lambda: None)
    # The system channel is a boot requirement of its own (round 9, C3); this
    # test is about the identity sync, so satisfy it and keep the subject.
    monkeypatch.setattr(output, "resolve_system_channel_id", lambda: 7777)

    async def _fake_gateway(*_: Any, **__: Any) -> int:
        return 0

    monkeypatch.setattr(gateway, "run_gateway", _fake_gateway)

    assert await register_callbacks._serve(object(), "token") == 0

    assert bindings.resolve_principal(gateway.SESSION_PREFIX, "1111") == ALICE
    assert bindings.has_role(ALICE, bindings.Role.TALKER) is True
    assert bindings.has_role(ALICE, bindings.Role.APPROVER) is True


async def test_p3_boot_refuses_to_serve_when_both_lists_are_empty(monkeypatch, capsys):
    """A bot nobody can talk to must say so instead of silently refusing all."""
    monkeypatch.setattr(register_callbacks, "_read_identity_lists", lambda: ([], []))
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
    assert "allow_from" in capsys.readouterr().err


async def test_p3_boot_refuses_when_the_lists_are_malformed(monkeypatch, capsys):
    """A typo in puppy.cfg must fail loudly, not half-configure the bot."""
    monkeypatch.setattr(
        register_callbacks, "_read_identity_lists", lambda: (["not-an-identity"], [])
    )
    monkeypatch.setattr(concurrency, "install", lambda: None)
    monkeypatch.setattr(concurrency, "uninstall", lambda: None)
    monkeypatch.setattr(concurrency, "selftest", lambda: (True, "ok"))

    async def _fake_gateway(*_: Any, **__: Any) -> int:
        raise AssertionError("must not serve with a broken identity list")

    monkeypatch.setattr(gateway, "run_gateway", _fake_gateway)

    assert await register_callbacks._serve(object(), "token") == 1
    assert capsys.readouterr().err.strip()


def test_p3_identity_lists_are_read_from_the_configured_keys(monkeypatch):
    values = {
        register_callbacks.ALLOW_FROM_CONFIG_KEY: "discord:1=alice, discord:2=bob",
        register_callbacks.APPROVERS_CONFIG_KEY: "discord:1=alice",
    }
    monkeypatch.setattr(
        "code_puppy.config.get_value", lambda key: values.get(key), raising=False
    )
    monkeypatch.delenv(register_callbacks.ALLOW_FROM_ENV_VAR, raising=False)
    monkeypatch.delenv(register_callbacks.APPROVERS_ENV_VAR, raising=False)

    allow_from, approvers = register_callbacks._read_identity_lists()

    assert allow_from == ["discord:1=alice", "discord:2=bob"]
    assert approvers == ["discord:1=alice"]


def test_p3_environment_overrides_the_config_file(monkeypatch):
    monkeypatch.setenv(register_callbacks.ALLOW_FROM_ENV_VAR, "discord:9=zoe")
    monkeypatch.setenv(register_callbacks.APPROVERS_ENV_VAR, "")
    monkeypatch.setattr(
        "code_puppy.config.get_value", lambda key: "discord:1=alice", raising=False
    )

    allow_from, approvers = register_callbacks._read_identity_lists()

    assert allow_from == ["discord:9=zoe"]
    assert approvers == []


# =========================================================================== #
# WARNING S3/P4 — allowed_mentions
# =========================================================================== #


def test_s3_the_shared_mention_policy_pings_nobody():
    """One policy, used by the client, by gates and by output."""
    pytest.importorskip("discord")
    mentions = gateway.allowed_mentions()

    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False


def test_s3_the_client_is_built_with_mentions_suppressed():
    """py-cord leaves ``allowed_mentions`` at ``None`` -> Discord's permissive
    default applies, and an ``@everyone`` inside a repo file would ping the
    whole server once the agent echoes it.
    """
    discord = pytest.importorskip("discord")

    built: Dict[str, Any] = {}

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            built.update(kwargs)

        user = None

        def event(self, func):
            return func

        async def start(self, token: str) -> None:
            return None

        def is_closed(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    module = type(
        "M",
        (),
        {
            "Intents": discord.Intents,
            "Client": _Client,
            "AllowedMentions": discord.AllowedMentions,
            "LoginFailure": discord.LoginFailure,
        },
    )()

    assert asyncio.run(gateway.run_gateway(module, "token")) == 0

    mentions = built.get("allowed_mentions")
    assert mentions is not None, "the client suppresses no mentions at all"
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False


async def test_s3_gate_messages_pass_allowed_mentions_explicitly(client):
    """Belt and braces: the gate carries its own suppression.

    The client default is process-wide state another frontend could change;
    the message that quotes an attacker-controlled command must not depend on
    it.
    """
    pytest.importorskip("discord")
    _approver(ALICE_DISCORD_ID, ALICE)
    approvals.install()

    with concurrency.session_scope(gateway.session_id_for(CHANNEL_A)):
        _own(CHANNEL_A, ALICE)
        hook = asyncio.ensure_future(
            approvals.on_run_shell_command(None, "echo @everyone", None, 60)
        )
        message = await _wait_for_gate(client.channels[CHANNEL_A])
        await _click(message, _ui().DENY_LABEL, ALICE_DISCORD_ID)
        await hook

    kwargs = client.channels[CHANNEL_A].send_kwargs[0]
    assert kwargs.get("allowed_mentions") is not None


async def test_s3_agent_output_is_posted_with_mentions_suppressed():
    """Agent output, shell stdout and diffs are forwarded verbatim."""
    channel = FakeChannel(CHANNEL_A)

    class Client:
        def get_channel(self, channel_id: int):
            return channel if channel_id == CHANNEL_A else None

    gateway.set_connection(Client(), asyncio.get_running_loop())
    output.install(system_channel_id=None, start_tasks=False)

    with concurrency.session_scope(gateway.session_id_for(CHANNEL_A)):
        await output.on_stream_event("part_delta", _text_delta("hello @everyone"))
    await output.flush_due(force=True)

    assert channel.send_kwargs, "nothing was sent"
    assert channel.send_kwargs[0].get("allowed_mentions") is not None


# =========================================================================== #
# WARNING S4 — the gate text must not be forgeable
# =========================================================================== #


def test_s4_a_backtick_in_the_command_cannot_break_out_of_the_code_span():
    """The rendered gate is what the human bases the decision on.

    ``echo x` **APPROVED** `` would otherwise close the span and inject bold
    text into the very message that asks for approval.
    """
    rendered = _ui().inline_code("echo x` **safe** `")

    # CommonMark closes a span at the first run of backticks of EQUAL length,
    # so the fence must be strictly longer than anything inside it -- that is
    # what makes the payload uncloseable.
    fence = rendered[: len(rendered) - len(rendered.lstrip("`"))]
    assert fence, "nothing fences the command"
    assert rendered.endswith(fence)
    body = rendered[len(fence) : -len(fence)]
    assert "echo x" in body
    assert _longest_backtick_run(body) < len(fence), (
        f"fence {fence!r} does not outrank the backticks in {body!r}"
    )
    # And the padding keeps a leading/trailing backtick from merging with it.
    assert body.startswith(" ") and body.endswith(" ")


def _longest_backtick_run(text: str) -> int:
    longest = run = 0
    for character in text:
        run = run + 1 if character == "`" else 0
        longest = max(longest, run)
    return longest


def test_s4_newlines_do_not_split_the_command_display():
    rendered = _ui().inline_code("echo one\nrm -rf /")

    assert "\n" not in rendered


def test_s4_an_ordinary_command_is_still_readable():
    assert "uv run pytest" in _ui().inline_code("uv run pytest")


async def test_s4_the_posted_gate_uses_the_escaped_rendering(client):
    pytest.importorskip("discord")
    _approver(ALICE_DISCORD_ID, ALICE)
    approvals.install()

    hostile = "ls` **APPROVED by admin** `"
    with concurrency.session_scope(gateway.session_id_for(CHANNEL_A)):
        _own(CHANNEL_A, ALICE)
        hook = asyncio.ensure_future(
            approvals.on_run_shell_command(None, hostile, None, 60)
        )
        message = await _wait_for_gate(client.channels[CHANNEL_A])
        await _click(message, _ui().DENY_LABEL, ALICE_DISCORD_ID)
        await hook

    assert message.posted.count("`") % 2 == 0


# =========================================================================== #
# WARNING S5 — the authorization database is not world-readable
# =========================================================================== #


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_s5_the_database_directory_is_private():
    bindings.bind(gateway.SESSION_PREFIX, "1", ALICE)

    mode = stat.S_IMODE(os.stat(bindings.db_path().parent).st_mode)
    assert mode == 0o700, f"directory mode is {oct(mode)}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_s5_the_database_file_is_private():
    """Write access to this file IS a complete authorization bypass."""
    bindings.bind(gateway.SESSION_PREFIX, "1", ALICE)

    mode = stat.S_IMODE(os.stat(bindings.db_path()).st_mode)
    assert mode & 0o077 == 0, f"file mode is {oct(mode)}"


def test_s5_the_private_modes_are_actually_applied(monkeypatch):
    """Cross-platform proof: the hardening runs, even where modes are inert.

    On Windows ``os.chmod`` cannot express 0o700, so asserting the resulting
    ``st_mode`` there would test the operating system rather than this code.
    Recording the calls checks the code path itself on every platform.
    """
    chmodded: Dict[str, int] = {}
    real_chmod = os.chmod

    def _record(path, mode, *args, **kwargs):
        chmodded[str(path)] = mode
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", _record)

    bindings.bind(gateway.SESSION_PREFIX, "1", ALICE)

    path = bindings.db_path()
    assert chmodded.get(str(path.parent)) == 0o700, f"chmod calls: {chmodded}"
    assert chmodded.get(str(path)) == 0o600, f"chmod calls: {chmodded}"


def test_s5_tightening_the_mode_never_breaks_the_database():
    """The hardening must be transparent to normal use."""
    bindings.bind(gateway.SESSION_PREFIX, "1", ALICE)
    bindings.grant(ALICE, bindings.Role.TALKER)

    assert bindings.resolve_principal(gateway.SESSION_PREFIX, "1") == ALICE
    assert bindings.has_role(ALICE, bindings.Role.TALKER) is True


# =========================================================================== #
# WARNING P6 / V-b — ONE strict session-id parser, shared by both halves
# =========================================================================== #


@pytest.mark.parametrize(
    "session_id",
    [
        " discord: +42 ",
        "discord:+42",
        "discord: 42",
        "discord:42 ",
        "discord:٤٢",  # Arabic-Indic digits: str.isdigit() accepts these
        "discord:４２",  # fullwidth digits
        "discord:",
        "discord",
        "Discord:42",
        "acp:42",
        "Shell Command",
        "",
        None,
        42,
    ],
)
def test_p6_the_strict_parser_rejects_everything_that_is_not_inv1(session_id):
    assert _session_ids().channel_id_of(session_id) is None


def test_p6_a_well_formed_session_id_parses():
    assert _session_ids().channel_id_of("discord:4242") == 4242


@pytest.mark.parametrize(
    "session_id", [" discord: +42 ", "discord:٤٢", "discord:+42", "Shell Command"]
)
def test_p6_both_halves_agree_on_every_rejected_form(session_id):
    """The bug was disagreement: the output router used ``int()``, the
    approval bridge ``isdigit()``.  ``" discord: +42 "`` was routed by one and
    refused by the other.
    """
    assert approvals._channel_id_of(session_id) is None
    assert output._channel_id_for(session_id) is None


def test_p6_both_halves_agree_on_an_accepted_form():
    assert approvals._channel_id_of("discord:4242") == 4242
    assert output._channel_id_for("discord:4242") == 4242


def test_p6_both_halves_call_the_one_parser():
    """A shared parser that both sides re-implement is not a shared parser."""
    assert approvals._channel_id_of is _session_ids().channel_id_of
    assert output._channel_id_for is _session_ids().channel_id_of


def test_p6_unicode_digits_never_become_a_channel():
    """V-b: ``str.isdigit()`` is unicode-aware; ``int()`` accepts these too."""
    assert _session_ids().channel_id_of("discord:٤٢") is None
    assert "٤٢".isdigit() is True  # the reason the naive check passed


# =========================================================================== #
# The CLI surface must keep working through all of the above
# =========================================================================== #


def test_the_discord_flag_still_registers():
    parser = argparse.ArgumentParser()
    register_callbacks.register_cli_args(parser)
    assert parser.parse_args(["--discord"]).discord is True
