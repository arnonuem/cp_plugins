"""Reporter state machine: run tracking, reason inference, hygiene.

Driven through a hand-rolled ``FakeClient`` recorder -- no pipe, no thread.
Anything that needs the REAL client (lanes, ``_cond``, the critical slot,
``_closing``, the generation high-water mark) lives in
``test_wmux_client.py`` instead; bolting those onto the recorder would be
testing the recorder.

Covers AC-10..AC-16 (AC-14 retired), AC-18..AC-24d, AC-33, AC-33b, AC-34,
AC-35 and AC-36.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest
from conftest import acquirable_from_another_thread

from wmux import reporter as rp
from wmux.reporter import WmuxReporter


class FakeClient:
    """Records report calls instead of touching a pipe."""

    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.states: List[Tuple[Dict[str, Any], int]] = []
        self.sessions: List[Optional[str]] = []
        self.metadata: List[Dict[str, Any]] = []
        self.activity: List[Dict[str, Any]] = []
        self.released = 0

    def report_state(self, params, generation):
        self.states.append((dict(params), generation))

    def report_session(self, session_id):
        self.sessions.append(session_id)

    def report_metadata(self, model=None, tokens=None, context_pct=None):
        self.metadata.append(
            {"model": model, "tokens": tokens, "context_pct": context_pct}
        )

    def report_activity(self, tool=None, done=False):
        self.activity.append({"tool": tool, "done": done})

    def release_and_close(self, timeout_s=1.0):
        self.released += 1


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def reporter(fake: FakeClient) -> WmuxReporter:
    return WmuxReporter(fake)


def depths(fake: FakeClient) -> List[int]:
    return [p["runDepth"] for p, _ in fake.states]


def last(fake: FakeClient) -> Dict[str, Any]:
    return fake.states[-1][0]


# --- run tracking (AC-10..AC-16) -------------------------------------------


def test_ac10_run_start_reports_absolute_depth_one(reporter, fake):
    reporter.on_run_start("g1")
    assert last(fake) == {"awaitingHuman": False, "runDepth": 1}


def test_ac11_nested_runs_refcount(reporter, fake):
    # A real production path: agent_run_start at _runtime.py:911 is NOT
    # guarded by is_nested_run, so shell_safety's nested run_with_mcp fires it.
    reporter.on_run_start("g1")
    reporter.on_run_start("g2")
    reporter.on_run_terminal("g2")
    assert last(fake)["runDepth"] == 1
    reporter.on_run_terminal("g1")
    assert last(fake)["runDepth"] == 0


def test_ac12_awaiting_returns_to_working_not_idle(reporter, fake):
    reporter.on_run_start("g1")
    reporter.on_tool_start("run_shell_command")
    reporter.on_awaiting_user_input(True)
    assert last(fake)["awaitingHuman"] is True
    reporter.on_awaiting_user_input(False)
    assert last(fake) == {"awaitingHuman": False, "runDepth": 1}


def test_ac13_awaiting_false_at_depth_zero_is_idle(reporter, fake):
    reporter.on_run_start("g1")
    reporter.on_tool_start("run_shell_command")
    reporter.on_awaiting_user_input(True)
    reporter.on_run_terminal("g1")
    reporter.on_awaiting_user_input(False)
    assert last(fake) == {"awaitingHuman": False, "runDepth": 0}


def test_ac14b_cancel_then_end_for_same_id_never_goes_negative(reporter, fake):
    # The real runtime sequence: the except* at _runtime.py:866 does not
    # re-raise, so the finally at :1111 fires end too.
    reporter.on_run_start("g1")
    reporter.on_run_terminal("g1")  # cancel
    before = len(fake.states)
    reporter.on_run_terminal("g1")  # end -- harmless no-op
    assert last(fake)["runDepth"] == 0
    assert len(fake.states) == before
    assert all(p["runDepth"] >= 0 for p, _ in fake.states)


def test_ac14c_cancel_with_unknown_id_changes_nothing(reporter, fake):
    reporter.on_run_start("foreground")
    before = list(fake.states)
    reporter.on_run_terminal("some-forked-subagent")
    assert fake.states == before
    assert last(fake)["runDepth"] == 1


def test_ac14d_nested_run_does_not_lose_the_outer_id(reporter, fake):
    reporter.on_run_start("outer")
    reporter.on_run_start("inner")
    reporter.on_run_terminal("inner")
    assert last(fake)["runDepth"] == 1
    reporter.on_run_terminal("outer")  # cancel
    reporter.on_run_terminal("outer")  # end
    assert last(fake)["runDepth"] == 0


def test_ac14f_siblings_terminate_out_of_order(reporter, fake):
    # Discord runs one task per message under a PER-CHANNEL lock, so
    # end-ordering is not LIFO -- removal must be by id, never by position.
    reporter.on_run_start("A")
    reporter.on_run_start("B")
    reporter.on_run_terminal("A")
    assert last(fake)["runDepth"] == 1
    assert set(reporter._live_runs) == {"B"}
    reporter.on_run_terminal("B")
    assert last(fake)["runDepth"] == 0


def test_ac14f_late_cancel_for_already_removed_sibling_is_inert(reporter, fake):
    reporter.on_run_start("A")
    reporter.on_run_start("B")
    reporter.on_run_terminal("A")
    before = list(fake.states)
    reporter.on_run_terminal("A")
    assert fake.states == before
    assert set(reporter._live_runs) == {"B"}


def test_ac14g_run_depth_is_derived_never_a_parallel_counter(reporter, fake):
    events = [
        ("start", "A"),
        ("start", "B"),
        ("start", "A"),  # duplicate start
        ("term", "ghost"),
        ("term", "A"),
        ("term", "A"),  # unknown + duplicate
        ("start", "C"),
        ("term", "B"),
        ("term", "C"),
        ("term", "C"),
    ]
    for kind, gid in events:
        if kind == "start":
            reporter.on_run_start(gid)
        else:
            reporter.on_run_terminal(gid)
        assert last(fake)["runDepth"] == len(reporter._live_runs)
    assert all(p["runDepth"] >= 0 for p, _ in fake.states)
    assert last(fake)["runDepth"] == 0


def test_ac14e_late_cancel_after_continuation_changes_nothing(reporter, fake):
    reporter.on_run_start("g1")
    reporter.on_run_terminal("g1")
    reporter.on_turn_end()
    reporter.on_run_start("g2")
    before = list(fake.states)
    reporter.on_run_terminal("g1")
    assert fake.states == before
    assert last(fake)["runDepth"] == 1


def test_ac14h_turn_events_do_not_clear_live_runs(reporter, fake):
    # The set can hold a Discord/ACP id, which is not bound to the
    # interactive turn loop -- clearing would report idle while it works.
    reporter.on_run_start("discord-run")
    reporter.on_turn_end()
    reporter.on_turn_cancel()
    assert last(fake)["runDepth"] == 1
    assert set(reporter._live_runs) == {"discord-run"}


def test_ac14i_sweep_evicts_and_publishes(monkeypatch, reporter, fake):
    monkeypatch.setattr(rp, "_RUN_TTL_S", 0.01)
    reporter.on_run_start("leaked")
    time.sleep(0.02)
    # NO events of any kind in between -- the sweep is the only trigger.
    reporter.sweep_once()
    # Internal set contents are explicitly not sufficient: the pane only
    # moves when something is REPORTED.
    assert last(fake)["runDepth"] == 0
    assert reporter._live_runs == {}


def test_ac14j_sweep_with_nothing_expired_emits_nothing(reporter, fake):
    reporter.on_run_start("live")
    before = list(fake.states)
    reporter.sweep_once()
    reporter.sweep_once()
    assert fake.states == before


# --- F4: a run PAST the TTL (AC-14o..AC-14q) -------------------------------


def test_ac14o_a_working_run_past_the_ttl_is_never_falsely_idled(
    monkeypatch, reporter, fake
):
    """Drive a run PAST ``_RUN_TTL_S`` while it is provably still working.

    Before the fix the sweep evicted mid-flight and published ``runDepth 0``
    -> a false ``idle`` while the agent works, which SPEC R-3's own
    asymmetry argument names as the WORSE of the two failures. Tool traffic
    is the evidence that the process is still doing agent work, so it
    re-stamps the live entries.
    """
    monkeypatch.setattr(rp, "_RUN_TTL_S", 0.05)
    reporter.on_run_start("long-run")
    started = time.monotonic()

    # Several TTL-lengths of genuine work, with tool traffic throughout.
    # The tool calls are the evidence; the sweeps are what would evict.
    for _ in range(5):
        time.sleep(0.03)
        reporter.on_tool_start("run_shell_command")
        reporter.on_tool_complete("run_shell_command")
        reporter.sweep_once()

    elapsed = time.monotonic() - started
    assert elapsed > rp._RUN_TTL_S, (
        f"precondition: the run must OUTLIVE the TTL ({elapsed:.3f}s vs "
        f"{rp._RUN_TTL_S}s), or this test proves nothing"
    )
    assert set(reporter._live_runs) == {"long-run"}
    assert last(fake)["runDepth"] == 1
    assert all(p["runDepth"] >= 1 for p, _ in fake.states), (
        "no payload may report runDepth 0 while the run is still working"
    )


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param(lambda r: r.on_tool_start("run_shell_command"), id="pre-only"),
        pytest.param(lambda r: r.on_tool_complete("run_shell_command"), id="post-only"),
    ],
)
def test_ac14o_each_tool_hook_independently_re_stamps(
    monkeypatch, reporter, fake, evidence
):
    """EITHER tool hook alone must keep a long run alive.

    Parametrized because the two hooks are separate call sites: a test that
    fires BOTH passes even when only one of them re-stamps, so removing the
    touch from ``on_tool_start`` alone survives it (measured: M4 did).

    ``post_tool_call`` matters on its own -- ``pydantic_patches.py:393``
    returns before the ``finally`` that fires it, and ``/fork`` fires an
    unmatched post -- so neither hook may be assumed to accompany the other.
    """
    monkeypatch.setattr(rp, "_RUN_TTL_S", 0.05)
    reporter.on_run_start("long-run")
    started = time.monotonic()

    for _ in range(5):
        time.sleep(0.03)
        evidence(reporter)
        reporter.sweep_once()

    assert time.monotonic() - started > rp._RUN_TTL_S, "must outlive the TTL"
    assert set(reporter._live_runs) == {"long-run"}
    assert last(fake)["runDepth"] == 1


def test_ac14p_a_leaked_id_in_a_quiescent_session_is_still_evicted(
    monkeypatch, reporter, fake
):
    """The control for AC-14o: leak containment must survive the fix.

    This is the exact scenario SPEC R-3 motivated the sweep with -- a start
    that leaked (``_runtime.py:911`` fires start; the ``finally`` firing end
    only opens at ``:999``) after which the user simply leaves the session
    idle. With NO tool traffic there is no evidence of work, so the entry
    expires and the eviction PUBLISHES.
    """
    monkeypatch.setattr(rp, "_RUN_TTL_S", 0.02)
    reporter.on_run_start("leaked")
    time.sleep(0.05)  # quiescent: no tools, no events of any kind
    reporter.sweep_once()
    assert reporter._live_runs == {}
    assert last(fake)["runDepth"] == 0


def test_ac14q_a_late_terminal_event_after_a_sweep_is_harmless(
    monkeypatch, reporter, fake
):
    """The swept run's eventual ``agent_run_end`` must not corrupt anything.

    It hits the ``pop(...) is None`` early return, which is CORRECT here:
    the depth is already 0, so there is nothing to correct and re-emitting
    would be noise. What must not happen is a negative depth or a resurrected
    entry.
    """
    monkeypatch.setattr(rp, "_RUN_TTL_S", 0.02)
    reporter.on_run_start("leaked")
    time.sleep(0.05)
    reporter.sweep_once()
    after_sweep = len(fake.states)

    reporter.on_run_terminal("leaked")  # cancel
    reporter.on_run_terminal("leaked")  # end

    assert len(fake.states) == after_sweep
    assert reporter._live_runs == {}
    assert all(p["runDepth"] >= 0 for p, _ in fake.states)


def test_ac14r_a_human_wait_past_the_ttl_still_reads_blocked(
    monkeypatch, reporter, fake
):
    """A run parked on a human overnight must not decay to a false state.

    ``awaitingHuman`` outranks ``runDepth`` server-side, so this stays
    ``blocked`` either way -- but the depth must not silently drop, or the
    eventual ``awaiting(False)`` would land on ``idle`` instead of
    ``working``.
    """
    monkeypatch.setattr(rp, "_RUN_TTL_S", 0.02)
    reporter.on_run_start("g1")
    reporter.on_tool_start("run_shell_command")
    reporter.on_awaiting_user_input(True)
    time.sleep(0.05)
    reporter.sweep_once()
    assert last(fake)["awaitingHuman"] is True
    assert last(fake)["runDepth"] == 1, "the parked run must stay counted"

    reporter.on_awaiting_user_input(False)
    assert last(fake) == {"awaitingHuman": False, "runDepth": 1}


# --- F5: overlapping awaiting edges are refcounted (AC-45..AC-45c) ---------


def test_ac45_overlapping_awaiting_pairs_do_not_collapse(reporter, fake):
    """``True -> True -> False`` must stay BLOCKED, not flip to working.

    Reachable: ``ask_user_question/terminal_ui.py:346`` and
    ``queue_console.py:220-255`` do not share the approval locks, and the
    sync and async approval locks are distinct objects
    (``tools/common.py:39-54``) -- so two waits genuinely overlap. With a
    plain bool the inner ``False`` cleared the flag and the pane reported
    ``working`` while a human was still parked, inverting the one signal
    this feature exists to provide.
    """
    reporter.on_run_start("g1")
    reporter.on_tool_start("run_shell_command")
    reporter.on_awaiting_user_input(True)
    reporter.on_tool_start("edit_file")
    reporter.on_awaiting_user_input(True)  # a SECOND, overlapping wait

    reporter.on_awaiting_user_input(False)  # the inner one finishes
    assert last(fake)["awaitingHuman"] is True, (
        "a human is still parked on the outer wait"
    )

    reporter.on_awaiting_user_input(False)  # the outer one finishes
    assert last(fake) == {"awaitingHuman": False, "runDepth": 1}


def test_ac45b_an_unmatched_false_never_drives_the_count_negative(reporter, fake):
    """An unmatched ``False`` must not bank a credit against a later ``True``.

    Same discipline as the run set: floor at zero. A negative count would
    make the NEXT genuine block report ``working``.
    """
    reporter.on_run_start("g1")
    reporter.on_tool_start("run_shell_command")

    reporter.on_awaiting_user_input(False)
    reporter.on_awaiting_user_input(False)
    reporter.on_awaiting_user_input(False)
    assert reporter._awaiting_depth == 0

    reporter.on_awaiting_user_input(True)
    assert last(fake)["awaitingHuman"] is True, (
        "unmatched False events must not be banked against a later True"
    )
    reporter.on_awaiting_user_input(False)
    assert last(fake)["awaitingHuman"] is False


def test_ac45c_a_suppressed_true_is_not_counted(reporter, fake):
    """A suppressed edge is DISCARDED, so it must not enter the refcount.

    Counting it would resurrect the latching behaviour AC-20c forbids: the
    matching ``False`` would decrement from 1 to 0 and everything would look
    fine, but a recompute in between (a sibling run starting) would report
    ``blocked`` while the human is merely in a menu.
    """
    reporter.on_awaiting_user_input(True)  # menu at an idle prompt
    assert reporter._awaiting_depth == 0
    assert fake.states == []

    reporter.on_run_start("sibling-discord-run")
    assert last(fake) == {"awaitingHuman": False, "runDepth": 1}


def test_ac15_duplicate_state_is_not_resent(reporter, fake):
    reporter.on_run_start("g1")
    count = len(fake.states)
    reporter.on_turn_cancel()  # recompute, identical payload
    reporter.on_turn_cancel()
    assert len(fake.states) == count


def test_ac0_startup_claim_bypasses_the_edge_trigger(reporter, fake):
    """The claim is emitted from a SETTLED idle state, not a pristine one.

    This distinction is the whole test. Against a pristine reporter
    ``_last_payload is None``, so the ``payload == self._last_payload``
    short-circuit in ``_sync_locked`` can never be reached and ``force=True``
    does nothing observable -- mutant M8 (delete ``force`` outright) survived
    all 100 tests. Driving a real run to completion FIRST makes
    ``_last_payload`` equal to the idle payload, so only a genuine bypass
    produces the additional report.
    """
    idle = {"awaitingHuman": False, "runDepth": 0}
    reporter.on_run_start("g-settle")
    reporter.on_run_terminal("g-settle")
    assert reporter._last_payload == idle, "precondition: settled on idle"
    settled = len(fake.states)

    reporter.on_startup()

    assert len(fake.states) == settled + 1, (
        "an ADDITIONAL report must be emitted even though the payload is "
        "identical to the last one -- otherwise a crashed predecessor's "
        "ghost `working` stands until the next genuine transition"
    )
    assert last(fake) == idle


def test_ac0b_an_edge_triggered_recompute_still_sends_nothing(reporter, fake):
    """The control for AC-0: ordinary recomputes must stay suppressed.

    Without this, "always send" would satisfy AC-0 while destroying the
    edge-trigger the rest of the design rests on. Every recompute path is
    driven -- turn events, terminal events and tool traffic alike -- because
    each one calls ``_sync_locked`` with its OWN default, and a mutant that
    flips the DEFAULT (``force: bool = True``) rather than the call site
    survives a test that only exercises one of them (measured: M8b did).
    """
    reporter.on_run_start("g-settle")
    reporter.on_run_terminal("g-settle")
    settled = len(fake.states)

    reporter.on_turn_cancel()
    reporter.on_turn_end()
    reporter.on_run_terminal("never-started")
    reporter.on_tool_start("read_file")
    reporter.on_tool_complete("read_file")
    reporter.on_awaiting_user_input(False)
    reporter.sweep_once()

    assert len(fake.states) == settled, (
        "no recompute may publish while the derived payload is unchanged"
    )


def test_ac0c_the_startup_claim_is_the_only_forced_publish(reporter, fake):
    """AC-15's guarantee holds for a run that CHANGES then repeats.

    Complements AC-0b from the other side: state genuinely moves, and then
    the identical payload is recomputed -- so a `force`-by-default mutation
    produces a visible duplicate.
    """
    reporter.on_run_start("g1")
    assert len(fake.states) == 1
    reporter.on_run_start("g1")  # duplicate start, same id -> same depth
    assert len(fake.states) == 1, "a re-added id must not republish"


def test_ac16_state_is_critical_metadata_and_activity_are_decorative(reporter, fake):
    with patch.object(
        rp.sources,
        "current_metadata",
        return_value={"tokens": "1k/2k", "context_pct": 5},
    ):
        reporter.on_run_start("g1")
        reporter.on_tool_start("read_file")
        reporter.on_turn_end()
    # State edges never ride the decorative reporting methods, and the
    # decorative payloads never masquerade as state.
    assert len(fake.states) == 1
    assert fake.activity == [{"tool": "read_file", "done": False}]
    assert len(fake.metadata) == 1


def test_ac9b_run_delta_never_appears_in_a_payload(reporter, fake):
    reporter.on_run_start("g1")
    reporter.on_run_start("g2")
    reporter.on_run_terminal("g1")
    assert all("runDelta" not in p for p, _ in fake.states)


def test_generation_increases_with_every_published_payload(reporter, fake):
    reporter.on_run_start("g1")
    reporter.on_run_start("g2")
    reporter.on_run_terminal("g2")
    gens = [g for _, g in fake.states]
    assert gens == sorted(gens) and len(set(gens)) == len(gens)


# --- blocked reason tiers (AC-18..AC-24d) ----------------------------------


def test_ac18_single_tool_in_flight_names_it(reporter, fake):
    reporter.on_run_start("g1")
    reporter.on_tool_start("run_shell_command")
    reporter.on_awaiting_user_input(True)
    assert last(fake)["reason"] == "permission: run_shell_command"


def test_ac19_multiple_tools_in_flight_are_ambiguous(reporter, fake):
    reporter.on_run_start("g1")
    reporter.on_tool_start("run_shell_command")
    reporter.on_tool_start("edit_file")
    reporter.on_awaiting_user_input(True)
    assert last(fake)["reason"] == "permission: 1 of 2 tools"


def test_ac20_menu_at_idle_prompt_reports_nothing(reporter, fake):
    reporter.on_awaiting_user_input(True)
    assert fake.states == []


def test_ac20b_zero_tools_mid_run_is_a_real_block(reporter, fake):
    reporter.on_run_start("g1")
    reporter.on_awaiting_user_input(True)
    assert last(fake)["awaitingHuman"] is True
    assert last(fake)["reason"] == "permission: unknown"


def test_ac20c_suppressed_edge_is_discarded_not_latched(reporter, fake):
    reporter.on_awaiting_user_input(True)  # menu at idle prompt -> suppressed
    reporter.on_run_start("sibling-discord-run")
    assert last(fake) == {"awaitingHuman": False, "runDepth": 1}


def test_ac21_block_before_any_tool_call_was_ever_observed(reporter, fake):
    # Deliberately overlaps AC-20b: same branch, different premise. Retained
    # as a regression guard -- there is no _saw_any_tool_call flag.
    assert reporter._inflight == []
    reporter.on_run_start("g1")
    reporter.on_awaiting_user_input(True)
    assert last(fake)["reason"] == "permission: unknown"


def test_ac22_inflight_set_is_thread_safe(reporter, fake):
    reporter.on_run_start("g1")
    barrier = threading.Barrier(2)
    seen: List[Any] = []

    def add_tools():
        barrier.wait()
        for i in range(200):
            reporter.on_tool_start(f"tool_{i}")

    def observe():
        barrier.wait()
        for _ in range(200):
            with reporter._lock:
                seen.append(len(reporter._inflight))

    threads = [threading.Thread(target=add_tools), threading.Thread(target=observe)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(reporter._inflight) == 200
    assert all(0 <= n <= 200 for n in seen)


def test_ac24_unmatched_fork_post_leaves_the_set_untouched(reporter, fake):
    # /fork fires on_post_tool_call("invoke_agent", ...) with NO matching pre,
    # concurrently with a foreground run. An integer counter would hit 0 here
    # and the real approval would be suppressed as if it were a menu.
    reporter.on_run_start("g1")
    reporter.on_tool_start("run_shell_command")
    reporter.on_tool_complete("invoke_agent")
    reporter.on_awaiting_user_input(True)
    assert last(fake)["reason"] == "permission: run_shell_command"


def test_ac24b_matched_post_removes_exactly_one_key(reporter, fake):
    reporter.on_run_start("g1")
    reporter.on_tool_start("read_file")
    reporter.on_tool_start("read_file")
    reporter.on_tool_complete("read_file")
    reporter.on_awaiting_user_input(True)
    assert last(fake)["reason"] == "permission: read_file"
    reporter.on_awaiting_user_input(False)
    reporter.on_tool_complete("read_file")
    reporter.on_awaiting_user_input(True)
    assert last(fake)["reason"] == "permission: unknown"


def test_ac24c_leaked_key_is_contained_by_ttl_not_a_clear(monkeypatch, reporter, fake):
    # A pre_tool_call with no following post -- what happens when a hook
    # blocks the tool (pydantic_patches.py:393 returns before the finally).
    monkeypatch.setattr(rp, "_INFLIGHT_TTL_S", 0.01)
    reporter.on_tool_start("blocked_tool")
    time.sleep(0.02)
    reporter.on_awaiting_user_input(True)
    assert fake.states == []  # AC-20 is reachable again
    assert reporter._inflight == []


def test_ac24d_live_key_survives_a_turn_boundary(reporter, fake):
    # /fork runs detached past the boundary, so a blanket clear would
    # suppress a real fork approval as if it were a menu.
    reporter.on_run_start("g1")
    reporter.on_tool_start("run_shell_command")
    reporter.on_turn_end()
    reporter.on_run_terminal("g1")
    reporter.on_run_start("g2")
    reporter.on_awaiting_user_input(True)
    assert last(fake)["reason"] == "permission: run_shell_command"


# --- activity (AC-36) ------------------------------------------------------


def test_ac36_activity_uses_tool_and_done_and_never_a_message(reporter, fake):
    reporter.on_tool_start("read_file")
    reporter.on_tool_complete("read_file")
    assert fake.activity == [
        {"tool": "read_file", "done": False},
        {"tool": None, "done": True},
    ]
    # pane.report_agent has NO message field -- that is herdr's protocol.
    for params, _ in fake.states:
        assert "message" not in params


# --- session (AC-28 wiring half) -------------------------------------------


def test_session_reported_once_until_it_changes(reporter, fake):
    with patch.object(rp.sources, "current_session_id", side_effect=["a", "a", "b"]):
        reporter.on_user_prompt()
        reporter.on_user_prompt()
        reporter.on_user_prompt()
    assert fake.sessions == ["a", "b"]


def test_shutdown_releases(reporter, fake):
    reporter.on_shutdown()
    assert fake.released == 1


# --- hygiene (AC-33, AC-33b) ------------------------------------------------


def test_ac33_no_reporter_lock_while_a_source_runs(reporter, fake):
    observed: Dict[str, Any] = {}

    def probe():
        observed["metadata"] = _held(reporter._lock)
        return {"tokens": "1k/2k", "context_pct": 5}

    def probe_session():
        observed["session"] = _held(reporter._lock)
        return "sess"

    with (
        patch.object(rp.sources, "current_metadata", side_effect=probe),
        patch.object(rp.sources, "current_session_id", side_effect=probe_session),
    ):
        reporter.on_turn_end()
        reporter.on_user_prompt()
    assert observed == {"metadata": False, "session": False}


def test_ac33b_no_reporter_lock_across_a_client_call(reporter, fake):
    observed: List[bool] = []
    real_state = fake.report_state
    real_metadata = fake.report_metadata
    real_activity = fake.report_activity
    real_session = fake.report_session

    def spy(inner):
        def wrapper(*a, **kw):
            observed.append(_held(reporter._lock))
            return inner(*a, **kw)

        return wrapper

    fake.report_state = spy(real_state)
    fake.report_metadata = spy(real_metadata)
    fake.report_activity = spy(real_activity)
    fake.report_session = spy(real_session)

    with (
        patch.object(
            rp.sources,
            "current_metadata",
            return_value={"tokens": "1k/2k", "context_pct": 5},
        ),
        patch.object(rp.sources, "current_session_id", return_value="sess"),
    ):
        reporter.on_run_start("g1")
        reporter.on_tool_start("read_file")
        reporter.on_tool_complete("read_file")
        reporter.on_awaiting_user_input(True)
        reporter.on_turn_end()
        reporter.on_user_prompt()
        reporter.on_run_terminal("g1")
    assert observed and not any(observed)


def _held(lock: threading.Lock) -> bool:
    """Report whether ``lock`` is held, judged from a FOREIGN thread."""
    return not acquirable_from_another_thread(lock)
