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
import logging
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
    """A gate quotes the command verbatim; an ``@everyone`` in it must not fire.

    Asserted on the WIRE form, not on ``is not None``: the connection-wide
    default (``broker_activation.py:233``) does NOT cover this send, because
    py-cord merges the per-send value over the connection's and the explicit
    one wins (measured: ``none().merge(all())`` ->
    ``{'parse': ['everyone', 'users', 'roles']}``).  ``broker_threads.py:216``
    passes exactly such an explicit value, so only ``{'parse': []}`` proves
    the suppression -- ``AllowedMentions()`` is not ``None`` either, and it
    permits ``@everyone``.
    """
    instance = registered(session, gateway)
    instance.submit_gate("g1", "Shell Command", "echo @everyone")
    gateway.wait_idle()

    assert posted_gate(channel).allowed_mentions.to_dict() == {"parse": []}


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


def gate_frame(broker_instance, session_id, *, seq=99, env_id=None, **params):
    """One hand-built ``M_GATE`` frame.

    ``env_id`` is OMITTED unless given, so every existing caller keeps sending
    the envelope it sent before R1: a ``seq`` and no id, which is the bottom
    row of the rule table (SPEC R2.6) and today's monotonicity.
    """
    payload = {
        "gate_id": "g1",
        "title": "Shell Command",
        "body": "ls",
        "remote_resolvable": True,
        "status": broker_server.GATE_OPEN,
    }
    payload.update(params)
    return envelope(
        broker_instance,
        session_id,
        broker_server.M_GATE,
        payload,
        seq=seq,
        env_id=env_id,
    )


def envelope(
    broker_instance, session_id, method, params=None, *, seq=None, env_id=None
):
    """One hand-built frame; ``seq`` and ``env_id`` are omitted when ``None``.

    Omitting rather than sending ``null`` is not a distinction the broker can
    make (``payload.get`` cannot tell them apart) -- it is simply the closer
    model of a sender that does not know the field, which is what R1.3's
    "optional, but only without a ``seq``" is about.
    """
    frame = {
        "token": broker_instance.token,
        "method": method,
        "session_id": session_id,
        "params": {} if params is None else params,
    }
    if seq is not None:
        frame["seq"] = seq
    if env_id is not None:
        frame["env_id"] = env_id
    return raw_call(broker_instance, frame)


def posted_bodies(channel):
    """Everything that reached the session's thread, in order."""
    assert channel.threads, "no thread was ever opened"
    return [message.content for message in channel.threads[0].sent]


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
# R1/R2/R3 — idempotence hangs on the envelope's IDENTITY, not on its number
#
# AC-1, AC-2, AC-3, AC-4, AC-4b, AC-4c, AC-4d, AC-5, AC-29.  All of them are
# STATE tests over hand-built frames: no test has to win a race (TEST_PLAN).
# Every frame that must survive the rule carries an ``env_id`` -- without one
# the bottom row of the rule table applies and the criterion is unmeetable.
# --------------------------------------------------------------------------- #


def test_ac1_two_gates_arriving_out_of_order_are_both_posted(
    broker, gateway, session, channel
):
    """AC-1: the reported failure -- a gate must not be swallowed by a number.

    ``_open_gate`` reports ``gate_opened`` on the reporter's thread while
    ``submit_gate`` sends on the caller's (ANALYSIS A3b), so the higher ``seq``
    can perfectly well arrive first.  Under M1 (monotonicity for every method)
    the second gate is discarded and never reaches the phone.
    """
    instance = registered(session, gateway)

    gate_frame(
        broker, instance.session_id, seq=7, env_id="e-late", gate_id="g2", body="later"
    )
    gateway.wait_idle()
    gate_frame(
        broker,
        instance.session_id,
        seq=5,
        env_id="e-early",
        gate_id="g1",
        body="earlier",
    )
    gateway.wait_idle()

    bodies = posted_bodies(channel)
    assert len(bodies) == 2, f"a gate was swallowed: {bodies!r}"
    assert any("later" in body for body in bodies)
    assert any("earlier" in body for body in bodies)


def test_ac2_an_identical_report_envelope_is_acked_but_not_applied(
    broker, gateway, session, channel
):
    """AC-2 on ``M_REPORT``: the method with no second dedupe of its own.

    Deliberately NOT built like ``test_a_replayed_envelope_is_answered_but_not
    _applied`` (``test_broker.py:924``), which is an ``M_STATE`` test: there the
    monotonicity R2 KEEPS would catch the replay, the ``env_id`` memory would
    never be exercised, and M2 would survive.  ``_on_report`` posts its chunks
    straight out, so a replay that got through would post twice.
    """
    instance = registered(session, gateway)
    params = {"chunks": ["a report"]}

    first = envelope(
        broker,
        instance.session_id,
        broker_server.M_REPORT,
        params,
        seq=50,
        env_id="e-report",
    )
    gateway.wait_idle()
    replay = envelope(
        broker,
        instance.session_id,
        broker_server.M_REPORT,
        params,
        seq=50,
        env_id="e-report",
    )
    gateway.wait_idle()

    assert first == {"ok": True}
    assert replay["ok"] is True, "a retry must be ACKED, or it retries forever"
    assert replay["duplicate"] is True
    assert posted_bodies(channel) == ["a report"]


def test_ac2_the_same_envelope_id_under_another_gate_id_is_still_a_replay(
    broker, gateway, session, channel
):
    """AC-2 on ``M_GATE``, with DIFFERENT gate ids on purpose.

    With the same ``gate_id`` the ``board.is_open`` dedupe
    (``broker_threads.py:244``) would mask the mechanism and M2 would survive
    here too.
    """
    instance = registered(session, gateway)

    gate_frame(
        broker, instance.session_id, seq=5, env_id="e-once", gate_id="g1", body="first"
    )
    gateway.wait_idle()
    replay = gate_frame(
        broker, instance.session_id, seq=5, env_id="e-once", gate_id="g2", body="second"
    )
    gateway.wait_idle()

    assert replay == {"ok": True, "duplicate": True}
    bodies = posted_bodies(channel)
    assert len(bodies) == 1, f"the replayed envelope was applied: {bodies!r}"


def test_ac3_a_stale_state_edge_is_still_discarded(broker, gateway, session, channel):
    """AC-3: ``M_STATE`` KEEPS its monotonicity -- against its own mark (R2).

    The two envelopes carry DIFFERENT ids on purpose: with the same one the
    memory would catch the second and M3 ("drop the monotonicity for
    ``M_STATE``") would survive.  A state edge is a WHOLE picture, so an older
    one arriving late would overwrite a newer one.
    """
    instance = registered(session, gateway)

    fresh = envelope(
        broker,
        instance.session_id,
        broker_server.M_STATE,
        {"state": "working", "message": "newer"},
        seq=6,
        env_id="e-6",
    )
    gateway.wait_idle()
    stale = envelope(
        broker,
        instance.session_id,
        broker_server.M_STATE,
        {"state": "working", "message": "older"},
        seq=5,
        env_id="e-5",
    )
    gateway.wait_idle()

    assert fresh == {"ok": True}
    assert stale == {"ok": True, "duplicate": True}
    assert posted_bodies(channel) == ["newer"]


def test_ac4_a_gate_below_the_shared_high_water_mark_is_applied(
    broker, gateway, session, channel
):
    """AC-4: the shared ``last_seq`` no longer discards anything (R2.2/R2.3).

    The heartbeat carries no ``env_id``, so it climbs the shared counter to 9
    the way it does today; the gate below it must still be posted.
    """
    instance = registered(session, gateway)

    beat = envelope(broker, instance.session_id, broker_server.M_HEARTBEAT, seq=9)
    gate = gate_frame(broker, instance.session_id, seq=5, env_id="e-gate")
    gateway.wait_idle()

    assert beat == {"ok": True}
    assert gate == {"ok": True}
    assert broker.registry.get(instance.session_id).last_seq == 9
    assert len(posted_bodies(channel)) == 1, "the gate never reached the thread"


@pytest.mark.parametrize("gate_first", [True, False])
def test_ac4c_a_gate_and_a_state_edge_that_raced_both_arrive(
    gate_first, broker, gateway, session, channel
):
    """AC-4c (and AC-4b): both arrival orders, both frames applied.

    Whoever loses the draw for the lower number arrives second and used to be
    discarded as a "replay" -- the gate under M1, the state edge under M10
    (state edges back on the shared counter).  Built as a state test in both
    orders rather than as a race: ``_after_gate_posted`` sits between the
    Discord and terminal branches and cannot reproduce the reporter race at
    all (TEST_PLAN).
    """
    instance = registered(session, gateway)

    def send_gate(seq):
        return gate_frame(
            broker, instance.session_id, seq=seq, env_id="e-gate", body="an approval"
        )

    def send_edge(seq):
        return envelope(
            broker,
            instance.session_id,
            broker_server.M_STATE,
            {"state": "blocked", "message": "waiting for approval"},
            seq=seq,
            env_id="e-edge",
        )

    winner, loser = (send_gate, send_edge) if gate_first else (send_edge, send_gate)
    assert winner(7) == {"ok": True}
    gateway.wait_idle()
    assert loser(6) == {"ok": True}, "the second frame was answered as a duplicate"
    gateway.wait_idle()

    bodies = posted_bodies(channel)
    assert any("an approval" in body for body in bodies), (
        f"the gate was swallowed: {bodies!r}"
    )
    assert any("waiting for approval" in body for body in bodies), (
        f"the state edge was swallowed: {bodies!r}"
    )


def test_ac4d_a_frame_without_an_envelope_id_keeps_todays_rule(
    broker, gateway, session, channel
):
    """AC-4d / R1.3: ``env_id`` is optional -- but only WITHOUT a ``seq``.

    Both halves are asserted, because dropping the second one would leave
    ``M_REPORT`` with no protection at all: a client without ``env_id`` retries
    three times (``SEND_ATTEMPTS``) and the report would land three times.
    """
    instance = registered(session, gateway)
    numbered = {"chunks": ["numbered"]}

    unnumbered = envelope(
        broker,
        instance.session_id,
        broker_server.M_REPORT,
        {"chunks": ["no numbers at all"]},
    )
    gateway.wait_idle()
    first = envelope(
        broker, instance.session_id, broker_server.M_REPORT, numbered, seq=40
    )
    gateway.wait_idle()
    replay = envelope(
        broker, instance.session_id, broker_server.M_REPORT, numbered, seq=40
    )
    gateway.wait_idle()

    assert unnumbered == {"ok": True}, "a frame with neither field was refused"
    assert first == {"ok": True}
    assert replay == {"ok": True, "duplicate": True}
    assert posted_bodies(channel) == ["no numbers at all", "numbered"]


@pytest.mark.parametrize("unusable", ["5", True])
def test_ac29_an_unusable_seq_is_not_a_bad_request(
    unusable, broker, gateway, session, channel
):
    """AC-29: the type guards moved with the rule, behaviour unchanged (R2.6).

    ``broker_server.py:241`` lets a ``"seq": "5"`` through WITHOUT comparing it
    today.  Comparing it in the registry instead would raise, and ``_dispatch``
    turns an exception into ``bad_request`` -- a silent behaviour change at the
    network edge, which is what M25 puts back.

    The shared counter is pushed to 40 FIRST, and that is what makes the
    ``True`` case load-bearing: ``bool`` IS an ``int`` in Python, so an
    unguarded ``true`` compares as 1, and only against a counter above 1 does
    the frame get silently discarded instead of posted.  Without the climb the
    bool half of the guard could be deleted and no test would notice.
    """
    instance = registered(session, gateway)

    climb = envelope(
        broker,
        instance.session_id,
        broker_server.M_REPORT,
        {"chunks": ["numbered"]},
        seq=40,
    )
    gateway.wait_idle()
    answer = envelope(
        broker,
        instance.session_id,
        broker_server.M_REPORT,
        {"chunks": ["unusable seq"]},
        seq=unusable,
    )
    gateway.wait_idle()

    assert climb == {"ok": True}
    assert answer == {"ok": True}
    assert posted_bodies(channel) == ["numbered", "unusable seq"]


def test_ac29_an_unusable_envelope_id_is_not_a_bad_request(
    broker, gateway, session, channel
):
    """AC-29, other half: an ``env_id`` that is no ``str`` means "none".

    Unguarded it would be looked up in the memory and a ``dict`` is not
    hashable, so the answer would turn into ``bad_request``.  ``seq`` is
    carried too, so the frame really does travel the bottom row of the rule
    table -- "no id" is what makes that row apply.
    """
    instance = registered(session, gateway)

    answer = envelope(
        broker,
        instance.session_id,
        broker_server.M_REPORT,
        {"chunks": ["unusable env id"]},
        seq=41,
        env_id={"not": "a string"},
    )
    gateway.wait_idle()

    assert answer == {"ok": True}
    assert posted_bodies(channel) == ["unusable env id"]


def test_ac5_ten_concurrent_gates_all_reach_the_thread(
    broker, gateway, session, channel, monkeypatch
):
    """AC-5: N=10 over the REAL client -- 10 distinct ids, 10 posts, 0 lost.

    ``_round_trip`` is module-global, so wrapping it records every envelope
    that actually went out without touching the client's own path (TEST_PLAN).
    Ten threads on ONE session is the sharpest form: they share the ``seq``
    counter and the socket, which is exactly where the gates were being lost.
    """
    instance = registered(session, gateway)
    frames = []
    guard = threading.Lock()
    original = client._round_trip

    def recording(host, port, payload):
        with guard:
            frames.append(json.loads(payload.decode("utf-8")))
        return original(host, port, payload)

    monkeypatch.setattr(client, "_round_trip", recording)

    accepted = []

    def submit(index):
        accepted.append(
            instance.submit_gate(f"g{index}", "Shell Command", f"ls {index}")
        )

    workers = [threading.Thread(target=submit, args=(index,)) for index in range(10)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
    gateway.wait_idle()

    assert accepted == [True] * 10, f"a gate was refused: {accepted!r}"
    gates = [frame for frame in frames if frame["method"] == broker_server.M_GATE]
    assert len(gates) == 10
    assert len({frame["env_id"] for frame in gates}) == 10, (
        "two gates shared an envelope id, so one of them is a replay"
    )
    assert len(posted_bodies(channel)) == 10, "a gate never reached the thread"


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
# §3.2b — WHY the gateway gets a factory and not a view
# --------------------------------------------------------------------------- #


def test_building_a_view_off_the_loop_raises():
    """The measured fact the factory exists for, pinned so it cannot rot.

    ``discord/ui/core.py:79`` calls ``asyncio.get_running_loop()`` inside
    ``View.__init__`` and line 83 makes a future on it.  A gate frame arrives
    on the broker's TCP handler thread, which has NO loop -- so a view built
    where the frame is parsed dies before it can reach Discord.

    Should py-cord ever make that lazy, this test fails and the factory
    indirection can be reconsidered on evidence rather than on a comment.
    """
    import discord

    failure: dict = {}

    def on_a_thread_without_a_loop() -> None:
        try:
            discord.ui.View(timeout=None)
        except BaseException as exc:  # noqa: BLE001 - the type IS the assertion
            failure["exc"] = exc

    thread = threading.Thread(target=on_a_thread_without_a_loop)
    thread.start()
    thread.join()

    assert isinstance(failure.get("exc"), RuntimeError)
    assert "no running event loop" in str(failure["exc"])


def test_the_view_is_built_on_the_gateway_loop_not_the_caller_thread(
    broker, gateway, session, channel
):
    """The factory is INVOKED where Discord's loop runs, not where it is made.

    Without this the previous test only proves a py-cord property; this one
    proves our plumbing actually exploits it.
    """
    instance = registered(session, gateway)

    built_on: list = []
    original = approvals_ui.build_gate_view

    def recording(gate_id, report):
        # The loop AT BUILD TIME -- asserting merely "a different thread" would
        # still pass if the view were built on the broker's handler thread,
        # which has no loop at all and is exactly the crash we are preventing.
        built_on.append(asyncio.get_running_loop())
        return original(gate_id, report)

    approvals_ui.build_gate_view = recording
    try:
        assert instance.submit_gate("g-loop", "Shell Command", "`ls`") is True
        gateway.wait_idle()
    finally:
        approvals_ui.build_gate_view = original

    assert built_on, "the view factory was never invoked"
    assert built_on == [gateway._loop]
    assert posted_gate(channel).view is not None


def test_the_gate_view_is_stored_so_a_press_reaches_its_callback():
    """``store=False`` posts buttons that can never answer.

    py-cord's own wording (``discord/ui/view.py:572``): setting it to False
    "will ignore item callbacks". ``ViewStore.add_view`` bails out on
    ``if not view._store`` (``:990``) BEFORE registering anything, so the
    gate looks perfectly normal and every press dies on Discord's 3 s
    acknowledgement deadline -- "the application did not respond in time".

    Not hypothetical: it shipped that way and made every Approve/Deny
    button in production dead. Calling ``button.callback`` directly in a
    test still passed, which is exactly why this asserts the STORE flag
    instead -- that is the part Discord actually goes through.
    """

    async def report(decision, discord_user_id):
        return None

    async def build():
        return approvals_ui.build_gate_view("g-store", report)

    view = asyncio.new_event_loop().run_until_complete(build())

    assert view._store is True, (
        "the gate view must be stored: an unstored view ignores its button "
        "callbacks and every press times out"
    )


def test_the_gate_view_carries_no_timeout_of_its_own():
    """The buttons must not expire while the agent is still waiting.

    Sibling of the store test above, and it exists for the same reason: the
    view shipped with ``timeout=120`` and produced buttons that looked alive
    and answered nothing.  A user away from the machine lost a full day to
    it -- 120 s is nothing when the whole point is answering from a phone.

    py-cord arms no timeout task at all for ``timeout=None``
    (``discord/ui/view.py:411``), so the callbacks stay registered.  How long
    a gate lives is decided in ``approvals`` -- while a terminal prompt is
    open, nothing expires; unattended, ``_arm_deadline_if_unattended`` sets
    the floor.  A view that timed out on its own would overrule both.
    """

    async def report(decision, discord_user_id):
        return None

    async def build():
        return approvals_ui.build_gate_view("g-timeout", report)

    view = asyncio.new_event_loop().run_until_complete(build())

    assert view.timeout is None, (
        "the gate view must not expire on its own: its buttons have to stay "
        "pressable for as long as the agent is waiting"
    )


def test_the_gate_text_promises_no_deadline():
    """The message must not name a countdown it does not keep.

    Mutation MG4 survived a full run: nothing asserted on this text, so
    putting "expires in 120 s" back would have gone unnoticed -- and that
    sentence is why a user left the buttons alone and lost a day.  While the
    agent waits there is no deadline to name.
    """
    text = approvals_ui.gate_text(
        title="Secrets Guard",
        message="an agent wants to read something",
        preview=None,
        remote_resolvable=True,
    )

    assert "expires" not in text.lower(), (
        f"the gate text promises an expiry that does not exist: {text!r}"
    )
    assert "120" not in text, f"the old countdown is back: {text!r}"
    assert "waiting" in text.lower(), (
        f"the text should say the agent is waiting, got: {text!r}"
    )


# --------------------------------------------------------------------------- #
# AC-17 (R6) — a gate that never reaches Discord is VISIBLE in the log
# --------------------------------------------------------------------------- #


def post_one_gate(manager, board, session_id="cp_discord:a", gate_id="g1"):
    """Drive ``post_gate`` to completion on its own loop, like the pump does."""
    asyncio.run(manager.post_gate(session_id, gate_id, "ls", None, board))


def warnings_in(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]


def test_ac17_a_gate_without_a_thread_warns(channel, caplog):
    """AC-17 / R6.1: today this path is silent -- no log line at ANY level.

    A gate the phone never sees is the exact failure this bridge exists to
    prevent, and the operator's only evidence is the log.
    """
    manager = broker_threads.ThreadManager(lambda: channel)
    board = broker_threads.GateBoard()

    with caplog.at_level(logging.DEBUG, logger="cp_discord.broker_threads"):
        post_one_gate(manager, board)

    messages = warnings_in(caplog)
    assert len(messages) == 1, f"expected one warning, got {messages!r}"
    assert "cp_discord:a" in messages[0] and "g1" in messages[0]


def test_ac17_a_gate_whose_post_explodes_warns_differently(channel, caplog):
    """AC-17 / R6.1 + R6.3: a DIFFERENT text, and it blames the POST.

    The ``except`` also covers ``_revive`` (``broker_threads.py:252``), so a
    text that named ``thread.send`` would claim more than it knows.  Two
    incidents with the same wording would be one incident to whoever greps.
    """

    class ExplodingThread(FakeThread):
        async def send(self, content, **kwargs):
            raise RuntimeError("Discord said no")

    manager = broker_threads.ThreadManager(lambda: channel)
    board = broker_threads.GateBoard()
    manager.adopt("cp_discord:a", ExplodingThread(2001, "cp_plugins/main"))

    with caplog.at_level(logging.DEBUG, logger="cp_discord.broker_threads"):
        post_one_gate(manager, board)

    messages = warnings_in(caplog)
    assert len(messages) == 1, f"expected one warning, got {messages!r}"
    assert "cp_discord:a" in messages[0] and "g1" in messages[0]
    assert "send" not in messages[0].lower(), (
        f"the text blames the send, but the except also covers _revive: {messages[0]!r}"
    )


def test_ac17_the_two_warnings_are_distinguishable(channel, caplog):
    """R6.1: "unterscheidbar" is the requirement, so it gets its own test."""

    class ExplodingThread(FakeThread):
        async def send(self, content, **kwargs):
            raise RuntimeError("Discord said no")

    board = broker_threads.GateBoard()
    threadless = broker_threads.ThreadManager(lambda: channel)
    exploding = broker_threads.ThreadManager(lambda: channel)
    exploding.adopt("cp_discord:a", ExplodingThread(2001, "cp_plugins/main"))

    with caplog.at_level(logging.DEBUG, logger="cp_discord.broker_threads"):
        post_one_gate(threadless, board)
        post_one_gate(exploding, board, gate_id="g2")

    missing, failed = warnings_in(caplog)
    assert missing != failed, "both incidents log the same sentence"


def test_ac17_the_boards_own_dedupe_stays_silent(channel, caplog):
    """AC-17 / R6.2: the ``board.is_open`` branch gets NO new log line.

    Asserted as an ABSENCE, deliberately: that branch is the wanted dedupe --
    ``test_a_replayed_gate_frame_is_not_posted_twice`` rests on it -- and a
    warning on a normal path is noise that trains people to ignore the log.
    """
    manager = broker_threads.ThreadManager(lambda: channel)
    board = broker_threads.GateBoard()
    manager.adopt("cp_discord:a", FakeThread(2001, "cp_plugins/main"))

    post_one_gate(manager, board)
    with caplog.at_level(logging.DEBUG, logger="cp_discord.broker_threads"):
        post_one_gate(manager, board)

    assert warnings_in(caplog) == [], "the wanted dedupe warns"


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


def test_a_transport_retry_carries_one_identity_and_applies_once(
    broker, gateway, session, channel
):
    """AC-8 at the real client: three attempts, one envelope, one post.

    This is the property the whole change rests on -- if a retry ever minted a
    fresh id per attempt, a lost answer would post the same report three
    times.  It had no test: the only ``_round_trip`` patch in the suite always
    succeeds, so nothing exercised attempt 2 or 3.
    """
    instance = registered(session, gateway)
    seen = []
    answers = []
    real = client._round_trip

    def flaky(host, port, payload):
        import json

        seen.append(json.loads(payload.decode("utf-8")))
        answer = real(host, port, payload)
        answers.append(answer)
        # The first two answers are lost on the way back; the broker HAS
        # applied them, the client just never hears it and retries.
        return None if len(seen) < 3 else answer

    original = client._round_trip
    client._round_trip = flaky
    try:
        from cp_discord import reporter as reporter_module

        instance.sink(reporter_module.ReportEvent(["a report chunk"]))
    finally:
        client._round_trip = original

    gateway.wait_idle()

    assert len(seen) == 3, "the transport must have retried twice"
    assert len({frame["env_id"] for frame in seen}) == 1, (
        "every attempt must carry the SAME envelope id, or the retry stops "
        "being idempotent"
    )
    assert len({frame["seq"] for frame in seen}) == 1
    # The broker answers the whole truth even though the client only hears the
    # last one: attempt 1 was APPLIED, attempts 2 and 3 were recognised as the
    # same envelope.  That is AC-8 -- three deliveries, one application.
    assert answers[0] == {"ok": True}, f"first attempt not applied: {answers[0]}"
    assert all(a == {"ok": True, "duplicate": True} for a in answers[1:]), (
        f"a retry was applied a second time: {answers}"
    )
