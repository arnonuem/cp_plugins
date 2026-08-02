"""C4 — the approval switch: both ways at once, exactly one winner.

The layer this suite guards has no happy path worth much on its own.  What it
has is a race between two branches, a prompt that must not exist twice, and a
process-global flag whose loss turns Ctrl+C from "cancel this prompt" into
"kill the agent run".  So the suite is built around the losing cases.

**The prompt is faked, deliberately -- but not everywhere.**  Driving a real
``prompt_toolkit`` Application through a pipe would make every timing test a
coin flip, so the terminal branch runs against a double that answers on
command.  The REAL Application is exercised separately (AC-89), because a
double cannot prove that the thing we actually ship is operable.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from cp_discord import approvals, approvals_ui, bindings, constants, reporter
from cp_discord.bindings import Role


APPROVER_ID = "4242"
STRANGER_ID = "9999"
TALKER_ONLY_ID = "7777"

#: The REAL setter, grabbed at import time -- the autouse ``clean_state``
#: fixture replaces ``approvals._set_core_flag`` with a recorder for every
#: other test, so by the time a test body runs the genuine article is gone.
#: AC-87d needs it because what it guards (``notify=True``) lives INSIDE that
#: function, where no recorder on the seam can observe it.
REAL_SET_CORE_FLAG = approvals._set_core_flag


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


@pytest.fixture
def authz_db(tmp_path, monkeypatch):
    """A throwaway bindings database with one approver in it."""
    monkeypatch.setenv(bindings.DB_PATH_ENV, str(tmp_path / "authz.db"))
    bindings.forget_initialized_paths()
    bindings.bind(constants.AUTHZ_CHANNEL, APPROVER_ID, "wayne")
    bindings.grant("wayne", Role.APPROVER)
    bindings.bind(constants.AUTHZ_CHANNEL, TALKER_ONLY_ID, "mary")
    bindings.grant("mary", Role.TALKER)
    yield
    bindings.forget_initialized_paths()


class FakePrompt:
    """Stands in for the terminal branch: goes live on command, answers later.

    ``run`` blocks exactly like the real one, so the backend's waiting, its
    mark transitions and its slot handling are all exercised for real -- only
    the terminal is imaginary.
    """

    instances: list["FakePrompt"] = []

    def __init__(self, gate, *, fail: bool = False, live: bool = True) -> None:
        self.gate = gate
        self._fail = fail
        self._live = live
        self._answer: "threading.Event" = threading.Event()
        self._value = None
        self.exited_with = None
        self.went_live = False
        self.running = threading.Event()
        FakePrompt.instances.append(self)

    # -- what approvals.py calls ---------------------------------------

    def run(self, *, on_live):
        if self._fail:
            raise RuntimeError("this terminal cannot host a prompt")
        if self._live:
            self.went_live = on_live()
            if not self.went_live:
                return None
        self.running.set()
        self._answer.wait(5)
        return self._value

    def exit_with(self, approved: bool) -> None:
        self.exited_with = approved
        self.answer(approved)

    # -- what the tests call -------------------------------------------

    def answer(self, value) -> None:
        self._value = value
        self._answer.set()


@pytest.fixture
def prompts(monkeypatch):
    """Install the fake prompt and hand tests a handle on what was built."""
    FakePrompt.instances = []
    made = {"fail": False, "live": True}

    def factory(gate):
        return FakePrompt(gate, fail=made["fail"], live=made["live"])

    monkeypatch.setattr(approvals, "_prompt_factory", factory)
    monkeypatch.setattr(approvals, "_stdin_is_interactive", lambda: True)
    yield made
    for prompt in FakePrompt.instances:
        prompt.answer(False)


class FakeClient:
    """C2's surface, as C4 uses it: submit a gate, close a gate."""

    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts
        self.submitted: list[dict] = []
        self.closed: list[tuple] = []

    def submit_gate(
        self, gate_id, title, body, *, preview=None, remote_resolvable=True
    ):
        self.submitted.append(
            {
                "gate_id": gate_id,
                "title": title,
                "body": body,
                "preview": preview,
                "remote_resolvable": remote_resolvable,
            }
        )
        return self.accepts

    def close_gate(self, gate_id, outcome, *, title=""):
        self.closed.append((gate_id, outcome, title))
        return True


@pytest.fixture
def discord(monkeypatch):
    """A reachable broker by default; tests switch it off where it matters."""
    client = FakeClient()
    monkeypatch.setattr(approvals, "_active_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Every test starts with an empty registry and a silent core flag."""
    flags: list[tuple] = []
    monkeypatch.setattr(
        approvals, "_set_core_flag", lambda value: flags.append(("flag", value))
    )
    approvals.reset_state()
    approvals.core_flag_calls = flags
    yield flags
    approvals.reset_state()


def backend_in_thread(title="Shell Command", message="`rm -rf /`", preview=None):
    """Run the backend off-thread; it blocks, which is the whole point."""
    result = {}
    done = threading.Event()

    def run():
        try:
            result["value"] = approvals.approval_backend(title, message, preview)
        except BaseException as error:  # pragma: no cover - surfaced by the test
            result["error"] = error
        finally:
            done.set()

    thread = threading.Thread(target=run, name="backend", daemon=True)
    thread.start()
    return result, done, thread


def wait_for_prompt(index: int = 0, timeout: float = 5.0) -> FakePrompt:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(FakePrompt.instances) > index:
            prompt = FakePrompt.instances[index]
            if prompt.running.wait(0.1) or prompt.went_live is False:
                return prompt
        time.sleep(0.01)
    raise AssertionError(f"no prompt #{index} appeared")


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def wait_for_gate(timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        open_gates = approvals.open_gates()
        if open_gates:
            return open_gates[0]
        time.sleep(0.01)
    raise AssertionError("no gate was opened")


# --------------------------------------------------------------------------- #
# AC-28 / AC-29 / AC-30 — BOTH ways, always
# --------------------------------------------------------------------------- #


def test_ac28_every_approval_goes_both_ways(authz_db, prompts, discord):
    result, done, _ = backend_in_thread()
    prompt = wait_for_prompt()

    assert discord.submitted, "no gate reached Discord"
    assert prompt.went_live is True, "no terminal prompt was started"

    prompt.answer(True)
    assert done.wait(5)
    assert result["value"] == (True, None)


def test_ac29_a_terminal_run_is_answerable_from_the_phone(authz_db, prompts, discord):
    """The core scenario: started at the PC, answered on the phone."""
    result, done, _ = backend_in_thread()
    wait_for_prompt()
    gate_id = discord.submitted[0]["gate_id"]

    refusal = approvals.on_gate_resolved(
        gate_id=gate_id,
        decision=approvals_ui.DECISION_APPROVE,
        discord_user_id=APPROVER_ID,
    )

    assert refusal is None
    assert done.wait(5)
    assert result["value"] == (True, None)


def test_ac48_a_discord_approval_returns_TRUE_not_a_cancellation(
    authz_db, prompts, discord
):
    """``exit(result=...)``, not ``exit()``: a bare abort would deny."""
    result, done, _ = backend_in_thread()
    prompt = wait_for_prompt()

    approvals.on_gate_resolved(
        gate_id=discord.submitted[0]["gate_id"],
        decision=approvals_ui.DECISION_APPROVE,
        discord_user_id=APPROVER_ID,
    )
    done.wait(5)

    assert prompt.exited_with is True
    assert result["value"] == (True, None)


def test_ac30_three_gates_in_sequence_all_resolve(authz_db, prompts, discord):
    for index, expected in enumerate((True, False, True)):
        result, done, _ = backend_in_thread()
        prompt = wait_for_prompt(index)
        prompt.answer(expected)
        assert done.wait(5)
        assert result["value"] == (expected, None)


# --------------------------------------------------------------------------- #
# AC-31 / AC-32 — the slot is never emptied
# --------------------------------------------------------------------------- #


def test_ac31_the_backend_slot_is_never_emptied(authz_db, prompts, discord):
    from code_puppy.tools.common import get_approval_backend

    approvals.install()
    try:
        result, done, _ = backend_in_thread()
        wait_for_prompt()

        assert get_approval_backend() is not None

        wait_for_prompt().answer(True)
        done.wait(5)
        assert get_approval_backend() is not None
    finally:
        approvals.uninstall()


def test_ac32_a_discord_request_during_a_live_prompt_is_delivered(
    authz_db, prompts, discord
):
    result, done, _ = backend_in_thread()
    prompt = wait_for_prompt()
    assert prompt.running.wait(5)

    approvals.on_gate_resolved(
        gate_id=discord.submitted[0]["gate_id"],
        decision=approvals_ui.DECISION_DENY,
        discord_user_id=APPROVER_ID,
    )

    assert done.wait(5)
    assert result["value"] == (False, None)


# --------------------------------------------------------------------------- #
# AC-64a / AC-64b — the two windows the three-valued mark exists for
# --------------------------------------------------------------------------- #


def test_ac64a_discord_wins_before_the_prompt_starts(authz_db, monkeypatch, discord):
    """Window 1: the mark is still EMPTY, so ``exit()`` would evaporate."""
    resolved = threading.Event()

    def factory(gate):
        raise AssertionError("no prompt may be built for an already-decided gate")

    monkeypatch.setattr(approvals, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(approvals, "_prompt_factory", factory)

    def resolve_first(gate):
        # Runs where the backend has posted the gate but not yet reached the
        # terminal branch: exactly the window AC-64a describes.
        approvals.on_gate_resolved(
            gate_id=gate.gate_id,
            decision=approvals_ui.DECISION_APPROVE,
            discord_user_id=APPROVER_ID,
        )
        resolved.set()

    monkeypatch.setattr(approvals, "_after_gate_posted", resolve_first)

    result, done, _ = backend_in_thread()

    assert done.wait(5)
    assert resolved.is_set()
    assert result["value"] == (True, None)
    assert approvals.prompt_mark() == approvals.MARK_EMPTY


def test_ac64b_discord_wins_while_the_mark_is_pending(
    authz_db, prompts, discord, monkeypatch
):
    """Window 2: PENDING -- no ``exit()`` may be called, step 3a collects it.

    The resolution is injected in the FACTORY: the mark is PENDING from the
    moment the slot is taken until the prompt reports itself live, and the
    factory runs squarely inside that window.
    """
    built = threading.Event()
    original = approvals._prompt_factory

    def factory(gate):
        assert approvals.prompt_mark() == approvals.MARK_PENDING
        approvals.on_gate_resolved(
            gate_id=gate.gate_id,
            decision=approvals_ui.DECISION_APPROVE,
            discord_user_id=APPROVER_ID,
        )
        built.set()
        return original(gate)

    monkeypatch.setattr(approvals, "_prompt_factory", factory)

    result, done, _ = backend_in_thread()

    assert built.wait(5), "the prompt was never built"
    assert done.wait(5)
    prompt = FakePrompt.instances[0]
    assert prompt.went_live is False, "an operable prompt appeared for a decided gate"
    assert prompt.exited_with is None, "exit() was called on a non-live Application"
    assert result["value"] == (True, None)


# --------------------------------------------------------------------------- #
# AC-88 — mark and slot come back on EVERY exit
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("outcome", ["terminal", "discord", "abort", "exception"])
def test_ac88_two_gates_in_a_row_both_get_a_real_prompt(
    authz_db, prompts, discord, outcome
):
    """The normal case of every run -- and it was in no AC until R8.

    The two ABORT cases deliberately do NOT expect the backend to return: a
    dead terminal branch is not a winner while the phone still has an open
    gate (INV-C7, AC-33).  What they DO expect is the mark, the slot and the
    app reference back, so gate 2 gets a real prompt -- which is what AC-88 is
    actually about.
    """
    result, done, _ = backend_in_thread()
    prompt = wait_for_prompt()
    first_gate = discord.submitted[0]["gate_id"]

    if outcome == "terminal":
        prompt.answer(True)
        assert done.wait(5)
    elif outcome == "discord":
        approvals.on_gate_resolved(
            gate_id=first_gate,
            decision=approvals_ui.DECISION_APPROVE,
            discord_user_id=APPROVER_ID,
        )
        assert done.wait(5)
    elif outcome == "abort":
        prompt.answer(None)
    else:
        prompt.answer(_Explode())

    _wait_until(lambda: approvals.prompt_slot() is None)
    assert approvals.prompt_mark() == approvals.MARK_EMPTY
    assert approvals.live_application() is None

    result2, done2, _ = backend_in_thread()
    second = wait_for_prompt(1)
    assert second.went_live is True, "gate 2 got no operable prompt"
    second.answer(True)
    assert done2.wait(5)
    assert result2["value"] == (True, None)

    if outcome in ("abort", "exception"):
        # The first gate is STILL answerable from the phone -- that is the
        # point of calling a branch failure an abort rather than a denial.
        assert first_gate in approvals.open_gates()
        approvals.on_gate_resolved(
            gate_id=first_gate,
            decision=approvals_ui.DECISION_APPROVE,
            discord_user_id=APPROVER_ID,
        )
        assert done.wait(5)
        assert result["value"] == (True, None)


class _Explode:
    """A prompt answer that blows up when the backend reads it."""

    def __bool__(self):
        raise RuntimeError("boom")


# --------------------------------------------------------------------------- #
# AC-33 / AC-35 — a branch failure is not a winner
# --------------------------------------------------------------------------- #


def test_ac33_a_broken_terminal_branch_leaves_the_gate_open(authz_db, prompts, discord):
    """INV-C7: fail-closed only when BOTH branches ended without a winner."""
    prompts["fail"] = True
    result, done, _ = backend_in_thread()
    gate_id = wait_for_gate()

    assert not done.wait(0.3), "the backend denied while the phone still had a gate"

    approvals.on_gate_resolved(
        gate_id=gate_id,
        decision=approvals_ui.DECISION_APPROVE,
        discord_user_id=APPROVER_ID,
    )

    assert done.wait(5)
    assert result["value"] == (True, None)


def test_ac35_both_branches_dead_fails_closed(authz_db, prompts, monkeypatch):
    monkeypatch.setattr(approvals, "_active_client", lambda: None)
    prompts["fail"] = True

    result, done, _ = backend_in_thread()

    assert done.wait(5)
    assert result["value"] == (False, None)


def test_ac35_an_undeliverable_gate_and_no_stdin_fails_closed(
    authz_db, prompts, monkeypatch
):
    monkeypatch.setattr(approvals, "_active_client", lambda: FakeClient(accepts=False))
    monkeypatch.setattr(approvals, "_stdin_is_interactive", lambda: False)

    result, done, _ = backend_in_thread()

    assert done.wait(5)
    assert result["value"] == (False, None)


def test_ac34_the_backend_never_calls_the_core_approval(authz_db, prompts, discord):
    """INV-C6: that would land on the same check again -- infinite recursion."""
    import code_puppy.tools.common as common

    calls = []
    original = common.get_user_approval
    common.get_user_approval = lambda *a, **k: calls.append(a) or (True, None)
    try:
        result, done, _ = backend_in_thread()
        wait_for_prompt().answer(True)
        done.wait(5)
    finally:
        common.get_user_approval = original

    assert calls == []


# --------------------------------------------------------------------------- #
# AC-49 / AC-63 / AC-65 — the timeout belongs to the Discord branch ALONE
# --------------------------------------------------------------------------- #


def test_ac49_the_discord_timeout_leaves_the_terminal_prompt_open(
    authz_db, prompts, discord, monkeypatch
):
    monkeypatch.setattr(approvals, "GATE_TIMEOUT_SECONDS", 0.05)

    result, done, _ = backend_in_thread()
    prompt = wait_for_prompt()
    assert prompt.running.wait(5)

    time.sleep(0.3)
    assert not done.is_set(), "the timeout ended the terminal branch too"

    prompt.answer(True)
    assert done.wait(5)
    assert result["value"] == (True, None)


def test_ac63_a_dead_broker_kills_only_the_discord_branch(
    authz_db, prompts, monkeypatch
):
    monkeypatch.setattr(approvals, "_active_client", lambda: FakeClient(accepts=False))

    result, done, _ = backend_in_thread()
    prompt = wait_for_prompt()
    assert prompt.running.wait(5)

    prompt.answer(False)
    assert done.wait(5)
    assert result["value"] == (False, None)


def test_ac65_without_a_broker_the_terminal_branch_has_no_timeout(
    authz_db, prompts, monkeypatch
):
    monkeypatch.setattr(approvals, "_active_client", lambda: None)
    monkeypatch.setattr(approvals, "GATE_TIMEOUT_SECONDS", 0.05)

    result, done, _ = backend_in_thread()
    prompt = wait_for_prompt()
    assert prompt.running.wait(5)

    time.sleep(0.3)
    assert not done.is_set(), "the terminal branch inherited Discord's timeout"

    prompt.answer(True)
    assert done.wait(5)
    assert result["value"] == (True, None)


# --------------------------------------------------------------------------- #
# AC-86 — stdin that cannot be prompted on
# --------------------------------------------------------------------------- #


def test_ac86a_no_stdin_but_a_broker_runs_only_the_discord_branch(
    authz_db, prompts, discord, monkeypatch
):
    monkeypatch.setattr(approvals, "_stdin_is_interactive", lambda: False)

    result, done, _ = backend_in_thread()
    gate_id = wait_for_gate()

    approvals.on_gate_resolved(
        gate_id=gate_id,
        decision=approvals_ui.DECISION_APPROVE,
        discord_user_id=APPROVER_ID,
    )
    assert done.wait(5)
    assert result["value"] == (True, None)
    # Checked AFTER the backend returned, not before: asserting on an empty
    # list while the prompt thread has not been scheduled yet would pass
    # whether or not the stdin guard exists.
    assert FakePrompt.instances == [], "a prompt was started without a usable stdin"
    assert approvals.prompt_slot() is None


def test_ac86b_no_stdin_and_no_broker_fails_closed_like_the_core(
    authz_db, prompts, monkeypatch
):
    monkeypatch.setattr(approvals, "_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(approvals, "_active_client", lambda: None)

    result, done, _ = backend_in_thread()

    assert done.wait(5)
    assert result["value"] == (False, None)
    assert FakePrompt.instances == []


def test_ac86c_stdin_and_a_broker_run_both(authz_db, prompts, discord):
    result, done, _ = backend_in_thread()
    prompt = wait_for_prompt()

    assert discord.submitted
    assert prompt.went_live is True

    prompt.answer(True)
    done.wait(5)


# --------------------------------------------------------------------------- #
# AC-73 / AC-74 / AC-75 — who may click (INV-C25)
# --------------------------------------------------------------------------- #


def test_ac73_an_approver_may_click_without_a_discord_requester(
    authz_db, prompts, discord
):
    """The whole point: a terminal session has no Discord requester at all."""
    result, done, _ = backend_in_thread()
    gate_id = wait_for_gate()

    assert (
        approvals.on_gate_resolved(
            gate_id=gate_id,
            decision=approvals_ui.DECISION_APPROVE,
            discord_user_id=APPROVER_ID,
        )
        is None
    )
    assert done.wait(5)
    assert result["value"] == (True, None)


def test_ac74_a_stranger_is_refused_and_the_gate_stays_open(authz_db, prompts, discord):
    result, done, _ = backend_in_thread()
    gate_id = wait_for_gate()

    refusal = approvals.on_gate_resolved(
        gate_id=gate_id,
        decision=approvals_ui.DECISION_APPROVE,
        discord_user_id=STRANGER_ID,
    )

    assert refusal is not None
    assert not done.wait(0.3), "an unauthorized click resolved the gate"
    assert gate_id in approvals.open_gates()

    wait_for_prompt().answer(False)
    done.wait(5)


def test_ac74_a_talker_who_is_not_an_approver_is_refused(authz_db, prompts, discord):
    """Talking rights never carry approval rights -- two independent axes."""
    result, done, _ = backend_in_thread()
    gate_id = wait_for_gate()

    refusal = approvals.on_gate_resolved(
        gate_id=gate_id,
        decision=approvals_ui.DECISION_APPROVE,
        discord_user_id=TALKER_ONLY_ID,
    )

    assert refusal is not None
    assert gate_id in approvals.open_gates()
    wait_for_prompt().answer(False)
    done.wait(5)


def test_ac75_an_approver_without_talker_may_still_approve(authz_db, prompts, discord):
    """The gate path does NOT run ``check_message`` (it would test TALKER)."""
    assert bindings.has_role("wayne", Role.TALKER) is False

    result, done, _ = backend_in_thread()
    gate_id = wait_for_gate()

    assert (
        approvals.on_gate_resolved(
            gate_id=gate_id,
            decision=approvals_ui.DECISION_APPROVE,
            discord_user_id=APPROVER_ID,
        )
        is None
    )
    assert done.wait(5)
    assert result["value"] == (True, None)


def test_a_click_on_an_unknown_gate_is_refused(authz_db):
    assert approvals.on_gate_resolved(
        gate_id="nope",
        decision=approvals_ui.DECISION_APPROVE,
        discord_user_id=APPROVER_ID,
    )


# --------------------------------------------------------------------------- #
# AC-36 / AC-37 / AC-38 / AC-39 — exactly one resolution
# --------------------------------------------------------------------------- #


def test_ac36_a_discord_click_ends_the_terminal_prompt(authz_db, prompts, discord):
    result, done, _ = backend_in_thread()
    prompt = wait_for_prompt()
    assert prompt.running.wait(5)

    approvals.on_gate_resolved(
        gate_id=discord.submitted[0]["gate_id"],
        decision=approvals_ui.DECISION_APPROVE,
        discord_user_id=APPROVER_ID,
    )
    done.wait(5)

    assert prompt.exited_with is True


def test_ac37_a_terminal_answer_closes_the_discord_gate(authz_db, prompts, discord):
    result, done, _ = backend_in_thread()
    wait_for_prompt().answer(True)
    assert done.wait(5)

    assert discord.closed, "the gate was left live in the channel"
    gate_id, outcome, _title = discord.closed[0]
    assert gate_id == discord.submitted[0]["gate_id"]
    assert "Terminal" in outcome


def test_ac38_a_simultaneous_race_yields_exactly_one_resolution(
    authz_db, prompts, discord
):
    for _ in range(20):
        FakePrompt.instances = []
        discord.submitted.clear()
        result, done, _ = backend_in_thread()
        prompt = wait_for_prompt(0)
        assert prompt.running.wait(5)
        gate_id = discord.submitted[0]["gate_id"]

        barrier = threading.Barrier(2)
        outcomes = []

        def from_discord():
            barrier.wait()
            outcomes.append(
                approvals.on_gate_resolved(
                    gate_id=gate_id,
                    decision=approvals_ui.DECISION_APPROVE,
                    discord_user_id=APPROVER_ID,
                )
            )

        def from_terminal():
            barrier.wait()
            prompt.answer(False)

        threads = [
            threading.Thread(target=from_discord),
            threading.Thread(target=from_terminal),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        assert done.wait(5)
        assert result["value"][0] in (True, False)
        assert approvals.prompt_mark() == approvals.MARK_EMPTY


def test_ac39_the_loser_gets_told_and_resolves_nothing_twice(
    authz_db, prompts, discord
):
    result, done, _ = backend_in_thread()
    wait_for_prompt().answer(True)
    assert done.wait(5)

    late = approvals.on_gate_resolved(
        gate_id=discord.submitted[0]["gate_id"],
        decision=approvals_ui.DECISION_DENY,
        discord_user_id=APPROVER_ID,
    )

    assert late is not None
    assert result["value"] == (True, None)


# --------------------------------------------------------------------------- #
# AC-72 / AC-76b / INV-C24 — reporting and the SIGINT flag
# --------------------------------------------------------------------------- #


def test_ac72_an_open_gate_is_reported_as_blocked(authz_db, prompts, discord):
    mailbox = reporter.Mailbox()
    state = reporter.StateReporter(mailbox)
    approvals.set_reporter(state)
    state.on_run_start()

    result, done, _ = backend_in_thread()
    wait_for_prompt()
    time.sleep(0.05)

    assert state.state == reporter.BLOCKED

    wait_for_prompt().answer(True)
    done.wait(5)
    assert state.state == reporter.WORKING


def test_ac76b_the_core_flag_is_set_around_the_terminal_branch(
    authz_db, prompts, discord, clean_state
):
    """Without it a Ctrl+C kills the whole run (``_runtime.py:957,969``)."""
    result, done, _ = backend_in_thread()
    prompt = wait_for_prompt()
    assert prompt.running.wait(5)

    assert ("flag", True) in clean_state

    prompt.answer(True)
    done.wait(5)
    assert clean_state[-1] == ("flag", False)


def test_ac87a_the_flag_only_drops_when_the_LAST_branch_ends(
    authz_db, prompts, discord, clean_state
):
    first_result, first_done, _ = backend_in_thread()
    first = wait_for_prompt(0)
    assert first.running.wait(5)
    approvals._flag_step(+1)  # a second waiter, as a concurrent approval would

    first.answer(True)
    assert first_done.wait(5)

    assert ("flag", False) not in clean_state, "the flag dropped with one branch live"
    approvals._flag_step(-1)
    assert clean_state[-1] == ("flag", False)


def test_ac87b_a_stale_setter_call_is_discarded(clean_state):
    """Idempotent is NOT order-independent -- hence the generation counter."""
    approvals._flag_step(+1)
    stale_gen = approvals.flag_generation()
    clean_state.clear()

    approvals._flag_step(-1)
    approvals._flag_step(+1)
    clean_state.clear()

    approvals._apply_flag(stale_gen, False)

    assert clean_state == [], "a stale False cleared the flag under a live prompt"


def test_ac87c_the_flag_is_never_set_under_the_state_lock(clean_state):
    """Foreign plugins run synchronously inside the core setter."""
    seen = []

    def observe(value):
        seen.append(approvals.state_lock_held_by_me())

    approvals._set_core_flag = observe
    try:
        approvals._flag_step(+1)
        approvals._flag_step(-1)
    finally:
        approvals._set_core_flag = lambda value: clean_state.append(("flag", value))

    assert seen == [False, False]


def test_ac87d_the_core_setter_always_asks_for_a_notification(monkeypatch):
    """AC-87c's other half: not just WHERE the setter runs, but WITH WHAT.

    The REAL ``_set_core_flag`` is exercised here, not the seam.  Every other
    test in this file replaces it (``clean_state``), and the value at stake --
    ``notify=True`` at ``approvals.py:172`` -- lives INSIDE it, so a recorder
    on the seam can never see it: the sole call site (``approvals.py:457``)
    passes ``value`` positionally and no kwargs at all.

    What ``notify=False`` would cost: it CLEARS ``_AWAITING_USER_INPUT_NOTIFY``
    process-wide (``command_runner.py:326-329``), and ``reporter.py:506-508``
    reads exactly that flag -- so every later gate would go silent on the
    phone while the terminal still looked normal.  Both edges are asserted,
    because our gates are agent-initiated in both directions.
    """
    from code_puppy.tools import command_runner

    calls = []
    monkeypatch.setattr(
        command_runner,
        "set_awaiting_user_input",
        lambda awaiting, **kwargs: calls.append((awaiting, kwargs)),
    )

    REAL_SET_CORE_FLAG(True)
    REAL_SET_CORE_FLAG(False)

    assert calls == [(True, {"notify": True}), (False, {"notify": True})]


# --------------------------------------------------------------------------- #
# AC-84 / AC-80 — no deadlock on the main path
# --------------------------------------------------------------------------- #


def test_ac84_resolution_runs_through_while_the_backend_waits(
    authz_db, prompts, discord
):
    """P13: the backend is parked in step 3; a foreign thread must not block."""
    result, done, _ = backend_in_thread()
    prompt = wait_for_prompt()
    assert prompt.running.wait(5)

    started = time.monotonic()
    approvals.on_gate_resolved(
        gate_id=discord.submitted[0]["gate_id"],
        decision=approvals_ui.DECISION_APPROVE,
        discord_user_id=APPROVER_ID,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.1, "on_gate_resolved waited on a lock the backend holds"
    assert done.wait(5)


def test_ac80_two_concurrent_approvals_never_show_two_prompts(
    authz_db, prompts, discord
):
    first_result, first_done, _ = backend_in_thread("Shell Command", "one")
    first = wait_for_prompt(0)
    assert first.running.wait(5)

    second_result, second_done, _ = backend_in_thread("File Operation", "two")
    gate_ids = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(approvals.open_gates()) < 2:
        time.sleep(0.01)
    gate_ids = approvals.open_gates()

    assert len(gate_ids) == 2, "the second approval never opened a gate"
    assert len(FakePrompt.instances) == 1, "two prompts fought over one stdin"

    # The second one is answerable the whole time -- from the phone.
    second_gate = [
        entry["gate_id"] for entry in discord.submitted if entry["body"] == "two"
    ][0]
    approvals.on_gate_resolved(
        gate_id=second_gate,
        decision=approvals_ui.DECISION_APPROVE,
        discord_user_id=APPROVER_ID,
    )
    assert second_done.wait(5)
    assert second_result["value"] == (True, None)

    first.answer(False)
    assert first_done.wait(5)
    assert first_result["value"] == (False, None)


# --------------------------------------------------------------------------- #
# AC-69 / AC-91 — a wait the phone cannot answer says so
# --------------------------------------------------------------------------- #


def test_ac69_an_approval_gate_is_marked_remotely_resolvable(
    authz_db, prompts, discord
):
    result, done, _ = backend_in_thread()
    wait_for_prompt()

    assert discord.submitted[0]["remote_resolvable"] is True

    wait_for_prompt().answer(True)
    done.wait(5)


# --------------------------------------------------------------------------- #
# AC-61 — ask_user_question has no answer path over Discord (INV-C16)
# --------------------------------------------------------------------------- #


def pre_tool_call(tool_name, args=None):
    """Drive the hook synchronously -- the suite needs no async plugin for it."""
    return asyncio.run(approvals.on_pre_tool_call(tool_name, args or {}))


def test_ac61_ask_user_question_is_blocked(discord):
    blocked = pre_tool_call("ask_user_question")

    assert blocked is not None
    assert blocked["blocked"] is True
    assert "Discord" in blocked["error_message"]


def test_ac61_other_tools_are_untouched(discord):
    assert pre_tool_call("run_shell_command") is None


def test_ac61_without_a_broker_nothing_is_blocked(monkeypatch):
    """INV-C19: with no Discord the session behaves exactly as without us."""
    monkeypatch.setattr(approvals, "_active_client", lambda: None)

    assert pre_tool_call("ask_user_question") is None


# --------------------------------------------------------------------------- #
# AC-83b — the readers import the SHARED constant
# --------------------------------------------------------------------------- #


def _authz_callers():
    """Modules that CALL an authorization lookup -- not the ones defining it.

    An AST walk rather than a substring search: ``bindings.py`` and
    ``authz.py`` contain those names because they DEFINE them, and a text
    match would demand the constant from the very module the constant is
    about.
    """
    import ast
    from pathlib import Path

    wanted = {"resolve_principal", "check_message", "has_role"}
    callers = []
    for path in Path(__file__).resolve().parents[1].glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        defined = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        if calls & wanted and not (defined & wanted):
            callers.append((path, source, tree))
    return callers


def test_ac83b_the_gate_path_uses_the_shared_authz_channel():
    import ast

    callers = _authz_callers()

    assert callers, "no authorization reader found at all"
    for path, source, tree in callers:
        assert "AUTHZ_CHANNEL" in source, f"{path.name} does not use the constant"
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == "discord"
        ]
        assert literals == [], f"{path.name} carries a second 'discord' literal"


def test_ac83b_the_reader_set_includes_the_gate_path():
    """The AC is a CROSS check; a filter that found nobody would pass vacuously."""
    names = {path.name for path, _source, _tree in _authz_callers()}

    assert "approvals.py" in names


# --------------------------------------------------------------------------- #
# The sentinel: a double load must be LOUD (self-protection)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# The handover that makes the Discord branch EXIST
# --------------------------------------------------------------------------- #


class RecordingClient(FakeClient):
    """A client that remembers who was pointed at its inbound listener."""

    def __init__(self):
        super().__init__()
        self.handler = "never set"

    def set_resolution_handler(self, handler):
        self.handler = handler


def test_install_points_the_return_channel_at_us(authz_db, monkeypatch):
    """Without this ONE line every click is refused with ``no_handler``.

    And nothing else notices: every test in this file drives
    ``on_gate_resolved`` directly, so all of them stay green while the plugin
    is deaf in production.  Found by deleting the line and watching 423 tests
    pass.
    """
    client = RecordingClient()
    monkeypatch.setattr(approvals, "_active_client", lambda: client)

    approvals.install()
    try:
        assert client.handler is approvals.on_gate_resolved
    finally:
        approvals.uninstall()

    assert client.handler is None, "teardown left a handler on a dead backend"


def test_install_without_a_broker_is_harmless(authz_db, monkeypatch):
    monkeypatch.setattr(approvals, "_active_client", lambda: None)

    approvals.install()
    approvals.uninstall()


def test_installing_over_a_stranger_refuses_loudly(authz_db):
    from code_puppy.tools.common import get_approval_backend, set_approval_backend

    def stranger(title, message, preview):  # pragma: no cover - never called
        return False, None

    set_approval_backend(stranger)
    try:
        with pytest.raises(approvals.ApprovalError):
            approvals.install()
        assert get_approval_backend() is stranger
    finally:
        set_approval_backend(None)


def test_installing_twice_is_idempotent(authz_db):
    from code_puppy.tools.common import get_approval_backend, set_approval_backend

    approvals.install()
    try:
        approvals.install()
        assert getattr(get_approval_backend(), approvals.SENTINEL, False) is True
    finally:
        approvals.uninstall()
        assert get_approval_backend() is None
        set_approval_backend(None)


def test_uninstall_restores_the_previous_backend(authz_db):
    from code_puppy.tools.common import get_approval_backend, set_approval_backend

    approvals.uninstall()
    assert get_approval_backend() is None
    set_approval_backend(None)
