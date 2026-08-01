"""C3 — what the session is doing, in three words and without ever waiting.

The layer answers one question for the phone: *is this session working, is it
parked on me, or is it idle?*  It derives that from facts it owns directly --
how many agent runs are in flight, and whether something is waiting for a
human -- and hands the answer to a **mailbox** that a worker thread drains.

Two properties are load-bearing, and both are invariants, not preferences:

**INV-C4 -- no handler ever blocks.**  ``awaiting_user_input`` is a synchronous
core hook on a hot path (``callbacks.py:519-522``: *"the callers are sync and
on hot paths"*).  Anything slow in there -- a socket, a file, a lock somebody
else holds -- freezes the terminal session, which is the interface this whole
feature exists to keep usable.  So every handler does the same two things:
take a short lock, drop a value in a slot.  Delivery happens elsewhere.

**Coalescing is mandatory, not a nicety.**  Discord rate-limits hard, and an
agent that makes a thousand tool calls must not make a thousand messages.  The
mailbox therefore has fixed SLOTS, not a queue: a newer value overwrites the
older one it supersedes, so memory is bounded and the channel always converges
on the latest truth rather than replaying a backlog nobody wants.

Two lanes:

* **critical** -- state edges and the report; drained first, flushed on close;
* **decorative** -- the activity line; throttled, and dropped on close.

**Where the state comes from is NOT just the hook.**  Once an approval backend
is installed -- and INV-C5/C19 require exactly that -- ``awaiting_user_input``
stops firing for shell and file approvals: ``common.py:1443-1445`` returns
before ``:1502``.  The backend therefore reports its gates itself through
:meth:`StateReporter.gate_opened` / :meth:`StateReporter.gate_closed`
(INV-C24), and because INV-C27 makes it *also* set the core flag around its
terminal branch, the same wait arrives twice.  An open gate wins and silences
the hook (AC-76a).

That distinction is worth more than deduplication: a gate is answerable from
the phone, a menu is not.  A ``BLOCKED`` the phone cannot resolve is labelled
as such (INV-C23) instead of leaving someone tapping at a message that will
never respond.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

WORKING = "working"
BLOCKED = "blocked"
IDLE = "idle"

MODE_REPORT = "report"
MODE_STREAM = "stream"

#: The status line ``report`` mode shows for the whole run (§8b).
ACTIVITY_CODING = "coding…"

#: What a gate says.  It is answerable from the phone, so it says nothing else.
BLOCKED_ON_GATE = "wartet auf deine Freigabe"

#: INV-C23: a wait the phone CANNOT resolve must say so.  ``notify=False`` is
#: not enough of a filter -- only ``model_picker_completion.py:591`` sets it,
#: while ``agent_menu.py:644``, ``autosave_menu.py:885``, ``set_menu.py:171``
#: and ``judges_menu.py:754`` run with the default.
LOCAL_ONLY_MARKER = "nur am PC beantwortbar"
BLOCKED_LOCALLY = f"wartet auf eine Eingabe — {LOCAL_ONLY_MARKER}"

#: §8b: at most one decorative edit every two seconds.
DEFAULT_MIN_INTERVAL = 2.0


@dataclass(frozen=True, slots=True)
class StateEvent:
    """One state edge.  ``remote_resolvable`` carries INV-C23's answer."""

    state: str
    message: Optional[str]
    remote_resolvable: bool


@dataclass(frozen=True, slots=True)
class ReportEvent:
    """A finished report, already split into Discord-sized chunks (§8b)."""

    chunks: Tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReleaseEvent:
    """The session is going away.  Always the last event on the wire."""


def derive_state(awaiting: bool, run_depth: int) -> str:
    """The whole state model (§4.1).

    ``BLOCKED`` beats ``WORKING`` because somebody waiting on a human is the
    more urgent fact, and ``run_depth`` is a COUNT rather than a flag because
    sub-agents fire the same hooks as the root run -- with a bool the session
    would report itself idle the moment the first sub-agent finished, while
    the main run was still going.
    """
    if awaiting:
        return BLOCKED
    if run_depth > 0:
        return WORKING
    return IDLE


class Mailbox:
    """Coalescing slots drained by one worker thread.

    Nothing here talks to Discord.  The *sink* is a plain callable supplied by
    C2; until it is wired the mailbox is a well-behaved no-op, which is what
    lets C3 come up before its transport does.
    """

    def __init__(
        self,
        sink: Optional[Callable[[Any], None]] = None,
        *,
        clock: Callable[[], float] = monotonic,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        autostart: bool = False,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._min_interval = float(min_interval)
        self._cond = threading.Condition()
        # Critical lane.
        self._report: Optional[ReportEvent] = None
        self._state: Optional[StateEvent] = None
        # Decorative lane -- throttled, discarded on close.
        self._activity: Optional[StateEvent] = None
        # Terminal slot.
        self._release: Optional[ReleaseEvent] = None
        self._closing = False
        self._last_activity_at: Optional[float] = None
        self._worker: Optional[threading.Thread] = None
        if autostart:
            self.start()

    # -- wiring ---------------------------------------------------------

    def set_sink(self, sink: Optional[Callable[[Any], None]]) -> None:
        """Point the mailbox at a delivery path (or at nothing)."""
        with self._cond:
            self._sink = sink

    def start(self) -> None:
        """Start the drain thread.  Idempotent."""
        with self._cond:
            if self._worker is not None:
                return
            self._worker = threading.Thread(
                target=self._run, name="cp_discord-reporter", daemon=True
            )
        self._worker.start()

    # -- posting (called from hot paths: lock, assign, return) ----------

    def post_report(self, event: ReportEvent) -> None:
        self._post("_report", event)

    def post_state(self, event: StateEvent) -> None:
        self._post("_state", event)

    def post_activity(self, event: StateEvent) -> None:
        self._post("_activity", event)

    def _post(self, slot: str, event: Any) -> None:
        with self._cond:
            if self._closing:
                return
            setattr(self, slot, event)
            self._cond.notify()

    # -- draining -------------------------------------------------------

    def _next_locked(self) -> Tuple[Optional[Any], Optional[float]]:
        """The next event, or how long to wait for one.  Caller holds the lock.

        Priority is report -> state -> activity -> release.  The report goes
        FIRST because it belongs to the wait point the state edge announces:
        the phone should read what happened before it is asked to decide.
        """
        if self._report is not None:
            event, self._report = self._report, None
            return event, None
        if self._state is not None:
            event, self._state = self._state, None
            return event, None
        if self._activity is not None and not self._closing:
            delay = self._throttle_delay_locked()
            if delay > 0:
                return None, delay
            event, self._activity = self._activity, None
            self._last_activity_at = self._clock()
            return event, None
        if self._closing and self._release is not None:
            event, self._release = self._release, None
            return event, None
        return None, None

    def _throttle_delay_locked(self) -> float:
        if self._min_interval <= 0 or self._last_activity_at is None:
            return 0.0
        return self._last_activity_at + self._min_interval - self._clock()

    def drain_now(self) -> None:
        """Deliver everything deliverable right now, on the calling thread.

        The worker uses the same primitive; tests use it to keep timing out of
        the picture.  Never called from a core hook -- that is INV-C4.
        """
        while True:
            with self._cond:
                event, _delay = self._next_locked()
            if event is None:
                return
            self._deliver(event)

    def _run(self) -> None:
        while True:
            with self._cond:
                event, delay = self._next_locked()
                while event is None:
                    if self._closing and self._release is None:
                        return
                    self._cond.wait(delay)
                    event, delay = self._next_locked()
            self._deliver(event)
            if isinstance(event, ReleaseEvent):
                return

    def _deliver(self, event: Any) -> None:
        """Hand one event to the sink.  A broken sink is never the agent's problem."""
        sink = self._sink
        if sink is None:
            return
        try:
            sink(event)
        except Exception:
            logger.debug("cp_discord: delivering %r failed", event, exc_info=True)

    # -- shutdown -------------------------------------------------------

    def close(self, timeout: float = 2.0) -> None:
        """Flush the critical lane, drop the decorative one, then release."""
        with self._cond:
            already_closing = self._closing
            if not already_closing:
                self._closing = True
                self._activity = None
                self._release = ReleaseEvent()
                self._cond.notify_all()
            worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout)
        elif not already_closing:
            self.drain_now()


class StateReporter:
    """Turns lifecycle hooks into state edges.  Every method returns at once."""

    def __init__(self, mailbox: Mailbox, *, mode: str = MODE_REPORT) -> None:
        self._mailbox = mailbox
        self._mode = mode if mode in (MODE_REPORT, MODE_STREAM) else MODE_REPORT
        self._lock = threading.Lock()
        self._run_depth = 0
        # Gates are counted for the same reason runs are: two can overlap
        # (a sync and an async approval), and the second must not clear the
        # first (INV-C29 lives in C4, but the count has to be right here).
        self._gate_depth = 0
        self._awaiting = False
        self._awaiting_notify = True
        self._activity: Optional[str] = None
        # IDLE is the resting state, so it is where the edge tracker starts:
        # a session that never does anything should not announce itself.
        self._last_state: Optional[str] = IDLE
        self._last_message: Optional[str] = None
        self._wait_observers: List[Callable[[], None]] = []

    # -- observation ----------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def add_wait_point_observer(self, observer: Callable[[], None]) -> None:
        """Register a callback for "work stopped, a human is needed".

        C7 hangs its report on this.  Observers run on the hot path, so they
        must be as non-blocking as the handlers themselves (INV-C4).
        """
        with self._lock:
            self._wait_observers.append(observer)

    # -- hooks ----------------------------------------------------------

    def on_run_start(self, *_args: Any, **_kwargs: Any) -> None:
        with self._lock:
            self._run_depth += 1
        self._sync()

    def on_run_end(self, *_args: Any, **_kwargs: Any) -> None:
        with self._lock:
            self._run_depth = max(0, self._run_depth - 1)
        self._sync()

    def on_run_cancel(self, *_args: Any, **_kwargs: Any) -> None:
        self._reset_to_idle()

    def on_turn_end(self, *_args: Any, **_kwargs: Any) -> None:
        self._reset_to_idle()

    def on_awaiting_user_input(
        self, awaiting: bool, *, notify: bool = True, **_kwargs: Any
    ) -> None:
        """The core hook.  INV-C4: this is the one that must never block.

        An open gate silences it (AC-76a): INV-C27 makes C4 set the core flag
        around its terminal branch -- which it must, or Ctrl+C would kill the
        whole run instead of the prompt (``_runtime.py:957,969``) -- so the
        same wait would otherwise be announced twice.
        """
        with self._lock:
            if self._gate_depth > 0:
                return
            self._awaiting = bool(awaiting)
            self._awaiting_notify = bool(notify)
        self._sync()

    def on_tool_start(self, tool_name: str) -> None:
        """Decorative only.  In ``report`` mode it changes nothing visible."""
        from . import rendering

        activity = rendering.tool_start_text(tool_name, None)
        with self._lock:
            self._activity = activity
        self._sync()

    def on_tool_end(self, *_args: Any, **_kwargs: Any) -> None:
        with self._lock:
            self._activity = None
        self._sync()

    def on_activity(self, text: str) -> None:
        """Latest decorative text (``stream`` mode's live output)."""
        with self._lock:
            self._activity = text or None
        self._sync()

    # -- the reporting interface C4 uses (INV-C24) ----------------------

    def gate_opened(self) -> None:
        """An approval gate is open and answerable from the phone."""
        with self._lock:
            self._gate_depth += 1
            # The hook cannot speak while a gate is open, so anything it left
            # behind is stale by definition.
            self._awaiting = False
            self._awaiting_notify = True
        self._sync()

    def gate_closed(self) -> None:
        with self._lock:
            self._gate_depth = max(0, self._gate_depth - 1)
        self._sync()

    # -- derivation -----------------------------------------------------

    def _state_locked(self) -> str:
        return derive_state(self._awaiting or self._gate_depth > 0, self._run_depth)

    def _message_locked(self, state: str) -> Optional[str]:
        if state == BLOCKED:
            return BLOCKED_ON_GATE if self._gate_depth > 0 else BLOCKED_LOCALLY
        if state == WORKING:
            if self._mode == MODE_STREAM:
                return self._activity or ACTIVITY_CODING
            return ACTIVITY_CODING
        return None

    def _reset_to_idle(self) -> None:
        with self._lock:
            self._run_depth = 0
            self._awaiting = False
            self._awaiting_notify = True
            self._activity = None
        self._sync()

    def _sync(self) -> None:
        """Post the current state if it actually changed.  Never blocks.

        A state edge is critical, a message-only change is decorative -- so
        colour commentary can never delay the authoritative fact behind it.
        """
        with self._lock:
            state = self._state_locked()
            if state == BLOCKED and self._gate_depth == 0 and not self._awaiting_notify:
                # A user-initiated menu (``/model``).  Reporting it would say
                # "waiting for you" about something the user just opened, and
                # touching the edge trackers would emit a bogus edge when the
                # menu closes again.
                return
            message = self._message_locked(state)
            state_changed = state != self._last_state
            if not state_changed and message == self._last_message:
                return
            wait_point = state_changed and self._last_state == WORKING
            self._last_state = state
            self._last_message = message
            event = StateEvent(state, message, self._gate_depth > 0)
            observers = tuple(self._wait_observers) if wait_point else ()

        # Outside the lock: an observer must never be able to deadlock a hook.
        # It runs BEFORE the edge is posted so the report reaches the mailbox
        # first -- the slot priority agrees, but not by accident (AC-24).
        for observer in observers:
            try:
                observer()
            except Exception:
                logger.debug("cp_discord: wait-point observer failed", exc_info=True)

        if state_changed:
            self._mailbox.post_state(event)
        else:
            self._mailbox.post_activity(event)


# --------------------------------------------------------------------------- #
# Plugin surface (C6 drives this)
# --------------------------------------------------------------------------- #

_mailbox: Optional[Mailbox] = None
_reporter: Optional[StateReporter] = None
_sink: Optional[Callable[[Any], None]] = None

_HOOKS: Tuple[Tuple[str, str], ...] = (
    ("agent_run_start", "_on_run_start"),
    ("agent_run_end", "_on_run_end"),
    ("agent_run_cancel", "_on_run_cancel"),
    ("interactive_turn_end", "_on_turn_end"),
    ("interactive_turn_cancel", "_on_turn_end"),
    ("awaiting_user_input", "_on_awaiting_user_input"),
)


def active_reporter() -> Optional[StateReporter]:
    return _reporter


def active_mailbox() -> Optional[Mailbox]:
    return _mailbox


def set_sink(sink: Optional[Callable[[Any], None]]) -> None:
    """Wire (or unwire) the delivery path.

    C2 installs before C3 does, but a sink may also arrive later -- after an
    election, say -- so this works in either order and at any time.
    """
    global _sink
    _sink = sink
    if _mailbox is not None:
        _mailbox.set_sink(sink)


def _on_run_start(*args: Any, **kwargs: Any) -> None:
    if _reporter is not None:
        _reporter.on_run_start()


def _on_run_end(*args: Any, **kwargs: Any) -> None:
    if _reporter is not None:
        _reporter.on_run_end()


def _on_run_cancel(*args: Any, **kwargs: Any) -> None:
    if _reporter is not None:
        _reporter.on_run_cancel()


def _on_turn_end(*args: Any, **kwargs: Any) -> None:
    if _reporter is not None:
        _reporter.on_turn_end()


def _on_awaiting_user_input(*args: Any, **_kwargs: Any) -> None:
    """Keep the hook's one-argument signature; read the intent separately.

    ``notify`` is not a parameter of the callback (``callbacks.py:505``); the
    core exposes it through ``command_runner`` instead, and reading it there
    is what keeps ``/model`` from claiming the session is parked on the user.
    """
    if _reporter is None:
        return
    try:
        from code_puppy.tools.command_runner import should_notify_awaiting_user_input

        notify = should_notify_awaiting_user_input()
    except Exception:  # pragma: no cover - defensive; the core always has it
        notify = True
    _reporter.on_awaiting_user_input(bool(args[0]) if args else False, notify=notify)


def install(config: Any) -> None:
    """Bring C3 up.  Called by ``register_callbacks`` in COMPONENTS order."""
    global _mailbox, _reporter

    if _reporter is not None:
        uninstall()

    from code_puppy.callbacks import register_callback

    _mailbox = Mailbox(_sink, autostart=True)
    _reporter = StateReporter(_mailbox, mode=getattr(config, "mode", MODE_REPORT))
    for phase, handler in _HOOKS:
        register_callback(phase, globals()[handler])
    logger.debug("cp_discord: C3 reporter installed (mode=%s)", _reporter._mode)


def uninstall() -> None:
    """Take C3 down.  Never raises: teardown must reach every layer."""
    global _mailbox, _reporter

    from code_puppy.callbacks import unregister_callback

    for phase, handler in _HOOKS:
        unregister_callback(phase, globals()[handler])
    _reporter = None
    if _mailbox is not None:
        _mailbox.close()
        _mailbox = None


__all__: Sequence[str] = (
    "ACTIVITY_CODING",
    "BLOCKED",
    "BLOCKED_LOCALLY",
    "BLOCKED_ON_GATE",
    "DEFAULT_MIN_INTERVAL",
    "IDLE",
    "LOCAL_ONLY_MARKER",
    "MODE_REPORT",
    "MODE_STREAM",
    "Mailbox",
    "ReleaseEvent",
    "ReportEvent",
    "StateEvent",
    "StateReporter",
    "WORKING",
    "active_mailbox",
    "active_reporter",
    "derive_state",
    "install",
    "set_sink",
    "uninstall",
)
