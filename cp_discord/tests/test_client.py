"""C2 — the session client: registration, sending, heartbeat, return channel.

The load-bearing property of this layer is a NEGATIVE one: nothing here may
disturb the terminal session (INV-C1).  So the suite spends most of its effort
on the absence cases -- no broker, a broker that dies mid-flight, a broker that
refuses our token -- and only then on the happy path.

Nothing here talks to Discord.  The gateway is a recording double, which is
what lets a real TCP round trip on ``127.0.0.1`` be asserted end to end
without a bot token.
"""

from __future__ import annotations

import errno
import json
import logging
import socket
import threading
import time
import types

import pytest

from cp_discord import broker_election as election
from cp_discord import broker_server, client, client_inbound, reporter, session_ids


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


@pytest.fixture
def bridge_dir(tmp_path, monkeypatch):
    """Point the bridge at a throwaway directory (portfile + registry)."""
    target = tmp_path / "cp_discord"
    monkeypatch.setenv(election.BRIDGE_DIR_ENV_VAR, str(target))
    return target


class RecordingGateway:
    """Everything :class:`Broker` needs from Discord, and nothing else.

    Deliberately local rather than imported from ``test_broker``: a shared
    test double couples two suites, and this one only has to answer "what
    arrived, in which order".
    """

    def __init__(self) -> None:
        self.opened = []
        self.posts = []
        self.channel_posts = []
        self.archived = []
        self.adopted = []

    def adopt(self, records):
        self.adopted.extend(record.session_id for record in records)

    def open_thread(self, session_id, title):
        self.opened.append((session_id, title))

    def post(self, session_id, body):
        self.posts.append((session_id, body))

    def post_channel(self, body):
        self.channel_posts.append(body)

    def archive(self, session_id):
        self.archived.append(session_id)

    def bodies_for(self, session_id):
        return [body for target, body in self.posts if target == session_id]


@pytest.fixture
def gateway() -> RecordingGateway:
    return RecordingGateway()


@pytest.fixture
def broker(bridge_dir, gateway):
    """A real broker on loopback, with its address published (§3.1 step 1)."""
    instance = broker_server.Broker(gateway, token="s3cret")
    instance.start()
    election.write_portfile(instance.address)
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture
def clients(bridge_dir):
    """Factory for clients, all stopped again at the end of the test."""
    made = []

    def make(**kwargs):
        kwargs.setdefault("title", "cp_plugins/main")
        instance = client.SessionClient(**kwargs)
        made.append(instance)
        return instance

    yield make
    for instance in made:
        instance.stop()


def state_event(message: str, *, remote_resolvable: bool = True):
    return reporter.StateEvent(reporter.WORKING, message, remote_resolvable)


# --------------------------------------------------------------------------- #
# Identity (INV-C9, INV-C13)
# --------------------------------------------------------------------------- #


def test_the_client_mints_its_own_session_id(clients):
    """INV-C9: a session with no broker must still HAVE an id.

    Minting in the broker would mean a session that never reaches one is
    nameless -- and INV-C1 says that session must keep working.
    """
    instance = clients()

    assert session_ids.nonce_of(instance.session_id) is not None
    assert instance.session_id.startswith(f"{session_ids.CP_SESSION_PREFIX}:")


def test_two_clients_get_different_ids(clients):
    assert clients().session_id != clients().session_id


def test_a_supplied_id_is_kept(clients):
    """C4/C5 look sessions up by id, so it has to be injectable."""
    assert clients(session_id="cp_discord:fixed").session_id == "cp_discord:fixed"


def test_the_identity_is_pid_and_start_time(clients, broker):
    """INV-C13: a PID alone is recycled, and a recycled PID never archives."""
    instance = clients()
    instance.register_now()

    record = broker.registry.get(instance.session_id)
    expected_pid, expected_start = election.process_identity()

    assert record.pid == expected_pid
    assert record.started_at == expected_start


# --------------------------------------------------------------------------- #
# AC-2 / AC-3 — INV-C1: the session survives every broker failure
# --------------------------------------------------------------------------- #


def test_no_broker_leaves_the_session_running(clients, bridge_dir):
    """AC-2: no portfile, no broker -- and no consequence for the session."""
    instance = clients()
    instance.start()

    instance.sink(state_event("coding…"))
    instance.sink(reporter.ReportEvent(("a report",)))
    instance.sink(reporter.ReleaseEvent())

    assert instance.registered is False


def test_registration_without_a_broker_gives_up_quietly(clients, bridge_dir):
    """§3.1 step 4: after N attempts, give up -- WITHOUT an error."""
    instance = clients()

    assert instance.register_now(attempts=2) is False
    assert instance.registered is False


def test_sending_without_a_broker_is_fast(clients, bridge_dir):
    """AC-2: the send path must not become a wait when nobody is listening."""
    instance = clients()

    started = time.monotonic()
    for _ in range(5):
        instance.sink(state_event("coding…"))
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "an absent broker must not cost the session time"


def test_a_broker_that_dies_mid_flight_does_not_hang_the_session(clients, broker):
    """AC-3: the broker dies in service; the session keeps going.

    The session drops its registration, and that is the CORRECT reaction, not
    a wound: the flag is what makes the next tick run the election again.  A
    session that kept claiming to be registered would keep sending into a
    closed port until something else noticed.
    """
    instance = clients()
    assert instance.register_now() is True

    broker.stop()

    started = time.monotonic()
    instance.sink(state_event("still coding…"))
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, "a dead broker must not become a wait"
    assert instance.registered is False


def test_a_broken_sink_payload_never_reaches_the_caller(clients, broker):
    """INV-C1: an unknown event is logged and dropped, never raised."""
    instance = clients()
    instance.register_now()

    instance.sink(object())


# --------------------------------------------------------------------------- #
# AC-1 — one broker, the rest are clients
# --------------------------------------------------------------------------- #


def test_five_sessions_yield_one_broker_and_five_registrations(
    bridge_dir, gateway, clients
):
    """AC-1: five sessions start at once -> 1 broker, 4 losers, 5 clients.

    The winner is a client too: it registers with its own broker like anybody
    else, so the count of registrations is five, not four.
    """
    from cp_discord import broker_activation

    supervisors = [
        broker_activation.BrokerSupervisor(lambda: gateway) for _ in range(5)
    ]
    try:
        won = [supervisor for supervisor in supervisors if supervisor.try_elect()]
        assert len(won) == 1, "Discord allows one gateway per bot token"

        sessions = [clients() for _ in range(5)]
        assert all(session.register_now() for session in sessions)

        registry = won[0].broker.registry
        assert {record.session_id for record in registry.records()} == {
            session.session_id for session in sessions
        }
    finally:
        for supervisor in supervisors:
            supervisor.stop()


# --------------------------------------------------------------------------- #
# AC-7 / AC-8 — the wire
# --------------------------------------------------------------------------- #


def test_ten_sessions_twenty_messages_arrive_in_order(broker, gateway, clients):
    """AC-7: everything arrives, and per session the order is intact."""
    sessions = [clients() for _ in range(10)]
    for session in sessions:
        assert session.register_now() is True

    def run(session):
        for index in range(20):
            session.sink(state_event(f"m{index}"))

    threads = [threading.Thread(target=run, args=(session,)) for session in sessions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    for session in sessions:
        bodies = gateway.bodies_for(session.session_id)
        assert bodies == [f"m{index}" for index in range(20)]


def test_seq_is_monotonic_per_session(broker, clients):
    """§3.2: the receiver discards ``seq <= last_seq``, so it must climb."""
    instance = clients()
    instance.register_now()

    for index in range(5):
        instance.sink(state_event(f"m{index}"))

    assert broker.registry.get(instance.session_id).last_seq >= 5


def test_a_retry_reuses_the_identical_envelope(clients, bridge_dir):
    """AC-8: a retry must be the SAME envelope, or it is not idempotent.

    A retry that re-numbered itself would be applied twice by a receiver that
    already took the first one -- exactly what the ``seq`` check prevents, and
    only if the sender keeps the number.
    """
    received = []
    ready = threading.Event()

    def serve(server):
        ready.set()
        for reply in (False, True):
            connection, _peer = server.accept()
            with connection, connection.makefile("rwb") as stream:
                received.append(json.loads(stream.readline(65536).decode("utf-8")))
                if reply:
                    stream.write(b'{"ok": true}\n')
                    stream.flush()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    election.write_portfile(
        election.BrokerAddress(port=server.getsockname()[1], token="s3cret")
    )
    worker = threading.Thread(target=serve, args=(server,), daemon=True)
    worker.start()
    ready.wait(5)
    try:
        instance = clients()
        instance.register_now(attempts=1)
    finally:
        worker.join(10)
        server.close()

    assert len(received) == 2, "a lost ack must be retried"
    assert received[0] == received[1], "the retry must be the identical envelope"


def test_the_wire_is_plain_json_never_pickle(clients, bridge_dir):
    """§3.2: newline-delimited UTF-8 JSON, so a non-Python client can speak it.

    A bearer-token socket that unpickled its input would be a remote code
    execution hole rather than a protocol.
    """
    captured = []
    ready = threading.Event()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def serve():
        ready.set()
        connection, _peer = server.accept()
        with connection, connection.makefile("rwb") as stream:
            captured.append(stream.readline(65536))
            stream.write(b'{"ok": true}\n')
            stream.flush()

    election.write_portfile(
        election.BrokerAddress(port=server.getsockname()[1], token="s3cret")
    )
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    ready.wait(5)
    try:
        clients().register_now(attempts=1)
    finally:
        worker.join(10)
        server.close()

    raw = captured[0]
    assert raw.endswith(b"\n")
    frame = json.loads(raw.decode("utf-8"))
    assert frame["method"] == broker_server.M_REGISTER
    assert set(frame) == {"token", "method", "session_id", "seq", "params"}
    assert set(frame["params"]) == {"title", "pid", "started_at", "inbound_port"}


# --------------------------------------------------------------------------- #
# AC-68 — the return-channel socket (INV-C18)
# --------------------------------------------------------------------------- #


def test_the_inbound_socket_is_loopback_only(clients):
    """INV-C18: this socket resolves shell approvals from off-machine clicks."""
    instance = clients()
    instance.start()

    assert instance.inbound_host == "127.0.0.1"
    assert instance.inbound_port > 0


def deliver_raw(instance, payload):
    with socket.create_connection(
        ("127.0.0.1", instance.inbound_port), timeout=5
    ) as sock:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        with sock.makefile("r", encoding="utf-8") as stream:
            return json.loads(stream.readline())


def resolve_frame(instance, *, token, gate_id="g1"):
    return {
        "token": token,
        "method": broker_server.M_RESOLVE,
        "session_id": instance.session_id,
        "params": {"gate_id": gate_id, "decision": "yes", "discord_user_id": "42"},
    }


def test_an_inbound_frame_without_a_token_is_refused(clients, broker):
    """AC-68: without auth this is a remote control for any local process."""
    seen = []
    instance = clients()
    instance.set_resolution_handler(lambda **kwargs: seen.append(kwargs))
    instance.start()
    instance.register_now()

    response = deliver_raw(
        instance,
        {
            "method": broker_server.M_RESOLVE,
            "session_id": instance.session_id,
            "params": {"gate_id": "g1", "decision": "yes", "discord_user_id": "42"},
        },
    )

    assert response["ok"] is False
    assert response["error"] == broker_server.ERR_UNAUTHORIZED
    assert seen == []


def test_an_inbound_frame_with_a_wrong_token_is_refused(clients, broker):
    seen = []
    instance = clients()
    instance.set_resolution_handler(lambda **kwargs: seen.append(kwargs))
    instance.start()
    instance.register_now()

    response = deliver_raw(instance, resolve_frame(instance, token="wrong"))

    assert response["ok"] is False
    assert response["error"] == broker_server.ERR_UNAUTHORIZED
    assert seen == []


def test_garbage_on_the_inbound_socket_is_answered_and_survived(clients, broker):
    """INV-C1: a malformed frame is not a reason to stop listening."""
    instance = clients()
    instance.set_resolution_handler(lambda **kwargs: None)
    instance.start()
    instance.register_now()

    with socket.create_connection(
        ("127.0.0.1", instance.inbound_port), timeout=5
    ) as sock:
        sock.sendall(b"this is not json\n")
        with sock.makefile("r", encoding="utf-8") as stream:
            response = json.loads(stream.readline())

    assert response["ok"] is False
    assert deliver_raw(instance, resolve_frame(instance, token=broker.token))["ok"]


def test_a_resolution_without_a_handler_is_not_acked(clients, broker):
    """An ack means 'taken'.  Acking with nowhere to put it loses the click."""
    instance = clients()
    instance.start()
    instance.register_now()

    response = deliver_raw(instance, resolve_frame(instance, token=broker.token))

    assert response["ok"] is False
    assert response["error"] == client.ERR_NO_HANDLER


# --------------------------------------------------------------------------- #
# AC-66 — delivery broker -> session inside 100 ms (INV-C17)
# --------------------------------------------------------------------------- #


def test_a_resolution_reaches_the_session_within_100ms(clients, broker):
    """AC-66/INV-C17: a session parked on a gate sends nothing.

    So the resolution has to be PUSHED, and fast: the phone click must not
    wait for the next heartbeat.
    """
    arrived = threading.Event()
    seen = {}

    def handler(**kwargs):
        seen.update(kwargs)
        arrived.set()

    instance = clients()
    instance.set_resolution_handler(handler)
    instance.start()
    assert instance.register_now() is True

    started = time.monotonic()
    delivered = broker.deliver_resolution(instance.session_id, "g1", "yes", "42")
    elapsed = time.monotonic() - started

    assert delivered is True
    assert arrived.wait(1.0)
    assert elapsed < 0.1, "INV-C17 budgets 100 ms for this hop"
    assert seen == {"gate_id": "g1", "decision": "yes", "discord_user_id": "42"}


def test_the_inbound_port_is_registered_so_a_new_broker_can_find_us(clients, broker):
    """§3.3a: without the port in the register, a re-elected broker is mute."""
    instance = clients()
    instance.start()
    instance.register_now()

    assert (
        broker.registry.get(instance.session_id).inbound_port == instance.inbound_port
    )


# --------------------------------------------------------------------------- #
# C2b — the listener on its own (AC-68, INV-C18)
# --------------------------------------------------------------------------- #


@pytest.fixture
def listener():
    """A listener with no client behind it: the socket half, isolated."""
    made = []

    def build(
        *, authorize=lambda token: token == "good", handler=None, on_refused=None
    ):
        instance = client_inbound.InboundListener(
            authorize=authorize,
            on_refused=on_refused or (lambda: None),
            handler_provider=lambda: handler,
        )
        made.append(instance)
        return instance

    yield build
    for instance in made:
        instance.stop()


def test_the_listener_binds_loopback(listener):
    instance = listener()
    instance.start()

    assert instance.host == "127.0.0.1"
    assert instance.port > 0


def test_a_stopped_listener_reports_no_port(listener):
    """``0`` is what a registration would carry, and 0 is not a port."""
    instance = listener()

    assert instance.port == 0
    assert instance.host == "127.0.0.1"


def test_the_listener_can_restart(listener):
    """Start/stop/start: a re-installed session must be reachable again."""
    instance = listener()
    instance.start()
    first = instance.port
    instance.stop()
    instance.start()

    assert instance.port > 0
    assert first > 0


def test_a_transient_accept_error_does_not_kill_the_return_channel(
    listener, monkeypatch, caplog
):
    """``accept`` fails transiently (ECONNABORTED, EMFILE) -- and must not end us.

    Treating any ``OSError`` as teardown ends the accept loop for the LIFE of
    the session while the socket stays bound: ``inbound_port`` keeps reporting
    a valid port, so the broker keeps delivering into a queue nobody drains,
    reads TRANSPORT_FAILED, marks the session unreachable and finally archives
    the thread of a session that is alive and working.  Every phone click
    after that is lost -- the AC-15 damage the PID second signal exists to
    prevent.  The broker's own loop already gets this right
    (``broker_server.py:188-192``).
    """
    raised = []
    real_socket = socket.socket

    class FlakyAcceptSocket:
        """A real loopback socket whose FIRST ``accept`` aborts transiently."""

        def __init__(self, *args, **kwargs):
            self._sock = real_socket(*args, **kwargs)
            self._failures_left = 1

        def accept(self):
            if self._failures_left:
                self._failures_left -= 1
                raised.append(True)
                raise OSError(errno.ECONNABORTED, "software caused connection abort")
            return self._sock.accept()

        def __getattr__(self, name):
            return getattr(self._sock, name)

    monkeypatch.setattr(
        client_inbound,
        "socket",
        types.SimpleNamespace(
            socket=FlakyAcceptSocket,
            AF_INET=socket.AF_INET,
            SOCK_STREAM=socket.SOCK_STREAM,
            timeout=socket.timeout,
        ),
    )

    arrived = threading.Event()
    instance = listener(handler=lambda **kwargs: arrived.set())
    with caplog.at_level(logging.DEBUG, logger="cp_discord.client_inbound"):
        instance.start()
        assert instance.port > 0
        with socket.create_connection(("127.0.0.1", instance.port), timeout=3) as sock:
            sock.sendall(
                (
                    json.dumps(
                        {
                            "token": "good",
                            "method": broker_server.M_RESOLVE,
                            "params": {"gate_id": "g1", "decision": "yes"},
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
            with sock.makefile("r", encoding="utf-8") as stream:
                response = json.loads(stream.readline())

    assert raised, "the transient failure never fired; the test proves nothing"
    assert response["ok"] is True
    assert arrived.wait(2.0)
    # Silence is what made this a copy slip rather than a decision: an aborted
    # accept has to leave a trace, or the next one is invisible too.
    assert any(
        record.name == "cp_discord.client_inbound"
        and record.levelno == logging.DEBUG
        and record.exc_info is not None
        for record in caplog.records
    )


def test_a_teardown_accept_error_ends_the_loop_quietly(listener, caplog):
    """The other half: an ``accept`` aborted by our own ``stop`` is not a fault.

    Driven synchronously because this is a RACE -- ``stop`` closes the socket
    while ``accept`` is in flight -- and the loop condition alone cannot catch
    it: the throw happens INSIDE the iteration that already passed the check.
    Without the ``_stop`` guard every shutdown that loses this race files a
    traceback for a socket we closed on purpose, and the log stops meaning
    anything the moment a real transient failure needs to be seen.
    """
    instance = listener()

    class ClosedUnderneathUs:
        def accept(self):
            instance.stop()  # what really happens: teardown, mid-accept
            raise OSError(errno.EBADF, "bad file descriptor")

    with caplog.at_level(logging.DEBUG, logger="cp_discord.client_inbound"):
        instance._serve(ClosedUnderneathUs())

    assert [
        record
        for record in caplog.records
        if record.name == "cp_discord.client_inbound"
    ] == []


def test_a_non_resolve_method_is_refused(listener):
    """The return channel carries exactly one method (§3.2a)."""
    seen = []
    instance = listener(handler=lambda **kwargs: seen.append(kwargs))

    response = instance.dispatch(
        json.dumps({"token": "good", "method": "state", "params": {}}).encode("utf-8")
    )

    assert response["ok"] is False
    assert response["error"] == broker_server.ERR_BAD_REQUEST
    assert seen == []


def test_a_non_object_frame_is_refused(listener):
    response = listener().dispatch(b"[1, 2, 3]")

    assert response["ok"] is False
    assert response["error"] == broker_server.ERR_BAD_REQUEST


def test_a_throwing_handler_is_not_acked(listener):
    """An ack says 'taken'.  A handler that blew up did not take it."""

    def explode(**kwargs):
        raise RuntimeError("C4 fell over")

    response = listener(handler=explode).dispatch(
        json.dumps(
            {
                "token": "good",
                "method": broker_server.M_RESOLVE,
                "params": {"gate_id": "g1", "decision": "yes", "discord_user_id": "42"},
            }
        ).encode("utf-8")
    )

    assert response["ok"] is False


def test_a_throwing_refusal_hook_never_escapes(listener):
    """INV-C1: the heal is a courtesy, not a reason to drop the connection."""

    def explode():
        raise RuntimeError("the portfile is on fire")

    response = listener(on_refused=explode).dispatch(
        json.dumps({"token": "bad", "method": broker_server.M_RESOLVE}).encode("utf-8")
    )

    assert response["error"] == broker_server.ERR_UNAUTHORIZED


def test_token_matches_refuses_everything_unusable():
    """Fail-closed: no token, no expectation, wrong type -- all refused."""
    assert client_inbound.token_matches("good", "good") is True
    assert client_inbound.token_matches("good", "bad") is False
    assert client_inbound.token_matches(None, "good") is False
    assert client_inbound.token_matches("", "") is False
    assert client_inbound.token_matches("good", None) is False
    assert client_inbound.token_matches("good", 42) is False


# --------------------------------------------------------------------------- #
# AC-85c — error-driven token refresh, BOTH directions
# --------------------------------------------------------------------------- #


def test_a_rejected_send_refreshes_the_token_once(clients, broker, gateway):
    """AC-85c(a): the token rotated under us -> re-read AT ONCE.

    §3.1a only re-reads the portfile when the broker is UNREACHABLE, but after
    a rotation it is perfectly reachable.  Without this the session would send
    with the old token forever -- silently, because INV-C1 forbids it from
    complaining.
    """
    calls = []
    real = broker_server.refresh_token_from_portfile

    def counting_refresh():
        calls.append(1)
        return real()

    instance = clients(refresh_token=counting_refresh)
    instance.register_now()
    before = len(calls)

    instance.adopt_token_for_test("stale")
    instance.sink(state_event("after the rotation"))

    assert len(calls) == before + 1, "one re-read per rejection, immediately"
    assert gateway.bodies_for(instance.session_id) == ["after the rotation"]


def test_a_refused_inbound_frame_refreshes_the_token_once(clients, broker):
    """AC-85c(b): the OTHER direction, which nothing else would heal.

    A resolution from the new broker meets our old token, we refuse it -- and
    without this trigger nothing would ever make us look again.  The broker
    retries three times (AC-85d), so attempt two meets the healed token.
    """
    calls = []
    real = broker_server.refresh_token_from_portfile

    def counting_refresh():
        calls.append(1)
        return real()

    instance = clients(refresh_token=counting_refresh)
    instance.set_resolution_handler(lambda **kwargs: None)
    instance.start()
    instance.register_now()

    instance.adopt_token_for_test("stale")
    before = len(calls)
    response = deliver_raw(instance, resolve_frame(instance, token=broker.token))

    assert response["ok"] is False
    assert len(calls) == before + 1
    assert deliver_raw(instance, resolve_frame(instance, token=broker.token))["ok"]


def test_the_periodic_tick_refreshes_the_token(clients, broker):
    """§3.1 (b): the 30-second tick is the upper bound on healing."""
    calls = []
    real = broker_server.refresh_token_from_portfile

    def counting_refresh():
        calls.append(1)
        return real()

    instance = clients(refresh_token=counting_refresh)
    instance.register_now()
    before = len(calls)

    instance.tick()

    assert len(calls) > before


def test_the_refresh_seam_is_w1s_function(clients):
    """The portfile has ONE reader; a second one is where token rules drift."""
    assert client.SessionClient.default_refresh_token is (
        broker_server.refresh_token_from_portfile
    )


def test_a_second_refusal_after_healing_is_not_retried_again(clients, broker):
    """Each cause heals ONCE.  A loop here would busy-wait C3's worker thread.

    The refresh keeps 'succeeding' while the token stays wrong, which is what
    an endlessly retrying client would do forever: the count proves the retry
    is armed exactly once.
    """
    calls = []

    def useless_refresh():
        calls.append(1)
        return "still-wrong"

    instance = clients(refresh_token=useless_refresh)
    instance.register_now()
    instance.adopt_token_for_test("stale")
    calls.clear()

    assert instance.sink(state_event("never lands")) is None
    assert len(calls) == 1, "one heal attempt per rejection, then give up"


def test_a_stale_token_still_heals_an_unknown_session(clients, broker, gateway):
    """Both causes at once: heal the token, THEN discover we are unknown.

    Disarming both heals after the first would leave the session mute until
    the next tick even though the second cause was never actually tried.
    """
    instance = clients()
    instance.register_now()
    broker.registry.remove(instance.session_id)
    instance.adopt_token_for_test("stale")
    gateway.opened.clear()

    instance.sink(state_event("healed twice"))

    assert gateway.opened == [(instance.session_id, "cp_plugins/main")]
    assert gateway.bodies_for(instance.session_id) == ["healed twice"]


# --------------------------------------------------------------------------- #
# Heartbeat and re-registration (§7, §3.1a)
# --------------------------------------------------------------------------- #


def test_a_tick_beats_the_heart(clients, broker):
    """§7: 30 s of silence starts a 90 s countdown to being archived."""
    instance = clients()
    instance.register_now()
    broker.registry.touch(instance.session_id, now=0.0)

    instance.tick()

    assert broker.registry.get(instance.session_id).last_seen > 0.0


def test_a_tick_without_a_broker_is_harmless(clients, bridge_dir):
    """INV-C1: the supervision tick runs whether or not anyone is listening."""
    instance = clients()

    instance.tick(attempts=1)

    assert instance.registered is False


def test_an_unknown_session_re_registers_itself(clients, broker, gateway):
    """A broker with a fresh register does not know us; saying so must heal it."""
    instance = clients()
    instance.register_now()
    broker.registry.remove(instance.session_id)
    gateway.opened.clear()

    instance.sink(state_event("still here"))

    assert broker.registry.get(instance.session_id) is not None
    assert gateway.opened == [(instance.session_id, "cp_plugins/main")]
    assert gateway.bodies_for(instance.session_id) == ["still here"]


def test_a_new_broker_on_a_new_port_is_found_again(clients, broker, gateway):
    """§3.1a: after a re-election the address changed, not the session."""
    instance = clients()
    instance.register_now()
    broker.stop()

    successor = broker_server.Broker(gateway, token="s3cret")
    successor.start()
    election.write_portfile(successor.address)
    try:
        assert instance.register_now() is True
        instance.sink(state_event("moved"))
    finally:
        successor.stop()

    assert gateway.bodies_for(instance.session_id) == ["moved"]


# --------------------------------------------------------------------------- #
# The events C3 hands over
# --------------------------------------------------------------------------- #


def test_a_state_event_carries_its_local_only_marking(clients, broker, gateway):
    """INV-C23: a wait the phone cannot answer must say so in the thread."""
    instance = clients()
    instance.register_now()

    instance.sink(
        reporter.StateEvent(reporter.BLOCKED, "wartet auf eine Eingabe", False)
    )

    body = gateway.bodies_for(instance.session_id)[0]
    assert reporter.LOCAL_ONLY_MARKER in body


def test_a_report_event_arrives_chunk_by_chunk(clients, broker, gateway):
    """AC-81b's session half: C7 already cut it, so the order is the content."""
    instance = clients()
    instance.register_now()

    instance.sink(reporter.ReportEvent(("first", "second")))

    assert gateway.bodies_for(instance.session_id) == ["first", "second"]


def test_a_release_event_archives_the_thread(clients, broker, gateway):
    """§7 clean end: the thread is archived, never deleted (INV-C3)."""
    instance = clients()
    instance.register_now()

    instance.sink(reporter.ReleaseEvent())

    assert gateway.archived == [instance.session_id]
    assert broker.registry.get(instance.session_id) is None


# --------------------------------------------------------------------------- #
# The plugin surface -- and the handover that makes C3 audible
# --------------------------------------------------------------------------- #


class _Config:
    mode = "report"
    session_name = "unit-test"


@pytest.fixture
def installed(bridge_dir):
    yield
    client.uninstall()
    reporter.set_sink(None)


def test_install_wires_the_reporter_sink(installed, broker, gateway):
    """The handover of this wave: without it C3 is a clean, SILENT no-op.

    ``reporter._sink`` is read directly on purpose -- the wiring IS the
    contract under test, and asserting it through a mock would only prove the
    mock was called.
    """
    client.install(_Config())

    assert reporter._sink is not None

    instance = client.active_client()
    assert instance.register_now() is True
    reporter._sink(state_event("through the sink"))

    assert gateway.bodies_for(instance.session_id) == ["through the sink"]


def test_install_returns_at_once(installed, bridge_dir):
    """INV-C1: activation happens on a daemon thread, never on startup."""
    started = time.monotonic()
    client.install(_Config())
    elapsed = time.monotonic() - started

    assert elapsed < 0.5


def test_install_uses_the_session_name_override(installed, broker, gateway):
    """AC-12's session half: ``--session-name`` beats the derived title."""
    client.install(_Config())
    instance = client.active_client()

    assert instance.register_now() is True
    assert gateway.opened == [(instance.session_id, "unit-test")]


def test_uninstall_unwires_the_sink(installed, bridge_dir):
    client.install(_Config())
    client.uninstall()

    assert reporter._sink is None
    assert client.active_client() is None


def test_uninstall_without_install_is_harmless(installed, bridge_dir):
    client.uninstall()
