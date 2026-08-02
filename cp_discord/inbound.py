"""C5 — the return channel: a Discord message becomes agent input (SPEC §6).

Two directions, one decision.  A message that arrives while an agent is
working must interrupt it (``mode="now"``, taking effect BETWEEN tool calls in
the run already in flight); a message that arrives while the session waits
must become the next turn (``mode="queue"``).  Which of the two applies is
never asked of the sender -- they are on a phone, they do not know and should
not have to.  It follows from ``run_depth``.

**INV-C8 is the rule this module exists to keep: authorization runs FIRST.**
Not first among equals -- first, before the mode is even chosen, before the
text is copied anywhere, before anything that could later be read back.  A
Discord message is untrusted input aimed at an agent with shell and write
access, so an unknown sender's text is DISCARDED, never buffered and never
replayed.  :func:`_authorize` is therefore the first statement in
:meth:`InboundRouter.handle_message`, and a test asserts that ORDER rather
than merely the outcome -- an outcome-only test would still pass if the check
moved after the model touch it is supposed to precede.

**TALKER, and only TALKER.**  ``authz.check_message`` is the axis here.  The
APPROVER axis belongs to the gate path (:mod:`.approvals`), and the two are
independent on purpose (``authz.py:288-292``: *"may talk is checked nowhere
here on purpose"*).  Somebody listed only under ``DISCORD_APPROVERS`` may
release a gate and still not steer the agent; somebody listed only under
``DISCORD_ALLOW_FROM`` may steer and still not approve.

**INV-C25 applies unchanged:** ``authz.open_gate`` /
``authorize_resolution`` / ``timeout_decision`` are NOT used.  They demand a
session principal that a session started at a terminal never has, so calling
them here would fail-closed every message from a legitimate sender.

**The listening socket is NOT here.**  C2 owns it (§3.2a,
:mod:`.client_inbound`).  This module answers only what HAPPENS to a message
that has arrived, which is why it takes its collaborators as callables: it is
testable, and complete, without a broker, a socket or a bot.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, Tuple

from . import authz, constants

logger = logging.getLogger(__name__)

#: Interrupts the run already in flight: the core's steer history processor
#: drains this queue at the next model call, which happens BETWEEN tool calls
#: (``agents/_steer_processor.py:41-47``).  This is the phone scenario --
#: writing while the agent works.
MODE_NOW = "now"

#: Held until the current run finishes, then submitted as a fresh user turn
#: (``agents/_run_signals.py:159`` via ``_runtime.py:784``).
MODE_QUEUE = "queue"

#: Rejections that are not authorization failures.  Named rather than
#: stringly-typed at the call site so a caller can tell a stranger (say
#: nothing) from an empty message (also say nothing) from a transport problem
#: (worth a log line).
REASON_EMPTY = "empty"
REASON_UNDELIVERED = "undelivered"

#: The hooks that tell us whether an agent is running.  The same pair C3
#: counts (``reporter.py:309``/``:314``) -- and counted here SEPARATELY on
#: purpose: C3's number is folded into a state where BLOCKED outranks WORKING,
#: so a session parked on an approval reports BLOCKED while a run IS in
#: flight.  Steering that run needs ``now``; reading C3's state would send it
#: to ``queue``, where it would sit until the run it was meant to interrupt
#: had already finished.
_HOOKS: Tuple[Tuple[str, str], ...] = (
    ("agent_run_start", "_on_run_start"),
    ("agent_run_end", "_on_run_end"),
    ("agent_run_cancel", "_on_run_cancel"),
    ("interactive_turn_end", "_on_run_cancel"),
    ("interactive_turn_cancel", "_on_run_cancel"),
)


@dataclass(frozen=True, slots=True)
class Delivery:
    """What became of one inbound message.

    Returned rather than raised because every outcome here is ordinary: a
    stranger writing to the channel is not an error condition, it is Tuesday.
    The caller uses this to decide what (if anything) to say back.
    """

    accepted: bool
    mode: Optional[str] = None
    principal: Optional[str] = None
    reason: Optional[str] = None


class RunDepth:
    """How many agent runs are in flight.  A COUNT, not a flag.

    Sub-agents fire the same hooks as the root run, so a boolean would fall to
    "nothing running" the moment the first sub-agent finished -- and a message
    arriving in that window would be queued for a turn that is not coming for
    a long time, while the main run carried on without it.

    Every method is safe from any thread: the hooks fire on the agent's loop,
    the reads happen on whatever thread took the Discord message.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._depth = 0

    @property
    def value(self) -> int:
        with self._lock:
            return self._depth

    def __call__(self) -> int:
        return self.value

    def entered(self) -> None:
        with self._lock:
            self._depth += 1

    def exited(self) -> None:
        with self._lock:
            self._depth = max(0, self._depth - 1)

    def reset(self) -> None:
        """A cancelled run unwinds without firing its ``end`` hooks.

        Without this the count would drift upwards over a session's life, and
        every later message would be steered ``now`` into a run that is not
        running -- where the history processor never drains it.
        """
        with self._lock:
            self._depth = 0


def _default_check(external_id: str) -> authz.Decision:
    """May *external_id* TALK to this session?  (Not: may they approve.)

    The ONLY place this module names the authorization channel, and it names
    it through :data:`constants.AUTHZ_CHANNEL` (INV-C28, AC-83b).  A second
    ``"discord"`` literal anywhere in the bridge would not fail loudly -- the
    lookup would simply miss, and because the path is fail-closed the symptom
    would be a bridge that silently ignores everyone while its database looks
    perfectly populated.

    It is a real call rather than a bare reference to ``authz.check_message``
    so that this file registers as an authorization READER in the repo-wide
    cross-check, instead of quietly falling outside it.
    """
    return authz.check_message(constants.AUTHZ_CHANNEL, external_id)


def _default_steer(text: str, mode: str) -> None:
    """Hand *text* to the core's steering queue.

    Callable from ANY thread: ``request_steer`` takes a plain
    ``threading.Lock`` and has no loop affinity
    (``messaging/pause_controller.py:251-262``).  That is what lets a message
    that arrived on a socket thread reach the agent's thread (AC-43).
    """
    from code_puppy.messaging.pause_controller import get_pause_controller

    get_pause_controller().request_steer(text, mode=mode)


class InboundRouter:
    """Turns an authorized Discord message into agent input.

    The three things it does not decide for itself are injected -- who a
    sender is, whether a run is in flight, and where a steer goes.  That keeps
    the dependency pointing one way (nothing here reaches back into C2 or C3)
    and makes the whole layer provable without a socket.
    """

    def __init__(
        self,
        *,
        run_depth: Callable[[], int],
        steer: Optional[Callable[[str, str], None]] = None,
        check: Optional[Callable[[str], authz.Decision]] = None,
    ) -> None:
        self._run_depth = run_depth
        self._steer = steer or _default_steer
        self._check = check or _default_check

    # -- the one entry point --------------------------------------------

    def handle_message(self, external_id: Any, text: Any) -> Delivery:
        """Authorize, choose a mode, deliver.  In that order (INV-C8).

        Never raises.  This runs on whatever thread took the message from
        Discord, and a session must not be harmed by anything that arrives
        there (INV-C1).
        """
        decision = self._authorize(external_id)
        if not decision.allowed:
            # Dropped on the floor: not buffered, not queued, not logged with
            # its content.  The text of an unauthorized message is exactly
            # what must never end up anywhere a model can reach.
            #
            # The PRINCIPAL still travels when there is one: a known person
            # who may not talk is a different situation from a stranger, and
            # only the caller can decide whether to say so.  That is an
            # identity we already had -- never the refused text.
            return Delivery(
                False, principal=decision.principal, reason=_reason_of(decision)
            )

        message = text.strip() if isinstance(text, str) else ""
        if not message:
            return Delivery(False, principal=decision.principal, reason=REASON_EMPTY)

        mode = MODE_NOW if self._depth() > 0 else MODE_QUEUE
        if not self._deliver(message, mode):
            return Delivery(
                False, principal=decision.principal, reason=REASON_UNDELIVERED
            )
        return Delivery(True, mode=mode, principal=decision.principal)

    # -- internals -------------------------------------------------------

    def _authorize(self, external_id: Any) -> authz.Decision:
        """Is this sender allowed to TALK?  (Not: allowed to approve.)

        Fail-closed on every unusable id and on every failure of the lookup
        itself: a database that cannot be read is a database that authorizes
        nobody.
        """
        if not isinstance(external_id, str) or not external_id.strip():
            return authz.Decision(False, None, authz.Reason.UNKNOWN_SENDER)
        try:
            return self._check(external_id.strip())
        except Exception:
            logger.debug("cp_discord: an authorization lookup failed", exc_info=True)
            return authz.Decision(False, None, authz.Reason.UNKNOWN_SENDER)

    def _depth(self) -> int:
        """The run depth, or ``0`` if it cannot be established.

        Falling back to ``0`` means falling back to ``queue``, which is the
        conservative half: a queued message waits for the next turn, while a
        ``now`` message posted with no run in flight has nothing to drain it
        until one starts.  Either way the message is KEPT -- not being sure
        whether an agent is busy is no reason to throw a person's words away.
        """
        try:
            return max(0, int(self._run_depth()))
        except Exception:
            logger.debug("cp_discord: could not read the run depth", exc_info=True)
            return 0

    def _deliver(self, text: str, mode: str) -> bool:
        try:
            self._steer(text, mode)
            return True
        except Exception:
            logger.debug("cp_discord: steering a message failed", exc_info=True)
            return False


# --------------------------------------------------------------------------- #
# Module state and the plugin surface
# --------------------------------------------------------------------------- #

_depth = RunDepth()
_router = InboundRouter(run_depth=_depth)
_installed = False


def handle_message(external_id: Any, text: Any) -> Delivery:
    """Route one inbound Discord message (the production entry point)."""
    return _router.handle_message(external_id, text)


def is_installed() -> bool:
    return _installed


def reset_state() -> None:
    """Forget the run depth.  Used by :func:`uninstall` and by tests."""
    _depth.reset()


def set_run_depth_for_test(value: int) -> None:
    """Force the run depth, reproducing a state a real run would create.

    Driving it through the hooks would mean starting an actual agent run; the
    mode choice is a pure function of this number, and this is the honest way
    to pin it.
    """
    _depth.reset()
    for _ in range(max(0, int(value))):
        _depth.entered()


def _depth_for_test() -> int:
    return _depth.value


def _on_run_start(*_args: Any, **_kwargs: Any) -> None:
    _depth.entered()


def _on_run_end(*_args: Any, **_kwargs: Any) -> None:
    _depth.exited()


def _on_run_cancel(*_args: Any, **_kwargs: Any) -> None:
    _depth.reset()


def install(config: Any = None) -> None:
    """Bring C5 up.  Returns at once (INV-C1).

    Only hooks: there is no socket and no thread here.  The messages arrive
    through C2's listener (§3.2a), and this layer is what happens to them
    afterwards.
    """
    global _installed

    from code_puppy.callbacks import register_callback

    if _installed:
        uninstall()

    reset_state()
    for phase, handler in _HOOKS:
        register_callback(phase, globals()[handler])
    _installed = True
    logger.debug("cp_discord: C5 return channel installed")


def uninstall() -> None:
    """Take C5 down.  Never raises: teardown has to reach every layer."""
    global _installed

    from code_puppy.callbacks import unregister_callback

    for phase, handler in _HOOKS:
        try:
            unregister_callback(phase, globals()[handler])
        except Exception:
            logger.debug("cp_discord: unregistering %s failed", phase, exc_info=True)
    _installed = False
    reset_state()


def _reason_of(decision: authz.Decision) -> Optional[str]:
    reason = decision.reason
    return reason.value if reason is not None else None


__all__: Sequence[str] = (
    "MODE_NOW",
    "MODE_QUEUE",
    "REASON_EMPTY",
    "REASON_UNDELIVERED",
    "Delivery",
    "InboundRouter",
    "RunDepth",
    "handle_message",
    "install",
    "is_installed",
    "reset_state",
    "set_run_depth_for_test",
    "uninstall",
)
