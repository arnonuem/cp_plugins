"""End-to-end wiring: the hooks, the guard, and the callback contract.

A module that exists but is not wired into ``register_callbacks.py`` is an
orphan. These tests drive the REGISTERED handlers -- exactly as
``callbacks.py`` would -- rather than the reporter's methods, so a wiring
mistake (a hook bound to the wrong phase, an identity read from the wrong
argument position) cannot pass.

Covers AC-1 (the inert half), AC-23, and the hook-to-effect table.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from conftest import Wire, spin

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"), reason="wmux is a Windows named-pipe protocol"
)


@pytest.fixture
def plugin(wmux_env, monkeypatch):
    """Reload the plugin inside an active pane, with the wire stubbed."""
    from code_puppy import callbacks

    from wmux import client as cl

    wire = Wire()
    monkeypatch.setattr(cl, "_transport", wire)
    # Keep the real phase KEYS -- register_callback validates against them,
    # so an invented phase name is a hard error rather than a silent no-op.
    monkeypatch.setattr(
        callbacks, "_callbacks", {phase: [] for phase in callbacks._callbacks}
    )

    import wmux.register_callbacks as rc

    importlib.reload(rc)
    rc.wire = wire
    try:
        yield rc
    finally:
        rc._client.release_and_close(timeout_s=0.5)


def handlers(rc, phase):
    from code_puppy import callbacks

    return callbacks.get_callbacks(phase)


def test_active_plugin_registers_every_hook_exactly_once(plugin):
    for phase, handler in plugin._HOOKS:
        registered = handlers(plugin, phase)
        assert handler in registered, f"{phase} is not wired"
        assert registered.count(handler) == 1


def test_ac1_inactive_plugin_registers_nothing(monkeypatch):
    from code_puppy import callbacks

    monkeypatch.delenv("WMUX", raising=False)
    monkeypatch.delenv("WMUX_SURFACE_ID", raising=False)
    monkeypatch.setattr(
        callbacks, "_callbacks", {phase: [] for phase in callbacks._callbacks}
    )

    import wmux.register_callbacks as rc

    importlib.reload(rc)
    assert rc._reporter.active is False
    assert rc._client._worker is None
    assert all(not v for v in callbacks._callbacks.values())


def test_ac23_tool_hooks_return_none(plugin):
    # A dict with `blocked` would ABORT the real tool call.
    assert plugin._on_tool_start("run_shell_command", {}, None) is None
    assert plugin._on_tool_complete("run_shell_command", {}, "out", 1.0, None) is None


def test_run_identity_is_read_from_the_right_argument_of_each_hook(plugin):
    reporter = plugin._reporter
    # agent_run_start(agent_name, model_name, session_id)
    plugin._on_run_start("puppy", "gpt-5", "gid-1")
    assert set(reporter._live_runs) == {"gid-1"}
    # agent_run_end(agent_name, model_name, session_id, success, ...)
    plugin._on_run_end("puppy", "gpt-5", "gid-1", True, None, None, None)
    assert reporter._live_runs == {}
    # agent_run_cancel(group_id) -- FIRST positional, not third.
    plugin._on_run_start("puppy", "gpt-5", "gid-2")
    plugin._on_run_cancel("gid-2")
    assert reporter._live_runs == {}


def test_agent_name_is_never_mistaken_for_a_run_id(plugin):
    """``agent_run_end``'s first argument is the AGENT NAME.

    A positional fallback chain would happily track it as a run id, and the
    pane would then be one phantom run deep forever.
    """
    plugin._on_run_end("puppy", "gpt-5", None, True, None, None, None)
    assert plugin._reporter._live_runs == {}


def test_a_full_turn_reaches_the_wire_in_order(plugin):
    """idle -> working -> blocked(reason) -> working -> idle, on the wire.

    Each event is drained before the next is fired. Both lanes are
    coalescing latest-wins slots BY DESIGN, so a synchronous burst may
    legitimately collapse edges -- letting them race would make this test
    assert a timing accident rather than the state machine.
    """
    from unittest.mock import patch

    def step(fire):
        before = len(_by_method(plugin, "pane.report_agent"))
        fire()
        assert spin(
            lambda: len(_by_method(plugin, "pane.report_agent")) > before,
            timeout=5.0,
        ), "expected a state report that never arrived"

    with patch("code_puppy.config.get_current_session_name", return_value="sess-1"):
        step(plugin._on_startup)
        plugin._on_user_prompt("hello", "gid-1")
        step(lambda: plugin._on_run_start("puppy", "gpt-5", "gid-1"))
        plugin._on_tool_start("run_shell_command", {}, None)
        step(lambda: plugin._on_awaiting_user_input(True))
        step(lambda: plugin._on_awaiting_user_input(False))
        plugin._on_tool_complete("run_shell_command", {}, "ok", 1.0, None)
        step(
            lambda: plugin._on_run_end(
                "puppy", "gpt-5", "gid-1", True, None, None, None
            )
        )

    states = _by_method(plugin, "pane.report_agent")
    assert [(p["awaitingHuman"], p["runDepth"]) for p in states] == [
        (False, 0),  # startup claim
        (False, 1),  # run start
        (True, 1),  # blocked
        (False, 1),  # unblocked, still working
        (False, 0),  # idle
    ]
    assert states[2]["reason"] == "permission: run_shell_command"
    assert spin(lambda: _by_method(plugin, "pane.report_agent_session"), timeout=5.0)
    assert _by_method(plugin, "pane.report_agent_session")[0]["sessionId"] == "sess-1"


def test_shutdown_releases_and_stops_accepting_work(plugin):
    plugin._on_shutdown()
    assert "pane.release_agent" in plugin.wire.methods()
    plugin._on_run_start("puppy", "gpt-5", "gid-late")
    assert plugin.wire.methods().count("pane.release_agent") == 1
    plugin._on_shutdown()
    assert plugin.wire.methods().count("pane.release_agent") == 1


def test_no_envelope_ever_carries_a_message_field(plugin):
    plugin._on_run_start("puppy", "gpt-5", "gid-1")
    plugin._on_tool_start("read_file", {}, None)
    plugin._on_tool_complete("read_file", {}, "ok", 1.0, None)
    assert spin(lambda: _by_method(plugin, "agent.activity"), timeout=5.0)
    assert plugin.wire.sent
    assert all("message" not in e["params"] for e in plugin.wire.sent)


def _by_method(plugin, method):
    return [e["params"] for e in plugin.wire.sent if e["method"] == method]
