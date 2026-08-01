"""L2 transport — AC-12..16, AC-50, AC-55.

The transport layer owns the CLI flag, the guarded py-cord import, the
per-channel agent registry and the turn lifecycle.  Two of these tests answer
questions the spec left open (ASSUMPTION-4/-5) and therefore refuse to mock the
thing under test: AC-50 drives a REAL stdio MCP server through the REAL manager
singleton, and AC-55 cancels a REAL in-flight run task.
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import os
import sys
import types
from contextlib import suppress
from typing import Any, List, Optional

import pytest

from code_puppy.plugins.cp_discord import gateway, register_callbacks

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeResult:
    """Stands in for a pydantic-ai run result."""

    def __init__(self, output: str, messages: List[Any]) -> None:
        self.output = output
        self._messages = messages

    def all_messages(self) -> List[Any]:
        return list(self._messages)


class FakeAgent:
    """Records the history it ran with, and what was written back to it."""

    def __init__(self, name: str = "fake-agent", delay: float = 0.0) -> None:
        self.name = name
        self.delay = delay
        self._message_history: List[Any] = []
        self.prompts: List[str] = []
        self.histories_at_run: List[List[Any]] = []
        self.started = asyncio.Event()
        self.completed = False

    def get_message_history(self) -> List[Any]:
        return list(self._message_history)

    def set_message_history(self, history: List[Any]) -> None:
        self._message_history = list(history)

    async def run_with_mcp(self, prompt: str, **_: Any) -> FakeResult:
        self.prompts.append(prompt)
        self.histories_at_run.append(list(self._message_history))
        self.started.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.completed = True
        messages = list(self._message_history) + [
            f"user:{prompt}",
            f"assistant:{prompt}",
        ]
        return FakeResult(f"reply:{prompt}", messages)


class AgentFactory:
    """Hands each channel its own agent and keeps them addressable by test."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.created: List[FakeAgent] = []

    def __call__(self) -> FakeAgent:
        agent = FakeAgent(f"fake-agent-{len(self.created)}", delay=self.delay)
        self.created.append(agent)
        return agent


@pytest.fixture(autouse=True)
def _clean_gateway_state():
    """Never let a registry, authorizer or in-flight task leak between tests."""
    gateway.reset_state()
    yield
    gateway.reset_state()


@pytest.fixture
def allow_all():
    """A permissive authorizer, so transport tests aren't secretly AuthZ tests."""
    gateway.set_authorizer(lambda message: f"principal-{message.author_id}")


@pytest.fixture
def agents(monkeypatch) -> AgentFactory:
    factory = AgentFactory()
    monkeypatch.setattr(gateway, "_new_agent", factory)
    return factory


def _incoming(channel_id: int, text: str, author_id: int = 1):
    return gateway.IncomingMessage(
        channel_id=channel_id, author_id=author_id, content=text
    )


# ---------------------------------------------------------------------------
# AC-12 — the plugin stays passive without --discord
# ---------------------------------------------------------------------------


def test_ac12_without_flag_handle_cli_args_returns_none():
    assert register_callbacks.handle_cli_args(argparse.Namespace(discord=False)) is None


def test_ac12_missing_attribute_is_also_passive():
    """Another plugin's namespace must not trip us."""
    assert register_callbacks.handle_cli_args(argparse.Namespace()) is None


def test_ac12_flag_is_registered_on_the_parser():
    parser = argparse.ArgumentParser()
    register_callbacks.register_cli_args(parser)
    assert parser.parse_args([]).discord is False
    assert parser.parse_args(["--discord"]).discord is True


# ---------------------------------------------------------------------------
# AC-13 / AC-14 — the guarded import checks IDENTITY, not importability
# ---------------------------------------------------------------------------


def test_ac13_discord_py_is_rejected_as_the_wrong_library(monkeypatch):
    """py-cord and discord.py both provide the module name ``discord``.

    A bare ``import discord`` therefore succeeds with either one and proves
    nothing.  Only py-cord exposes ``ApplicationContext``.
    """
    impostor = types.ModuleType("discord")
    impostor.__version__ = "2.4.0"  # a genuine discord.py version
    monkeypatch.setitem(sys.modules, "discord", impostor)

    module, error = register_callbacks.load_pycord()

    assert module is None
    assert error is not None and "discord.py" in error


def test_ac13_missing_package_is_reported_not_raised(monkeypatch):
    real_import = builtins.__import__

    def _fail(name, *args, **kwargs):
        if name == "discord":
            raise ImportError("No module named 'discord'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "discord", raising=False)
    monkeypatch.setattr(builtins, "__import__", _fail)

    module, error = register_callbacks.load_pycord()

    assert module is None
    assert error is not None


def test_ac13_real_pycord_is_accepted():
    module, error = register_callbacks.load_pycord()
    assert error is None
    assert module is not None and hasattr(module, "ApplicationContext")


def test_ac14_error_names_uv_sync_and_never_pip_install(monkeypatch):
    """``pip`` is absent from this .venv — advising it would be a dead end."""
    monkeypatch.setitem(sys.modules, "discord", types.ModuleType("discord"))

    _module, error = register_callbacks.load_pycord()

    assert "uv sync --extra discord" in error
    assert "pip install" not in error


def test_ac14_boot_stays_inactive_and_surfaces_the_hint(monkeypatch, capsys):
    """A wrong or missing library must not crash the CLI (AC-13 + AC-14)."""
    monkeypatch.setattr(
        register_callbacks,
        "load_pycord",
        lambda: (None, "no py-cord: uv sync --extra discord"),
    )

    result = register_callbacks.handle_cli_args(argparse.Namespace(discord=True))

    assert result == {"handled": True, "exit_code": 1}
    assert "uv sync --extra discord" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# AC-15 — one agent instance per channel; _CURRENT_AGENT untouched
# ---------------------------------------------------------------------------


async def test_ac15_each_channel_gets_its_own_agent(allow_all, agents):
    await gateway.handle_message(_incoming(111, "hi A"))
    await gateway.handle_message(_incoming(222, "hi B"))

    channel_a = gateway.get_channel_agent(111)
    channel_b = gateway.get_channel_agent(222)

    assert channel_a is not None and channel_b is not None
    assert channel_a.agent is not channel_b.agent
    assert len(agents.created) == 2


async def test_ac15_same_channel_reuses_its_agent(allow_all, agents):
    await gateway.handle_message(_incoming(111, "turn one"))
    await gateway.handle_message(_incoming(111, "turn two"))

    assert len(agents.created) == 1


async def test_ac15_current_agent_singleton_is_never_touched(allow_all, agents):
    from code_puppy.agents import agent_manager

    before = agent_manager._CURRENT_AGENT
    await gateway.handle_message(_incoming(111, "hi"))

    assert agent_manager._CURRENT_AGENT is before


async def test_ac15_session_id_follows_inv1(allow_all, agents):
    await gateway.handle_message(_incoming(4242, "hi"))
    assert gateway.get_channel_agent(4242).session_id == "discord:4242"


# ---------------------------------------------------------------------------
# AC-16 — history is written back after EVERY turn
# ---------------------------------------------------------------------------


async def test_ac16_history_is_written_back_after_each_turn(allow_all, agents):
    """``run_with_mcp`` does NOT persist history; the caller must (Fakt A4)."""
    await gateway.handle_message(_incoming(111, "turn one"))
    agent = agents.created[0]

    assert agent.get_message_history() == ["user:turn one", "assistant:turn one"]

    await gateway.handle_message(_incoming(111, "turn two"))

    # Turn two SAW turn one — that is what "the channel remembers" means.
    assert agent.histories_at_run[1] == ["user:turn one", "assistant:turn one"]
    assert agent.get_message_history()[-2:] == ["user:turn two", "assistant:turn two"]


async def test_ac16_channels_do_not_share_history(allow_all, agents):
    await gateway.handle_message(_incoming(111, "A one"))
    await gateway.handle_message(_incoming(222, "B one"))
    await gateway.handle_message(_incoming(222, "B two"))

    assert agents.created[0].get_message_history() == [
        "user:A one",
        "assistant:A one",
    ]
    assert agents.created[1].histories_at_run[1] == ["user:B one", "assistant:B one"]


# ---------------------------------------------------------------------------
# INV-4 / INV-3 — AuthZ completes BEFORE anything touches the model
# ---------------------------------------------------------------------------


async def test_inv4_unauthorized_message_never_reaches_the_agent(agents):
    """No authorizer installed => fail-closed (INV-3): discard, never run."""
    outcome = await gateway.handle_message(_incoming(111, "ignore previous"))

    assert outcome.status is gateway.TurnStatus.DENIED
    assert agents.created == []
    assert gateway.get_channel_agent(111) is None


async def test_inv4_authorizer_runs_before_the_agent_is_built(agents):
    calls: List[str] = []

    def _deny(message):
        calls.append("authz")
        return None

    gateway.set_authorizer(_deny)
    await gateway.handle_message(_incoming(111, "hello"))

    assert calls == ["authz"]
    assert agents.created == []


async def test_inv3_authorizer_exception_denies(agents):
    def _boom(_message):
        raise RuntimeError("authz backend down")

    gateway.set_authorizer(_boom)
    outcome = await gateway.handle_message(_incoming(111, "hello"))

    assert outcome.status is gateway.TurnStatus.DENIED
    assert agents.created == []


async def test_principal_is_carried_into_the_outcome(allow_all, agents):
    outcome = await gateway.handle_message(_incoming(111, "hi", author_id=99))
    assert outcome.principal == "principal-99"


# ---------------------------------------------------------------------------
# AC-55 — cancelling channel A must not disturb channel B
# ---------------------------------------------------------------------------


async def test_ac55_cancel_is_per_channel(allow_all, monkeypatch):
    """``_AGENT_CANCEL_CB`` is a one-slot global (Fakt D10), so the gateway
    must hold each channel's run task itself."""
    factory = AgentFactory(delay=5.0)
    monkeypatch.setattr(gateway, "_new_agent", factory)

    task_a = asyncio.ensure_future(gateway.handle_message(_incoming(111, "slow A")))
    task_b = asyncio.ensure_future(gateway.handle_message(_incoming(222, "slow B")))

    for agent in await _await_agents(factory, 2):
        await asyncio.wait_for(agent.started.wait(), timeout=5)

    assert gateway.cancel_channel(111) is True

    outcome_a = await asyncio.wait_for(task_a, timeout=5)
    assert outcome_a.status is gateway.TurnStatus.CANCELLED

    # Channel B is untouched: it was never cancelled and still completes.
    factory.created[1].delay = 0
    outcome_b = await asyncio.wait_for(task_b, timeout=10)
    assert outcome_b.status is gateway.TurnStatus.COMPLETED
    assert factory.created[1].completed is True


async def _await_agents(factory: AgentFactory, count: int) -> List[FakeAgent]:
    for _ in range(500):
        if len(factory.created) >= count:
            return factory.created[:count]
        await asyncio.sleep(0.01)
    raise AssertionError(f"only {len(factory.created)} agents were created")


async def test_ac55_cancel_of_an_idle_channel_is_a_no_op(allow_all, agents):
    assert gateway.cancel_channel(111) is False
    await gateway.handle_message(_incoming(111, "hi"))
    assert gateway.cancel_channel(111) is False


async def test_ac55_cancelled_turn_does_not_corrupt_history(allow_all, monkeypatch):
    """A cancelled run must not overwrite the channel's memory."""
    factory = AgentFactory()
    monkeypatch.setattr(gateway, "_new_agent", factory)

    await gateway.handle_message(_incoming(111, "turn one"))
    agent = factory.created[0]
    agent.delay = 5.0
    agent.started.clear()

    task = asyncio.ensure_future(gateway.handle_message(_incoming(111, "turn two")))
    await asyncio.wait_for(agent.started.wait(), timeout=5)
    assert gateway.cancel_channel(111) is True
    outcome = await asyncio.wait_for(task, timeout=5)

    assert outcome.status is gateway.TurnStatus.CANCELLED
    assert agent.get_message_history() == ["user:turn one", "assistant:turn one"]


async def test_run_returning_none_is_reported_as_failed(allow_all, monkeypatch):
    """A cancelled ``run_with_mcp`` returns None instead of raising (measured).

    Writing that None into the history would silently wipe the channel.
    """

    class NoneAgent(FakeAgent):
        async def run_with_mcp(self, prompt: str, **_: Any):
            self.started.set()
            return None

    agent = NoneAgent()
    monkeypatch.setattr(gateway, "_new_agent", lambda: agent)

    outcome = await gateway.handle_message(_incoming(111, "hi"))

    assert outcome.status is gateway.TurnStatus.FAILED
    assert agent.get_message_history() == []


async def test_agent_exception_is_reported_not_raised(allow_all, monkeypatch):
    class BoomAgent(FakeAgent):
        async def run_with_mcp(self, prompt: str, **_: Any):
            raise RuntimeError("model exploded")

    monkeypatch.setattr(gateway, "_new_agent", BoomAgent)

    outcome = await gateway.handle_message(_incoming(111, "hi"))

    assert outcome.status is gateway.TurnStatus.FAILED
    assert "model exploded" in (outcome.detail or "")


# ---------------------------------------------------------------------------
# Seams handed to W4 (approvals) and W5 (output)
# ---------------------------------------------------------------------------


def test_connection_seam_is_empty_until_connected():
    assert gateway.get_client() is None
    assert gateway.get_loop() is None


async def test_connection_seam_publishes_client_and_loop():
    """L4 blocks on this loop from an executor thread; L5 needs the client."""
    sentinel = object()
    loop = asyncio.get_running_loop()
    gateway.set_connection(sentinel, loop)

    assert gateway.get_client() is sentinel
    assert gateway.get_loop() is loop

    gateway.reset_state()
    assert gateway.get_client() is None
    assert gateway.get_loop() is None


async def test_outcome_sink_receives_every_turn(allow_all, agents):
    seen: List[gateway.TurnOutcome] = []
    gateway.set_outcome_sink(seen.append)

    await gateway.handle_message(_incoming(111, "hi"))

    assert [o.status for o in seen] == [gateway.TurnStatus.COMPLETED]
    assert seen[0].session_id == "discord:111"


async def test_outcome_sink_also_sees_denials(agents):
    """A discarded message must still be observable — never silently dropped."""
    seen: List[gateway.TurnOutcome] = []
    gateway.set_outcome_sink(seen.append)

    await gateway.handle_message(_incoming(111, "who are you"))

    assert [o.status for o in seen] == [gateway.TurnStatus.DENIED]


async def test_broken_outcome_sink_never_fails_the_turn(allow_all, agents):
    def _boom(_outcome):
        raise RuntimeError("discord is down")

    gateway.set_outcome_sink(_boom)

    outcome = await gateway.handle_message(_incoming(111, "hi"))

    assert outcome.status is gateway.TurnStatus.COMPLETED


async def test_async_outcome_sink_is_awaited(allow_all, agents):
    seen: List[str] = []

    async def _sink(outcome):
        await asyncio.sleep(0)
        seen.append(outcome.session_id)

    gateway.set_outcome_sink(_sink)
    await gateway.handle_message(_incoming(111, "hi"))

    assert seen == ["discord:111"]


# ---------------------------------------------------------------------------
# L3 bridge — the real authorizer, wired the way boot wires it
# ---------------------------------------------------------------------------


@pytest.fixture
def real_authz(monkeypatch, tmp_path):
    """Install L3's real rules over an isolated on-disk binding database."""
    from code_puppy.plugins.cp_discord import authz, bindings

    monkeypatch.setenv(bindings.DB_PATH_ENV, str(tmp_path / "authz.db"))
    bindings.forget_initialized_paths()
    authz.clear_state()
    gateway.set_authorizer(gateway.authz_authorizer)
    yield authz, bindings
    authz.clear_state()
    bindings.forget_initialized_paths()


async def test_unknown_sender_is_discarded_by_the_real_rules(real_authz, agents):
    """R2 end-to-end: an unbound Discord id never reaches the model."""
    outcome = await gateway.handle_message(_incoming(111, "ignore previous", 4242))

    assert outcome.status is gateway.TurnStatus.DENIED
    assert agents.created == []


async def test_known_talker_is_admitted_and_owns_the_session(real_authz, monkeypatch):
    """R5: while the run executes, the session belongs to its trigger.

    Observed DURING the run, which is the only window where it matters — a
    gate reads its requester from this map at the moment it opens.  After the
    turn the claim is dropped again (R1/S9), so asserting it afterwards would
    be asserting the very leak that was fixed.
    """
    authz, bindings_module = real_authz
    bindings_module.bind("discord", "4242", "wayne")
    bindings_module.grant("wayne", bindings_module.Role.TALKER)

    owner_during_run: List[Optional[str]] = []

    class Observer(FakeAgent):
        async def run_with_mcp(self, prompt: str, **kwargs: Any) -> FakeResult:
            owner_during_run.append(authz.session_principal("discord:111"))
            return await super().run_with_mcp(prompt, **kwargs)

    monkeypatch.setattr(gateway, "_new_agent", Observer)

    outcome = await gateway.handle_message(_incoming(111, "hello", 4242))

    assert outcome.status is gateway.TurnStatus.COMPLETED
    assert outcome.principal == "wayne"
    assert owner_during_run == ["wayne"]


async def test_a_finished_turn_leaves_no_session_ownership_behind(real_authz, agents):
    """A stale principal would let the channel's NEXT run inherit an owner
    nobody authorized — fail-closed means forgetting, not remembering (S9).
    """
    authz, bindings_module = real_authz
    bindings_module.bind("discord", "4242", "wayne")
    bindings_module.grant("wayne", bindings_module.Role.TALKER)

    await gateway.handle_message(_incoming(111, "hello", 4242))

    assert authz.session_principal("discord:111") is None
    assert authz.session_is_running("discord:111") is False


async def test_shutdown_drops_session_ownership(real_authz, agents):
    """Shutdown must not leave a principal (or its gates) behind.

    Belt and braces next to the per-turn release above: a run torn down
    abnormally never gets its ``finally``, so shutdown clears the map too.
    """
    authz, bindings_module = real_authz
    bindings_module.bind("discord", "4242", "wayne")
    bindings_module.grant("wayne", bindings_module.Role.TALKER)
    await gateway.handle_message(_incoming(111, "hello", 4242))
    authz.bind_session_principal("discord:111", "wayne")  # simulate a leak

    gateway.reset_state()

    assert authz.session_principal("discord:111") is None


async def test_talker_role_is_required_not_merely_a_binding(real_authz, agents):
    """A known identity without the TALKER role is still refused."""
    _authz, bindings_module = real_authz
    bindings_module.bind("discord", "4242", "wayne")

    outcome = await gateway.handle_message(_incoming(111, "hello", 4242))

    assert outcome.status is gateway.TurnStatus.DENIED
    assert agents.created == []


# ---------------------------------------------------------------------------
# AC-50 — the MCP singleton must carry N parallel runs
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_mcp(monkeypatch, tmp_path):
    """Point the MCP registry at a tmp dir BEFORE the manager is constructed.

    ``ServerRegistry.register`` persists immediately (registry.py:89), so
    without this the test would write a bogus server into the developer's real
    ``~/.code_puppy/mcp_registry.json``.
    """
    from code_puppy.mcp_ import manager as manager_module
    from code_puppy.mcp_ import registry as registry_module

    real_registry_init = registry_module.ServerRegistry.__init__

    def _tmp_init(self, storage_path=None):
        real_registry_init(self, storage_path=str(tmp_path / "mcp_registry.json"))

    monkeypatch.setattr(registry_module.ServerRegistry, "__init__", _tmp_init)
    monkeypatch.setattr("code_puppy.config.load_mcp_server_configs", dict)
    monkeypatch.setattr(manager_module, "_manager_instance", None)

    yield manager_module

    monkeypatch.setattr(manager_module, "_manager_instance", None)


async def test_ac50_mcp_singleton_carries_parallel_runs(isolated_mcp):
    """ASSUMPTION-5: does ONE shared MCP server instance survive N runs?

    Deliberately unmocked — a mocked MCP layer would answer a different
    question than the one blocking requirement (1).
    """
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from code_puppy.mcp_.managed_server import ServerConfig

    manager = isolated_mcp.get_mcp_manager()
    server_script = os.path.join(os.path.dirname(__file__), "_mcp_probe_server.py")
    server_id = manager.register_server(
        ServerConfig(
            id="",
            name="cp-discord-ac50",
            type="stdio",
            config={"command": sys.executable, "args": [server_script]},
        )
    )
    assert await manager.start_server(server_id) is True

    try:
        servers = manager.get_servers_for_agent()
        assert len(servers) == 1
        shared = servers[0]
        # Every agent really is handed the SAME object — that is what makes
        # this a singleton question at all.
        assert manager.get_servers_for_agent()[0] is shared

        def _model_for(tag: str) -> FunctionModel:
            def _fn(messages, info) -> ModelResponse:
                parts = [p for m in messages for p in getattr(m, "parts", [])]
                returned = [
                    getattr(p, "content", None)
                    for p in parts
                    if type(p).__name__ == "ToolReturnPart"
                ]
                if returned:
                    return ModelResponse(parts=[TextPart(str(returned[-1]))])
                names = [t.name for t in info.function_tools]
                echo = next((n for n in names if n.endswith("echo")), None)
                assert echo, f"probe server exposed no echo tool: {names}"
                return ModelResponse(parts=[ToolCallPart(echo, {"text": tag})])

            return FunctionModel(_fn)

        async def _one(index: int) -> str:
            tag = f"chan-{index}"
            agent = Agent(_model_for(tag), toolsets=[shared])
            result = await agent.run(f"say {tag}")
            return str(result.output)

        outputs = await asyncio.gather(*(_one(i) for i in range(3)))

        # No cross-talk: each run got back exactly ITS OWN tag.
        assert outputs == ["echo:chan-0", "echo:chan-1", "echo:chan-2"]
    finally:
        with suppress(Exception):
            await asyncio.wait_for(manager.stop_server(server_id), timeout=30)
