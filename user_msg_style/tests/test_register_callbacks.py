"""Tests for the patch installation (AC-5, AC-8, AC-9, AC-10).

These exercise the real ``register_callbacks`` module but never import
``code_puppy.cli_runner`` -- importing it would run
``plugins.load_plugin_callbacks()`` at cli_runner.py:53 and pull in the
user's real plugin set. Instead a stub module is injected under the
``code_puppy.cli_runner`` name, which is exactly what the patch targets.
"""

import sys
import types

import pytest
from rich.text import Text

from user_msg_style import register_callbacks as rc


def _core_prompt_echo_text(task: str):
    """Stand-in for the real cli_runner._prompt_echo_text (cli_runner.py:603-611)."""
    return Text(f"\n> {task}", style="bold")


@pytest.fixture
def stub_cli_runner(monkeypatch):
    """Inject a stub ``code_puppy.cli_runner`` and yield it."""
    import code_puppy

    stub = types.ModuleType("code_puppy.cli_runner")
    stub._prompt_echo_text = _core_prompt_echo_text
    monkeypatch.setitem(sys.modules, "code_puppy.cli_runner", stub)
    monkeypatch.setattr(code_puppy, "cli_runner", stub, raising=False)
    return stub


@pytest.fixture(autouse=True)
def silence_emitters(monkeypatch):
    """Capture emitted messages instead of writing to the real message queue."""
    captured = {"error": [], "warning": []}
    monkeypatch.setattr(rc, "_emit_error", lambda msg: captured["error"].append(msg))
    monkeypatch.setattr(
        rc, "_emit_warning", lambda msg: captured["warning"].append(msg)
    )
    rc._reset_warning_dedupe()
    return captured


@pytest.fixture(autouse=True)
def no_prompt_color(monkeypatch):
    """Default: no ambient prompt color (isolates tests from the real config)."""
    monkeypatch.setattr(rc, "_prompt_color", lambda: None)


def _config(**values):
    """Build a fake ``config.get_value`` returning ``values``."""
    return lambda key: values.get(key)


# --------------------------------------------------------------------------
# AC-8: installing twice patches once
# --------------------------------------------------------------------------


def test_ac8_install_is_idempotent(stub_cli_runner, monkeypatch):
    monkeypatch.setattr(rc, "_get_value", _config())

    rc._install_patch()
    after_first = stub_cli_runner._prompt_echo_text
    rc._install_patch()

    assert stub_cli_runner._prompt_echo_text is after_first
    assert getattr(stub_cli_runner, rc._PATCH_ATTR) is _core_prompt_echo_text


def test_ac8_original_is_stashed_and_still_callable(stub_cli_runner, monkeypatch):
    monkeypatch.setattr(rc, "_get_value", _config())

    rc._install_patch()
    original = getattr(stub_cli_runner, rc._PATCH_ATTR)

    assert original is _core_prompt_echo_text
    assert original("hi").plain == "\n> hi"


def test_ac8_startup_twice_does_not_double_patch(stub_cli_runner, monkeypatch):
    monkeypatch.setattr(rc, "_get_value", _config())

    rc._on_startup()
    after_first = stub_cli_runner._prompt_echo_text
    rc._on_startup()

    assert stub_cli_runner._prompt_echo_text is after_first


# --------------------------------------------------------------------------
# AC-1 / AC-10: the patched function is a drop-in for both call sites
# --------------------------------------------------------------------------


def test_ac1_patched_output_matches_original_when_unconfigured(
    stub_cli_runner, monkeypatch
):
    monkeypatch.setattr(rc, "_get_value", _config())
    rc._install_patch()

    patched = stub_cli_runner._prompt_echo_text("hello")
    reference = _core_prompt_echo_text("hello")

    assert patched.plain == reference.plain
    assert str(patched.style) == str(reference.style)


def test_ac10_same_function_serves_typed_and_queued_input(stub_cli_runner, monkeypatch):
    """Both cli_runner call sites (:866 and :878) resolve the module global."""
    monkeypatch.setattr(rc, "_get_value", _config(user_msg_color="cyan"))
    rc._install_patch()

    typed = stub_cli_runner._prompt_echo_text("typed")
    queued = stub_cli_runner._prompt_echo_text("queued")

    assert str(typed.style) == str(queued.style) == "bold cyan"


# --------------------------------------------------------------------------
# AC-5: config is read PER RENDER, so /set works without a restart
# --------------------------------------------------------------------------


def test_ac5_config_is_read_on_every_render(stub_cli_runner, monkeypatch):
    values = {"user_msg_color": "cyan"}
    monkeypatch.setattr(rc, "_get_value", lambda key: values.get(key))
    rc._install_patch()

    assert str(stub_cli_runner._prompt_echo_text("a").style) == "bold cyan"

    values["user_msg_color"] = "magenta"
    values["user_msg_style"] = "dim"

    assert str(stub_cli_runner._prompt_echo_text("b").style) == "dim magenta"


def test_ac5_prompt_color_is_resolved_per_render(stub_cli_runner, monkeypatch):
    monkeypatch.setattr(rc, "_get_value", _config())
    colors = ["#111111", "#222222"]
    monkeypatch.setattr(rc, "_prompt_color", lambda: colors.pop(0))
    rc._install_patch()

    assert str(stub_cli_runner._prompt_echo_text("a").style) == "bold #111111"
    assert str(stub_cli_runner._prompt_echo_text("b").style) == "bold #222222"


# --------------------------------------------------------------------------
# AC-6 / AC-9: nothing in this plugin may ever crash the REPL
# --------------------------------------------------------------------------


def test_ac9_missing_target_leaves_module_untouched(monkeypatch, silence_emitters):
    """A core refactor that removes _prompt_echo_text must not crash startup."""
    import code_puppy

    stub = types.ModuleType("code_puppy.cli_runner")  # no _prompt_echo_text
    monkeypatch.setitem(sys.modules, "code_puppy.cli_runner", stub)
    monkeypatch.setattr(code_puppy, "cli_runner", stub, raising=False)

    rc._on_startup()  # must not raise

    assert not hasattr(stub, "_prompt_echo_text")
    assert not hasattr(stub, rc._PATCH_ATTR)
    assert len(silence_emitters["error"]) == 1
    assert "user_msg_style" in silence_emitters["error"][0]


def test_ac9_import_failure_is_reported_not_raised(monkeypatch, silence_emitters):
    def boom():
        raise ImportError("no cli_runner here")

    monkeypatch.setattr(rc, "_import_cli_runner", boom)

    rc._on_startup()  # must not raise

    assert len(silence_emitters["error"]) == 1


def test_ac9_render_failure_falls_back_to_the_original(stub_cli_runner, monkeypatch):
    def boom(key):
        raise RuntimeError("config exploded")

    monkeypatch.setattr(rc, "_get_value", boom)
    rc._install_patch()

    result = stub_cli_runner._prompt_echo_text("hi")

    assert result.plain == "\n> hi"
    assert str(result.style) == "bold"


def test_ac6_bad_config_warns_once_not_every_render(
    stub_cli_runner, monkeypatch, silence_emitters
):
    monkeypatch.setattr(rc, "_get_value", _config(user_msg_color="notacolor"))
    rc._install_patch()

    for _ in range(3):
        assert str(stub_cli_runner._prompt_echo_text("hi").style) == "bold"

    assert len(silence_emitters["warning"]) == 1
    assert "notacolor" in silence_emitters["warning"][0]


def test_ac6_a_failing_warning_emitter_never_breaks_the_render(
    stub_cli_runner, monkeypatch
):
    monkeypatch.setattr(rc, "_get_value", _config(user_msg_color="notacolor"))

    def boom(_msg):
        raise RuntimeError("message queue down")

    monkeypatch.setattr(rc, "_emit_warning", boom)
    rc._install_patch()

    assert str(stub_cli_runner._prompt_echo_text("hi").style) == "bold"


# --------------------------------------------------------------------------
# R1-1: the patch must not pin the core signature. A TypeError at call
# binding happens BEFORE the body's try and escapes the REPL loop
# (cli_runner.py:903-914 catches only KeyboardInterrupt/CancelledError/EOF).
# --------------------------------------------------------------------------


def _future_core_prompt_echo_text(task: str, width: int = 80):
    """A plausible future core signature: one added parameter."""
    return Text(f"\n> {task}|{width}", style="bold")


@pytest.fixture
def future_cli_runner(monkeypatch):
    """Stub whose _prompt_echo_text takes MORE arguments than today's."""
    import code_puppy

    stub = types.ModuleType("code_puppy.cli_runner")
    stub._prompt_echo_text = _future_core_prompt_echo_text
    monkeypatch.setitem(sys.modules, "code_puppy.cli_runner", stub)
    monkeypatch.setattr(code_puppy, "cli_runner", stub, raising=False)
    return stub


def test_r1_1_extra_positional_arg_does_not_raise(future_cli_runner, monkeypatch):
    monkeypatch.setattr(rc, "_get_value", _config(user_msg_color="cyan"))
    rc._install_patch()

    result = future_cli_runner._prompt_echo_text("hi", 120)

    assert result.plain == "\n> hi|120"


def test_r1_1_extra_keyword_arg_does_not_raise(future_cli_runner, monkeypatch):
    monkeypatch.setattr(rc, "_get_value", _config(user_msg_color="cyan"))
    rc._install_patch()

    result = future_cli_runner._prompt_echo_text("hi", width=120)

    assert result.plain == "\n> hi|120"


def test_r1_1_todays_call_shape_is_still_styled(future_cli_runner, monkeypatch):
    """An added parameter must not switch the plugin off for normal calls."""
    monkeypatch.setattr(rc, "_get_value", _config(user_msg_color="cyan"))
    rc._install_patch()

    assert str(future_cli_runner._prompt_echo_text("hi").style) == "bold cyan"


def test_r1_1_keyword_task_is_styled(stub_cli_runner, monkeypatch):
    """Both core call sites pass positionally today, but keyword must work."""
    monkeypatch.setattr(rc, "_get_value", _config(user_msg_color="cyan"))
    rc._install_patch()

    result = stub_cli_runner._prompt_echo_text(task="hi")

    assert result.plain == "\n> hi"
    assert str(result.style) == "bold cyan"


def test_r1_1_unknown_call_shape_is_not_warned_about(
    future_cli_runner, monkeypatch, silence_emitters
):
    """Delegating is normal operation, not a config problem worth a warning."""
    monkeypatch.setattr(rc, "_get_value", _config())
    rc._install_patch()

    future_cli_runner._prompt_echo_text("hi", 120)

    assert silence_emitters["warning"] == []
    assert silence_emitters["error"] == []


# --------------------------------------------------------------------------
# R1-2: /plugins disable must take effect immediately. The core only filters
# CALLBACK dispatch (callbacks.py:228-243); an installed patch survives it,
# so the gate lives inside the patched function.
# --------------------------------------------------------------------------


@pytest.fixture
def disabled_plugins(monkeypatch):
    """Control the real core disabled-plugin set (not the plugin's own gate).

    Patching ``get_disabled_plugins`` keeps the REAL ``is_plugin_disabled``
    in the loop, so a wrong plugin name in the plugin would fail the test.
    """
    from code_puppy.plugins import config as plugin_config

    names = set()
    monkeypatch.setattr(plugin_config, "get_disabled_plugins", lambda: set(names))
    return names


def test_r1_2_plugin_name_matches_the_folder_the_loader_derives_it_from():
    """The loader uses the directory name (``plugins/__init__.py:135``).

    ``/plugins disable`` writes THAT name, so a mismatch would leave the
    gate permanently closed-open with no other visible symptom.
    """
    from pathlib import Path

    assert rc.PLUGIN_NAME == Path(rc.__file__).resolve().parent.name


def test_r1_2_disabling_restores_the_core_output_without_a_restart(
    stub_cli_runner, monkeypatch, disabled_plugins
):
    monkeypatch.setattr(rc, "_get_value", _config(user_msg_color="cyan"))
    rc._install_patch()

    assert str(stub_cli_runner._prompt_echo_text("hi").style) == "bold cyan"

    disabled_plugins.add("user_msg_style")

    result = stub_cli_runner._prompt_echo_text("hi")
    reference = _core_prompt_echo_text("hi")
    assert result.plain == reference.plain
    assert str(result.style) == str(reference.style)


def test_r1_2_re_enabling_takes_effect_without_a_restart(
    stub_cli_runner, monkeypatch, disabled_plugins
):
    monkeypatch.setattr(rc, "_get_value", _config(user_msg_color="cyan"))
    rc._install_patch()
    disabled_plugins.add("user_msg_style")

    assert str(stub_cli_runner._prompt_echo_text("hi").style) == "bold"

    disabled_plugins.discard("user_msg_style")

    assert str(stub_cli_runner._prompt_echo_text("hi").style) == "bold cyan"


def test_r1_2_another_disabled_plugin_does_not_gate_this_one(
    stub_cli_runner, monkeypatch, disabled_plugins
):
    monkeypatch.setattr(rc, "_get_value", _config(user_msg_color="cyan"))
    rc._install_patch()
    disabled_plugins.add("some_other_plugin")

    assert str(stub_cli_runner._prompt_echo_text("hi").style) == "bold cyan"


def test_r1_2_an_unanswerable_gate_never_breaks_the_render(
    stub_cli_runner, monkeypatch
):
    """If the config layer explodes, keep rendering instead of crashing."""
    from code_puppy.plugins import config as plugin_config

    def boom():
        raise RuntimeError("puppy.cfg is unreadable")

    monkeypatch.setattr(plugin_config, "get_disabled_plugins", boom)
    monkeypatch.setattr(rc, "_get_value", _config(user_msg_color="cyan"))
    rc._install_patch()

    assert str(stub_cli_runner._prompt_echo_text("hi").style) == "bold cyan"


# --------------------------------------------------------------------------
# R1-4: the one line that makes the plugin exist at runtime
# --------------------------------------------------------------------------


def test_r1_4_importing_the_module_registers_the_startup_hook():
    """Without this the plugin can be silently switched off by a bad merge."""
    from code_puppy.callbacks import get_callbacks

    assert rc._on_startup in get_callbacks("startup", include_disabled=True)
