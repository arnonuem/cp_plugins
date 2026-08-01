"""C4b — the REAL prompt: operable like the core's, and interruptible (AC-89).

Everything else in this layer is tested against a prompt double, which is
right: the double keeps the concurrency tests deterministic.  But a double
cannot prove that the thing we SHIP works, and the bridge builds its own
``Application`` (the core helper gives out no handle), so INV-C19's promise --
\"exactly as it behaves without the plugin\" -- needs evidence on the level a
human actually touches: the keys.

So this suite drives the real ``prompt_toolkit`` app through a pipe input and
presses real keys.  ``DummyOutput`` keeps it off the terminal; the INPUT side,
which is what AC-89 is about, is genuine.
"""

from __future__ import annotations

import contextlib
import threading
import time

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cp_discord import approvals_prompt
from cp_discord.approvals_prompt import TerminalPrompt


@pytest.fixture
def terminal(monkeypatch):
    """Neutralise the two terminal takeovers.

    On a test runner there is no bottom bar and no key listener; both would
    otherwise reach into the real process.  They are proven separately, on
    their own, further down -- so switching them off here does not hide them.
    """
    monkeypatch.setattr(
        "code_puppy.messaging.run_ui.suspended_run_ui",
        lambda: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        "code_puppy.agents._key_listeners.suspended_key_listener",
        lambda: contextlib.nullcontext(),
    )


def drive(prompt: TerminalPrompt, keys: str, *, on_live=lambda: True, after_live=None):
    """Run *prompt* on a worker thread and press *keys* once it is live.

    The pipe is installed through ``create_app_session`` INSIDE that thread:
    an ``Application`` resolves its input and output in its constructor from
    the ambient session (``application.py:267``), and a session is a
    ContextVar -- which a new thread does not inherit.
    """
    result = {}
    live = threading.Event()
    ready = threading.Event()
    handle = {}

    def go_live():
        allowed = on_live()
        live.set()
        return allowed

    def run():
        with create_pipe_input() as pipe:
            handle["pipe"] = pipe
            ready.set()
            with create_app_session(input=pipe, output=DummyOutput()):
                try:
                    result["value"] = prompt.run(on_live=go_live)
                except BaseException as error:  # pragma: no cover - surfaced below
                    result["error"] = error

    thread = threading.Thread(target=run, name="real-prompt", daemon=True)
    thread.start()
    assert ready.wait(5), "the pipe was never opened"
    assert live.wait(5), "the prompt never went live"
    time.sleep(0.05)
    if keys:
        handle["pipe"].send_text(keys)
    if after_live is not None:
        after_live()
    thread.join(5)
    assert not thread.is_alive(), "the prompt never came back"
    assert "error" not in result, result.get("error")
    return result


def run_prompt(keys: str, *, on_live=lambda: True):
    prompt = TerminalPrompt("Shell Command", "`rm -rf /`")
    return prompt, drive(prompt, keys, on_live=on_live)


# --------------------------------------------------------------------------- #
# AC-89 — the same keys, the same meaning
# --------------------------------------------------------------------------- #


def test_enter_on_the_first_choice_approves(terminal):
    _prompt, result = run_prompt("\r")

    assert result.get("value") is True


def test_arrow_down_then_enter_rejects(terminal):
    _prompt, result = run_prompt("\x1b[B\r")

    assert result.get("value") is False


def test_ctrl_n_walks_like_arrow_down(terminal):
    """The core binds both; a prompt that only takes arrows is not the same."""
    _prompt, result = run_prompt("\x0e\r")

    assert result.get("value") is False


def test_ctrl_p_walks_back_up(terminal):
    _prompt, result = run_prompt("\x0e\x10\r")

    assert result.get("value") is True


def test_arrow_up_wraps_to_the_last_choice(terminal):
    _prompt, result = run_prompt("\x1b[A\r")

    assert result.get("value") is False


def test_ctrl_c_is_no_answer_at_all(terminal):
    """A cancel is a branch ABORT, never a rejection (INV-C7, AC-33).

    Returning ``False`` here would make Ctrl+C mean \"deny\" -- and with a gate
    open on somebody's phone that is a decision nobody made.
    """
    _prompt, result = run_prompt("\x03")

    assert result.get("value") is None


# --------------------------------------------------------------------------- #
# AC-48 / AC-64b — the Discord branch reaching in
# --------------------------------------------------------------------------- #


def test_a_foreign_thread_substitutes_the_result(terminal):
    """AC-48: ``exit(result=...)``, not ``exit()`` -- a bare abort would deny."""
    prompt = TerminalPrompt("Shell Command", "`rm -rf /`")
    ticks = []

    def answer_from_elsewhere():
        # Also AC-50: the main thread ticks while the prompt is up.
        for _ in range(5):
            ticks.append(time.monotonic())
            time.sleep(0.01)
        assert prompt.exit_with(True) is True

    result = drive(prompt, "", on_live=lambda: True, after_live=answer_from_elsewhere)

    assert result["value"] is True, "a Discord approval came back as a rejection"
    assert len(ticks) == 5


def test_a_prompt_that_is_told_not_to_go_live_shows_nothing(terminal):
    """AC-64b: the gate was resolved while we were building."""
    _prompt, result = run_prompt("", on_live=lambda: False)

    assert result.get("value") is None


def test_exit_with_before_the_prompt_runs_is_harmless(terminal):
    prompt = TerminalPrompt("Shell Command", "ls")

    assert prompt.exit_with(True) is False


# --------------------------------------------------------------------------- #
# The terminal duties the core assigns to whoever takes stdin
# --------------------------------------------------------------------------- #


def test_the_prompt_suspends_the_run_ui_and_the_key_listener(monkeypatch):
    """Both, and around the WHOLE prompt (``command_runner.py:314-317``).

    ``suspended_key_listener`` would come along with the core helper; we do
    not use it, so both have to be ours -- otherwise our Application and the
    bottom bar write to one terminal at once.
    """
    import contextlib

    order = []

    @contextlib.contextmanager
    def track(name):
        order.append(f"enter:{name}")
        try:
            yield
        finally:
            order.append(f"exit:{name}")

    monkeypatch.setattr(
        "code_puppy.messaging.run_ui.suspended_run_ui", lambda: track("run_ui")
    )
    monkeypatch.setattr(
        "code_puppy.agents._key_listeners.suspended_key_listener",
        lambda: track("keys"),
    )

    prompt = TerminalPrompt("Shell Command", "ls")
    monkeypatch.setattr(prompt, "_build", lambda: _InstantApp())
    prompt.run(on_live=lambda: True)

    assert order == ["enter:run_ui", "enter:keys", "exit:keys", "exit:run_ui"]


class _InstantApp:
    """An Application that answers immediately -- the wrapping is the subject."""

    is_running = True
    future = None

    async def run_async(self, pre_run=None):
        if pre_run is not None:
            pre_run()
        return approvals_prompt.APPROVE_CHOICE


def test_stdin_is_checked_through_the_cores_own_predicate(monkeypatch):
    """INV-C19: we stand BEFORE the core's guard, so we must not invent one."""
    import code_puppy.tools.common as common

    monkeypatch.setattr(common, "_stdin_supports_interactive_approval", lambda: False)
    assert approvals_prompt.stdin_is_interactive() is False

    monkeypatch.setattr(common, "_stdin_supports_interactive_approval", lambda: True)
    assert approvals_prompt.stdin_is_interactive() is True


def test_the_prompt_offers_no_feedback_choice():
    """Feedback has no path back through a gate; offering it would lie."""
    assert len(approvals_prompt.CHOICES) == 2
    assert not any("feedback" in choice.lower() for choice in approvals_prompt.CHOICES)


def test_the_header_carries_title_message_and_preview():
    prompt = TerminalPrompt("File Operation", "write config.py", "--- a\n+++ b")

    header = prompt._header()

    assert "File Operation" in header
    assert "write config.py" in header
    assert "+++ b" in header
