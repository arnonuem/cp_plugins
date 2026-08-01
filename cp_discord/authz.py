"""Authorization rules R1-R5 for the Discord channel plugin.

A Discord channel is a remote control for an agent with shell and write access.
Five rules carry that:

* **R1** — a gate may only be resolved by someone who is an approver **and**
  triggered the run (AND, not OR).  Deliberately stricter than OpenClaw, whose
  check compares against the approver list alone: in a team channel that lets
  person B release person A's ``rm -rf`` without knowing the context.
* **R2** — a message from an unknown sender is DISCARDED, never queued.  The
  check runs before any model, history or agent structure sees the text
  (INV-4); "process first, authorize later" is already too late, because the
  text was in the context of an agent with shell rights.
* **R3** — an expired gate counts as a rejection (fail-closed, INV-3).
* **R4** — ``yolo_mode`` does not apply over Discord.  The enforcement point
  differs per path; see :func:`shell_gate_required` and
  :func:`file_gate_callback_active`.
* **R5** — sub-agents inherit the session id and the triggering principal.
  This falls out of the design: a gate takes its principal from the session
  map, so a sub-agent running in the caller's session is checked against the
  caller's principal.

Consumed by L2 (transport) via :func:`check_message` and by L4 (approval
bridge) via :func:`open_gate` / :func:`authorize_resolution`.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, Iterator, Optional, Sequence, Set, Tuple

from code_puppy.config import get_yolo_mode

from . import bindings
from .bindings import Role

GATE_TIMEOUT_SECONDS = 120.0
"""Not freely chosen — bounded by Discord.

A click must be acknowledged within 3 s (``defer()``) and an interaction token
dies after 15 minutes, which is the hard ceiling for any gate.  ACP's 600 s
would sit uncomfortably close to that ceiling; 120 s leaves room and is ample
for a deliberate yes/no.
"""


class AuthzError(RuntimeError):
    """Raised when a gate cannot be opened at all (never a silent pass)."""


class Reason(str, Enum):
    """Why a request was refused.  Every value is a rejection."""

    UNKNOWN_SENDER = "unknown_sender"
    NOT_ALLOWED = "not_allowed"
    NOT_APPROVER = "not_approver"
    NOT_REQUESTER = "not_requester"
    UNKNOWN_GATE = "unknown_gate"
    GATE_EXPIRED = "gate_expired"


@dataclass(frozen=True, slots=True)
class Decision:
    """Outcome of an authorization check.

    ``reason`` is ``None`` exactly when ``allowed`` is ``True``; callers may
    surface it verbatim to the channel.
    """

    allowed: bool
    principal: Optional[str] = None
    reason: Optional[Reason] = None


@dataclass(slots=True)
class Gate:
    """A pending approval, bound to the principal who triggered the run."""

    gate_id: str
    session_id: str
    requested_by_principal: str
    deadline: float
    title: str = ""
    created_at: float = field(default_factory=time.monotonic)


_STATE_GUARD = threading.RLock()
_GATES: Dict[str, Gate] = {}
_SESSION_PRINCIPALS: Dict[str, str] = {}

#: Sessions with a run in flight right now.  Ownership is frozen for as long
#: as an id is in here; see :func:`session_turn` for why that is a rule and
#: not an optimisation.
_RUNNING_SESSIONS: Set[str] = set()


# --------------------------------------------------------------------------- #
# Session ownership (R5) — a run belongs to the principal who started it
# --------------------------------------------------------------------------- #


def bind_session_principal(session_id: str, principal: str) -> None:
    """Record who triggered the run in *session_id*.

    Called by L2 right after :func:`check_message` succeeds and before the
    model is touched.  Sub-agents run inside the same session id and therefore
    inherit this principal without doing anything (R5).

    **Refuses to rebind a session whose run is still in flight** (R1).  A gate
    takes its requester from this map at the moment it opens, so letting a
    second talker rebind the channel mid-run would stamp THEIR name on every
    gate the first talker's run opens from then on — and if the second talker
    is an approver, they could release commands they did not trigger.  That is
    precisely the cross-approval R1 exists to prevent.  Re-binding the SAME
    principal is a no-op, so the owner sending a second message is fine.
    """
    if not session_id or not principal:
        raise AuthzError(
            f"session_id and principal must be non-empty, "
            f"got {session_id!r} / {principal!r}"
        )
    with _STATE_GUARD:
        current = _SESSION_PRINCIPALS.get(session_id)
        if session_id in _RUNNING_SESSIONS and current not in (None, principal):
            raise AuthzError(
                f"session {session_id!r} is owned by {current!r} and has a run "
                f"in flight; refusing to rebind it to {principal!r}"
            )
        _SESSION_PRINCIPALS[session_id] = principal


@contextlib.contextmanager
def session_turn(session_id: str) -> Iterator[None]:
    """Mark *session_id* as running for the duration of one turn.

    Entered by L2 INSIDE the channel lock, so "who owns this channel" and "a
    run is in flight" are decided together and cannot interleave.  Ownership
    is released with the turn: a principal left behind would let a later run
    in the same channel inherit an owner nobody authorized (S9).
    """
    with _STATE_GUARD:
        _RUNNING_SESSIONS.add(session_id)
    try:
        yield
    finally:
        with _STATE_GUARD:
            _RUNNING_SESSIONS.discard(session_id)


def session_is_running(session_id: str) -> bool:
    """Whether a turn is in flight for *session_id*."""
    with _STATE_GUARD:
        return session_id in _RUNNING_SESSIONS


def session_principal(session_id: Optional[str]) -> Optional[str]:
    """The principal owning *session_id*, or ``None`` when unattributable."""
    if not session_id:
        return None
    with _STATE_GUARD:
        return _SESSION_PRINCIPALS.get(session_id)


def release_session(session_id: str) -> None:
    """Forget a session and drop every gate still open for it (fail-closed)."""
    with _STATE_GUARD:
        _SESSION_PRINCIPALS.pop(session_id, None)
        _RUNNING_SESSIONS.discard(session_id)
        for gate_id in [
            gid for gid, gate in _GATES.items() if gate.session_id == session_id
        ]:
            del _GATES[gate_id]


def clear_state() -> None:
    """Drop all in-memory state.  Used at shutdown and between tests."""
    with _STATE_GUARD:
        _GATES.clear()
        _SESSION_PRINCIPALS.clear()
        _RUNNING_SESSIONS.clear()


# --------------------------------------------------------------------------- #
# R2 — inbound messages
# --------------------------------------------------------------------------- #


def check_message(channel: str, external_id: str) -> Decision:
    """Decide whether a message may be processed at all.

    MUST be called before the text touches the model, history or any agent
    structure (INV-4).  An unknown sender is refused here and the message is
    dropped by the caller — never buffered, never replayed later.

    Note what this function deliberately does NOT do: it never registers the
    sender.  A "remember them for next time" would turn the guard into a
    self-service enrollment.
    """
    principal = bindings.resolve_principal(channel, external_id)
    if principal is None:
        return Decision(False, None, Reason.UNKNOWN_SENDER)
    if not bindings.has_role(principal, Role.TALKER):
        return Decision(False, principal, Reason.NOT_ALLOWED)
    return Decision(True, principal)


# --------------------------------------------------------------------------- #
# R1/R3 — gates
# --------------------------------------------------------------------------- #


def open_gate(
    session_id: str,
    title: str = "",
    timeout_s: float = GATE_TIMEOUT_SECONDS,
) -> Gate:
    """Open a gate for *session_id*, bound to that session's principal.

    Raises :class:`AuthzError` when the session has no principal: an approval
    nobody can be held to is not a gate, and letting it through would be the
    exact bypass this layer exists to prevent (INV-3).
    """
    principal = session_principal(session_id)
    if principal is None:
        raise AuthzError(
            f"no principal bound to session {session_id!r}; refusing to open a gate"
        )
    gate = Gate(
        gate_id=str(uuid.uuid4()),
        session_id=session_id,
        requested_by_principal=principal,
        deadline=time.monotonic() + float(timeout_s),
        title=title,
    )
    with _STATE_GUARD:
        _GATES[gate.gate_id] = gate
    return gate


def get_gate(gate_id: str) -> Optional[Gate]:
    """The open gate with this id, or ``None`` if it was resolved or unknown."""
    with _STATE_GUARD:
        return _GATES.get(gate_id)


def close_gate(gate_id: str) -> bool:
    """Remove a gate.  Idempotent — a second click must not resolve twice."""
    with _STATE_GUARD:
        return _GATES.pop(gate_id, None) is not None


def is_expired(gate: Gate, now: Optional[float] = None) -> bool:
    """Whether *gate* has passed its own deadline."""
    return (time.monotonic() if now is None else now) >= gate.deadline


def timeout_decision(gate: Gate) -> Decision:
    """Turn an expired gate into an explicit rejection (R3).

    A gate that runs out is not "still pending" and never vanishes quietly:
    the caller reports the rejection to the channel.
    """
    close_gate(gate.gate_id)
    return Decision(False, gate.requested_by_principal, Reason.GATE_EXPIRED)


def authorize_resolution(gate_id: str, channel: str, external_id: str) -> Decision:
    """May this clicker resolve this gate?  (R1 — approver AND requester.)

    The gate is left OPEN on refusal: an unauthorized click must not consume
    somebody else's pending approval.  On success the gate stays open too —
    the caller resolves the future and then calls :func:`close_gate`, so the
    two steps cannot half-happen.
    """
    principal = bindings.resolve_principal(channel, external_id)
    if principal is None:
        return Decision(False, None, Reason.UNKNOWN_SENDER)

    gate = get_gate(gate_id)
    if gate is None:
        return Decision(False, principal, Reason.UNKNOWN_GATE)
    if is_expired(gate):
        return Decision(False, principal, Reason.GATE_EXPIRED)

    # Both conditions, in this order: "may talk" is checked nowhere here on
    # purpose — talking rights never carry approval rights (AC-18).
    if not bindings.has_role(principal, Role.APPROVER):
        return Decision(False, principal, Reason.NOT_APPROVER)
    if principal != gate.requested_by_principal:
        return Decision(False, principal, Reason.NOT_REQUESTER)
    return Decision(True, principal)


# --------------------------------------------------------------------------- #
# R4 — yolo_mode does not apply over Discord (two enforcement points)
# --------------------------------------------------------------------------- #


def shell_gate_required() -> bool:
    """Always ``True`` — the shell hook must never consult ``yolo_mode``.

        The ``run_shell_command`` hook runs independently of the core's yolo branch
    (``tools/command_runner.py:1234,1241``), so simply not checking
    ``get_yolo_mode()`` is the whole enforcement for this path (AC-39a).  The
    function exists so that intent is stated once instead of being an absence
    someone "cleans up" later.
    """
    return True


def file_gate_callback_active() -> bool:
    """Whether the plugin's own ``file_permission`` callback must raise a gate.

    The file path needs its own enforcement point: ``if get_yolo_mode(): return
    True, None`` sits BEFORE the approval backend
    (``plugins/file_permission_handler/register_callbacks.py:466``), so with
    yolo on the backend is never reached (AC-39b).

    It must be active ONLY while yolo is on.  With yolo off the core handler
    already goes through the approval backend, and since every registered
    callback runs (``callbacks.py:304-327``) an always-on callback would raise
    a SECOND gate per file operation (AC-52).  The tri-state contract carries
    this: ``False`` denies, ``True`` approves, ``None`` means no opinion
    (``tools/file_modifications.py:42-48``) — so the callback returns ``None``
    whenever this predicate is ``False``.

    Ordering is explicitly NOT relied upon: plugins load via ``iterdir()``
    (``plugins/__init__.py:84,129``) and their order is not guaranteed.
    """
    return bool(get_yolo_mode())


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def parse_identity(entry: str) -> Tuple[str, str, Optional[str]]:
    """Split ``"discord:1234=alice"`` into ``("discord", "1234", "alice")``.

    The principal part is optional (``"discord:1234"``); it is then resolved
    against an existing binding.  The channel prefix is mandatory — INV-2, and
    the reason a bare Discord id is never a key.
    """
    raw = (entry or "").strip()
    address, _, principal = raw.partition("=")
    channel, sep, external_id = address.strip().partition(":")
    if not sep or not channel.strip() or not external_id.strip():
        raise AuthzError(
            f"identity {entry!r} must look like '<channel>:<external_id>' "
            f"(optionally '=<principal>')"
        )
    return channel.strip(), external_id.strip(), principal.strip() or None


def _resolve_entries(entries: Iterable[str]) -> Set[str]:
    """Map config entries to principals, binding new ones on the way.

    An entry without an explicit principal and without an existing binding is a
    configuration ERROR, not a silent skip: guessing a principal here would
    invent an identity, and skipping would quietly drop somebody's access.
    """
    principals: Set[str] = set()
    for entry in entries:
        channel, external_id, principal = parse_identity(entry)
        if principal is None:
            principal = bindings.resolve_principal(channel, external_id)
            if principal is None:
                raise AuthzError(
                    f"identity {entry!r} has no principal and no existing "
                    f"binding; write it as '{channel}:{external_id}=<principal>'"
                )
        else:
            bindings.bind(channel, external_id, principal)
        principals.add(principal)
    return principals


def sync_from_config(
    allow_from: Sequence[str], approvers: Sequence[str]
) -> Dict[Role, Set[str]]:
    """Reconcile the two axes with the configured lists.

    ``allow_from`` and ``approvers`` are INDEPENDENT inputs — ``approvers`` is
    never derived from ``allow_from``, not even as a fallback or a "practical
    default" (AC-18).  Both directions are reconciled fully: whoever is no
    longer listed loses the role, so removing an approver from the config
    actually removes them.

    Returns the resulting role assignment, for logging by the caller.
    """
    desired: Dict[Role, Set[str]] = {
        Role.TALKER: _resolve_entries(allow_from),
        Role.APPROVER: _resolve_entries(approvers),
    }
    for role, wanted in desired.items():
        current = bindings.principals_with_role(role)
        for principal in wanted - current:
            bindings.grant(principal, role)
        for principal in current - wanted:
            bindings.revoke(principal, role)
    return desired
