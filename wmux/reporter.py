"""Map code-puppy lifecycle events onto the facts wmux wants.

wmux derives the state word itself; we send FACTS -- ``awaitingHuman``,
``reason`` and an absolute ``runDepth`` -- and it computes::

    awaitingHuman  -> blocked
    runDepth > 0   -> working
    otherwise      -> idle

code-puppy is not one linear foreground run, and that drives every decision
in this module:

* **Runs are tracked as an ID SET keyed by ``group_id``**, and ``runDepth``
  is DERIVED as ``len(_live_runs)``. There is no parallel counter, because
  one structure cannot disagree with itself. Nested runs push their own
  uuid4, concurrent siblings terminate out of order, and a cancelled run
  fires cancel THEN end for the same id -- so all three terminal events are
  idempotent remove-if-present, by ID and never by position.
* **Turn boundaries clear nothing.** They are not quiescent (a ``/fork``
  runs on past them), so a blanket clear would report ``idle`` while an
  agent works. Individual entries expire instead.
* **``_live_runs`` expiry is SWEPT and PUBLISHES.** Unlike the in-flight
  tool set, whose never-read case is genuinely harmless, ``_live_runs`` is
  the source of a value already published to wmux -- an internal eviction
  that reports nothing leaves the pane wrong forever. :meth:`sweep_once` is
  the whole eviction-and-publish decision; the client merely ticks it.
* **A live run's TTL is RE-STAMPED by evidence of work** (see
  :meth:`_touch_live_runs_locked`). The sweep must distinguish "leaked" from
  "long", and the plain insert stamp cannot: it made a run that merely
  outlived the TTL indistinguishable from one whose ``agent_run_end`` was
  lost.

The blocked *reason* is inferred, because code-puppy's choke-point passes
only a bool. The in-flight tool structure is a keyed multiset with per-key
expiry -- never an integer (``/fork`` fires an unmatched ``post_tool_call``
that would zero a counter while a real tool is still in flight) and never
blanket-cleared.

The human-wait signal is a REFCOUNT, not a bool, for the same reason
``runDepth`` is: waits overlap. ``ask_user_question/terminal_ui.py:346``,
``queue_console.py:220-255`` and the menus do not share the approval locks,
and the sync and async approval locks are distinct objects
(``tools/common.py:39-54``) -- so a ``True -> True -> False`` sequence is
reachable, and a bool would report ``working`` while a human is still
parked. That inverts the one signal the whole feature exists to carry.

Lock discipline (SPEC R-12): the reporter lock is never held across a client
call or a ``sources`` call. Compute under the lock, release, then hand off.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from wmux import sources
from wmux.client import WmuxClient

logger = logging.getLogger(__name__)

#: Idle-expiry for ``_live_runs``, measured from the last EVIDENCE OF WORK
#: for that run rather than from its start (see
#: :meth:`WmuxReporter._touch_live_runs_locked`).
#:
#: **Why idle-expiry and not age.** With an age-based stamp the sweep
#: evicted any run that merely LASTED longer than the TTL -- publishing
#: ``runDepth 0`` and a false ``idle`` while the agent was demonstrably
#: still working, and doing so STICKILY: the eventual ``agent_run_end`` hits
#: the ``pop(...) is None`` early return and emits nothing, so nothing
#: corrects the pane until the next genuine transition. SPEC R-3's own
#: asymmetry argument names that as the WORSE failure ("a stale id costs a
#: spurious ``working``, a too-short TTL costs a false ``idle`` mid-run"),
#: and an hour is not a safe upper bound on a real agent run.
#:
#: Re-stamping on evidence keeps the leak containment the sweep was added
#: for -- a leaked id in a quiescent session still has no traffic to touch
#: it, so it still expires -- while making the timer mean "nothing has
#: happened for an hour", which is a defensible claim, instead of "this run
#: is old", which is not.
_RUN_TTL_S = 3600.0
#: Per-key expiry for the in-flight tool multiset. ``post_tool_call`` is NOT
#: guaranteed (a hook that blocks a tool returns before the ``finally`` that
#: fires it), so a leaked key must not pin the set for the life of the
#: process. A leak costs a wrong reason string for at most this long.
_INFLIGHT_TTL_S = 300.0

_REASON_UNKNOWN = "permission: unknown"


class WmuxReporter:
    """Thread-safe, edge-triggered bridge from callbacks to :class:`WmuxClient`."""

    def __init__(self, client: WmuxClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        #: group_id -> monotonic stamp of the last EVIDENCE OF WORK for that
        #: run (not its start). ``runDepth == len(...)``.
        self._live_runs: Dict[str, float] = {}
        #: (uid, tool_name, monotonic stamp) -- a keyed multiset.
        self._inflight: List[Tuple[int, str, float]] = []
        self._uid = itertools.count()
        #: How many human waits are currently OPEN. A refcount, not a bool:
        #: waits overlap, and a bool made ``True -> True -> False`` report
        #: ``working`` while a human was still parked (see the module
        #: docstring). ``awaitingHuman`` is ``_awaiting_depth > 0``.
        self._awaiting_depth = 0
        self._reason: Optional[str] = None
        self._last_payload: Optional[Dict[str, Any]] = None
        self._session_id: Optional[str] = None
        #: Monotonic generation stamped under the lock at compute time, so a
        #: second writer (the sweep) can never overwrite a newer payload.
        self._generation = 0

    @property
    def active(self) -> bool:
        return self._client.active

    # -- derivation (caller holds the lock) ----------------------------

    def _expire_inflight_locked(self) -> None:
        cutoff = time.monotonic() - _INFLIGHT_TTL_S
        self._inflight = [e for e in self._inflight if e[2] > cutoff]

    def _reason_locked(self) -> Optional[str]:
        """Infer the blocked reason, or ``None`` when the edge is suppressed.

        ``None`` means "a menu at an idle prompt" -- no run is active, so no
        agent can be asking. The ``runDepth`` guard on the empty case is
        load-bearing: with a run in flight, an empty set means a REAL block
        whose tool key was lost, and a vague reason beats reporting nothing.
        """
        self._expire_inflight_locked()
        count = len(self._inflight)
        if count == 1:
            return f"permission: {self._inflight[0][1]}"
        if count > 1:
            return f"permission: 1 of {count} tools"
        if self._live_runs:
            return _REASON_UNKNOWN
        return None

    def _payload_locked(self) -> Dict[str, Any]:
        awaiting = self._awaiting_depth > 0
        payload: Dict[str, Any] = {
            "awaitingHuman": awaiting,
            "runDepth": len(self._live_runs),
        }
        if awaiting and self._reason:
            payload["reason"] = self._reason
        return payload

    def _touch_live_runs_locked(self) -> None:
        """Re-stamp every live run: this process is demonstrably working.

        Called from the tool hooks, which are the plugin's only continuous
        evidence that agent work is in progress. They deliberately do NOT
        carry a run identity (``on_pre_tool_call(tool_name, tool_args,
        context)`` -- ``callbacks.py:557``), so attributing activity to a
        SPECIFIC run is not possible; touching all live runs is the honest
        reading of the only fact available, namely "this process is running
        agent tools right now".

        Over-touching is the safe direction. The worst case is a genuinely
        leaked id kept alive by an unrelated concurrent run, which costs a
        spurious ``working`` -- exactly the cost SPEC R-3 already accepts for
        the long TTL, and the cheap half of its asymmetry. The alternative,
        under-touching, is the false ``idle`` mid-run that F4 removed.
        """
        if not self._live_runs:
            return
        now = time.monotonic()
        for gid in self._live_runs:
            self._live_runs[gid] = now

    def _sync_locked(self, force: bool = False) -> Optional[Tuple[Dict[str, Any], int]]:
        """Return the payload to publish, or ``None`` when nothing changed."""
        payload = self._payload_locked()
        if not force and payload == self._last_payload:
            return None
        self._last_payload = payload
        self._generation += 1
        return payload, self._generation

    def _publish(self, job: Optional[Tuple[Dict[str, Any], int]]) -> None:
        """Hand a computed payload to the client. MUST be called unlocked.

        Every caller computes under the reporter lock, releases, then calls
        this -- the reporter lock is never held across a client call
        (SPEC R-12 direction 1).
        """
        if job is not None:
            self._client.report_state(job[0], job[1])

    def _sync(self, force: bool = False) -> None:
        with self._lock:
            job = self._sync_locked(force)
        self._publish(job)

    # -- run lifecycle -------------------------------------------------

    def on_run_start(self, group_id: Optional[str]) -> None:
        if not group_id:
            logger.debug("wmux: agent_run_start without a group_id; not tracked")
            return
        with self._lock:
            self._live_runs[group_id] = time.monotonic()
            job = self._sync_locked()
        self._publish(job)

    def on_run_terminal(self, group_id: Optional[str]) -> None:
        """Handle ``agent_run_end`` / ``agent_run_cancel`` -- both identical.

        Remove-if-present, by ID. A cancelled run fires cancel THEN end for
        the same id, and sub-agents fire cancel with no matching start, so
        this must be idempotent and tolerant of unknown ids.
        """
        if not group_id:
            return
        with self._lock:
            if self._live_runs.pop(group_id, None) is None:
                return
            job = self._sync_locked()
        self._publish(job)

    def sweep_once(self) -> None:
        """Evict expired run ids and PUBLISH if ``runDepth`` changed.

        Plain and synchronous so it is directly callable (and testable). The
        client owns only the cadence; the decision is entirely here.
        """
        with self._lock:
            if self._awaiting_depth > 0:
                # A run parked on a human emits nothing WHILE IT WAITS, so
                # silence is not evidence of death here -- it is the
                # expected behaviour of the exact state this whole feature
                # exists to surface. Evicting would publish runDepth 0, and
                # the eventual awaiting(False) would then land on `idle`
                # instead of `working`.
                #
                # wmux's own server takes the identical position: its
                # `resolveState` bounds a `working` claim by a trust window
                # but returns `blocked` unconditionally, commenting that "a
                # decaying `blocked` would silently become `idle` -- the
                # exact bug this module exists to remove".
                return
            cutoff = time.monotonic() - _RUN_TTL_S
            expired = [gid for gid, at in self._live_runs.items() if at <= cutoff]
            if not expired:
                return
            for gid in expired:
                del self._live_runs[gid]
            logger.debug("wmux: swept %d expired run id(s)", len(expired))
            job = self._sync_locked()
        self._publish(job)

    # -- tool lifecycle ------------------------------------------------

    def on_tool_start(self, tool_name: str) -> None:
        name = str(tool_name or "")
        with self._lock:
            self._expire_inflight_locked()
            self._inflight.append((next(self._uid), name, time.monotonic()))
            # A tool call is EVIDENCE that agent work is in progress, which
            # is what keeps a long run from being swept mid-flight.
            self._touch_live_runs_locked()
        self._client.report_activity(tool=name or None)

    def on_tool_complete(self, tool_name: str) -> None:
        """Remove ONE key matching this tool name, oldest first.

        A post whose name is not in the set is IGNORED -- ``/fork`` fires an
        unmatched ``post_tool_call("invoke_agent", ...)`` concurrently with a
        foreground run, and decrementing on it would suppress a real approval.
        """
        name = str(tool_name or "")
        with self._lock:
            self._expire_inflight_locked()
            for index, entry in enumerate(self._inflight):
                if entry[1] == name:
                    del self._inflight[index]
                    break
            self._touch_live_runs_locked()
        self._client.report_activity(done=True)

    # -- human waits ---------------------------------------------------

    def on_awaiting_user_input(self, awaiting: bool) -> None:
        """Track a wait on the human, inferring the reason on the True edge.

        **Waits are REFCOUNTED, because they overlap.** The approval paths do
        not share a single lock -- ``ask_user_question/terminal_ui.py:346``
        and ``queue_console.py:220-255`` are independent of the approval
        locks, and the sync and async approval locks are distinct objects
        (``tools/common.py:39-54``). With a plain bool a
        ``True -> True -> False`` sequence cleared the flag on the INNER
        completion and reported ``working`` while a human was still parked
        on the outer one -- inverting the exact signal this feature exists
        to carry.

        A suppressed edge is DISCARDED, not latched, and therefore NOT
        counted: counting it would resurrect the latching behaviour, because
        the recompute in between would see a positive depth and report
        blocked while the human is merely in a menu.

        The floor at zero mirrors the run set: an unmatched ``False`` (a
        wait whose ``True`` was suppressed, or a duplicate) must not bank a
        negative credit that swallows the NEXT genuine block.
        """
        with self._lock:
            if awaiting:
                reason = self._reason_locked()
                if reason is None:
                    logger.debug("wmux: suppressed awaiting edge (menu at idle prompt)")
                    return
                self._awaiting_depth += 1
                self._reason = reason
            else:
                self._awaiting_depth = max(0, self._awaiting_depth - 1)
                if self._awaiting_depth == 0:
                    self._reason = None
            job = self._sync_locked()
        self._publish(job)

    # -- turn boundary / metadata / session ----------------------------

    def on_startup(self) -> None:
        """Claim the surface with ONE unconditional report.

        wmux retains state per surface and a crashed predecessor leaves
        ``working`` behind with no release ever sent, so this deliberately
        bypasses the edge trigger -- the local state is already ``idle`` and
        an edge-triggered reporter would send nothing.
        """
        self._sync(force=True)

    def on_turn_end(self) -> None:
        """Refresh metadata. Live runs are NOT cleared (the boundary is not
        quiescent -- a ``/fork`` runs on past it)."""
        self._sync()
        self._emit_metadata()

    def on_turn_cancel(self) -> None:
        self._sync()

    def on_user_prompt(self) -> None:
        """Report the durable session reference, only when it changed."""
        session_id = sources.current_session_id()
        if not session_id:
            return
        with self._lock:
            changed = session_id != self._session_id
            self._session_id = session_id
        if changed:
            self._client.report_session(session_id)

    def _emit_metadata(self) -> None:
        """Compute and enqueue pane metadata. Never holds the reporter lock."""
        payload = sources.current_metadata()
        if payload:
            self._client.report_metadata(**payload)

    def on_shutdown(self) -> None:
        """Release pane authority so a dead process falls back to ``unknown``."""
        self._client.release_and_close()


__all__ = ["WmuxReporter"]
