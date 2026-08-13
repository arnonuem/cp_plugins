"""Test harness for the cp_discord plugin.

Makes the plugin importable the same way Code Puppy's user-tier loader does
it: the loader puts the PLUGINS directory on ``sys.path`` and imports
``register_callbacks.py`` by file location (``code_puppy/plugins/__init__.py``
``:124-127``, ``:288-292``).  Tests reproduce that layout by putting the
plugin's PARENT directory on ``sys.path``, so ``import cp_discord.authz``
resolves against the working tree.

Why this file exists at all: the suites were written when the plugin still
lived inside the code_puppy repo and imported itself as
``code_puppy.plugins.cp_discord``.  That path is gone -- ``cp_plugins`` is now
the single source of truth -- so the old import raises ``ModuleNotFoundError``
before a single assertion runs.  The sibling plugins (``wmux``,
``user_msg_style``) already solve it exactly this way.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PLUGIN_PARENT = str(Path(__file__).resolve().parents[2])

if _PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, _PLUGIN_PARENT)


#: Modules that read an environment variable.  Three, not one: a first pass
#: derived the list from ``register_callbacks`` alone and missed
#: ``CP_DISCORD_DIR`` (broker_election) and ``CODE_PUPPY_DISCORD_AUTHZ_DB``
#: (bindings) -- both fall back to the operator's REAL bridge directory when
#: unset, portfile and bearer token included.
_MODULES_WITH_ENV = ("register_callbacks", "broker_election", "bindings")

#: Both suffixes in use.  ``bindings`` spells it ``DB_PATH_ENV``, so keying on
#: ``_ENV_VAR`` alone would skip it.
_ENV_SUFFIXES = ("_ENV_VAR", "_ENV")


def _env_var_names() -> tuple:
    """Every environment variable the plugin reads a setting from.

    DERIVED, not hand-listed: a hand-maintained list covered 3 of 10 and would
    lose its effect SILENTLY the moment a constant is renamed -- no test would
    go red.  ``test_the_env_isolation_covers_every_module_that_reads_one``
    is the guard against the convention itself drifting.
    """
    import importlib

    names = []
    for module_name in _MODULES_WITH_ENV:
        try:
            module = importlib.import_module(f"cp_discord.{module_name}")
        except Exception:  # pragma: no cover - import errors surface elsewhere
            continue
        for attr, value in vars(module).items():
            if isinstance(value, str) and attr.endswith(_ENV_SUFFIXES):
                names.append(value)
    return tuple(dict.fromkeys(names))


@pytest.fixture(autouse=True)
def _isolate_from_the_operators_config(monkeypatch):
    """Cut the suite off from the machine's real configuration.

    Both sources are cut, because ``_setting`` consults BOTH and the
    environment WINS: the config reader is stubbed to report nothing, and every
    ``*_ENV_VAR`` the plugin knows is cleared.  Covering only one of the two
    leaves exactly the same machine-dependency one variable further along.

    ``_setting()`` reads the environment first and the live puppy.cfg second,
    which is right in production and wrong in a test: the moment an operator
    turns a feature OFF on their own machine, every test asserting its default
    goes red -- on THEIR machine only, with a failure that points at the
    feature instead of at the config.  That happened: switching
    ``cp_discord_tool_log`` off turned 11 collector tests red while the code
    was correct, and the next run then had to prove the failures were not its
    own.

    Neutralising the environment is not enough, because the config file is the
    second source -- so ``_get_config_value`` is stubbed to report "nothing
    configured".  The stub sits on the CONFIG READER, not on ``_setting``:
    tests that exercise the config surface replace ``_get_config_value``
    themselves (``test_autojoin.py:232``), and overriding ``_setting`` above
    them would silently neuter their stub -- the fixture would then be testing
    itself.  A test that wants a value sets it with ``monkeypatch.setenv`` or
    replaces the reader, and both keep working.
    """
    try:
        from cp_discord import register_callbacks
    except Exception:  # pragma: no cover - import failures surface elsewhere
        return

    for env_var in _env_var_names():
        monkeypatch.delenv(env_var, raising=False)

    monkeypatch.setattr(register_callbacks, "_get_config_value", lambda key: None)
