"""C6 — registration, lifecycle and configuration for the terminal bridge.

This is the only place the plugin touches Code Puppy's lifecycle.  It reads the
configuration (SPEC §8a), loads the identities into the authorization database,
and brings the six layers up and down in a defined order.

**The activation path is INVERTED compared to the old plugin.**  That one
contributed a ``--discord`` flag and, in ``handle_cli_args``, booted a gateway
and blocked on it, which is exactly how it kept the interactive TUI from ever
starting.  Here the terminal IS the primary interface (INV-C1): activation
hangs off the ``startup`` callback, which returns immediately, and everything
long-running lives behind a layer's ``install()``.

``handle_cli_args`` survives in one strictly narrower role: recording
``--session-name``.  It is the only hook that ever sees parsed arguments
(``cli_runner.py:209`` keeps them in a local), and it returns ``None``, so it
cannot short-circuit startup -- only a dict with ``handled=True`` does that
(``cli_runner.py:213-216``).

Two details are load-bearing and easy to get wrong:

**The import guard checks IDENTITY, not importability.**  py-cord and
discord.py both install a top-level module named ``discord`` and are mutually
exclusive.  ``import discord`` therefore succeeds with the wrong library too,
and the plugin would run on and fail later in some diffuse way.  Only py-cord
exposes ``ApplicationContext``, so that is what we test for.

**A failure here is never allowed to end the session** (INV-C1).  It is also
never allowed to be silent: ``_trigger_callbacks`` (``callbacks.py:303-325``)
catches every exception from a callback and only logs it, so a bare ``raise``
would vanish.  Failures go through :func:`_emit_error` instead.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence, Tuple

from code_puppy.callbacks import register_callback

from . import constants

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration surface (SPEC §8a)
# --------------------------------------------------------------------------- #
#
# Two naming families, on purpose:
#
# * ``DISCORD_*`` are PRE-EXISTING keys and are taken over unchanged, so no
#   operator has to migrate a working configuration.
# * ``CP_DISCORD_*`` are the keys this rebuild adds.  The prefix makes it
#   obvious at a glance which is which.

ENABLED_ENV_VAR = "CP_DISCORD"
ENABLED_CONFIG_KEY = "cp_discord_enabled"

TOKEN_ENV_VAR = "DISCORD_BOT_TOKEN"
TOKEN_CONFIG_KEY = "discord_bot_token"

CHANNEL_ID_ENV_VAR = "CP_DISCORD_CHANNEL_ID"
CHANNEL_ID_CONFIG_KEY = "cp_discord_channel_id"

MODE_ENV_VAR = "CP_DISCORD_MODE"
MODE_CONFIG_KEY = "cp_discord_mode"

#: Whether a new thread pulls the approvers in.  Unlike every other switch
#: here the default is ON, because OFF is the state in which the bridge posts
#: an approval gate that reaches nobody's phone -- see :mod:`.broker_autojoin`.
#: Worth switching off only when many parallel sessions make the
#: notifications more noise than signal.
AUTOJOIN_ENV_VAR = "CP_DISCORD_AUTOJOIN"
AUTOJOIN_CONFIG_KEY = "cp_discord_autojoin"

#: Who may talk to the bot, and who may release its approval gates.  Two
#: INDEPENDENT lists — ``approvers`` is never derived from ``allow_from``.
ALLOW_FROM_ENV_VAR = "DISCORD_ALLOW_FROM"
ALLOW_FROM_CONFIG_KEY = "discord_allow_from"
APPROVERS_ENV_VAR = "DISCORD_APPROVERS"
APPROVERS_CONFIG_KEY = "discord_approvers"

MODE_REPORT = "report"
MODE_STREAM = "stream"
MODES = (MODE_REPORT, MODE_STREAM)

TOOL_LOG_ENV_VAR = "CP_DISCORD_TOOL_LOG"
TOOL_LOG_CONFIG_KEY = "cp_discord_tool_log"

INSTALL_HINT = "uv sync --extra discord"

#: Values that mean "on".  Anything else -- including the empty string an
#: operator uses to switch the bridge off without editing puppy.cfg -- is off.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Everything a layer needs to come up.  Immutable once activation starts."""

    token: str
    channel_id: int
    mode: str
    session_name: Optional[str]


@dataclass(frozen=True, slots=True)
class Component:
    """One layer, in startup order."""

    layer: str
    module: str


#: The six layers, in STARTUP order.  Teardown walks this list backwards, which
#: yields C5 -> C4 -> C7/C3 -> C2 -> C1: close the inbound paths first, then the
#: reporters, then deregister the session, and only then stop the broker.  The
#: other way round a session would deregister from a broker that is already
#: gone.
COMPONENTS: Tuple[Component, ...] = (
    Component("C1", "broker_server"),
    Component("C2", "client"),
    Component("C3", "reporter"),
    Component("C7", "collector"),
    Component("C4", "approvals"),
    Component("C5", "inbound"),
)

# --------------------------------------------------------------------------- #
# Module state
# --------------------------------------------------------------------------- #

_config: Optional[BridgeConfig] = None
_warnings: Tuple[str, ...] = ()
_session_name: Optional[str] = None
_installed: List[Component] = []


def activation_config() -> Optional[BridgeConfig]:
    """The configuration the bridge came up with, or ``None`` if it did not."""
    return _config


def activation_warnings() -> Tuple[str, ...]:
    """Warnings raised while activating.

    C1 posts these into the channel (INV-C15, AC-60b): a user staring at a
    thread that never answers deserves to be told why.
    """
    return _warnings


def session_name_override() -> Optional[str]:
    """The ``--session-name`` value, or ``None`` (AC-62)."""
    return _session_name


def reset_state() -> None:
    """Forget everything.  Used by :func:`on_shutdown` and by tests."""
    global _config, _warnings, _session_name
    _config = None
    _warnings = ()
    _session_name = None
    _installed.clear()


# --------------------------------------------------------------------------- #
# Talking to the operator
# --------------------------------------------------------------------------- #


def _emit_warning(message: str) -> None:
    from code_puppy.messaging import emit_warning

    emit_warning(f"cp_discord: {message}")


def _emit_error(message: str) -> None:
    from code_puppy.messaging import emit_error

    emit_error(f"cp_discord: {message}")


def _get_config_value(key: str) -> Optional[str]:
    """Read *key* from puppy.cfg, treating an unreadable config as absent."""
    try:
        from code_puppy.config import get_value

        return get_value(key)
    except Exception:
        logger.debug("cp_discord: could not read %s from config", key, exc_info=True)
        return None


def _setting(env_var: str, config_key: str) -> Optional[str]:
    """The configured value, environment first.

    The environment wins wherever it is SET -- including when set to the empty
    string, which is how an operator switches something off for one run without
    touching puppy.cfg.  ``None`` means "not configured anywhere".
    """
    raw = os.environ.get(env_var)
    if raw is None:
        raw = _get_config_value(config_key)
    if raw is None:
        return None
    return raw.strip()


# --------------------------------------------------------------------------- #
# py-cord
# --------------------------------------------------------------------------- #


def load_pycord() -> Tuple[Optional[Any], Optional[str]]:
    """Import py-cord, verifying it really IS py-cord.

    Returns ``(module, None)`` on success and ``(None, message)`` otherwise.
    Never raises: a missing or wrong optional dependency must leave the plugin
    inactive, not break the CLI for everyone else.
    """
    try:
        import discord
    except ImportError:
        return None, (
            "the Discord bridge needs py-cord, which is not installed. "
            f"Install it with: {INSTALL_HINT}"
        )

    if not hasattr(discord, "ApplicationContext"):
        # discord.py is import-compatible but API-incompatible, and the two
        # cannot coexist -- being explicit here turns a late, confusing failure
        # into an immediate, actionable one.
        return None, (
            "the installed 'discord' module is discord.py, not py-cord. "
            "They share the module name and cannot coexist. Uninstall "
            f"discord.py, then install py-cord with: {INSTALL_HINT}"
        )

    return discord, None


# --------------------------------------------------------------------------- #
# Reading the configuration
# --------------------------------------------------------------------------- #


def is_enabled() -> bool:
    """Whether the operator switched the bridge on (SPEC §8a).

    Off is the default and costs nothing: no thread, no socket, no database
    access.  A plugin that is merely installed must not make the CLI slower.
    """
    return (_setting(ENABLED_ENV_VAR, ENABLED_CONFIG_KEY) or "").lower() in _TRUTHY


def autojoin_enabled() -> bool:
    """Whether a new thread pulls the approvers in (SPEC §8a).  Default ON.

    Inverted relative to :func:`is_enabled` on purpose: the whole bridge
    defaults to off because a merely installed plugin must cost nothing, but
    once it IS on, a thread nobody is notified about defeats the point.  So
    "not configured" means on, and only an explicit falsy value turns it off.
    """
    raw = _setting(AUTOJOIN_ENV_VAR, AUTOJOIN_CONFIG_KEY)
    return True if raw is None else raw.lower() in _TRUTHY


def tool_log_enabled() -> bool:
    """Whether the report lists the tools that ran.  Default ON.

    Same shape as :func:`autojoin_enabled`: not configured means on, only an
    explicit falsy value turns it off.  Off leaves the assistant's answer,
    the gates and the status line untouched -- it drops ONLY the
    ``-> tool`` / ``<- tool (n ms)`` inventory, which is noise to some
    readers and the whole point to others.
    """
    raw = _setting(TOOL_LOG_ENV_VAR, TOOL_LOG_CONFIG_KEY)
    return True if raw is None else raw.lower() in _TRUTHY


def _read_mode(warn: Callable[[str], None]) -> str:
    """The report/stream mode, defaulting to ``report``."""
    raw = (_setting(MODE_ENV_VAR, MODE_CONFIG_KEY) or "").lower()
    if not raw:
        return MODE_REPORT
    if raw not in MODES:
        warn(
            f"{MODE_ENV_VAR}={raw!r} is not one of {', '.join(MODES)}; "
            f"falling back to {MODE_REPORT}"
        )
        return MODE_REPORT
    return raw


def _read_channel_id(warn: Callable[[str], None]) -> Optional[int]:
    """The one channel the bridge posts into, or ``None`` if unusable."""
    raw = _setting(CHANNEL_ID_ENV_VAR, CHANNEL_ID_CONFIG_KEY)
    if not raw:
        warn(
            f"no channel is configured, so there is nowhere to post. Set "
            f"{CHANNEL_ID_ENV_VAR} or '{CHANNEL_ID_CONFIG_KEY} = <channel id>' "
            "in puppy.cfg. This session continues without Discord."
        )
        return None
    if not raw.isascii() or not raw.isdigit():
        warn(
            f"{CHANNEL_ID_ENV_VAR}={raw!r} is not a channel id. Discord ids are "
            "numeric. This session continues without Discord."
        )
        return None
    return int(raw)


def read_config(warn: Callable[[str], None]) -> Optional[BridgeConfig]:
    """Assemble the configuration, or ``None`` if the bridge cannot run.

    Every rejection goes through *warn* first: a missing key means "no Discord
    for this session", never "no session" (INV-C1/INV-C15).
    """
    token = _setting(TOKEN_ENV_VAR, TOKEN_CONFIG_KEY)
    if not token:
        warn(
            f"no bot token is configured. Set {TOKEN_ENV_VAR} or "
            f"'{TOKEN_CONFIG_KEY} = ...' in puppy.cfg. This session continues "
            "without Discord."
        )
        return None

    channel_id = _read_channel_id(warn)
    if channel_id is None:
        return None

    return BridgeConfig(
        token=token,
        channel_id=channel_id,
        mode=_read_mode(warn),
        session_name=_session_name,
    )


# --------------------------------------------------------------------------- #
# Identities (INV-C26)
# --------------------------------------------------------------------------- #


def _split_identities(raw: Optional[str]) -> List[str]:
    """Split a comma-separated identity list, dropping empty entries."""
    return [entry.strip() for entry in (raw or "").split(",") if entry.strip()]


def sync_identities(warn: Callable[[str], None]) -> bool:
    """Load the configured identities into the bindings database.

    Returns ``True`` when the bridge may proceed.

    This is the only WRITER.  ``check_message`` and ``has_role`` are pure
    readers, so without this call the table stays empty, and because the
    authorization path is fail-closed the bridge would discard every message
    and refuse every click -- while looking perfectly healthy.

    An empty configuration is NOT a hard stop, unlike in the old plugin: the
    terminal session is the primary interface and must keep running (INV-C1).
    The broker posts the warning into the channel instead (INV-C15), which is
    where the person wondering why nothing answers is actually looking.
    """
    from . import authz

    allow_from = _split_identities(_setting(ALLOW_FROM_ENV_VAR, ALLOW_FROM_CONFIG_KEY))
    approvers = _split_identities(_setting(APPROVERS_ENV_VAR, APPROVERS_CONFIG_KEY))

    if not allow_from and not approvers:
        warn(
            "no identities are configured, so nobody can talk to this bridge "
            "and nobody can approve anything. Set "
            f"'{ALLOW_FROM_CONFIG_KEY}' (and '{APPROVERS_CONFIG_KEY}') in "
            f"puppy.cfg, or {ALLOW_FROM_ENV_VAR}/{APPROVERS_ENV_VAR}, as "
            f"comma-separated '{constants.AUTHZ_CHANNEL}:<user id>=<principal>' "
            "entries."
        )
        return True

    try:
        roles = authz.sync_from_config(allow_from, approvers)
    except authz.AuthzError as error:
        warn(
            f"the identity configuration is unusable: {error}. This session "
            "continues without Discord."
        )
        return False

    logger.info(
        "cp_discord: %d talker(s), %d approver(s) configured",
        len(roles[authz.Role.TALKER]),
        len(roles[authz.Role.APPROVER]),
    )
    return True


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def _import_component(component: Component) -> Any:
    """Import one layer's module.  Own seam, so tests can substitute a fake."""
    from importlib import import_module

    return import_module(f".{component.module}", __package__)


def _install_components(config: BridgeConfig) -> bool:
    """Bring the layers up in order, rolling back if one refuses.

    A half-installed bridge is worse than none: the layers hook into core call
    paths, and one that is up while its neighbour is missing would route work
    into a dead end.  So a failure unwinds what already came up, in the same
    order teardown uses.
    """
    for component in COMPONENTS:
        try:
            module = _import_component(component)
            module.install(config)
        except Exception as error:
            _emit_error(
                f"layer {component.layer} ({component.module}) failed to start: "
                f"{error}. Rolling back; this session continues without Discord."
            )
            logger.exception("cp_discord: %s failed to install", component.module)
            _uninstall_components()
            return False
        _installed.append(component)
    return True


def _uninstall_components() -> None:
    """Tear the layers down in reverse order.  Never raises.

    Every layer gets its turn even if an earlier one throws: leaving C1's
    socket and lock behind because C5 misbehaved would block the next session
    from ever becoming the broker.
    """
    while _installed:
        component = _installed.pop()
        try:
            _import_component(component).uninstall()
        except Exception as error:
            _emit_error(
                f"layer {component.layer} ({component.module}) failed to stop: {error}."
            )
            logger.exception("cp_discord: %s failed to uninstall", component.module)


def _activate(warn: Callable[[str], None]) -> Optional[BridgeConfig]:
    """Bring the bridge up, or return ``None`` if it cannot run."""
    discord_module, error = load_pycord()
    if discord_module is None:
        _emit_error(f"{error} This session continues without Discord.")
        return None

    config = read_config(warn)
    if config is None:
        return None

    if not sync_identities(warn):
        return None

    return config if _install_components(config) else None


def on_startup() -> None:
    """Activate the bridge.  Returns immediately (INV-C1).

    Nothing here blocks: the broker and the session client run on their own
    threads, so the terminal stays the primary interface no matter what
    Discord is doing.
    """
    global _config, _warnings

    if not is_enabled():
        return

    if _installed:
        logger.debug("cp_discord: already active, ignoring a second startup")
        return

    collected: List[str] = []

    def warn(message: str) -> None:
        collected.append(message)
        _emit_warning(message)

    try:
        _config = _activate(warn)
    finally:
        # Kept even on the failure paths: C1 posts these into the channel
        # (INV-C15), and the reasons the bridge did NOT come up are exactly
        # the ones somebody staring at a silent thread needs to read.
        _warnings = tuple(collected)


def on_shutdown() -> None:
    """Tear the bridge down: C5 -> C4 -> C7/C3 -> C2 -> C1."""
    _uninstall_components()
    reset_state()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def register_cli_args(parser: Any) -> None:
    """Contribute ``--session-name``.

    The core has no such option of its own (``cli_runner.py:149-201``), but the
    bridge names every Discord thread after its session, and an operator needs
    a way to say what a session is called.

    The old ``--discord`` flag is deliberately gone: it existed only to boot
    the gateway that this rebuild deletes, so keeping it would offer a switch
    that does nothing.  Activation is configuration now (``CP_DISCORD`` /
    ``cp_discord_enabled``), which is what makes it survive a plain
    ``code-puppy`` with no arguments -- the normal way people start a session.
    """
    parser.add_argument(
        "--session-name",
        default=None,
        metavar="NAME",
        help=(
            "Name this session, overriding the derived '<directory>/<branch>' "
            "title the Discord bridge would otherwise use."
        ),
    )


def handle_cli_args(args: Any) -> None:
    """Record ``--session-name``.  Always returns ``None``.

    This hook is the ONLY place parsed arguments are visible -- ``cli_runner``
    keeps them in a local (``:209``).  Returning ``None`` is what keeps it
    harmless: only a dict with ``handled=True`` short-circuits startup
    (``:213-216``), and short-circuiting is precisely the behaviour of the old
    plugin that this rebuild exists to remove.
    """
    global _session_name
    _session_name = getattr(args, "session_name", None) or None
    return None


def register() -> None:
    """Register the bridge's lifecycle hooks."""
    register_callback("register_cli_args", register_cli_args)
    register_callback("handle_cli_args", handle_cli_args)
    register_callback("startup", on_startup)
    register_callback("shutdown", on_shutdown)
    logger.debug("cp_discord: callbacks registered")


register()


__all__: Sequence[str] = (
    "BridgeConfig",
    "COMPONENTS",
    "Component",
    "activation_config",
    "activation_warnings",
    "autojoin_enabled",
    "handle_cli_args",
    "is_enabled",
    "load_pycord",
    "on_shutdown",
    "on_startup",
    "read_config",
    "register_cli_args",
    "reset_state",
    "session_name_override",
    "sync_identities",
)
