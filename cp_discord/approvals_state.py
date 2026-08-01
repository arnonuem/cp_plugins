"""C4a — the shared state of the approval switch, and the rules around it.

Split out of :mod:`.approvals` along the line INV-C29 already draws: that
module decides WHAT happens on an approval, this one owns the handful of
process-wide values two threads fight over and the discipline that keeps them
consistent.  Everything here is short, guarded and non-blocking on purpose --
the moment something in this file waits, the deadlock INV-C29 warns about is
back.

Four values, and each is a singleton because the thing it stands for is:

* the **gate registry** with its CAS -- who answered, once;
* the **prompt mark**, three-valued (``EMPTY -> PENDING -> LIVE``), because a
  bool cannot express \"an Application exists but is not operable yet\", and an
  ``exit()`` in that state EVAPORATES against a ``DummyApplication``;
* the **prompt slot**, a single ownership token for stdin -- two
  ``Application``s on one terminal is the collision INV-C29 lists first;
* the **awaiting-user-input counter** with a GENERATION, because the core's
  flag is process-global and idempotent is not the same as order-independent.

**The lock is for TRANSITIONS, never for waits.**  ``_state_lock`` is taken,
a few fields move, it is released.  Never across a prompt, never across a
socket, never around ``set_awaiting_user_input`` -- that one fans out
synchronously into foreign plugins (``command_runner.py:334-340`` ->
``callbacks.py:519-522``), where a hang would be invisible behind a bare
``except: pass`` and would set OUR hold time.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: The three-valued prompt mark (§5.2a).
MARK_EMPTY = "empty"
MARK_PENDING = "pending"
MARK_LIVE = "live"

#: Who resolved a gate.  Named, not bare strings: the value decides whether
#: ``exit()`` is called on a prompt, so a typo would be a silent hang.
WINNER_TERMINAL = "terminal"
WINNER_REMOTE = "remote"


@dataclass
class Gate:
    """One approval, and the CAS that decides who answered it.

    ``resolution`` is a single ``Optional[bool]`` under the registry lock: two
    branches race for it, and the loser has to be able to tell that it lost
    (INV-C7).
    """

    gate_id: str
    title: str
    message: str
    preview: Optional[str] = None
    resolution: Optional[bool] = None
    winner: str = ""
    discord_alive: bool = True
    prompt: Any = None
    timer: Any = field(default=None, repr=False)

    @property
    def resolved(self) -> bool:
        return self.resolution is not None


class SwitchState:
    """The gates, the mark, the slot and the flag -- with one lock over them.

    A single object rather than four module globals because they are decided
    TOGETHER: whether a resolution may call ``exit()`` depends on the mark,
    which depends on whether the slot was acquired, which decides whether the
    flag counter moved.  Spread across four independent guards, every one of
    those pairs is a race.
    """

    def __init__(self) -> None:
        # An RLock, and a Condition over the SAME lock: the backend waits on
        # the condition outside every critical section, and a resolution has
        # to be able to notify it without taking a second lock in between.
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._depth = threading.local()

        self._gates: Dict[str, Gate] = {}
        self._mark = MARK_EMPTY
        self._slot: Optional[str] = None
        self._live_app: Any = None
        self._flag_count = 0
        self._flag_gen = 0

    # -- the lock itself ------------------------------------------------

    def __enter__(self) -> "SwitchState":
        self._lock.acquire()
        self._depth.value = getattr(self._depth, "value", 0) + 1
        return self

    def __exit__(self, *_exc) -> bool:
        self._depth.value -= 1
        self._lock.release()
        return False

    def held_here(self) -> bool:
        """Whether THIS thread is inside the lock (AC-87c's evidence)."""
        return getattr(self._depth, "value", 0) > 0

    def wait(self, timeout: float) -> None:
        with self._changed:
            self._changed.wait(timeout)

    def wake(self) -> None:
        with self._changed:
            self._changed.notify_all()

    # -- gates ----------------------------------------------------------

    def add(self, gate: Gate) -> None:
        with self:
            self._gates[gate.gate_id] = gate

    def get(self, gate_id: Any) -> Optional[Gate]:
        with self:
            return self._gates.get(str(gate_id))

    def drop(self, gate_id: str) -> None:
        with self:
            gate = self._gates.pop(gate_id, None)
        if gate is not None:
            cancel_timer(gate)

    def open_ids(self) -> List[str]:
        with self:
            return [
                gate_id for gate_id, gate in self._gates.items() if not gate.resolved
            ]

    def resolve(self, gate: Gate, approved: bool, winner: str) -> Tuple[bool, Any]:
        """The CAS.  Returns ``(won, prompt_to_exit)``.

        The prompt is RETURNED rather than exited here, because ``exit()``
        reaches into another thread's event loop and INV-C29 rule 2 forbids
        holding the lock across that.  It is handed out only at ``LIVE``: at
        ``PENDING`` or ``EMPTY`` there is no operable Application, and the
        call would evaporate.
        """
        with self._changed:
            if gate.resolved:
                return False, None
            gate.resolution = approved
            gate.winner = winner
            prompt = gate.prompt if self._mark == MARK_LIVE else None
            self._changed.notify_all()
        return True, prompt if winner != WINNER_TERMINAL else None

    def expire(self, gate: Gate) -> bool:
        """End the DISCORD branch only (INV-C10).  ``True`` if it was alive.

        Explicitly does NOT touch mark or slot: the terminal prompt is still
        open (AC-49), and giving the slot back here would let a concurrent
        approval put a second Application on the same stdin.
        """
        with self:
            if gate.resolved or not gate.discord_alive:
                return False
            gate.discord_alive = False
            return True

    # -- the prompt slot and the mark ------------------------------------

    def acquire_slot(self, gate: Gate) -> bool:
        """Take stdin for *gate* and move the mark to PENDING.

        Refused when the gate is already resolved (§5.2a step 2: then no
        prompt may be built at all) or when somebody else owns the slot
        (INV-C29 rule 4).
        """
        with self:
            if gate.resolved or self._slot is not None:
                return False
            self._slot = gate.gate_id
            self._mark = MARK_PENDING
            return True

    def go_live(self, gate: Gate, prompt: Any) -> bool:
        """§5.2a steps 3a AND 3b, in ONE critical section.

        Splitting them reopens the window the three-valued mark exists to
        close: a resolution landing in between would see a mark that is not
        yet LIVE, skip its ``exit()``, and leave an operable prompt in front
        of an already-answered gate -- with no timeout (INV-C10).
        """
        with self:
            if gate.resolved:
                return False
            self._mark = MARK_LIVE
            self._live_app = prompt
            gate.prompt = prompt
            return True

    def release_slot(self, gate: Gate) -> bool:
        """§5.2a step 5.  ``True`` when this call actually gave it back.

        Owner-checked: only the gate that took the slot may return it, or a
        late branch would hand away a prompt that belongs to the NEXT gate.
        """
        with self:
            if self._slot != gate.gate_id:
                return False
            self._slot = None
            self._mark = MARK_EMPTY
            self._live_app = None
            gate.prompt = None
            return True

    def owns_slot(self, gate: Gate) -> bool:
        with self:
            return self._slot == gate.gate_id

    # -- observation ----------------------------------------------------

    @property
    def mark(self) -> str:
        with self:
            return self._mark

    @property
    def slot(self) -> Optional[str]:
        with self:
            return self._slot

    @property
    def live_app(self) -> Any:
        with self:
            return self._live_app

    @property
    def flag_generation(self) -> int:
        with self:
            return self._flag_gen

    # -- the core flag counter (INV-C27) --------------------------------

    def step_flag(self, delta: int) -> Optional[Tuple[int, bool]]:
        """Move the waiter count.  Returns ``(generation, target)`` or ``None``.

        ``None`` means the truth did not change and nothing must be called.
        The caller then does the calling -- OUTSIDE this lock, which is the
        whole reason this returns instead of acting.
        """
        with self:
            before = self._flag_count > 0
            self._flag_count = max(0, self._flag_count + delta)
            target = self._flag_count > 0
            if target == before:
                return None
            self._flag_gen += 1
            return self._flag_gen, target

    def flag_is_current(self, generation: int) -> bool:
        """Whether *generation* is still the newest transition (AC-87b).

        Idempotent is not order-independent: without this an older branch's
        ``False`` can land after a newer branch's ``True`` and clear the
        SIGINT guard while a prompt is live -- a Ctrl+C would then abort the
        whole agent run (``_runtime.py:957``, ``:969``).
        """
        with self:
            return generation == self._flag_gen

    # -- teardown -------------------------------------------------------

    def reset(self) -> None:
        with self:
            for gate in self._gates.values():
                cancel_timer(gate)
            self._gates.clear()
            self._mark = MARK_EMPTY
            self._slot = None
            self._live_app = None
            self._flag_count = 0
            self._flag_gen = 0


def cancel_timer(gate: Gate) -> None:
    """Stop a gate's expiry timer.  Idempotent; never raises."""
    timer, gate.timer = gate.timer, None
    if timer is not None:
        try:
            timer.cancel()
        except Exception:  # pragma: no cover - defensive
            logger.debug("cp_discord: cancelling a gate timer failed", exc_info=True)


__all__: Sequence[str] = (
    "MARK_EMPTY",
    "MARK_LIVE",
    "MARK_PENDING",
    "WINNER_REMOTE",
    "WINNER_TERMINAL",
    "Gate",
    "SwitchState",
    "cancel_timer",
)
