"""C4 — the approval switch: both ways at once, exactly one winner (SPEC §5).

**There is no switch any more, and that is the whole design.**  The previous
version routed EITHER to Discord OR to the terminal, decided by a ContextVar
that is only set when the turn CAME from Discord.  Requirement 8 is the
opposite case -- start at the PC, leave the house, answer on the phone -- so
every approval of such a run would have landed at a terminal nobody was
sitting at.  Hence: every approval goes BOTH ways, and the first answer wins.

Four things carry that, and each exists because its absence is a hang or a
silent denial:

**The slot is never emptied** (INV-C5).  The core's backend check sits outside
every plugin lock (``common.py:1442-1443``), so a request arriving during a
swap would sail straight past us.  No lock can close that hole -- so there is
no swap.

**We never call the core approval** (INV-C6).  It would land on the same check
and recurse.  The terminal branch is driven directly, in
:mod:`.approvals_prompt`.

**The mark is three-valued, not a bool** (INV-C20, §5.2a).  ``exit()`` on an
Application that is not running yet EVAPORATES -- ``get_app()`` hands back a
``DummyApplication`` -- and behind it a prompt would then wait for an already
resolved gate, with no timeout at all (INV-C10).  The mark, the slot and the
CAS live in :mod:`.approvals_state`, and everything is given back on EVERY
exit (§5.2a step 5): without that the terminal branch is dead after the FIRST
approval of a run, and a run makes many.

The timeout is not an exit: it ends the DISCORD branch only (INV-C10).  The
terminal prompt has no timeout, exactly as without this plugin.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from code_puppy.callbacks import register_callback, unregister_callback

from . import approvals_prompt, approvals_ui, bindings, constants
from .approvals_state import (
    MARK_EMPTY,
    MARK_LIVE,
    MARK_PENDING,
    WINNER_REMOTE,
    WINNER_TERMINAL,
    Gate,
    SwitchState,
)
from .approvals_ui import DECISION_APPROVE, GATE_TIMEOUT_SECONDS
from .bindings import Role

logger = logging.getLogger(__name__)

#: Marks OUR backend so a double load is loud rather than silently
#: overwriting.  Local on purpose: ``concurrency.py`` (its previous home) is
#: deleted, this module is the only reader, and putting it in ``constants.py``
#: would hang C4 off another layer for one string.
SENTINEL = "_cp_discord"

#: What the channel is told when the other side answered first (AC-37/39).
DECIDED_IN_TERMINAL = "im Terminal entschieden"
DECIDED_IN_DISCORD = "in Discord entschieden"
GATE_EXPIRED = "abgelaufen - nur noch am PC beantwortbar"

#: How often the waiting backend re-checks whether both branches are done.
#: Short, because it also bounds how long a branch ABORT (as opposed to an
#: answer) takes to be noticed -- an answer wakes it immediately.
_WAIT_TICK = 0.05

_INTERACTIVE_TOOLS = frozenset({"ask_user_question"})
_INTERACTIVE_BLOCK: Dict[str, Any] = {
    "blocked": True,
    "error_message": (
        "[BLOCKED] Interactive pickers cannot be shown over Discord. Ask the "
        "user your question directly in your normal text response and wait for "
        "their reply -- do not call this tool."
    ),
}
"""INV-C16/AC-61: the phone would see BLOCKED with no way to answer.

Only while a broker is reachable.  Without one the session is a plain terminal
session and must behave exactly as it would without this plugin (INV-C19) --
blocking a picker there would take away a tool that works perfectly well.
"""


class ApprovalError(RuntimeError):
    """Raised when the bridge cannot be installed (never a silent bypass)."""


#: Everything two threads share (INV-C29).  One object, one lock -- see
#: :mod:`.approvals_state` for why they are decided together.
_state = SwitchState()

_reporter: Any = None
_installed = False
_previous_backend: Any = None


# -- observation (tests and neighbours) ------------------------------------


def open_gates() -> List[str]:
    return _state.open_ids()


def prompt_mark() -> str:
    return _state.mark


def prompt_slot() -> Optional[str]:
    return _state.slot


def live_application() -> Any:
    return _state.live_app


def flag_generation() -> int:
    return _state.flag_generation


def state_lock_held_by_me() -> bool:
    return _state.held_here()


def set_reporter(reporter: Any) -> None:
    """Point gate reporting at C3 (INV-C24), or at nothing."""
    global _reporter
    _reporter = reporter


def reset_state() -> None:
    """Drop every in-memory trace.  Used at teardown and between tests."""
    _state.reset()


# --------------------------------------------------------------------------- #
# Seams (bound by NAME so a test can substitute them without patching imports)
# --------------------------------------------------------------------------- #


def _active_client() -> Any:
    """C2, or ``None`` when this session has no broker (INV-C19)."""
    from . import client

    return client.active_client()


def _prompt_factory(gate: Gate) -> Any:
    return approvals_prompt.TerminalPrompt(gate.title, gate.message, gate.preview)


def _stdin_is_interactive() -> bool:
    return approvals_prompt.stdin_is_interactive()


def _set_core_flag(value: bool) -> None:
    """Set the core's ``awaiting_user_input`` flag.  Called OUTSIDE the lock.

    ``notify=True`` is binding: our gates are agent-initiated.  ``notify=False``
    is for user-initiated menus (``/model``) and would clear
    ``_AWAITING_USER_INPUT_NOTIFY`` process-wide (``command_runner.py:326-329``),
    undermining AC-22.
    """
    from code_puppy.tools.command_runner import set_awaiting_user_input

    set_awaiting_user_input(value, notify=True)


def _after_gate_posted(gate: Gate) -> None:
    """Seam between posting a gate and starting the prompt.

    Production does nothing here.  It exists because AC-64a's window --
    Discord resolving while the mark is still EMPTY -- is otherwise only
    reachable by winning a race, and a test that hopes for an interleave
    proves nothing.
    """


# --------------------------------------------------------------------------- #
# The backend (§5.2) -- runs on an executor thread, with no loop of its own
# --------------------------------------------------------------------------- #


def approval_backend(
    title: str = "", message: str = "", preview: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    """Bridge one core approval onto BOTH paths.  Blocks its own thread.

    Feedback is always ``None``: two buttons are a yes/no, not a text channel,
    and offering feedback on one branch only would make the answer depend on
    which branch happened to win.
    """
    gate = _open_gate(title, message, preview)
    try:
        remote = _start_discord_branch(gate)
        _after_gate_posted(gate)
        local = _run_terminal_branch(gate)
        if not remote and not local:
            # Neither branch ever existed: nobody could have said yes.  This
            # is the ONLY fail-closed path (INV-C7, AC-33/35).
            logger.warning("cp_discord: no approval path available; denying %s", title)
            return False, None
        return _await_resolution(gate), None
    finally:
        _close_gate(gate)


approval_backend._cp_discord = True  # the SENTINEL, see above


def _open_gate(title: str, message: str, preview: Optional[str]) -> Gate:
    gate = Gate(
        gate_id=str(uuid.uuid4()), title=title, message=message, preview=preview
    )
    _state.add(gate)
    _report("gate_opened")
    return gate


def _close_gate(gate: Gate) -> None:
    """Forget the gate and tell C3 it is over.  Runs on EVERY exit."""
    _state.drop(gate.gate_id)
    _report("gate_closed")


# -- branch 1: Discord -----------------------------------------------------


def _start_discord_branch(gate: Gate) -> bool:
    """Put the gate in the thread.  ``False`` means the phone cannot answer.

    A failed HINWEG is NOT a failure and NOT a branch winner (INV-C1, AC-92):
    it only means this gate has to be answered at the machine.
    """
    client = _active_client()
    if client is None:
        gate.discord_alive = False
        return False
    try:
        delivered = client.submit_gate(
            gate.gate_id,
            gate.title,
            gate.message,
            preview=gate.preview,
            remote_resolvable=True,
        )
    except Exception:
        logger.debug("cp_discord: submitting a gate failed", exc_info=True)
        delivered = False
    gate.discord_alive = bool(delivered)
    if delivered:
        gate.timer = threading.Timer(GATE_TIMEOUT_SECONDS, _expire, args=(gate,))
        gate.timer.daemon = True
        gate.timer.start()
    return gate.discord_alive


def _expire(gate: Gate) -> None:
    """The 120 s deadline: ends the DISCORD branch, nothing else (INV-C10).

    Not a winner, not an exit: the mark and the slot stay, because the
    terminal prompt is still open (AC-49).  Giving them back here would let a
    concurrent approval put a SECOND Application on the same stdin.
    """
    if not _state.expire(gate):
        return
    _tell_channel(gate, GATE_EXPIRED)
    _state.wake()


# -- branch 2: the terminal ------------------------------------------------


def _run_terminal_branch(gate: Gate) -> bool:
    """Start the prompt, unless we may not or cannot.  Returns whether it ran.

    Three refusals, all deliberate:

    * stdin is not interactive -- the core would have refused too, and we
      stand BEFORE its check (INV-C19's stdin clause, AC-86);
    * the slot is taken -- another approval owns stdin, and a second
      Application would fight it (INV-C29 rule 4, AC-80);
    * the gate is already resolved -- §5.2a step 2: no prompt at all.
    """
    if not _stdin_is_interactive() or not _state.acquire_slot(gate):
        return False
    _flag_step(+1)
    threading.Thread(
        target=_drive_prompt, args=(gate,), name="cp_discord-approval", daemon=True
    ).start()
    return True


def _drive_prompt(gate: Gate) -> None:
    """Own the prompt from build to release.  Runs on its own thread.

    Off the backend's thread on purpose: the backend has to stay free to
    notice a Discord resolution, and a prompt that owned the waiting thread
    would make the phone wait for the terminal (AC-84).
    """
    try:
        prompt = _prompt_factory(gate)
        answer = prompt.run(on_live=lambda: _state.go_live(gate, prompt))
        if answer is not None:
            _resolve(gate, bool(answer), winner=WINNER_TERMINAL)
    except Exception:
        # A branch that cannot run is a branch ABORT, never a rejection: the
        # human may be standing in front of the gate on their phone (AC-33).
        logger.warning("cp_discord: the terminal approval branch failed", exc_info=True)
    finally:
        if _state.release_slot(gate):
            _flag_step(-1)
        _state.wake()


# -- the race --------------------------------------------------------------


def _resolve(gate: Gate, approved: bool, *, winner: str) -> bool:
    """The CAS.  ``True`` for the winner, ``False`` for everybody after.

    The ``exit()`` happens AFTER the lock is released (INV-C29 rule 2): it
    reaches into another thread's event loop, and holding a lock across that
    is how the phone ends up waiting for the terminal.
    """
    won, prompt = _state.resolve(gate, approved, winner)
    if won and prompt is not None:
        prompt.exit_with(approved)
    return won


def _await_resolution(gate: Gate) -> bool:
    """§5.2 step 3: wait for the first answer.  OUTSIDE every lock.

    Fail-closed only once BOTH branches are done without a winner (INV-C7):
    an exception in the terminal branch, or an expired Discord branch, is an
    abort -- not a rejection.
    """
    while gate.resolution is None:
        if not gate.discord_alive and not _state.owns_slot(gate):
            logger.info("cp_discord: both approval branches ended without an answer")
            return False
        _state.wait(_WAIT_TICK)

    if gate.winner == WINNER_TERMINAL:
        _tell_channel(gate, DECIDED_IN_TERMINAL)
    return bool(gate.resolution)


def _tell_channel(gate: Gate, outcome: str) -> None:
    """Close the gate in the thread.  Never raises (INV-C1)."""
    if not gate.discord_alive and outcome == DECIDED_IN_TERMINAL:
        return
    client = _active_client()
    if client is None:
        return
    try:
        client.close_gate(gate.gate_id, outcome, title=gate.title)
    except Exception:
        logger.debug("cp_discord: closing a gate in Discord failed", exc_info=True)


# --------------------------------------------------------------------------- #
# The return channel (§3.2a) -- runs on C2's listener thread
# --------------------------------------------------------------------------- #


def on_gate_resolved(
    gate_id: Any = None, decision: Any = None, discord_user_id: Any = None
) -> Optional[str]:
    """A click came back from Discord.  Returns a refusal, or ``None``.

    Authorization happens HERE, not in the broker (§3.2a): the bindings
    database belongs to the session.  And it checks the APPROVER role ONLY --
    ``authz.open_gate``/``authorize_resolution`` demand a session principal
    and a requester identity (``authz.py:228-230``, ``:295``) that a session
    started at a terminal never has, so every gate would be refused (INV-C25).

    ``check_message`` is deliberately not consulted: that tests TALKER, and
    the two axes are independent -- somebody listed only in
    ``DISCORD_APPROVERS`` must still be able to approve (AC-75).
    """
    gate = _state.get(gate_id)
    if gate is None or gate.resolved:
        return approvals_ui.ALREADY_DECIDED

    principal = _principal_for(discord_user_id)
    if principal is None or not bindings.has_role(principal, Role.APPROVER):
        # The gate stays OPEN: an outsider's click must not consume somebody
        # else's pending approval (AC-74).
        return "You may not answer this request."

    if not _resolve(gate, decision == DECISION_APPROVE, winner=WINNER_REMOTE):
        return approvals_ui.ALREADY_DECIDED
    # Off this thread: we are ON the session's inbound listener, and the
    # broker budgets 100 ms for the whole hop (INV-C17).  ``close_gate`` is a
    # socket round trip BACK to that broker -- up to 1.5 s if it just died,
    # which is exactly when a phone click matters.  Answering late would make
    # a LIVE session look unreachable and get its thread archived (AC-15).
    _in_background(_tell_channel, gate, DECIDED_IN_DISCORD)
    return None


def _principal_for(discord_user_id: Any) -> Optional[str]:
    """The principal behind a Discord id, or ``None`` for a stranger."""
    if not discord_user_id:
        return None
    try:
        return bindings.resolve_principal(constants.AUTHZ_CHANNEL, str(discord_user_id))
    except Exception:
        logger.debug("cp_discord: resolving a principal failed", exc_info=True)
        return None


def _in_background(call: Any, *args: Any) -> None:
    """Run *call* on a throwaway thread.  Never raises, never waits."""

    def run() -> None:
        try:
            call(*args)
        except Exception:
            logger.debug("cp_discord: a background gate step failed", exc_info=True)

    threading.Thread(target=run, name="cp_discord-gate-close", daemon=True).start()


# --------------------------------------------------------------------------- #
# The core flag (INV-C27) -- counted under the lock, CALLED outside it
# --------------------------------------------------------------------------- #


def _flag_step(delta: int) -> None:
    """Move the waiter count and, if the truth changed, tell the core.

    The setter is NOT a plain flag setter: it fans out synchronously into
    ``on_awaiting_user_input`` on the same thread (``command_runner.py:334-340``
    -> ``callbacks.py:519-522``), where foreign plugins can hang -- invisibly,
    because ``:339-340`` is a bare ``except: pass``.  Calling it under our
    lock would let foreign code decide our hold time (INV-C29).
    """
    transition = _state.step_flag(delta)
    if transition is not None:
        _apply_flag(*transition)


def _apply_flag(generation: int, value: bool) -> None:
    """Set the flag, unless a NEWER transition has happened meanwhile (AC-87b)."""
    if not _state.flag_is_current(generation):
        return
    try:
        _set_core_flag(value)
    except Exception:
        logger.debug("cp_discord: setting the core flag failed", exc_info=True)


def _report(method: str) -> None:
    """Tell C3 about a gate.  The core hook does NOT fire for us (INV-C24).

    Once a backend is installed, ``awaiting_user_input`` stops firing for
    shell and file approvals (``common.py:1443-1445`` returns before
    ``:1502``), so the state the phone cares about most would be invisible.
    """
    reporter = _reporter
    if reporter is None:
        from . import reporter as reporter_module

        reporter = reporter_module.active_reporter()
    if reporter is None:
        return
    try:
        getattr(reporter, method)()
    except Exception:
        logger.debug("cp_discord: reporting %s failed", method, exc_info=True)


async def on_pre_tool_call(
    tool_name: str, tool_args: Dict[str, Any], context: Any = None
) -> Optional[Dict[str, Any]]:
    """Block what the phone could see but never answer (INV-C16, AC-61).

    Only while a broker is reachable: without one this is an ordinary terminal
    session, and taking a working tool away from it would be a regression the
    plugin has no business causing (INV-C19).
    """
    if tool_name in _INTERACTIVE_TOOLS and _active_client() is not None:
        return dict(_INTERACTIVE_BLOCK)
    return None


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

_HOOKS = (("pre_tool_call", on_pre_tool_call),)


def install(config: Any = None) -> None:
    """Install the backend.  Idempotent; refuses to evict a stranger.

    ``_APPROVAL_BACKEND`` is a one-slot global with no chaining
    (``common.py:98``), so overwriting an occupant would silently switch
    another frontend off.  Ours is recognised by its sentinel -- kept as
    SELF-protection (a double load must be loud), not as a boundary against
    some other plugin: there is no other plugin any more.
    """
    global _installed, _previous_backend
    from code_puppy.tools.common import get_approval_backend, set_approval_backend

    existing = get_approval_backend()
    if existing is not None and not getattr(existing, SENTINEL, False):
        raise ApprovalError(
            "another approval backend is already installed "
            f"({getattr(existing, '__name__', existing)!r}); refusing to "
            "replace it, which would silently disable that frontend"
        )

    if not _installed:
        _previous_backend = existing
    set_approval_backend(approval_backend)
    for phase, handler in _HOOKS:
        register_callback(phase, handler)
    _installed = True
    _wire_return_channel(on_gate_resolved)
    logger.debug("cp_discord: C4 approval switch installed")


def uninstall() -> None:
    """Take C4 down.  Never raises: teardown must reach every layer."""
    global _installed, _previous_backend
    from code_puppy.tools.common import get_approval_backend, set_approval_backend

    for phase, handler in _HOOKS:
        try:
            unregister_callback(phase, handler)
        except Exception:
            logger.debug("cp_discord: unregistering %s failed", phase, exc_info=True)

    _wire_return_channel(None)
    try:
        current = get_approval_backend()
        if current is None or getattr(current, SENTINEL, False):
            set_approval_backend(_previous_backend)
    except Exception:
        logger.exception("cp_discord: could not restore the approval backend")

    _previous_backend = None
    _installed = False
    set_reporter(None)
    reset_state()


def _wire_return_channel(handler: Any) -> None:
    """Point C2's listener at us, or at nothing.  Never raises.

    This one line is what makes the Discord branch exist at all: without it
    every click is refused with ``no_handler`` and every test here still
    passes, because nothing else touches the listener.
    """
    try:
        client = _active_client()
        if client is not None:
            client.set_resolution_handler(handler)
    except Exception:
        logger.debug("cp_discord: wiring the return channel failed", exc_info=True)


__all__: Sequence[str] = (
    "DECIDED_IN_DISCORD",
    "DECIDED_IN_TERMINAL",
    "GATE_EXPIRED",
    "GATE_TIMEOUT_SECONDS",
    "MARK_EMPTY",
    "MARK_LIVE",
    "MARK_PENDING",
    "SENTINEL",
    "WINNER_REMOTE",
    "WINNER_TERMINAL",
    "ApprovalError",
    "Gate",
    "approval_backend",
    "flag_generation",
    "install",
    "live_application",
    "on_gate_resolved",
    "on_pre_tool_call",
    "open_gates",
    "prompt_mark",
    "prompt_slot",
    "reset_state",
    "set_reporter",
    "state_lock_held_by_me",
    "uninstall",
)
