"""L4 approval bridge — gates are answered in Discord, never on stdin.

**Two independent paths are required, and missing the second one is the most
dangerous single defect in this plugin.**

``set_approval_backend`` covers file operations.  Shell runs straight past it:
the core only prompts when ``not running_as_subagent and sys.stdin.isatty()``
(``tools/command_runner.py:1236,1241``).  Headless both are False, so the prompt
is skipped and the command *executes*.  A bot that only sets the backend secures
files and waves ``rm -rf`` through.  Hence the ``run_shell_command`` hook — and
note it has TWO reasons to exist, not one: even *with* a TTY a sub-agent never
gets a core approval, so the hook is the only protection sub-agent shell has.

``yolo_mode`` does not apply over Discord (L3/R4).  Nothing here reads it; the
shell path enforces that by simply never asking, and the file path needs its own
callback because the core short-circuits *before* the backend
(``plugins/file_permission_handler/register_callbacks.py:466``).  That callback
abstains while the bypass is off, or every file operation would be gated twice
(``callbacks.py`` runs every registered callback).

**Sub-agents do not inherit the Discord session id.**  Measured, contradicting
SPEC-L3/R5: ``tools/subagent_invocation.py:310`` calls ``set_session_context``
with a freshly generated child id, which L1 patch A2-set mirrors into the
Discord ContextVar — so inside a sub-agent both the shell hook and the approval
backend see ``qa-expert-session-a3f2b1``, not ``discord:<channel_id>``.  Left
alone that is fail-closed (every sub-agent gate refused, AC-40 unreachable), so
:func:`on_pre_tool_call` records ``child sid -> triggering discord sid`` while
it still can, and :func:`_resolve_session` reads it back.  A plain dict, not a
ContextVar: the backend runs in an executor thread, which inherits none (INV-6).

**That map is shared, so it is the LAST resort, never the first.**  The child
id is model-chosen (``invoke_agent``'s ``session_id`` parameter, taken verbatim
for a session that already has history) and the generated form is a six-hex-
character hash, so two channels can end up using one id.  Whoever asks the map
first therefore risks posting this channel's gate into the channel that stamped
that id last — against that channel's principal.  :func:`_current_session`
prefers the context-local ``_ORIGIN_SID``, which is right per run by
construction; and where only the map exists (the executor thread), an id two
live runs both claimed resolves to ``None`` and the gate is refused (INV-3)
rather than silently routed to one of them.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from code_puppy.callbacks import register_callback, unregister_callback

from . import approvals_ui, authz, concurrency, gateway
from .session_ids import channel_id_of, is_session_id

logger = logging.getLogger(__name__)

GATE_TIMEOUT_SECONDS = authz.GATE_TIMEOUT_SECONDS
"""Kept identical to L3's, so a gate and its button die at the same moment."""

_RESULT_GRACE_SECONDS = 30.0
"""Slack for the executor-side wait, so the INNER timeout is what fires.

The blocking ``future.result()`` must outlive the gate itself; otherwise the
outer wait wins the race and the channel is left with a live gate nobody is
listening to any more.
"""

#: Re-exported: the labels are part of L4's public surface (tests click them).
APPROVE_LABEL = approvals_ui.APPROVE_LABEL
DENY_LABEL = approvals_ui.DENY_LABEL

_MAX_TRACKED_SUBAGENTS = 512
"""Bound on the child-session map — sub-agent ids are never reused."""

_INTERACTIVE_TOOLS = frozenset({"ask_user_question"})
_INTERACTIVE_BLOCK: Dict[str, Any] = {
    "blocked": True,
    "error_message": (
        "[BLOCKED] Interactive pickers cannot be shown over Discord. Ask the "
        "user your question directly in your normal text response and wait for "
        "their reply in the channel — do not call this tool."
    ),
}
"""Steering the model beats letting it read a dead stdin (SPEC-L4 §4.4)."""

_UNGATEABLE_TOOLS = frozenset({"universal_constructor"})
_UNGATEABLE_BLOCK: Dict[str, Any] = {
    "blocked": True,
    "error_message": (
        "[BLOCKED] universal_constructor is disabled over Discord. It writes "
        "model-authored Python to disk and executes it without passing either "
        "approval path, so nobody in the channel could review it. Use the "
        "ordinary file and shell tools instead — those raise a gate the user "
        "can actually answer."
    ),
}
"""Blocked, not gated — and every action, not just the writing ones.

``universal_constructor`` writes model-chosen Python with ``Path.write_text``
(``tools/universal_constructor.py:544,681``) and runs it through
``executor.submit`` (``:355``).  Neither touches ``on_file_permission`` (grep:
no hit in that file) nor the shell runner, so BOTH seams this module installs
are bypassed.  It is reachable from a plain Discord message: the default agent
has ``invoke_agent`` (``agents/agent_code_puppy.py:27``), ``helios`` has the
tool (``agents/agent_helios.py:26``) and it is on by default
(``tools/__init__.py:355``).

Why blocked rather than gated: a gate whose payload is arbitrary Python is not
reviewable in a chat message, and "approve" would mean approving something the
reader cannot evaluate.  Why every action: the discriminator is a model-chosen
``action`` string, so allow-listing ``list``/``info`` would put the model in
charge of whether code execution is gated.
"""


class ApprovalError(RuntimeError):
    """Raised when the bridge cannot be installed (never a silent bypass)."""


# --------------------------------------------------------------------------- #
# Module state
# --------------------------------------------------------------------------- #

#: The Discord session that triggered the work running right now.  Stamped on
#: the loop while the id is still visible, so it survives into the tasks a
#: sub-agent invocation spawns (context copy).
_ORIGIN_SID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "cp_discord_origin_sid", default=None
)

#: child session id -> the Discord sessions that claimed it.  A SET, not a
#: single id: the child id is model-chosen (``invoke_agent``'s ``session_id``
#: parameter, taken verbatim for an existing session —
#: ``subagent_invocation.py:290``) and the auto-generated form is a 6-character
#: sha1 slice, so two channels really can claim one id.  With more than one
#: claimant the map cannot say whose operation this is, and last-writer-wins
#: would only choose a different victim — so it resolves to ``None`` and the
#: caller refuses (INV-3).  See the module docstring for why it is a dict.
_SUBAGENT_ORIGIN: "OrderedDict[str, set]" = OrderedDict()
_ORIGIN_GUARD = threading.Lock()
"""The map is written on the gateway loop and read from an executor thread."""

_INSTALLED = False
_PREVIOUS_BACKEND: Any = None


def pending_gates() -> Dict[str, approvals_ui.PendingGate]:
    """The gates currently awaiting a click.  Read-only view for tests/L5."""
    return approvals_ui.pending_gates()


def subagent_origins() -> Dict[str, set]:
    """Child session id -> claiming Discord sessions.  Read-only copy."""
    with _ORIGIN_GUARD:
        return {child: set(origins) for child, origins in _SUBAGENT_ORIGIN.items()}


def reset_state() -> None:
    """Drop every in-memory trace.  Used at teardown and between tests."""
    approvals_ui.reset_state()
    with _ORIGIN_GUARD:
        _SUBAGENT_ORIGIN.clear()


def release_session(session_id: str) -> None:
    """Forget the child sessions *session_id*'s run claimed.

    Called when a turn ends, alongside L1's and L3's own release.  Without it a
    finished run keeps its claim forever, and a later channel reusing the same
    child id would be refused as ambiguous for the rest of the process.
    """
    with _ORIGIN_GUARD:
        for child in [
            child
            for child, origins in _SUBAGENT_ORIGIN.items()
            if session_id in origins
        ]:
            origins = _SUBAGENT_ORIGIN[child]
            origins.discard(session_id)
            if not origins:
                del _SUBAGENT_ORIGIN[child]


# --------------------------------------------------------------------------- #
# Session resolution (INV-1, and the sub-agent boundary)
# --------------------------------------------------------------------------- #


#: The channel behind an INV-1 session id, or ``None`` if it is not one.
#:
#: Deliberately strict, and deliberately the SAME callable L5 routes output
#: with (:mod:`.session_ids`).  When L1 has already rolled back, the core calls
#: this backend positionally with three arguments and ``sid`` silently receives
#: the TITLE string (measured, INV-7 clause 5) — ``"Shell Command"`` must fail
#: this check, which is what turns that misbinding into a refusal instead of a
#: misrouted gate.
_channel_id_of = channel_id_of
_is_discord_session = is_session_id


def _remember_origin(child_sid: str, origin_sid: str) -> None:
    """Record that *child_sid* belongs to the run *origin_sid* started.

    Every run adds its own claim; a claim is never skipped because the id is
    already known.  Two live claimants make the id unattributable rather than
    silently rebinding it to whoever stamped last.
    """
    with _ORIGIN_GUARD:
        origins = _SUBAGENT_ORIGIN.get(child_sid)
        if origins is None:
            origins = set()
            _SUBAGENT_ORIGIN[child_sid] = origins
        origins.add(origin_sid)
        _SUBAGENT_ORIGIN.move_to_end(child_sid)
        while len(_SUBAGENT_ORIGIN) > _MAX_TRACKED_SUBAGENTS:
            _SUBAGENT_ORIGIN.popitem(last=False)


def _origin_of(child_sid: str) -> Optional[str]:
    """The single Discord session that claimed *child_sid*, else ``None``."""
    with _ORIGIN_GUARD:
        origins = _SUBAGENT_ORIGIN.get(child_sid)
        if origins is None or len(origins) != 1:
            return None
        return next(iter(origins))


def _resolve_session(session_id: Any) -> Optional[str]:
    """Map any session id onto the Discord session that must answer for it.

    ``None`` means "not attributable" — the caller then refuses (INV-3).  A gate
    nobody can be held to is not a gate.

    Argument-driven on purpose: it answers "who answers for THIS id", so a
    stray non-session argument (INV-7 clause 5's title string) can never be
    resolved out of ambient state.  Callers that mean "who answers for the code
    running right now" use :func:`_current_session`.
    """
    if _is_discord_session(session_id):
        return session_id
    if isinstance(session_id, str) and session_id:
        return _origin_of(session_id)
    return None


def _current_session() -> Optional[str]:
    """The Discord session for the code running right now, or ``None``.

    Only valid on the gateway loop; the executor-side backend is handed its id
    by L1 patch C instead (INV-6/INV-7).

    Order is load-bearing.  An active ``session_scope`` is authoritative, then
    the ContextVar — which is context-LOCAL and therefore right for this run
    even when another channel's sub-agent happens to use the same child id.
    The shared map is the last resort only; asking it first routes this
    channel's gate into whichever channel stamped that id last.
    """
    current = concurrency.current_session_id()
    if _is_discord_session(current):
        return current
    origin = _ORIGIN_SID.get()
    if origin is not None:
        return origin
    return _resolve_session(current)


# --------------------------------------------------------------------------- #
# Posting and resolving a gate — runs ON the gateway loop
# --------------------------------------------------------------------------- #


async def _request_approval(
    session_id: str, title: str, message: str, preview: Optional[str] = None
) -> bool:
    """Post a gate to *session_id*'s channel and wait for the verdict.

    Runs on the gateway loop.  Returns ``False`` on every failure path (INV-3):
    unroutable session, unknown principal, Discord error, timeout.  The widget
    itself lives in :mod:`.approvals_ui`; this function owns only the decision
    of whether a gate may be opened and what its outcome means.
    """
    channel_id = _channel_id_of(session_id)
    if channel_id is None:
        logger.warning("Discord: refusing an approval for session %r", session_id)
        return False

    try:
        gate = authz.open_gate(session_id, title=title, timeout_s=GATE_TIMEOUT_SECONDS)
    except authz.AuthzError:
        logger.warning("Discord: no principal owns %s; refusing the gate", session_id)
        return False

    try:
        pending = await approvals_ui.post_gate(
            channel_id, gate, title, message, preview
        )
    except Exception:
        logger.exception("Discord: could not post the approval gate; denying")
        authz.close_gate(gate.gate_id)
        return False

    try:
        return await asyncio.wait_for(pending.future, GATE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        # R3: an expired gate is an explicit rejection and is reported as one.
        # It never just disappears from the channel.
        authz.close_gate(gate.gate_id)
        await approvals_ui.finish_message(
            pending, f"**EXPIRED — treated as DENIED** — {title}"
        )
        return False
    except asyncio.CancelledError:
        # A cancellation is NOT a timeout, and the difference is the whole
        # point of ``/cancel``.  Returning ``False`` here would swallow it:
        # ``wait_for`` delivers the ``CancelledError`` to us, and once it is
        # caught nothing is pending any more — so ``cancel_channel`` would
        # merely deny this one gate while the run carried on to its next tool
        # and its next gate, exactly when the abort matters most (an open
        # ``rm -rf`` gate).  It belongs to whoever cancelled us, so it is
        # re-raised; the channel is told the truth rather than "EXPIRED".
        authz.close_gate(gate.gate_id)
        await approvals_ui.finish_message(pending, f"**CANCELLED** — {title}")
        raise
    except Exception:
        logger.exception("Discord: approval gate failed; denying")
        return False
    finally:
        approvals_ui.forget(gate.gate_id)


# --------------------------------------------------------------------------- #
# Path 1 — the approval backend (file operations), called off-loop
# --------------------------------------------------------------------------- #


def approval_backend(
    sid: Optional[str] = None,
    title: str = "",
    message: str = "",
    preview: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Bridge a core approval onto Discord.  Synchronous, blocks its thread.

    Four arguments by INV-7 clause 1; L1 patch C binds ``sid`` on the loop and
    hands the core the 3-arg shape it expects.  ``sid`` carries a default so a
    stray 3-arg call degrades into a refusal rather than a ``TypeError`` — the
    refusal then comes from the unroutable id, not from a ``None`` check
    (measured, clause 5).

    Feedback is always ``None``: two buttons are a yes/no, not a text channel.
    """
    loop = gateway.get_loop()
    if loop is None:
        logger.warning("Discord: no gateway loop; denying an approval")
        return False, None

    # Blocking on our own loop would deadlock the gateway: nothing could ever
    # service the click we are waiting for.
    try:
        running: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        logger.error("Discord: approval backend hit on the gateway loop; denying")
        return False, None

    session_id = _resolve_session(sid)
    if session_id is None:
        logger.warning("Discord: approval for unattributable session %r", sid)
        return False, None

    future = asyncio.run_coroutine_threadsafe(
        _request_approval(session_id, title, message, preview), loop
    )
    try:
        allowed = future.result(GATE_TIMEOUT_SECONDS + _RESULT_GRACE_SECONDS)
    except Exception:
        # Includes TimeoutError. Abandon the request so it cannot linger on the
        # loop with nobody listening.
        future.cancel()
        logger.exception("Discord: approval bridge failed; denying")
        return False, None
    return bool(allowed), None


approval_backend._cp_discord = True  # INV-7 clause 1: patch C only wraps ours


# --------------------------------------------------------------------------- #
# Path 2 — the shell hook.  Without this, shell runs UNCHECKED.
# --------------------------------------------------------------------------- #


async def on_run_shell_command(
    context: Any, command: str, cwd: Optional[str] = None, timeout: int = 60
) -> Optional[Dict[str, Any]]:
    """Gate every shell command through Discord.

    ``None`` allows, a ``blocked`` dict refuses.  This never consults the
    approval-bypass setting — that omission IS the enforcement for this path
    (L3/R4), because the core's own bypass branch is upstream of us and is
    simply not reached headless.

    **The hook owns its own failures.**  ``_trigger_callbacks`` catches every
    exception and appends ``None`` (``callbacks.py:321-326``), and the runner
    reads anything that is not a ``blocked`` dict as permission to execute
    (``command_runner.py:1099-1112``).  So an escaping raise is not a crash
    here — it is an *ungated command*.  Letting the dispatcher "handle" it
    would make this seam fail OPEN, which is the one thing it must never do.
    """
    try:
        session_id = _current_session()
        if session_id is None:
            return _blocked(
                "No Discord session could be attributed to this command, so no "
                "one could approve it."
            )
        # Escaped, not interpolated: a backtick in the command would otherwise
        # close the code span and let the rest render as markdown — forging the
        # very message the human bases the decision on.
        rendered = approvals_ui.inline_code(command)
        approved = await _request_approval(session_id, "Shell Command", rendered)
    except Exception:
        # Deliberately NOT ``BaseException``: ``CancelledError`` must keep
        # propagating, or /cancel would be swallowed here instead of at the
        # gate — the same defect one layer up.
        logger.exception("Discord: the shell gate failed; blocking the command")
        return _blocked("The Discord approval gate failed, so nothing was run.")
    if approved:
        return None
    return _blocked("The command was denied in Discord.")


def _blocked(reason: str) -> Dict[str, Any]:
    return {
        "blocked": True,
        "error_message": f"Command blocked: {reason}",
        "reasoning": reason,
    }


# --------------------------------------------------------------------------- #
# Path 2b — the file callback, for when the core short-circuits
# --------------------------------------------------------------------------- #


async def on_file_permission(
    context: Any,
    file_path: str,
    operation: str,
    preview: Optional[str] = None,
    message_group: Optional[str] = None,
    operation_data: Any = None,
) -> Optional[bool]:
    """Raise a file gate only when the core would otherwise skip its own.

    Tri-state (``tools/file_modifications.py:42-48``): ``False`` denies, ``True``
    approves, ``None`` is no opinion.  Abstaining is the normal case — the
    backend above already gates file operations, and since every registered
    callback runs, an always-on answer here would ask the user twice for one
    operation (AC-52).

    That tri-state is also why this hook guards itself, exactly as the shell
    hook does: ``_permission_denied`` denies only on an explicit ``False``
    (``file_modifications.py:42-48``), so the ``None`` the dispatcher appends
    for a raising callback reads as "no opinion" — and while yolo is on this
    callback is the ONLY gate on the file path (``authz.file_gate_callback_
    active``), so failing open here means an ungated write.
    """
    try:
        if not authz.file_gate_callback_active():
            return None
        session_id = _current_session()
        if session_id is None:
            return False
        return await _request_approval(
            session_id,
            "File Operation",
            f"{operation} {approvals_ui.inline_code(file_path)}",
            preview,
        )
    except Exception:
        # Again not ``BaseException``: a cancellation belongs to the caller.
        logger.exception("Discord: the file gate failed; denying the operation")
        return False


# --------------------------------------------------------------------------- #
# Path 3 — tool gating and the sub-agent session bridge
# --------------------------------------------------------------------------- #


def _stamp_origin() -> None:
    """Learn who this work belongs to, while the loop still knows.

    On a normal turn the current id IS the Discord session, so we record it.
    Inside a sub-agent it is the child id instead — then we bind that child to
    the origin we inherited through the context copy, which is what lets the
    sub-agent's gates reach the triggering channel (AC-40).
    """
    try:
        current = concurrency.current_session_id()
        if _is_discord_session(current):
            _ORIGIN_SID.set(current)
            return
        origin = _ORIGIN_SID.get()
        if origin is not None and isinstance(current, str) and current:
            _remember_origin(current, origin)
    except Exception:
        logger.debug("Discord: could not stamp the session origin", exc_info=True)


async def on_pre_tool_call(
    tool_name: str, tool_args: Dict[str, Any], context: Any = None
) -> Optional[Dict[str, Any]]:
    """Block what cannot be answered or gated, and keep the bridge current.

    Two refusals, for two different reasons.  An interactive picker has no
    surface over Discord at all; ``universal_constructor`` has one, but it
    reaches neither approval seam, so allowing it would mean arbitrary code
    execution with no gate anywhere (see :data:`_UNGATEABLE_BLOCK`).
    """
    _stamp_origin()
    if tool_name in _INTERACTIVE_TOOLS:
        return dict(_INTERACTIVE_BLOCK)
    if tool_name in _UNGATEABLE_TOOLS:
        logger.warning("Discord: refusing ungateable tool %r", tool_name)
        return dict(_UNGATEABLE_BLOCK)
    return None


# --------------------------------------------------------------------------- #
# Lifecycle (INV-5 / INV-7 clause 5)
# --------------------------------------------------------------------------- #

_HOOKS = (
    ("run_shell_command", on_run_shell_command),
    ("file_permission", on_file_permission),
    ("pre_tool_call", on_pre_tool_call),
)


def install() -> None:
    """Install both approval paths.  Idempotent; refuses to evict a stranger.

    ``_APPROVAL_BACKEND`` is a one-slot global with no chaining
    (``tools/common.py:98``).  Overwriting an occupied slot would silently
    switch another frontend off, so an unknown occupant is a loud failure.  Our
    own backend is recognised by its sentinel — including through L1 patch C's
    closure, which inherits it (INV-7 clause 1); without that, a re-install
    could never succeed.
    """
    global _INSTALLED, _PREVIOUS_BACKEND
    from code_puppy.tools.common import get_approval_backend, set_approval_backend

    existing = get_approval_backend()
    if existing is not None and not getattr(existing, concurrency.SENTINEL, False):
        raise ApprovalError(
            "another approval backend is already installed "
            f"({getattr(existing, '__name__', existing)!r}); refusing to "
            "replace it, which would silently disable that frontend"
        )

    if not _INSTALLED:
        _PREVIOUS_BACKEND = existing
    set_approval_backend(approval_backend)

    for phase, handler in _HOOKS:
        register_callback(phase, handler)  # duplicate registration is a no-op
    _INSTALLED = True


def uninstall() -> None:
    """Restore the previous state.  MUST run before L1 rolls patch C back.

    Otherwise ``_APPROVAL_BACKEND`` still holds our 4-arg callable while the
    core calls it with three again — a misbinding that would route gates by a
    title string (INV-7 clause 5).
    """
    global _INSTALLED, _PREVIOUS_BACKEND
    from code_puppy.tools.common import get_approval_backend, set_approval_backend

    for phase, handler in _HOOKS:
        try:
            unregister_callback(phase, handler)
        except Exception:
            logger.debug("Discord: unregistering %s failed", phase, exc_info=True)

    try:
        current = get_approval_backend()
        if current is None or getattr(current, concurrency.SENTINEL, False):
            set_approval_backend(_PREVIOUS_BACKEND)
    except Exception:
        logger.exception("Discord: could not restore the approval backend")

    _PREVIOUS_BACKEND = None
    _INSTALLED = False
    reset_state()
