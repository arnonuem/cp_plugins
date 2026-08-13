"""C6 — registration, lifecycle and configuration (W6, wave 1).

Covers AC-44, AC-58n-a, AC-60a, AC-62, AC-71a, AC-77, AC-79 and AC-83a.

AC-45n (``tests/test_authz.py`` stays green) is a regression criterion over an
existing file, not a test written here.

Every test that touches the bindings database redirects it into ``tmp_path``
via the override that ``bindings`` already exposes.  Production code never
sets that variable -- the database lives at its default location (SPEC §8,
INV-C26) -- but a test that wrote into the operator's real
``~/.code_puppy/discord/authz.db`` would revoke their roles as a side effect,
because :func:`authz.sync_from_config` reconciles in BOTH directions.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from cp_discord import authz, bindings, constants, register_callbacks, session_ids

PLUGIN_DIR = Path(register_callbacks.__file__).resolve().parent

WAYNE_ID = "123456789"
WAYNE = "wayne"
MARY_ID = "987654321"
MARY = "mary"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Own database + clean in-memory state for every test."""
    monkeypatch.setenv(bindings.DB_PATH_ENV, str(tmp_path / "authz.db"))
    bindings.forget_initialized_paths()
    authz.clear_state()
    yield
    authz.clear_state()
    bindings.forget_initialized_paths()


@pytest.fixture(autouse=True)
def _clean_activation_state():
    """Reset the module's activation state around every test."""
    register_callbacks.reset_state()
    yield
    register_callbacks.reset_state()


@pytest.fixture
def config(monkeypatch):
    """A complete, valid configuration.

    Every key is set EXPLICITLY -- including the ones a test wants absent, via
    the empty string -- so no test can accidentally read the developer's own
    ``puppy.cfg``.
    """

    def _set(**overrides: str) -> None:
        values: Dict[str, str] = {
            register_callbacks.ENABLED_ENV_VAR: "1",
            register_callbacks.TOKEN_ENV_VAR: "a-bot-token",
            register_callbacks.CHANNEL_ID_ENV_VAR: "4242",
            register_callbacks.MODE_ENV_VAR: "report",
            register_callbacks.ALLOW_FROM_ENV_VAR: f"discord:{WAYNE_ID}={WAYNE}",
            register_callbacks.APPROVERS_ENV_VAR: f"discord:{WAYNE_ID}={WAYNE}",
        }
        values.update(overrides)
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    _set()
    return _set


class _Sink:
    """Fake message sink (AC-60a: the warning is GENERATED, not posted)."""

    def __init__(self) -> None:
        self.warnings: List[str] = []
        self.errors: List[str] = []


@pytest.fixture
def sink(monkeypatch) -> _Sink:
    collected = _Sink()
    monkeypatch.setattr(register_callbacks, "_emit_warning", collected.warnings.append)
    monkeypatch.setattr(register_callbacks, "_emit_error", collected.errors.append)
    return collected


class _FakeComponent:
    """Stands in for a layer module: ``install()`` / ``uninstall()``."""

    def __init__(self, log: List[str], name: str, *, fails: bool = False) -> None:
        self._log = log
        self._name = name
        self._fails = fails

    def install(self, config: Any) -> None:
        assert isinstance(config, register_callbacks.BridgeConfig)
        if self._fails:
            raise RuntimeError(f"{self._name} refuses to install")
        self._log.append(f"install:{self._name}")

    def uninstall(self) -> None:
        self._log.append(f"uninstall:{self._name}")


def _use_fake_components(
    monkeypatch, log: List[str], failing: Optional[str] = None
) -> Dict[str, _FakeComponent]:
    """Replace the real layer modules with fakes, keeping the real order."""
    modules = {
        component.module: _FakeComponent(
            log, component.module, fails=component.module == failing
        )
        for component in register_callbacks.COMPONENTS
    }
    monkeypatch.setattr(
        register_callbacks,
        "_import_component",
        lambda component: modules[component.module],
    )
    return modules


def _pycord_available() -> bool:
    module, _error = register_callbacks.load_pycord()
    return module is not None


def _pretend_pycord_is_installed(monkeypatch) -> None:
    """py-cord is an OPTIONAL dependency; the lifecycle tests do not need it."""
    if not _pycord_available():
        monkeypatch.setattr(register_callbacks, "load_pycord", lambda: (object(), None))


# --------------------------------------------------------------------------- #
# AC-44 — session_ids.py is EXTENDED, the legacy form keeps working
# --------------------------------------------------------------------------- #


def test_ac44_legacy_form_is_still_recognised():
    assert session_ids.channel_id_of("discord:4242") == 4242
    assert session_ids.is_session_id("discord:4242") is True
    assert session_ids.session_id_for(4242) == "discord:4242"


@pytest.mark.parametrize(
    "hostile",
    [
        "discord: +42",  # whitespace and a sign
        "discord:4_2",  # int() would accept this
        "discord:\u0664\u0662",  # unicode digits: str.isdigit() accepts them
        "Shell Command",  # a positional title, not a session id
        "discord:",
        42,
        None,
    ],
)
def test_ac44_legacy_strictness_is_unchanged(hostile):
    """The fail-closed edge of the old parser must survive the extension."""
    assert session_ids.channel_id_of(hostile) is None


def test_ac44_new_prefix_is_recognised():
    session_id = session_ids.new_session_id()

    assert session_id.startswith(f"{session_ids.CP_SESSION_PREFIX}:")
    assert session_ids.nonce_of(session_id)
    assert session_ids.is_session_id(session_id) is True


def test_ac44_new_prefix_differs_from_the_legacy_one():
    """Same prefix would send a nonce through the digits check -> None."""
    assert session_ids.CP_SESSION_PREFIX != session_ids.SESSION_PREFIX


def test_ac44_minted_ids_are_unique():
    minted = {session_ids.new_session_id() for _ in range(100)}

    assert len(minted) == 100


def test_ac44_the_two_forms_do_not_bleed_into_each_other():
    assert session_ids.channel_id_of("cp_discord:deadbeef") is None
    assert session_ids.nonce_of("discord:4242") is None


@pytest.mark.parametrize(
    "hostile",
    ["cp_discord:", "cp_discord: abc", "cp_discord:ab-cd", "cp_discord:\u0664", 7],
)
def test_ac44_new_form_is_just_as_strict(hostile):
    assert session_ids.nonce_of(hostile) is None
    assert session_ids.is_session_id(hostile) is False


# --------------------------------------------------------------------------- #
# AC-83a — the shared constant exists and the sync writes UNDER it
# --------------------------------------------------------------------------- #


def test_ac83a_the_shared_constant_is_discord():
    assert constants.AUTHZ_CHANNEL == "discord"


def test_ac83a_the_sync_writes_under_the_shared_constant(config, sink, monkeypatch):
    _pretend_pycord_is_installed(monkeypatch)
    _use_fake_components(monkeypatch, [])

    register_callbacks.on_startup()

    assert bindings.resolve_principal(constants.AUTHZ_CHANNEL, WAYNE_ID) == WAYNE


def test_ac83a_the_operator_hint_uses_the_shared_constant(config, sink, monkeypatch):
    """No second ``"discord"`` literal -- not even in the message we print."""
    _pretend_pycord_is_installed(monkeypatch)
    _use_fake_components(monkeypatch, [])
    config(
        **{
            register_callbacks.ALLOW_FROM_ENV_VAR: "",
            register_callbacks.APPROVERS_ENV_VAR: "",
        }
    )

    register_callbacks.on_startup()

    assert any(
        f"{constants.AUTHZ_CHANNEL}:<user id>=<principal>" in warning
        for warning in sink.warnings
    )


# --------------------------------------------------------------------------- #
# AC-77 / AC-79 — the identities are WRITTEN, and read back from the same DB
# --------------------------------------------------------------------------- #


def test_ac77_startup_fills_the_bindings_database(config, sink, monkeypatch):
    _pretend_pycord_is_installed(monkeypatch)
    _use_fake_components(monkeypatch, [])

    assert bindings.principals_with_role(bindings.Role.TALKER) == set()

    register_callbacks.on_startup()

    assert bindings.principals_with_role(bindings.Role.TALKER) == {WAYNE}
    assert bindings.principals_with_role(bindings.Role.APPROVER) == {WAYNE}


def test_ac79_the_reader_sees_what_the_writer_wrote(config, sink, monkeypatch):
    """Writer and reader demonstrably hit the SAME database."""
    _pretend_pycord_is_installed(monkeypatch)
    _use_fake_components(monkeypatch, [])
    config(
        **{
            register_callbacks.ALLOW_FROM_ENV_VAR: f"discord:{MARY_ID}={MARY}",
            register_callbacks.APPROVERS_ENV_VAR: "",
        }
    )

    register_callbacks.on_startup()

    decision = authz.check_message(constants.AUTHZ_CHANNEL, MARY_ID)
    assert decision.allowed is True
    assert decision.principal == MARY


def test_ac79_an_unconfigured_sender_stays_refused(config, sink, monkeypatch):
    """Fail-closed is intact: the sync grants, it does not open the door."""
    _pretend_pycord_is_installed(monkeypatch)
    _use_fake_components(monkeypatch, [])

    register_callbacks.on_startup()

    decision = authz.check_message(constants.AUTHZ_CHANNEL, MARY_ID)
    assert decision.allowed is False
    assert decision.reason is authz.Reason.UNKNOWN_SENDER


def test_ac79_production_never_redirects_the_database(monkeypatch):
    """No env var for the DB path any more (SPEC §8, R11) -- default path only."""
    source = Path(register_callbacks.__file__).read_text(encoding="utf-8")

    assert bindings.DB_PATH_ENV not in source


# --------------------------------------------------------------------------- #
# AC-60a — missing configuration warns, the session keeps running
# --------------------------------------------------------------------------- #


def test_ac60a_disabled_plugin_does_nothing_at_all(config, sink, monkeypatch):
    started: List[str] = []
    _use_fake_components(monkeypatch, started)
    config(**{register_callbacks.ENABLED_ENV_VAR: "0"})

    register_callbacks.on_startup()

    assert started == []
    assert sink.warnings == []
    assert sink.errors == []
    assert register_callbacks.activation_config() is None


@pytest.mark.parametrize(
    "missing",
    [register_callbacks.TOKEN_ENV_VAR, register_callbacks.CHANNEL_ID_ENV_VAR],
)
def test_ac60a_missing_config_warns_and_stands_down(config, sink, monkeypatch, missing):
    _pretend_pycord_is_installed(monkeypatch)
    started: List[str] = []
    _use_fake_components(monkeypatch, started)
    config(**{missing: ""})

    register_callbacks.on_startup()  # must not raise -- INV-C1

    assert started == []
    assert sink.warnings, "a missing key must not fail silently (INV-C15)"
    assert sink.errors == []


def test_ac60a_missing_identities_warn_but_the_bridge_still_starts(
    config, sink, monkeypatch
):
    """INV-C15: empty identities are a WARNING, not the hard stop they used to be."""
    _pretend_pycord_is_installed(monkeypatch)
    started: List[str] = []
    _use_fake_components(monkeypatch, started)
    config(
        **{
            register_callbacks.ALLOW_FROM_ENV_VAR: "",
            register_callbacks.APPROVERS_ENV_VAR: "",
        }
    )

    register_callbacks.on_startup()

    assert started, "the broker must come up so it can post the warning (AC-60b)"
    assert sink.warnings
    assert register_callbacks.activation_warnings() == tuple(sink.warnings)


def test_ac60a_an_unusable_mode_falls_back_to_report(config, sink, monkeypatch):
    _pretend_pycord_is_installed(monkeypatch)
    _use_fake_components(monkeypatch, [])
    config(**{register_callbacks.MODE_ENV_VAR: "interpretive-dance"})

    register_callbacks.on_startup()

    assert register_callbacks.activation_config().mode == register_callbacks.MODE_REPORT
    assert sink.warnings


def test_ac60a_a_non_numeric_channel_id_is_refused_not_guessed(
    config, sink, monkeypatch
):
    _pretend_pycord_is_installed(monkeypatch)
    started: List[str] = []
    _use_fake_components(monkeypatch, started)
    config(**{register_callbacks.CHANNEL_ID_ENV_VAR: "general"})

    register_callbacks.on_startup()

    assert started == []
    assert sink.warnings


def test_ac60a_a_broken_identity_list_warns_and_stands_down(config, sink, monkeypatch):
    """A malformed entry must not take the terminal session down (INV-C1)."""
    _pretend_pycord_is_installed(monkeypatch)
    started: List[str] = []
    _use_fake_components(monkeypatch, started)
    config(**{register_callbacks.ALLOW_FROM_ENV_VAR: "no-channel-prefix"})

    register_callbacks.on_startup()

    assert started == []
    assert sink.warnings


# --------------------------------------------------------------------------- #
# AC-71a — a failed activation step is LOUD (a bare raise is not)
# --------------------------------------------------------------------------- #


def test_ac71a_a_failing_component_emits_an_error(config, sink, monkeypatch):
    _pretend_pycord_is_installed(monkeypatch)
    log: List[str] = []
    failing = register_callbacks.COMPONENTS[2].module
    _use_fake_components(monkeypatch, log, failing=failing)

    register_callbacks.on_startup()  # must not raise -- _trigger_callbacks eats it

    assert sink.errors, "a bare raise is swallowed by _trigger_callbacks"
    assert failing in sink.errors[0]


def test_ac71a_a_failed_activation_rolls_back_in_teardown_order(
    config, sink, monkeypatch
):
    _pretend_pycord_is_installed(monkeypatch)
    log: List[str] = []
    failing = register_callbacks.COMPONENTS[2].module
    _use_fake_components(monkeypatch, log, failing=failing)

    register_callbacks.on_startup()

    first, second = (
        component.module for component in register_callbacks.COMPONENTS[:2]
    )
    assert log == [
        f"install:{first}",
        f"install:{second}",
        f"uninstall:{second}",
        f"uninstall:{first}",
    ]


def test_ac71a_a_missing_pycord_is_loud_and_harmless(config, sink, monkeypatch):
    started: List[str] = []
    _use_fake_components(monkeypatch, started)
    monkeypatch.setattr(
        register_callbacks, "load_pycord", lambda: (None, "py-cord is not installed")
    )

    register_callbacks.on_startup()

    assert started == []
    assert sink.errors


def test_ac71a_a_failing_teardown_does_not_stop_the_others(config, sink, monkeypatch):
    _pretend_pycord_is_installed(monkeypatch)
    log: List[str] = []
    modules = _use_fake_components(monkeypatch, log)
    register_callbacks.on_startup()
    log.clear()

    first = register_callbacks.COMPONENTS[0].module
    last = register_callbacks.COMPONENTS[-1].module

    def _explode() -> None:
        raise RuntimeError("teardown went wrong")

    monkeypatch.setattr(modules[last], "uninstall", _explode)

    register_callbacks.on_shutdown()

    assert f"uninstall:{first}" in log
    assert sink.errors


# --------------------------------------------------------------------------- #
# Lifecycle — startup starts, shutdown tears down in the reverse order
# --------------------------------------------------------------------------- #


def test_startup_starts_every_layer_broker_first(config, sink, monkeypatch):
    log: List[str] = []
    _pretend_pycord_is_installed(monkeypatch)
    _use_fake_components(monkeypatch, log)

    register_callbacks.on_startup()

    assert log == [
        f"install:{component.module}" for component in register_callbacks.COMPONENTS
    ]


def test_shutdown_tears_down_c5_to_c1(config, sink, monkeypatch):
    """C5 -> C4 -> C3/C7 -> C2 -> C1: the broker goes last."""
    log: List[str] = []
    _pretend_pycord_is_installed(monkeypatch)
    _use_fake_components(monkeypatch, log)
    register_callbacks.on_startup()
    log.clear()

    register_callbacks.on_shutdown()

    assert log == [
        f"uninstall:{component.module}"
        for component in reversed(register_callbacks.COMPONENTS)
    ]


def test_the_teardown_order_is_the_exact_reverse_of_the_startup_order():
    layers = [component.layer for component in register_callbacks.COMPONENTS]

    assert layers == ["C1", "C2", "C3", "C7", "C4", "C5", "C8"]


def test_startup_twice_starts_the_layers_once(config, sink, monkeypatch):
    log: List[str] = []
    _pretend_pycord_is_installed(monkeypatch)
    _use_fake_components(monkeypatch, log)

    register_callbacks.on_startup()
    register_callbacks.on_startup()

    assert log.count(f"install:{register_callbacks.COMPONENTS[0].module}") == 1


def test_shutdown_without_startup_is_a_noop(config, sink, monkeypatch):
    log: List[str] = []
    _use_fake_components(monkeypatch, log)

    register_callbacks.on_shutdown()

    assert log == []
    assert sink.errors == []


def test_the_startup_callback_is_registered_for_the_startup_phase():
    """Not ``handle_cli_args``: that one parks the TUI (INV-C1)."""
    from code_puppy.callbacks import get_callbacks

    assert register_callbacks.on_startup in get_callbacks("startup")
    assert register_callbacks.on_shutdown in get_callbacks("shutdown")


# --------------------------------------------------------------------------- #
# AC-62 — --session-name is registered and overrides
# --------------------------------------------------------------------------- #


def test_ac62_session_name_is_registered_on_the_parser():
    import argparse

    parser = argparse.ArgumentParser()
    register_callbacks.register_cli_args(parser)

    assert parser.parse_args(["--session-name", "nightly"]).session_name == "nightly"
    assert parser.parse_args([]).session_name is None


def test_ac62_the_parsed_value_overrides():
    class _Args:
        session_name = "nightly"

    assert register_callbacks.handle_cli_args(_Args()) is None
    assert register_callbacks.session_name_override() == "nightly"


def test_ac62_without_the_flag_there_is_no_override():
    class _Args:
        session_name = None

    register_callbacks.handle_cli_args(_Args())

    assert register_callbacks.session_name_override() is None


def test_ac62_the_override_reaches_the_layers(config, sink, monkeypatch):
    """``handle_cli_args`` (:213) runs before ``on_startup`` (:403), so it lands."""
    _pretend_pycord_is_installed(monkeypatch)
    _use_fake_components(monkeypatch, [])

    class _Args:
        session_name = "nightly"

    register_callbacks.handle_cli_args(_Args())
    register_callbacks.on_startup()

    assert register_callbacks.activation_config().session_name == "nightly"


def test_ac62_without_the_flag_the_layers_derive_their_own_name(
    config, sink, monkeypatch
):
    _pretend_pycord_is_installed(monkeypatch)
    _use_fake_components(monkeypatch, [])

    register_callbacks.on_startup()

    assert register_callbacks.activation_config().session_name is None


def test_ac62_an_empty_name_is_not_an_override(config, sink, monkeypatch):
    """``--session-name ""`` must not name a thread after the empty string."""

    class _Args:
        session_name = ""

    register_callbacks.handle_cli_args(_Args())

    assert register_callbacks.session_name_override() is None


def test_ac62_the_cli_hook_never_parks_the_tui():
    """Only a dict with ``handled=True`` short-circuits ``cli_runner``."""

    class _Args:
        session_name = "nightly"

    assert register_callbacks.handle_cli_args(_Args()) is None


def test_ac62_the_discord_flag_is_gone():
    """The flag booted the gateway; the gateway is gone, so the flag is too."""
    import argparse

    parser = argparse.ArgumentParser()
    register_callbacks.register_cli_args(parser)

    with pytest.raises(SystemExit):
        parser.parse_args(["--discord"])


# --------------------------------------------------------------------------- #
# AC-58n-a — the deletion is complete
# --------------------------------------------------------------------------- #

DELETED_MODULES = ("gateway", "output", "concurrency", "concurrency_selftest")

#: The modules W6 answers for.  ``approvals.py`` / ``approvals_ui.py`` still
#: import the deleted code on purpose -- W4 rebuilds them in wave 3 (AC-58n-b).
W6_MODULES = ("register_callbacks.py", "session_ids.py", "constants.py")


@pytest.mark.parametrize("module", DELETED_MODULES)
def test_ac58n_a_the_deleted_modules_are_gone(module):
    assert not (PLUGIN_DIR / f"{module}.py").exists()


@pytest.mark.parametrize(
    "test_module", ["output", "concurrency", "concurrency_shell", "transport"]
)
def test_ac58n_a_their_tests_are_gone(test_module):
    assert not (PLUGIN_DIR / "tests" / f"test_{test_module}.py").exists()


@pytest.mark.parametrize("module", W6_MODULES)
def test_ac58n_a_no_w6_module_imports_the_deleted_code(module):
    import ast

    tree = ast.parse((PLUGIN_DIR / module).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
            if node.module:
                imported.add(node.module.rsplit(".", 1)[-1])
        elif isinstance(node, ast.Import):
            imported.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)

    assert imported.isdisjoint(DELETED_MODULES)


@pytest.mark.parametrize("module", DELETED_MODULES)
def test_ac58n_a_no_bytecode_leftovers(module):
    """A stale ``.pyc`` keeps a deleted module importable and hides the break."""
    cache = PLUGIN_DIR / "__pycache__"

    assert not list(cache.glob(f"{module}.*.pyc"))
    assert not list(cache.glob(f"{module}.pyc"))


@pytest.mark.parametrize(
    "test_module", ["output", "concurrency", "concurrency_shell", "transport"]
)
def test_ac58n_a_no_bytecode_leftovers_for_their_tests(test_module):
    cache = PLUGIN_DIR / "tests" / "__pycache__"

    assert not list(cache.glob(f"test_{test_module}.*.pyc"))
    assert not list(cache.glob(f"test_{test_module}.pyc"))


@pytest.mark.parametrize("module", DELETED_MODULES)
def test_ac58n_a_they_are_not_importable(module):
    import importlib

    sys.modules.pop(f"cp_discord.{module}", None)
    with pytest.raises(ImportError):
        importlib.import_module(f"cp_discord.{module}")


def test_ac58n_a_the_w6_modules_import_cleanly():
    import importlib

    for module in W6_MODULES:
        importlib.import_module(f"cp_discord.{module[:-3]}")


def test_ac47_w6_files_stay_under_600_lines():
    for module in W6_MODULES:
        lines = (PLUGIN_DIR / module).read_text(encoding="utf-8").count("\n")
        assert lines <= 600, f"{module} has {lines} lines"
