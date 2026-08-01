"""L2 transport — the ``--discord`` entry point.

This is the only place the Discord plugin touches Code Puppy's CLI.  It
contributes the ``--discord`` flag and, when that flag is set, boots the
gateway on its own event loop in its own thread, returning the short-circuit
sentinel so the interactive TUI never starts.

Two details here are load-bearing and easy to get wrong:

**The import guard checks IDENTITY, not importability.**  py-cord and
discord.py both install a top-level module named ``discord`` and are mutually
exclusive.  ``import discord`` therefore succeeds with the wrong library too,
and the plugin would run on and fail later in some diffuse way.  Only py-cord
exposes ``ApplicationContext``, so that is what we test for.

**The remedy we print is ``uv sync --extra discord``.**  This project is
uv-managed and its .venv has no ``pip`` at all, so a ``pip install`` hint —
which the ``dbos`` plugin does print — would send the user down a dead end.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from code_puppy.callbacks import register_callback

logger = logging.getLogger(__name__)

#: Where the bot token comes from.  Env var first (nothing secret is written
#: to disk by us), then puppy.cfg for convenience.
TOKEN_ENV_VAR = "DISCORD_BOT_TOKEN"
TOKEN_CONFIG_KEY = "discord_bot_token"

#: Who may talk to the bot, and who may release its approvals (SPEC-L3 §2.2).
#: Two INDEPENDENT lists — ``approvers`` is never derived from ``allow_from``.
ALLOW_FROM_CONFIG_KEY = "discord_allow_from"
APPROVERS_CONFIG_KEY = "discord_approvers"
ALLOW_FROM_ENV_VAR = "DISCORD_ALLOW_FROM"
APPROVERS_ENV_VAR = "DISCORD_APPROVERS"

INSTALL_HINT = "uv sync --extra discord"


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
            "The Discord plugin needs py-cord, which is not installed. "
            f"Install it with: {INSTALL_HINT}"
        )

    if not hasattr(discord, "ApplicationContext"):
        # discord.py is import-compatible but API-incompatible, and the two
        # cannot coexist -- being explicit here turns a late, confusing failure
        # into an immediate, actionable one.
        return None, (
            "The installed 'discord' module is discord.py, not py-cord. "
            "They share the module name and cannot coexist. Uninstall "
            f"discord.py, then install py-cord with: {INSTALL_HINT}"
        )

    return discord, None


def _resolve_token() -> Optional[str]:
    """Fetch the bot token from the environment or puppy.cfg."""
    token = os.environ.get(TOKEN_ENV_VAR)
    if token:
        return token.strip() or None
    try:
        from code_puppy.config import get_value

        value = get_value(TOKEN_CONFIG_KEY)
    except Exception:
        logger.debug("Discord: could not read the token from config", exc_info=True)
        return None
    return value.strip() if value else None


def _split_identities(raw: Optional[str]) -> List[str]:
    """Split a comma-separated identity list, dropping empty entries."""
    return [entry.strip() for entry in (raw or "").split(",") if entry.strip()]


def _read_identity_lists() -> Tuple[List[str], List[str]]:
    """Read ``allow_from`` and ``approvers`` from the environment or puppy.cfg.

    Entries look like ``discord:<user id>=<principal>`` (SPEC-L3 §2.2).  The
    environment wins where it is SET — including when set to an empty string,
    which is how an operator revokes a list without editing puppy.cfg.
    """

    def _read(env_var: str, config_key: str) -> List[str]:
        raw = os.environ.get(env_var)
        if raw is None:
            try:
                from code_puppy.config import get_value

                raw = get_value(config_key)
            except Exception:
                logger.debug("Discord: could not read %s", config_key, exc_info=True)
                raw = None
        return _split_identities(raw)

    return (
        _read(ALLOW_FROM_ENV_VAR, ALLOW_FROM_CONFIG_KEY),
        _read(APPROVERS_ENV_VAR, APPROVERS_CONFIG_KEY),
    )


def _sync_identities() -> Optional[str]:
    """Load the configured identities into the bindings database.

    Returns ``None`` on success, or an operator-facing error message.

    Without this the database stays empty, and because INV-3 is fail-closed a
    freshly installed bot refuses EVERY message from EVERYONE — with no
    supported way to grant access.  An empty configuration is therefore a hard
    error rather than a default: a bot nobody can operate should say so at
    boot, not look healthy and answer nothing.
    """
    from . import authz

    allow_from, approvers = _read_identity_lists()
    if not allow_from and not approvers:
        return (
            "no Discord identities are configured, so nobody could talk to "
            f"this bot. Set '{ALLOW_FROM_CONFIG_KEY}' (and "
            f"'{APPROVERS_CONFIG_KEY}') in puppy.cfg, or the "
            f"{ALLOW_FROM_ENV_VAR}/{APPROVERS_ENV_VAR} environment variables, "
            "as comma-separated 'discord:<user id>=<principal>' entries"
        )

    try:
        roles = authz.sync_from_config(allow_from, approvers)
    except authz.AuthzError as error:
        return str(error)

    logger.info(
        "Discord: %d talker(s), %d approver(s) configured",
        len(roles[authz.Role.TALKER]),
        len(roles[authz.Role.APPROVER]),
    )
    return None


def register_cli_args(parser: Any) -> None:
    """Add the ``--discord`` flag to Code Puppy's argument parser."""
    parser.add_argument(
        "--discord",
        action="store_true",
        help=(
            "Run as a Discord bot: every channel gets its own independent "
            "agent run, with approval gates answered in Discord."
        ),
    )


def handle_cli_args(args: Any) -> Optional[Dict[str, Any]]:
    """Boot the gateway when ``--discord`` is set; otherwise stand down.

    Returns the ``{"handled": True, "exit_code": ...}`` sentinel so the CLI
    runner exits after the gateway stops instead of falling through to the
    interactive TUI.  Any other invocation returns ``None`` (not ours).

    The gateway runs on its own loop in its own thread because this hook is
    called *synchronously from inside* Code Puppy's already-running
    ``asyncio.run(main())`` (Fakt F2) — a nested loop is impossible.  We then
    block on that thread, which parks ``main()`` and keeps the TUI down.
    """
    if not getattr(args, "discord", False):
        return None

    discord_module, error = load_pycord()
    if discord_module is None:
        print(error, file=sys.stderr)
        return {"handled": True, "exit_code": 1}

    token = _resolve_token()
    if not token:
        print(
            "The Discord plugin needs a bot token. Set the "
            f"{TOKEN_ENV_VAR} environment variable, or add "
            f"'{TOKEN_CONFIG_KEY} = ...' to puppy.cfg.",
            file=sys.stderr,
        )
        return {"handled": True, "exit_code": 1}

    return {"handled": True, "exit_code": _serve_in_thread(discord_module, token)}


def _serve_in_thread(discord_module: Any, token: str) -> int:
    """Run the gateway to completion on a dedicated thread + loop."""
    box: Dict[str, int] = {"exit_code": 0}

    def _run() -> None:
        try:
            box["exit_code"] = asyncio.run(_serve(discord_module, token))
        except Exception:
            logger.exception("Discord gateway crashed")
            box["exit_code"] = 1

    thread = threading.Thread(target=_run, name="discord-gateway")
    thread.start()
    thread.join()
    return box["exit_code"]


async def _serve(discord_module: Any, token: str) -> int:
    """Install the concurrency adapter, serve, then leave no seams behind.

    The adapter is verified with ``selftest()`` rather than trusted: it patches
    private core internals, so a core refactor can leave it installed but
    inert, which would degrade into silent cross-channel mixing instead of an
    error.  Failing loudly at boot is the whole point (AC-9).
    """
    from . import approvals, concurrency, gateway, output

    concurrency.install()
    ok, detail = concurrency.selftest()
    if not ok:
        concurrency.uninstall()
        print(
            f"The Discord plugin cannot run safely: {detail}. "
            "This usually means Code Puppy's internals changed. "
            "Refusing to start rather than risk mixing channels.",
            file=sys.stderr,
        )
        return 1

    # Load the configured identities BEFORE installing the rules: the rules
    # read the bindings database, and an empty one refuses everyone (INV-3).
    # A bot nobody can operate fails loudly here instead of going quiet.
    identity_error = _sync_identities()
    if identity_error is not None:
        concurrency.uninstall()
        print(
            f"The Discord plugin cannot run: {identity_error}. "
            "Refusing to start rather than accept nobody's messages.",
            file=sys.stderr,
        )
        return 1

    # Install L3's rules. Without this the gateway is fail-closed (INV-3) and
    # would refuse every message, so this wiring is load-bearing, not optional.
    gateway.set_authorizer(gateway.authz_authorizer)

    # L4 covers BOTH approval edges. Its shell hook is not optional: headless,
    # the core's own shell prompt is skipped entirely and the command would
    # otherwise run unchecked (SPEC-L4 §4.1).
    try:
        approvals.install()
    except approvals.ApprovalError as error:
        concurrency.uninstall()
        print(
            f"The Discord plugin cannot run safely: {error}. "
            "Refusing to start rather than run without approval gates.",
            file=sys.stderr,
        )
        return 1

    # L5 routes everything a run produces back into its channel. Installed
    # BEFORE serving: the very first message we handle already streams, and
    # the system channel has to exist before anything can miss its session.
    system_channel_id = output.resolve_system_channel_id()
    if system_channel_id is None:
        # Order is binding (INV-7 clause 5): L4 stands down before L1, or the
        # core would go back to calling our 4-arg backend with three.
        approvals.uninstall()
        concurrency.uninstall()
        print(
            "The Discord plugin cannot run: no system channel is configured. "
            "SPEC-L5 §5.3 makes it binding because it is the catch-all for "
            "everything that has no channel of its own — output from a "
            "finished command's reader threads, the whole legacy message "
            "queue, and the audit trail of senders who were REFUSED. Without "
            f"it those are dropped silently. Set {output.SYSTEM_CHANNEL_ENV_VAR} "
            f"or '{output.SYSTEM_CHANNEL_CONFIG_KEY} = <channel id>' in "
            "puppy.cfg. Refusing to start rather than lose the rejection "
            "audit.",
            file=sys.stderr,
        )
        return 1

    output.install(system_channel_id=system_channel_id)
    gateway.set_outcome_sink(output.on_outcome)

    try:
        return await gateway.run_gateway(discord_module, token)
    finally:
        # Order is binding (INV-7 clause 5): L4 stands down FIRST. The other
        # way round the core would go back to calling our 4-arg backend with
        # three arguments, routing gates by a title string.
        approvals.uninstall()
        output.uninstall()
        gateway.reset_state()
        concurrency.uninstall()


def register() -> None:
    """Register the Discord CLI hooks."""
    register_callback("register_cli_args", register_cli_args)
    register_callback("handle_cli_args", handle_cli_args)
    logger.debug("Discord plugin callbacks registered")


register()
