"""The HINWEG: a gate travels session -> broker -> Discord thread (SPEC §3.2b).

This suite owns the WIRE half of AC-90/91/92: the sixth method ``M_GATE``, the
client's ``submit_gate``/``close_gate``, and the gateway's ``post_gate``/
``finish_gate``.  The end-to-end proof through C4 lives in ``test_approvals``;
the two together are AC-90.

Why a separate file: this exercises W1's and W2's modules, not C4's policy.
Their existing suites (``test_broker`` 107, ``test_client`` 47) stay untouched,
so the additions are proven HERE and cannot be mistaken for edits to theirs.

Nothing here needs a bot token: the gateway runs on its own loop against a
fake channel, and the views it builds are REAL ``discord.ui.View`` objects --
which is the point, because the view is what carries the buttons, and building
one requires a running event loop (measured: ``discord/ui/core.py:79`` calls
``asyncio.get_running_loop()``).  That single fact is why the broker hands the
gateway a view FACTORY rather than a view.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest

from cp_discord import broker_election as election
from cp_discord import approvals_ui, broker_server, broker_threads, client


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


@pytest.fixture
def bridge_dir(tmp_path, monkeypatch):
    target = tmp_path / "cp_discord"
    monkeypatch.setenv(election.BRIDGE_DIR_ENV_VAR, str(target))
    return target


class FakeMessage:
    """What ``thread.send`` returns: something editable, and nothing else."""

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
    """A Discord thread with just the surface the gate path touches."""

    def __init__(self, thread_id: int, name: str) -> None:
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

    async def delete(self):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("INV-C3: threads are archived, never deleted")


class FakeChannel:
    def __init__(self) -> None:
        self.threads: list[FakeThread] = []
        self._next_id = 2000

    async def create_thread(self, *, name, auto_archive_duration=None, **_kwargs):
        self._next_id += 1
        thread = FakeThread(self._next_id, name)
        self.threads.append(thread)
        return thread

    async def send(self, content, **kwargs):
        return FakeMessage(content, kwargs.get("view"), kwargs.get("allowed_mentions"))


class FakeInteraction:
    """One button press by a given Discord user."""

    def __init__(self, user_id: str) -> None:
        self.user = type("User", (), {"id": user_id})()
        self.deferred = False
        self.replies: list[str] = []
        outer = self

        class _Response:
            async def defer(self):
                outer.deferred = True

            async def send_message(self, text, ephemeral=False):
                outer.replies.append(text)

        self.response = _Response()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def gateway(channel):
    instance = broker_threads.DiscordGateway()
    instance.start_loop()
    instance.set_channel(channel)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def broker(bridge_dir, gateway):
    instance = broker_server.Broker(gateway, token="s3cret")
    instance.start()
    election.write_portfile(instance.address)
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture
def session(bridge_dir):
    made = []

    def make(**kwargs):
        kwargs.setdefault("title", "cp_plugins/main")
        instance = client.SessionClient(**kwargs)
        made.append(instance)
        return instance

    yield make
    for instance in made:
        instance.stop()


def registered(session_factory, gateway):
    """A started, registered session whose thread already exists."""
    instance = session_factory()
    instance.start()
    assert instance.register_now() is True
    gateway.wait_idle()
    return instance


def click(view, label: str, user_id: str = "42") -> FakeInteraction:
    """Press the button carrying *label* and run its callback to completion."""
    interaction = FakeInteraction(user_id)
    for item in view.children:
        if item.label == label:
            asyncio.run(item.callback(interaction))
            return interaction
    raise AssertionError(f"no button labelled {label!r} in {view.children!r}")


def posted_gate(channel: FakeChannel) -> FakeMessage:
    assert channel.threads, "no thread was ever opened"
    sent = channel.threads[0].sent
    assert sent, "nothing was posted into the thread"
    return sent[-1]


# --------------------------------------------------------------------------- #
# AC-90 (wire half) — a gate reaches the thread WITH buttons
# --------------------------------------------------------------------------- #


def test_the_gate_frame_reaches_the_thread_with_a_view(
    broker, gateway, session, channel
):
    """§3.2b: without ``view=`` the thread gets text nobody can answer."""
    instance = registered(session, gateway)

    assert instance.submit_gate("g1", "Shell Command", "`rm -rf /`") is True
    gateway.wait_idle()

    message = posted_gate(channel)
    assert message.view is not None
    assert [item.label for item in message.view.children] == [
        approvals_ui.APPROVE_LABEL,
        approvals_ui.DENY_LABEL,
    ]
    assert "Shell Command" in message.content


def test_a_click_delivers_the_resolution_to_the_session(
    broker, gateway, session, channel
):
    """The missing link: without it ``deliver_resolution`` is dead code."""
    seen = {}
    arrived = threading.Event()

    def handler(**kwargs):
        seen.update(kwargs)
        arrived.set()

    instance = registered(session, gateway)
    instance.set_resolution_handler(handler)
    instance.submit_gate("g1", "Shell Command", "`rm -rf /`")
    gateway.wait_idle()

    interaction = click(posted_gate(channel).view, approvals_ui.APPROVE_LABEL, "4242")

    assert arrived.wait(2.0)
    assert seen == {
        "gate_id": "g1",
        "decision": approvals_ui.DECISION_APPROVE,
        "discord_user_id": "4242",
    }
    assert interaction.deferred is True, "Discord drops an un-acked interaction"


def test_the_deny_button_carries_the_other_decision(broker, gateway, session, channel):
    seen = {}
    arrived = threading.Event()
    instance = registered(session, gateway)
    instance.set_resolution_handler(
        lambda **kwargs: (seen.update(kwargs), arrived.set())
    )
    instance.submit_gate("g1", "Shell Command", "`rm -rf /`")
    gateway.wait_idle()

    click(posted_gate(channel).view, approvals_ui.DENY_LABEL)

    assert arrived.wait(2.0)
    assert seen["decision"] == approvals_ui.DECISION_DENY


def test_a_gate_post_pings_nobody(broker, gateway, session, channel):
    """A gate quotes the command verbatim; an ``@everyone`` in it must not fire."""
    instance = registered(session, gateway)
    instance.submit_gate("g1", "Shell Command", "echo @everyone")
    gateway.wait_idle()

    assert posted_gate(channel).allowed_mentions is not None


# --------------------------------------------------------------------------- #
# AC-91 — ``remote_resolvable=False`` posts NO widget
# --------------------------------------------------------------------------- #


def test_a_locally_only_gate_gets_no_widget(broker, gateway, session, channel):
    """INV-C23: a button that cannot resolve anything is worse than none."""
    instance = registered(session, gateway)

    assert (
        instance.submit_gate("g1", "Shell Command", "ls", remote_resolvable=False)
        is True
    )
    gateway.wait_idle()

    message = posted_gate(channel)
    assert message.view is None
    assert broker_server.LOCAL_ONLY_MARKER in message.content


def test_a_locally_only_gate_is_still_readable(broker, gateway, session, channel):
    instance = registered(session, gateway)
    instance.submit_gate(
        "g1", "File Operation", "write config.py", remote_resolvable=False
    )
    gateway.wait_idle()

    content = posted_gate(channel).content
    assert "File Operation" in content
    assert "write config.py" in content


# --------------------------------------------------------------------------- #
# AC-92 — a failed HINWEG never disturbs the session (INV-C1)
# --------------------------------------------------------------------------- #


def test_submitting_without_a_broker_fails_quietly(session, bridge_dir):
    instance = session()

    started = time.monotonic()
    assert instance.submit_gate("g1", "Shell Command", "ls") is False
    assert time.monotonic() - started < 1.0


def test_submitting_to_a_dead_broker_fails_quietly(session, broker, gateway):
    instance = registered(session, gateway)
    broker.stop()

    assert instance.submit_gate("g1", "Shell Command", "ls") is False


def test_a_gateway_that_explodes_does_not_break_the_broker(bridge_dir, session):
    """INV-C1: Discord being broken is Discord's problem."""

    class ExplodingGateway:
        def __getattr__(self, _name):
            def explode(*_args, **_kwargs):
                raise RuntimeError("boom")

            return explode

    instance_broker = broker_server.Broker(ExplodingGateway(), token="s3cret")
    instance_broker.start()
    election.write_portfile(instance_broker.address)
    try:
        instance = session()
        instance.start()
        assert instance.register_now() is True
        assert instance.submit_gate("g1", "Shell Command", "ls") is True
    finally:
        instance_broker.stop()


# --------------------------------------------------------------------------- #
# The frame itself
# --------------------------------------------------------------------------- #


def raw_call(broker_instance, payload):
    address = broker_instance.address
    with socket.create_connection((address.host, address.port), timeout=5) as sock:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        with sock.makefile("r", encoding="utf-8") as stream:
            return json.loads(stream.readline())


def gate_frame(broker_instance, session_id, *, seq=99, **params):
    payload = {
        "gate_id": "g1",
        "title": "Shell Command",
        "body": "ls",
        "remote_resolvable": True,
        "status": broker_server.GATE_OPEN,
    }
    payload.update(params)
    return raw_call(
        broker_instance,
        {
            "token": broker_instance.token,
            "method": broker_server.M_GATE,
            "session_id": session_id,
            "seq": seq,
            "params": payload,
        },
    )


def test_a_gate_without_a_token_is_refused(broker, gateway, session):
    instance = registered(session, gateway)

    response = raw_call(
        broker,
        {
            "method": broker_server.M_GATE,
            "session_id": instance.session_id,
            "params": {"gate_id": "g1"},
        },
    )

    assert response["ok"] is False
    assert response["error"] == broker_server.ERR_UNAUTHORIZED


def test_a_gate_from_an_unknown_session_is_refused(broker, gateway):
    response = gate_frame(broker, "cp_discord:nobody")

    assert response["ok"] is False
    assert response["error"] == broker_server.ERR_UNKNOWN_SESSION


def test_a_gate_without_an_id_is_a_bad_request(broker, gateway, session):
    instance = registered(session, gateway)

    response = gate_frame(broker, instance.session_id, gate_id="")

    assert response["ok"] is False
    assert response["error"] == broker_server.ERR_BAD_REQUEST


def test_a_gate_is_acked_without_a_result(broker, gateway, session):
    """§3.2b: the resolution comes back later over §3.2a, not in this answer."""
    instance = registered(session, gateway)

    response = gate_frame(broker, instance.session_id)

    assert response == {"ok": True}


def test_a_replayed_gate_frame_is_not_posted_twice(broker, gateway, session, channel):
    """AC-8 covers the sixth method too: a retry must stay idempotent."""
    instance = registered(session, gateway)

    gate_frame(broker, instance.session_id, seq=101)
    gateway.wait_idle()
    gate_frame(broker, instance.session_id, seq=101)
    gateway.wait_idle()

    assert len(channel.threads[0].sent) == 1


def test_a_re_sent_gate_under_a_new_seq_is_still_posted_once(
    broker, gateway, session, channel
):
    """A healed retry carries a NEW envelope, so ``seq`` cannot catch it."""
    instance = registered(session, gateway)

    gate_frame(broker, instance.session_id, seq=101)
    gateway.wait_idle()
    gate_frame(broker, instance.session_id, seq=102)
    gateway.wait_idle()

    assert len(channel.threads[0].sent) == 1


# --------------------------------------------------------------------------- #
# Closing a gate: AC-37 / AC-39 (the loser is told)
# --------------------------------------------------------------------------- #


def test_closing_a_gate_edits_the_message_and_kills_the_buttons(
    broker, gateway, session, channel
):
    """AC-37: a terminal answer must not leave a live button in the channel."""
    instance = registered(session, gateway)
    instance.submit_gate("g1", "Shell Command", "ls")
    gateway.wait_idle()

    assert instance.close_gate("g1", "im Terminal entschieden") is True
    gateway.wait_idle()

    message = posted_gate(channel)
    assert message.edits, "the gate message was never finalised"
    assert "im Terminal entschieden" in message.content
    assert all(item.disabled for item in message.view.children)


def test_closing_an_unknown_gate_is_harmless(broker, gateway, session):
    instance = registered(session, gateway)

    assert instance.close_gate("nope", "decided") is True
    gateway.wait_idle()


def test_closing_a_widgetless_gate_still_reports_the_outcome(
    broker, gateway, session, channel
):
    instance = registered(session, gateway)
    instance.submit_gate("g1", "Shell Command", "ls", remote_resolvable=False)
    gateway.wait_idle()

    instance.close_gate("g1", "im Terminal entschieden")
    gateway.wait_idle()

    assert "im Terminal entschieden" in channel.threads[0].sent[-1].content


def test_a_click_after_closing_is_answered_but_not_delivered(
    broker, gateway, session, channel
):
    """AC-39: a double click, or one on a gate the terminal already answered."""
    seen = []
    instance = registered(session, gateway)
    instance.set_resolution_handler(lambda **kwargs: seen.append(kwargs))
    instance.submit_gate("g1", "Shell Command", "ls")
    gateway.wait_idle()
    message = posted_gate(channel)
    instance.close_gate("g1", "im Terminal entschieden")
    gateway.wait_idle()

    interaction = click(message.view, approvals_ui.APPROVE_LABEL)

    assert seen == []
    assert interaction.replies, "the clicker deserves to know it was too late"


def test_a_second_click_is_not_delivered_twice(broker, gateway, session, channel):
    delivered = []
    instance = registered(session, gateway)
    instance.set_resolution_handler(lambda **kwargs: delivered.append(kwargs))
    instance.submit_gate("g1", "Shell Command", "ls")
    gateway.wait_idle()
    view = posted_gate(channel).view

    click(view, approvals_ui.APPROVE_LABEL)
    click(view, approvals_ui.DENY_LABEL)

    assert len(delivered) == 1
    assert delivered[0]["decision"] == approvals_ui.DECISION_APPROVE


# --------------------------------------------------------------------------- #
# AC-47 — the files W4 touched stay under 600 lines
# --------------------------------------------------------------------------- #


def test_ac47_every_source_file_stays_under_600_lines():
    """Measured over ALL of them, not a list somebody has to remember to grow."""
    from pathlib import Path

    plugin = Path(__file__).resolve().parents[1]
    oversized = {
        path.name: path.read_text(encoding="utf-8").count("\n")
        for path in plugin.glob("*.py")
        if path.read_text(encoding="utf-8").count("\n") > 600
    }

    assert oversized == {}
