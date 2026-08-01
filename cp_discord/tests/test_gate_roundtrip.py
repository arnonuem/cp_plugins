"""AC-90 end to end: a gate in C4 becomes a button, and the button answers it.

Every other suite proves ONE hop.  This one proves the hop CHAIN, because
that is where the R15 defect lived: each layer was correct on its own, and
between them there was no wire at all.  ``deliver_resolution`` was fully
tested and called by nobody; the CAS in C4 had one participant.

So nothing here is mocked between the layers: a real broker on loopback, a
real session client with its real listener, the real gateway on its own loop,
the real approval backend, and real ``discord.ui`` buttons.  Only Discord's
socket is a fake -- a channel that records instead of sending.

That also makes this the home of the ACs that need a whole system: the return
channel while the session is parked on a gate (AC-67), delivery after a
re-election (AC-85b), and the state the tree is left in (AC-58n-b).
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from cp_discord import broker_election as election
from cp_discord import (
    approvals,
    approvals_ui,
    bindings,
    broker_server,
    broker_threads,
    client,
    constants,
    reporter,
)
from cp_discord.bindings import Role


APPROVER_ID = "4242"
STRANGER_ID = "9999"


# --------------------------------------------------------------------------- #
# Harness -- the real stack, one fake channel
# --------------------------------------------------------------------------- #


class FakeMessage:
    def __init__(self, content, view, allowed_mentions):
        self.content = content
        self.view = view
        self.allowed_mentions = allowed_mentions
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(dict(kwargs))
        if "content" in kwargs:
            self.content = kwargs["content"]
        if "view" in kwargs:
            self.view = kwargs["view"]


class FakeThread:
    def __init__(self, thread_id, name):
        self.id = thread_id
        self.name = name
        self.archived = False
        self.sent: list[FakeMessage] = []

    async def send(self, content, **kwargs):
        message = FakeMessage(
            content, kwargs.get("view"), kwargs.get("allowed_mentions")
        )
        self.sent.append(message)
        return message

    async def edit(self, **kwargs):
        if "archived" in kwargs:
            self.archived = bool(kwargs["archived"])


class FakeChannel:
    def __init__(self):
        self.threads: list[FakeThread] = []
        self._next_id = 3000

    async def create_thread(self, *, name, auto_archive_duration=None, **_kwargs):
        self._next_id += 1
        thread = FakeThread(self._next_id, name)
        self.threads.append(thread)
        return thread

    async def send(self, content, **kwargs):
        return FakeMessage(content, kwargs.get("view"), kwargs.get("allowed_mentions"))


class FakeInteraction:
    def __init__(self, user_id):
        self.user = type("User", (), {"id": user_id})()
        self.replies: list[str] = []
        outer = self

        class _Response:
            async def defer(self):
                pass

            async def send_message(self, text, ephemeral=False):
                outer.replies.append(text)

        self.response = _Response()


@pytest.fixture
def bridge_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(election.BRIDGE_DIR_ENV_VAR, str(tmp_path / "cp_discord"))
    return tmp_path


@pytest.fixture
def authz_db(tmp_path, monkeypatch):
    monkeypatch.setenv(bindings.DB_PATH_ENV, str(tmp_path / "authz.db"))
    bindings.forget_initialized_paths()
    bindings.bind(constants.AUTHZ_CHANNEL, APPROVER_ID, "wayne")
    bindings.grant("wayne", Role.APPROVER)
    yield
    bindings.forget_initialized_paths()


@pytest.fixture
def channel():
    return FakeChannel()


@pytest.fixture
def stack(bridge_dir, authz_db, channel, monkeypatch):
    """The whole chain, wired exactly as production wires it."""
    gateway = broker_threads.DiscordGateway()
    gateway.start_loop()
    gateway.set_channel(channel)

    broker = broker_server.Broker(gateway, token="s3cret")
    broker.start()
    election.write_portfile(broker.address)

    session = client.SessionClient(title="cp_plugins/main")
    session.start()
    assert session.register_now() is True
    gateway.wait_idle()

    # The two handovers that make the chain a chain.  Both are one line, and
    # every test in every other suite stays green without them.
    session.set_resolution_handler(approvals.on_gate_resolved)
    monkeypatch.setattr(approvals, "_active_client", lambda: session)
    monkeypatch.setattr(approvals, "_stdin_is_interactive", lambda: False)
    approvals.reset_state()

    try:
        yield type(
            "Stack",
            (),
            {
                "gateway": gateway,
                "broker": broker,
                "session": session,
                "channel": channel,
            },
        )()
    finally:
        approvals.reset_state()
        session.stop()
        broker.stop()
        gateway.close()


def posted(channel):
    assert channel.threads, "no thread was opened"
    assert channel.threads[0].sent, "nothing was posted"
    return channel.threads[0].sent[-1]


def press(view, label, user_id=APPROVER_ID):
    interaction = FakeInteraction(user_id)
    for item in view.children:
        if item.label == label:
            asyncio.run(item.callback(interaction))
            return interaction
    raise AssertionError(f"no button {label!r}")


def in_background(fn):
    result = {}
    done = threading.Event()

    def run():
        try:
            result["value"] = fn()
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return result, done


# --------------------------------------------------------------------------- #
# AC-90 — all three layers, one gate
# --------------------------------------------------------------------------- #


def test_ac90_a_gate_becomes_a_button_and_the_button_answers_it(stack):
    """The chain: C4 -> submit_gate -> M_GATE -> post_gate(view=) -> click."""
    result, done = in_background(
        lambda: approvals.approval_backend("Shell Command", "`rm -rf /`")
    )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not stack.channel.threads[0].sent:
        stack.gateway.wait_idle()
        time.sleep(0.02)

    message = posted(stack.channel)
    assert message.view is not None, "the gate reached Discord without buttons"

    press(message.view, approvals_ui.APPROVE_LABEL)

    assert done.wait(5), "the click never reached the waiting backend"
    assert result["value"] == (True, None)


def test_ac90_a_denial_travels_the_same_way(stack):
    result, done = in_background(
        lambda: approvals.approval_backend("Shell Command", "ls")
    )
    _wait_for_post(stack)

    press(posted(stack.channel).view, approvals_ui.DENY_LABEL)

    assert done.wait(5)
    assert result["value"] == (False, None)


def test_ac90_the_gate_message_is_finalised_after_the_click(stack):
    result, done = in_background(
        lambda: approvals.approval_backend("Shell Command", "ls")
    )
    _wait_for_post(stack)
    message = posted(stack.channel)

    press(message.view, approvals_ui.APPROVE_LABEL)
    assert done.wait(5)
    # The closing edit is deliberately OFF the listener thread (INV-C17), so
    # it lands shortly after the backend has already returned.
    _wait_until(lambda: bool(message.edits) if _idle(stack) else False)

    assert message.edits, "the answered gate kept its live buttons"
    assert all(item.disabled for item in message.view.children)


def test_ac74_a_stranger_click_travels_but_is_refused_in_the_session(stack):
    """The whole chain, including the authorization that lives at its END."""
    result, done = in_background(
        lambda: approvals.approval_backend("Shell Command", "ls")
    )
    _wait_for_post(stack)

    interaction = press(
        posted(stack.channel).view, approvals_ui.APPROVE_LABEL, user_id=STRANGER_ID
    )

    assert interaction.replies, "the outsider was not told anything"
    assert not done.wait(0.5), "an unauthorized click resolved a gate"

    press(posted(stack.channel).view, approvals_ui.APPROVE_LABEL)
    assert done.wait(5)


# --------------------------------------------------------------------------- #
# AC-67 — the return channel works WHILE the session is parked on a gate
# --------------------------------------------------------------------------- #


def test_ac67_a_parked_session_still_receives(stack):
    """A session waiting on an approval sends nothing -- and must still hear.

    This is the case that ruled a piggyback design out, and it can only be
    built where a REAL gate exists: in the backend.
    """
    result, done = in_background(
        lambda: approvals.approval_backend("Shell Command", "ls")
    )
    gate_id = _wait_for_gate()

    started = time.monotonic()
    delivered = stack.broker.deliver_resolution(
        stack.session.session_id,
        gate_id,
        approvals_ui.DECISION_APPROVE,
        APPROVER_ID,
    )
    elapsed = time.monotonic() - started

    assert delivered is True
    assert elapsed < 0.1, "INV-C17 budgets 100 ms"
    assert done.wait(5)
    assert result["value"] == (True, None)


# --------------------------------------------------------------------------- #
# AC-85b — a real gate resolves after a re-election, without re-registering
# --------------------------------------------------------------------------- #


def test_ac85b_a_new_broker_reaches_an_existing_session(stack, channel):
    """The token is adopted, so the session need not have called in again."""
    result, done = in_background(
        lambda: approvals.approval_backend("Shell Command", "ls")
    )
    # Waiting for the POST, not merely for the gate: killing the broker while
    # ``submit_gate`` is still in flight would leave the backend with no
    # branch at all, and this test would then prove nothing about delivery.
    _wait_for_post(stack)
    gate_id = _wait_for_gate()

    stack.broker.stop()
    # A fresh Broker loads the persisted register in its constructor -- the
    # very mechanism AC-54 exists for, so the successor knows this session
    # without it having called in again.
    successor = broker_server.Broker(
        stack.gateway, token=election.adopt_or_mint_token()
    )
    successor.start()
    try:
        started = time.monotonic()
        delivered = successor.deliver_resolution(
            stack.session.session_id,
            gate_id,
            approvals_ui.DECISION_APPROVE,
            APPROVER_ID,
        )
        elapsed = time.monotonic() - started
    finally:
        successor.stop()

    assert delivered is True, "the successor could not reach a live session"
    assert elapsed < 0.1
    assert done.wait(5)
    assert result["value"] == (True, None)


# --------------------------------------------------------------------------- #
# INV-C24 / AC-72 — an open gate is BLOCKED, through the real reporter
# --------------------------------------------------------------------------- #


def test_ac72_a_shell_approval_reports_blocked_through_the_real_reporter(stack):
    mailbox = reporter.Mailbox()
    state = reporter.StateReporter(mailbox)
    approvals.set_reporter(state)
    state.on_run_start()
    try:
        result, done = in_background(
            lambda: approvals.approval_backend("Shell Command", "ls")
        )
        _wait_for_post(stack)

        assert state.state == reporter.BLOCKED

        press(posted(stack.channel).view, approvals_ui.APPROVE_LABEL)
        assert done.wait(5)
        assert state.state == reporter.WORKING
    finally:
        approvals.set_reporter(None)


# --------------------------------------------------------------------------- #
# AC-58n-b — the rebuild left nothing broken behind
# --------------------------------------------------------------------------- #


def test_ac58n_b_every_remaining_module_imports():
    """Especially ``approvals_ui``, which W6 left importing a deleted module."""
    import importlib
    from pathlib import Path

    plugin = Path(__file__).resolve().parents[1]
    for path in sorted(plugin.glob("*.py")):
        if path.name == "__init__.py":
            continue
        importlib.import_module(f"cp_discord.{path.stem}")


def test_ac58n_b_collection_is_clean_without_any_ignore():
    """``--collect-only`` over the whole suite, with NO ``--ignore`` (AC-58n-b).

    Run in a subprocess on purpose: a collection error inside this process
    would already have stopped this file from being collected at all.
    """
    import subprocess
    import sys
    from pathlib import Path

    tests = Path(__file__).resolve().parent
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tests),
            "--collect-only",
            "-q",
            "--no-cov",
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(sys.executable).parent),
    )
    output = completed.stdout + completed.stderr

    assert completed.returncode == 0, output
    # The exact words pytest uses for a broken module -- NOT a bare "error",
    # which matches half the TEST NAMES in this suite and would fail whether
    # or not anything is wrong.
    assert "errors during collection" not in output
    assert "ERROR " not in output
    assert "ModuleNotFoundError" not in output


def test_ac58n_b_the_deleted_suites_are_gone():
    from pathlib import Path

    tests = Path(__file__).resolve().parent
    for name in ("test_review_round_8.py", "test_review_round_9.py"):
        assert not (tests / name).exists()
    for name in ("gateway.py", "output.py", "concurrency.py"):
        assert not (tests.parent / name).exists()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _idle(stack) -> bool:
    stack.gateway.wait_idle()
    return True


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)


def _wait_for_post(stack, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stack.gateway.wait_idle()
        if stack.channel.threads and stack.channel.threads[0].sent:
            return
        time.sleep(0.02)
    raise AssertionError("nothing was ever posted into the thread")


def _wait_for_gate(timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gates = approvals.open_gates()
        if gates:
            return gates[0]
        time.sleep(0.01)
    raise AssertionError("no gate was opened")
