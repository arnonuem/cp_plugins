"""C1 — the broker: election, TCP server, Discord threads.

Structure mirrors the layer's three files: election first (who owns the
machine), then the thread bookkeeping, then the server that ties them
together.  Nothing here talks to Discord or opens a socket outside
``127.0.0.1``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import threading
import time

import pytest

from cp_discord import broker_election as election
from cp_discord import broker_threads as threads

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file modes; Windows is best-effort (AC-6)"
)


@pytest.fixture
def bridge_dir(tmp_path, monkeypatch):
    """Point the bridge at a throwaway directory (portfile + registry)."""
    target = tmp_path / "cp_discord"
    monkeypatch.setenv(election.BRIDGE_DIR_ENV_VAR, str(target))
    return target


# --------------------------------------------------------------------------- #
# Portfile (AC-6, AC-55, AC-85a)
# --------------------------------------------------------------------------- #


def test_portfile_roundtrip_carries_the_whole_address(bridge_dir):
    election.write_portfile(election.BrokerAddress(port=4711, token="t0k3n"))

    address = election.read_portfile()

    assert address is not None
    assert (address.host, address.port, address.token) == ("127.0.0.1", 4711, "t0k3n")


@POSIX_ONLY
def test_portfile_is_owner_only(bridge_dir):
    """AC-6: the token in there is a full authorization bypass."""
    election.write_portfile(election.BrokerAddress(port=4711, token="t0k3n"))

    mode = stat.S_IMODE(election.portfile_path().stat().st_mode)

    assert mode == 0o600


def test_portfile_write_is_atomic(bridge_dir):
    """AC-55: a reader never sees a half-written file.

    Proven at the mechanism rather than by racing: the payload reaches its
    final name through ``os.replace``, which is atomic on both platforms, and
    the temporary file is gone afterwards.
    """
    seen = []
    real_replace = os.replace

    def recording_replace(src, dst):
        seen.append((str(src), str(dst)))
        return real_replace(src, dst)

    election.write_portfile(election.BrokerAddress(port=1, token="a"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "replace", recording_replace)
        election.write_portfile(election.BrokerAddress(port=2, token="b"))

    assert len(seen) == 1
    source, destination = seen[0]
    assert destination == str(election.portfile_path())
    assert source != destination
    assert not os.path.exists(source)
    assert election.read_portfile().port == 2
    assert list(bridge_dir.glob("*.tmp*")) == []


def test_unreadable_portfile_reads_as_absent(bridge_dir):
    bridge_dir.mkdir(parents=True)
    election.portfile_path().write_text("{not json", encoding="utf-8")

    assert election.read_portfile() is None


def test_portfile_without_a_token_reads_as_absent(bridge_dir):
    """A tokenless portfile is unusable, so it must not look usable."""
    bridge_dir.mkdir(parents=True)
    election.portfile_path().write_text(json.dumps({"port": 1}), encoding="utf-8")

    assert election.read_portfile() is None


# --------------------------------------------------------------------------- #
# AC-85a — the token is TAKEN OVER, not re-minted
# --------------------------------------------------------------------------- #


def test_takeover_keeps_the_existing_token(bridge_dir):
    """AC-85a(a): a re-elected broker writes back the SAME token.

    Minting a fresh one would make every established session reject the new
    broker (INV-C18) and lose every gate resolution until it re-registered.
    """
    election.write_portfile(election.BrokerAddress(port=1, token="inherited"))

    token = election.adopt_or_mint_token()

    assert token == "inherited"


def test_missing_portfile_rotates_the_token(bridge_dir):
    """AC-85a(b): nothing to inherit -> a cold start, so mint."""
    assert not election.portfile_path().exists()

    token = election.adopt_or_mint_token()

    assert token and token != ""


def test_broken_portfile_rotates_the_token(bridge_dir):
    bridge_dir.mkdir(parents=True)
    election.portfile_path().write_text("{{{", encoding="utf-8")

    assert election.adopt_or_mint_token() != ""


def test_tokenless_portfile_rotates_the_token(bridge_dir):
    bridge_dir.mkdir(parents=True)
    election.portfile_path().write_text(
        json.dumps({"port": 5, "token": ""}), encoding="utf-8"
    )

    assert election.adopt_or_mint_token() != ""


def test_minted_tokens_are_unique(bridge_dir):
    assert election.mint_token() != election.mint_token()


def test_refresh_token_from_portfile_is_w2s_reading_seam(bridge_dir):
    """AC-85a(c): the session's 30-second re-read goes through here.

    Without it a rotation is irreversible: the session would keep sending a
    token the broker discards (INV-C2) and discard what the broker sends back
    (INV-C18) -- mute and deaf, and INV-C1 keeps it from even complaining.
    """
    assert election.refresh_token_from_portfile() is None

    election.write_portfile(election.BrokerAddress(port=9, token="rotated"))

    assert election.refresh_token_from_portfile() == "rotated"


# --------------------------------------------------------------------------- #
# Single-owner lock (§3.1, AC-70)
# --------------------------------------------------------------------------- #


def test_only_one_lock_holder_at_a_time(bridge_dir):
    first = election.SingleOwnerLock()
    second = election.SingleOwnerLock()
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        assert first.held is True
        assert second.held is False
    finally:
        first.release()
        second.release()


def test_releasing_lets_the_next_candidate_in(bridge_dir):
    """AC-70's mechanism: without this, everyone else fails forever."""
    first = election.SingleOwnerLock()
    second = election.SingleOwnerLock()
    try:
        assert first.acquire() is True
        first.release()
        assert first.held is False
        assert second.acquire() is True
    finally:
        first.release()
        second.release()


def test_acquire_is_idempotent(bridge_dir):
    lock = election.SingleOwnerLock()
    try:
        assert lock.acquire() is True
        assert lock.acquire() is True
    finally:
        lock.release()


def test_release_without_acquire_is_harmless(bridge_dir):
    election.SingleOwnerLock().release()


# --------------------------------------------------------------------------- #
# INV-C13 — identity is PID + start time (AC-51)
# --------------------------------------------------------------------------- #


def test_this_process_matches_its_own_identity():
    pid, started_at = election.process_identity()

    assert pid == os.getpid()
    assert election.process_matches(pid, started_at) is True


def test_start_time_is_stable_across_calls():
    """If it drifted, every session would look like a stranger after 90 s."""
    pid = os.getpid()

    assert election.process_start_time(pid) == election.process_start_time(pid)


def test_a_reused_pid_does_not_match(monkeypatch):
    """AC-51: the PID is alive but belongs to somebody else -> not ours.

    With a bare PID check this thread would never be archived.
    """
    pid, started_at = election.process_identity()
    assert started_at is not None, "this platform must report a start time"

    assert election.process_matches(pid, started_at - 3600.0) is False


def test_a_dead_pid_does_not_match():
    dead = _unused_pid()

    assert election.process_alive(dead) is False
    assert election.process_matches(dead, 1.0) is False


def _unused_pid() -> int:
    """A PID that is not in use.  Searched, never guessed."""
    for candidate in range(600000, 620000):
        if not election.process_alive(candidate):
            return candidate
    raise AssertionError("no free PID found")  # pragma: no cover


def test_unknown_start_time_falls_back_to_liveness(monkeypatch):
    """A platform without a start-time probe still gets PID liveness.

    Fail-open is deliberate here and only here: refusing would archive the
    thread of a LIVE session (INV-C14, AC-15), which is the worse of the two.
    """
    monkeypatch.setattr(election, "process_start_time", lambda pid: None)

    assert election.process_matches(os.getpid(), None) is True
    assert election.process_matches(_unused_pid(), None) is False


# --------------------------------------------------------------------------- #
# Thread titles (AC-9..AC-12)
# --------------------------------------------------------------------------- #


def test_title_is_directory_slash_branch():
    """AC-9: ``<directory>/<branch>``."""
    assert (
        threads.session_title("/home/w/cp_plugins", branch="feat-discord")
        == "cp_plugins/feat-discord"
    )


def test_title_without_a_repo_is_just_the_directory():
    """AC-10: no git repo -> the branch part is absent, not an error."""
    assert threads.session_title("/home/w/cp_plugins", branch=None) == "cp_plugins"
    assert threads.session_title("/home/w/cp_plugins", branch="  ") == "cp_plugins"


def test_session_name_override_wins():
    """AC-12: ``--session-name`` overrides the whole derivation."""
    assert (
        threads.session_title("/home/w/cp_plugins", branch="main", override="eigener")
        == "eigener"
    )


def test_a_branch_lookup_failure_is_not_a_session_failure(monkeypatch):
    """INV-C1: git is allowed to be broken, missing or slow."""

    def explode(cwd):
        raise OSError("git is not here")

    monkeypatch.setattr(threads, "detect_branch", explode)

    assert threads.derive_title("/home/w/cp_plugins", None) == "cp_plugins"


def test_collisions_get_numbered():
    """AC-11: two sessions, same directory and branch -> ``#1``, ``#2``."""
    taken = {"cp_plugins/main"}

    first = threads.disambiguate("cp_plugins/main", taken)
    taken.add(first)
    second = threads.disambiguate("cp_plugins/main", taken)

    assert first == "cp_plugins/main #1"
    assert second == "cp_plugins/main #2"


def test_an_unused_title_is_left_alone():
    assert threads.disambiguate("cp_plugins/main", set()) == "cp_plugins/main"


# --------------------------------------------------------------------------- #
# Discord threads: archive, never delete (AC-13, AC-16, AC-17, AC-56)
# --------------------------------------------------------------------------- #


class FakeThread:
    """A Discord thread with just the surface the broker touches.

    ``delete`` exists ON PURPOSE and is never expected to be called: a fake
    without it would make AC-17 pass by absence rather than by behaviour.
    """

    def __init__(self, thread_id: int, name: str) -> None:
        self.id = thread_id
        self.name = name
        self.archived = False
        self.auto_archive_duration = 60
        self.messages: list[str] = []
        self.deleted = False
        self.edits: list[dict] = []

    async def send(self, content):
        if self.archived:
            raise AssertionError("posted into an archived thread without reviving it")
        self.messages.append(content)
        return object()

    async def edit(self, **kwargs):
        self.edits.append(dict(kwargs))
        if "archived" in kwargs:
            self.archived = bool(kwargs["archived"])
        if "auto_archive_duration" in kwargs:
            self.auto_archive_duration = kwargs["auto_archive_duration"]

    async def delete(self):  # pragma: no cover - reaching this IS the failure
        self.deleted = True
        raise AssertionError("INV-C3: threads are archived, never deleted")


class FakeChannel:
    """A Discord text channel: creates threads and takes channel-level posts."""

    def __init__(self) -> None:
        self.threads: list[FakeThread] = []
        self.messages: list[str] = []
        self._next_id = 1000

    async def create_thread(self, *, name, auto_archive_duration=None, **_kwargs):
        self._next_id += 1
        thread = FakeThread(self._next_id, name)
        if auto_archive_duration is not None:
            thread.auto_archive_duration = auto_archive_duration
        self.threads.append(thread)
        return thread

    async def send(self, content):
        self.messages.append(content)
        return object()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def manager(channel) -> threads.ThreadManager:
    return threads.ThreadManager(lambda: channel)


def run(coro):
    return asyncio.run(coro)


def test_a_new_session_gets_a_thread(manager, channel):
    thread_id = run(manager.ensure_thread("cp_discord:a", "cp_plugins/main"))

    assert len(channel.threads) == 1
    assert channel.threads[0].name == "cp_plugins/main"
    assert thread_id == channel.threads[0].id


def test_threads_get_the_longest_auto_archive(manager, channel):
    """§3.3a: Discord's own 60-minute default would hide an IDLE session.

    Requirement 8 is precisely "nobody is at the terminal", so the default
    would archive exactly the sessions that matter most.
    """
    run(manager.ensure_thread("cp_discord:a", "cp_plugins/main"))

    assert channel.threads[0].auto_archive_duration == threads.AUTO_ARCHIVE_MAX


def test_a_known_session_keeps_its_thread(manager, channel):
    """INV-C14: re-registering must not shred the history."""
    first = run(manager.ensure_thread("cp_discord:a", "cp_plugins/main"))
    second = run(manager.ensure_thread("cp_discord:a", "cp_plugins/main"))

    assert first == second
    assert len(channel.threads) == 1


def test_an_adopted_thread_is_not_recreated(manager, channel):
    """AC-53: after a re-election the new broker ADOPTS, it does not rebuild."""
    existing = FakeThread(77, "cp_plugins/main")
    manager.adopt("cp_discord:a", existing)

    thread_id = run(manager.ensure_thread("cp_discord:a", "cp_plugins/main"))

    assert thread_id == 77
    assert channel.threads == []


def test_archiving_never_deletes(manager, channel):
    """AC-17 / INV-C3: the history has to stay readable."""
    run(manager.ensure_thread("cp_discord:a", "cp_plugins/main"))

    run(manager.archive("cp_discord:a"))

    thread = channel.threads[0]
    assert thread.archived is True
    assert thread.deleted is False


def test_archiving_an_unknown_session_is_quiet(manager):
    run(manager.archive("cp_discord:nobody"))


def test_posting_revives_an_auto_archived_thread(manager, channel):
    """AC-56: Discord archives on its own; activity brings the thread back."""
    run(manager.ensure_thread("cp_discord:a", "cp_plugins/main"))
    channel.threads[0].archived = True

    run(manager.post("cp_discord:a", "back to work"))

    assert channel.threads[0].archived is False
    assert channel.threads[0].messages == ["back to work"]


def test_long_posts_are_chunked(manager, channel):
    """AC-81b: the report reaches the thread INTACT, not truncated."""
    body = "\n".join(f"line {index:04d} " + "x" * 60 for index in range(120))
    run(manager.ensure_thread("cp_discord:a", "cp_plugins/main"))

    run(manager.post("cp_discord:a", body))

    posted = channel.threads[0].messages
    assert len(posted) > 1
    assert all(len(message) <= 2000 for message in posted)
    assert "line 0000" in posted[0]
    assert "line 0119" in posted[-1]


def test_a_discord_failure_never_escapes(manager, channel):
    """INV-C1: Discord being down is not the terminal session's problem."""

    async def refuse(*_args, **_kwargs):
        raise RuntimeError("discord is down")

    run(manager.ensure_thread("cp_discord:a", "cp_plugins/main"))
    channel.threads[0].send = refuse

    run(manager.post("cp_discord:a", "anything"))


def test_a_refused_thread_creation_says_why(manager, channel, caplog):
    """A bridge that fails SILENTLY is indistinguishable from one that works.

    ``debug`` is not enough: nobody runs a terminal at debug level, so the
    only symptom left was "no thread appears", with no route back to the
    cause -- which is exactly how a live outage cost 40 minutes of bisecting.
    """

    async def refuse(**_kwargs):
        raise RuntimeError("discord is down")

    channel.create_thread = refuse

    with caplog.at_level(logging.WARNING, logger="cp_discord.broker_threads"):
        assert run(manager.ensure_thread("cp_discord:a", "cp_plugins/main")) is None

    assert [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "cp_discord:a" in record.getMessage()
    ], "a thread that could not be created must be audible, with its session"


def test_a_missing_channel_says_why(caplog):
    """The state the live bridge was actually stuck in: no channel, no word.

    Reached while the bot is still logging in -- or forever, if the token or
    the channel id is wrong.  Both are faults an operator can fix, but only
    if somebody tells them.
    """
    manager = threads.ThreadManager(lambda: None)

    with caplog.at_level(logging.WARNING, logger="cp_discord.broker_threads"):
        assert run(manager.ensure_thread("cp_discord:a", "cp_plugins/main")) is None

    assert [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "cp_discord:a" in record.getMessage()
    ], "a session with no channel to open a thread in must be audible"


def test_channel_posts_go_to_the_channel(manager, channel):
    """AC-60b/AC-71b: activation warnings belong in the channel itself.

    A session that never came up has no thread, so a thread-only path would
    put the explanation exactly where nobody can read it.
    """
    run(manager.post_channel("no identities are configured"))

    assert channel.messages == ["no identities are configured"]


def test_titles_stay_unique_across_sessions(manager, channel):
    """AC-11 end to end: same directory, same branch, two sessions."""
    run(manager.ensure_thread("cp_discord:a", "cp_plugins/main"))
    run(manager.ensure_thread("cp_discord:b", "cp_plugins/main"))

    assert [thread.name for thread in channel.threads] == [
        "cp_plugins/main",
        "cp_plugins/main #1",
    ]


# --------------------------------------------------------------------------- #
# Session registry (§3.3a, AC-54) and liveness (AC-14, AC-15, AC-16, AC-51)
# --------------------------------------------------------------------------- #


def a_record(session_id="cp_discord:a", **overrides):
    """A record for a LIVE session -- this process, so liveness is real."""
    pid, started_at = election.process_identity()
    fields = {
        "session_id": session_id,
        "title": "cp_plugins/main",
        "pid": pid,
        "started_at": started_at,
        "inbound_port": 5555,
        "thread_id": 4242,
        "last_seen": 1000.0,
        "last_seq": 0,
    }
    fields.update(overrides)
    return threads.SessionRecord(**fields)


def test_registry_survives_a_broker_change(bridge_dir):
    """AC-54: a fresh broker must find the sessions the old one served.

    Without this, every re-election would make every thread look orphaned.
    """
    first = threads.SessionRegistry()
    first.upsert(a_record(inbound_port=6001))

    second = threads.SessionRegistry()

    restored = second.get("cp_discord:a")
    assert restored is not None
    assert restored.inbound_port == 6001
    assert restored.thread_id == 4242
    assert restored.title == "cp_plugins/main"


def test_the_registry_records_a_thread_id(bridge_dir):
    """The write-back the whole adoption path depends on (INV-C14, AC-54).

    ``adopt_registered_sessions`` has nothing to adopt unless the id the
    gateway created actually lands here -- and it has to land ON DISK, since
    the case adoption exists for is a broker that already died.
    """
    registry = threads.SessionRegistry()
    registry.upsert(a_record(thread_id=None))

    assert registry.set_thread_id("cp_discord:a", 4242) is True
    assert registry.get("cp_discord:a").thread_id == 4242
    assert threads.SessionRegistry().get("cp_discord:a").thread_id == 4242


def test_a_session_without_a_thread_may_claim_one(bridge_dir):
    """Befund B: "known" is not "served" -- but only ONE ask goes out per gap.

    Both halves matter.  Without the first, a registration that survived a
    Discord outage leaves the session threadless for good; without the second,
    the re-registrations of an election would each open one.
    """
    registry = threads.SessionRegistry()
    registry.upsert(a_record(thread_id=None))

    assert registry.claim_thread("cp_discord:a") is True
    assert registry.claim_thread("cp_discord:a") is False, "claimed twice"


def test_a_session_with_a_thread_claims_nothing(bridge_dir):
    """INV-C14: the history has to survive the re-registration after an election."""
    registry = threads.SessionRegistry()
    registry.upsert(a_record(thread_id=4242))

    assert registry.claim_thread("cp_discord:a") is False


def test_a_claim_is_dropped_with_the_session(bridge_dir):
    """A session that comes back after AC-13 needs a thread again.

    Its old one was archived, so a claim left behind would keep the returning
    session mute -- the same permanent silence, through the other door.
    """
    registry = threads.SessionRegistry()
    registry.upsert(a_record(thread_id=None))
    registry.claim_thread("cp_discord:a")

    registry.remove("cp_discord:a")
    registry.upsert(a_record(thread_id=None))

    assert registry.claim_thread("cp_discord:a") is True


def test_an_unknown_session_claims_nothing(bridge_dir):
    """Registering is what creates the right to a thread, as it does for ``seq``."""
    assert threads.SessionRegistry().claim_thread("cp_discord:ghost") is False


def test_recording_a_thread_id_for_a_gone_session_is_quiet(bridge_dir):
    """The session may have been released while Discord was still working.

    Re-inserting it here would resurrect a record that ``release`` deliberately
    removed, and the next sweep would then archive a thread nobody owns.
    """
    registry = threads.SessionRegistry()

    assert registry.set_thread_id("cp_discord:gone", 4242) is False
    assert registry.get("cp_discord:gone") is None


@POSIX_ONLY
def test_registry_file_is_owner_only(bridge_dir):
    """It carries every session's inbound port -- the return-channel address."""
    threads.SessionRegistry().upsert(a_record())

    mode = stat.S_IMODE(election.registry_path().stat().st_mode)

    assert mode == 0o600


def test_a_corrupt_registry_reads_as_empty(bridge_dir):
    bridge_dir.mkdir(parents=True)
    election.registry_path().write_text("[[[", encoding="utf-8")

    assert threads.SessionRegistry().records() == []


def test_a_record_without_a_pid_is_dropped(bridge_dir):
    """INV-C13: liveness is undecidable without a PID, so the entry is refused.

    Substituting a placeholder would be worse than dropping it: PID 1 exists
    on every machine, so the record would look permanently alive and its
    thread would never be archived.
    """
    bridge_dir.mkdir(parents=True)
    election.registry_path().write_text(
        json.dumps(
            [
                {"session_id": "cp_discord:no-pid", "title": "x"},
                {"pid": 4242, "title": "no session id"},
                a_record("cp_discord:good").as_json(),
            ]
        ),
        encoding="utf-8",
    )

    registry = threads.SessionRegistry()

    assert [record.session_id for record in registry.records()] == ["cp_discord:good"]


def test_removing_a_session_persists(bridge_dir):
    registry = threads.SessionRegistry()
    registry.upsert(a_record())

    registry.remove("cp_discord:a")

    assert threads.SessionRegistry().get("cp_discord:a") is None


# -- AC-8: replays are discarded --------------------------------------------


def test_a_replayed_sequence_number_is_discarded(bridge_dir):
    """AC-8: ``seq <= last_seq`` is dropped, so a retry is idempotent."""
    registry = threads.SessionRegistry()
    registry.upsert(a_record())

    assert registry.accept_seq("cp_discord:a", 1) is True
    assert registry.accept_seq("cp_discord:a", 2) is True
    assert registry.accept_seq("cp_discord:a", 2) is False
    assert registry.accept_seq("cp_discord:a", 1) is False
    assert registry.accept_seq("cp_discord:a", 3) is True


def test_sequence_numbers_are_tracked_per_session(bridge_dir):
    registry = threads.SessionRegistry()
    registry.upsert(a_record("cp_discord:a"))
    registry.upsert(a_record("cp_discord:b"))

    assert registry.accept_seq("cp_discord:a", 5) is True
    assert registry.accept_seq("cp_discord:b", 1) is True


def test_a_sequence_number_from_an_unknown_session_is_refused(bridge_dir):
    """Fail-closed: an unregistered session has no place in the registry."""
    assert threads.SessionRegistry().accept_seq("cp_discord:ghost", 1) is False


# -- AC-14 / AC-15 / AC-51: who gets archived -------------------------------


def test_a_silent_but_live_session_is_kept(bridge_dir):
    """AC-15: the dangerous one -- a live session with a lapsed heartbeat.

    Archiving here would make ACTIVE threads disappear.
    """
    registry = threads.SessionRegistry()
    registry.upsert(a_record(last_seen=0.0))

    assert registry.dead_sessions(now=10_000.0) == []


def test_a_silent_dead_session_is_archived(bridge_dir):
    """AC-14: hard exit -- no atexit, no SIGTERM, just silence then a dead PID."""
    registry = threads.SessionRegistry()
    registry.upsert(a_record(pid=_unused_pid(), started_at=1.0, last_seen=0.0))

    assert registry.dead_sessions(now=10_000.0) == ["cp_discord:a"]


def test_a_dead_session_within_the_grace_period_is_kept(bridge_dir):
    """The PID is only consulted AFTER the heartbeat lapses (§7)."""
    registry = threads.SessionRegistry()
    registry.upsert(a_record(pid=_unused_pid(), started_at=1.0, last_seen=0.0))

    assert registry.dead_sessions(now=threads.HEARTBEAT_GRACE - 1.0) == []


def test_a_recycled_pid_counts_as_dead(bridge_dir):
    """AC-51 / INV-C13: the PID lives, but it is somebody else's now.

    With a bare PID check this thread would never be archived at all.
    """
    pid, started_at = election.process_identity()
    registry = threads.SessionRegistry()
    registry.upsert(a_record(pid=pid, started_at=started_at - 3600.0, last_seen=0.0))

    assert registry.dead_sessions(now=10_000.0) == ["cp_discord:a"]


def test_touch_resets_the_grace_period(bridge_dir):
    registry = threads.SessionRegistry()
    registry.upsert(a_record(pid=_unused_pid(), started_at=1.0, last_seen=0.0))

    registry.touch("cp_discord:a", now=10_000.0)

    assert registry.dead_sessions(now=10_000.0) == []


# --------------------------------------------------------------------------- #
# The server: transport and protocol (AC-4, AC-5, AC-8)
# --------------------------------------------------------------------------- #


class RecordingGateway:
    """Stands in for the Discord side: records instead of posting.

    Every method is SYNCHRONOUS and returns at once, which is the contract the
    real gateway has too -- a TCP handler that waited for Discord would make
    the broker's latency Discord's latency (INV-C17).
    """

    def __init__(self) -> None:
        self.opened: list[tuple] = []
        self.posts: list[tuple] = []
        self.channel_posts: list[str] = []
        self.archived: list[str] = []
        self.adopted: list[str] = []

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
    from cp_discord import broker_server

    instance = broker_server.Broker(gateway, token="s3cret")
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def call(broker_instance, payload):
    """One request/response round trip on the real socket."""
    address = broker_instance.address
    with socket.create_connection((address.host, address.port), timeout=5) as sock:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        with sock.makefile("r", encoding="utf-8") as stream:
            return json.loads(stream.readline())


def register(broker_instance, session_id="cp_discord:a", *, token=None, **params):
    """Register with *broker_instance*, using ITS token unless one is given.

    Defaulting to the broker's own token matters: an elected broker ADOPTS or
    mints its token, so a hardcoded one would silently turn every registration
    into an authorization failure and make adoption tests pass vacuously.
    """
    pid, started_at = election.process_identity()
    payload = {
        "title": "cp_plugins/main",
        "pid": pid,
        "started_at": started_at,
        "inbound_port": 6100,
    }
    payload.update(params)
    return call(
        broker_instance,
        {
            "token": broker_instance.token if token is None else token,
            "method": "register",
            "session_id": session_id,
            "seq": 1,
            "params": payload,
        },
    )


def test_the_broker_binds_loopback_only(broker):
    """AC-4 / INV-C2: this socket brokers shell approvals."""
    assert broker.address.host == "127.0.0.1"
    assert broker.bound_host() == "127.0.0.1"
    assert broker.address.port > 0


def test_a_registration_creates_a_thread(broker, gateway):
    response = register(broker)

    assert response["ok"] is True
    assert gateway.opened == [("cp_discord:a", "cp_plugins/main")]


def test_a_registration_without_the_token_is_refused(broker, gateway):
    """AC-5: otherwise any local process could answer approval gates."""
    response = register(broker, token="wrong")

    assert response["ok"] is False
    assert response["error"] == broker.UNAUTHORIZED
    assert gateway.opened == []


def test_a_message_without_any_token_is_refused(broker, gateway):
    response = call(broker, {"method": "register", "session_id": "cp_discord:a"})

    assert response["ok"] is False
    assert response["error"] == broker.UNAUTHORIZED


def test_garbage_gets_an_answer_and_the_broker_survives(broker):
    """INV-C1: a malformed frame is not a reason to stop serving."""
    address = broker.address
    with socket.create_connection((address.host, address.port), timeout=5) as sock:
        sock.sendall(b"this is not json\n")
        with sock.makefile("r", encoding="utf-8") as stream:
            response = json.loads(stream.readline())

    assert response["ok"] is False
    assert register(broker)["ok"] is True


def test_a_replayed_envelope_is_answered_but_not_applied(broker, gateway):
    """AC-8: the transport retries identical envelopes; that must be safe."""
    register(broker)

    def state(seq):
        return call(
            broker,
            {
                "token": "s3cret",
                "method": "state",
                "session_id": "cp_discord:a",
                "seq": seq,
                "params": {"state": "working", "message": "coding…"},
            },
        )

    assert state(2)["ok"] is True
    replay = state(2)

    assert replay["ok"] is True, "a retry must be ACKED, or it retries forever"
    assert replay["duplicate"] is True
    assert len(gateway.bodies_for("cp_discord:a")) == 1


def test_an_unregistered_session_is_refused(broker, gateway):
    response = call(
        broker,
        {
            "token": "s3cret",
            "method": "state",
            "session_id": "cp_discord:ghost",
            "seq": 1,
            "params": {"state": "working"},
        },
    )

    assert response["ok"] is False
    assert gateway.posts == []


def test_a_heartbeat_keeps_the_session_alive(broker):
    register(broker)

    response = call(
        broker,
        {
            "token": "s3cret",
            "method": "heartbeat",
            "session_id": "cp_discord:a",
            "seq": 2,
            "params": {},
        },
    )

    assert response["ok"] is True
    assert broker.registry.get("cp_discord:a").last_seen > 0


def test_a_release_archives_the_thread(broker, gateway):
    """AC-13: ``session_end`` -> archived (never deleted, INV-C3)."""
    register(broker)

    response = call(
        broker,
        {
            "token": "s3cret",
            "method": "release",
            "session_id": "cp_discord:a",
            "seq": 9,
            "params": {},
        },
    )

    assert response["ok"] is True
    assert gateway.archived == ["cp_discord:a"]
    assert broker.registry.get("cp_discord:a") is None


def test_registering_twice_adopts_instead_of_rebuilding(broker, gateway):
    """INV-C14: a re-registration must not shred the history."""
    register(broker)
    register(broker)

    assert len(gateway.opened) == 1
    assert gateway.archived == []


def test_the_inbound_port_is_recorded(broker):
    """§3.3a: without it a re-elected broker has no return address."""
    register(broker, inbound_port=6123)

    assert broker.registry.get("cp_discord:a").inbound_port == 6123


def test_a_known_session_without_a_thread_still_gets_one(broker, gateway):
    """ "Known" is not "has a thread", and confusing them strands a session.

    A registration that got through while Discord was unreachable leaves an
    entry with an empty ``thread_id``.  Reading that entry as "already served"
    makes the session permanently invisible, and a restart does NOT heal it --
    the entry is precisely what survives one.  Every Discord outage during
    thread creation would otherwise leave a session behind for good.
    """
    broker.registry.upsert(a_record(thread_id=None))

    assert register(broker)["ok"] is True
    assert gateway.opened == [("cp_discord:a", "cp_plugins/main")]


def test_a_session_that_has_a_thread_gets_no_second_one(broker, gateway):
    """INV-C14, the other half: re-registering must not shred the history.

    Every session calls in again after an election, so rebuilding there would
    destroy exactly the history this feature exists to keep.  This is the
    guard on the fix above, not a restatement of it.
    """
    broker.registry.upsert(a_record(thread_id=4242))

    assert register(broker)["ok"] is True
    assert gateway.opened == []


def test_a_released_session_that_comes_back_gets_a_new_thread(broker, gateway):
    """AC-13 then a fresh start: its old thread was ARCHIVED, so it needs one.

    The broker remembers that it already asked for a thread, which is what
    keeps a re-registration from opening a second one.  Carrying that memory
    past the archive would leave the returning session mute for good -- the
    same permanent silence, entered through the other door.
    """
    register(broker)
    call(
        broker,
        {
            "token": broker.token,
            "method": "release",
            "session_id": "cp_discord:a",
            "seq": 9,
            "params": {},
        },
    )
    gateway.opened.clear()

    assert register(broker)["ok"] is True
    assert gateway.opened == [("cp_discord:a", "cp_plugins/main")]


def test_a_swept_session_that_comes_back_gets_a_new_thread(broker, gateway):
    """The same door, opened by the sweep instead of by a clean release (§7)."""
    register(broker)
    broker.registry.touch("cp_discord:a", now=0.0)
    broker.registry.upsert(
        a_record(pid=_unused_pid(), started_at=1.0, last_seen=0.0, thread_id=None)
    )
    broker.sweep_dead_sessions()
    gateway.opened.clear()

    assert register(broker)["ok"] is True
    assert gateway.opened == [("cp_discord:a", "cp_plugins/main")]


def test_a_lost_write_back_heals_without_a_second_thread(bridge_dir, channel):
    """A thread that exists while its record says ``None`` -- and the repair.

    The write-back is asynchronous, so the register can lag what Discord
    already holds.  Asking again must NOT produce a second thread (INV-C14):
    ``ensure_thread`` hands back the one that exists, which is also what puts
    the missing id back into the record.  Driven through the real gateway,
    because the idempotence under test is the gateway's.
    """
    from cp_discord import broker_server

    gateway = threads.DiscordGateway()
    gateway.start_loop()
    gateway.set_channel(channel)
    instance = broker_server.Broker(gateway, token="s3cret")
    instance.start()
    try:
        register(instance)
        gateway.wait_idle()
        # Exactly the damaged state: the thread is real, the record forgot it.
        instance.registry.remove("cp_discord:a")
        instance.registry.upsert(a_record(thread_id=None))

        assert register(instance)["ok"] is True
        gateway.wait_idle()
    finally:
        instance.stop()
        gateway.close()

    assert len(channel.threads) == 1, "a second thread was opened"
    assert instance.registry.get("cp_discord:a").thread_id == channel.threads[0].id


def test_the_created_thread_id_reaches_the_registry(bridge_dir, channel):
    """Befund A: the id ``ensure_thread`` returns has to be WRITTEN DOWN.

    Without it the column stays ``None`` forever, and
    ``adopt_registered_sessions`` finds nothing to adopt after a re-election --
    INV-C14 would hold in the suite and nowhere else.  Driven through the real
    gateway on purpose: the write-back crosses the queue, and a double would
    prove only that the double works.
    """
    from cp_discord import broker_server

    gateway = threads.DiscordGateway()
    gateway.start_loop()
    gateway.set_channel(channel)
    instance = broker_server.Broker(gateway, token="s3cret")
    instance.start()
    try:
        assert register(instance)["ok"] is True
        gateway.wait_idle()
    finally:
        instance.stop()
        gateway.close()

    assert len(channel.threads) == 1
    assert instance.registry.get("cp_discord:a").thread_id == channel.threads[0].id


def test_a_recorded_thread_id_survives_into_the_next_broker(bridge_dir, channel):
    """AC-53/AC-54 end to end: what the write-back is FOR.

    The successor loads the register from disk and adopts the thread instead
    of building a new one -- the tab switch the user never sees.
    """
    from cp_discord import broker_server

    gateway = threads.DiscordGateway()
    gateway.start_loop()
    gateway.set_channel(channel)
    first = broker_server.Broker(gateway, token="s3cret")
    first.start()
    try:
        register(first)
        gateway.wait_idle()
    finally:
        first.stop()

    successor = RecordingGateway()
    second = broker_server.Broker(successor, token="s3cret")
    second.adopt_registered_sessions()
    gateway.close()

    assert successor.adopted == ["cp_discord:a"]


# --------------------------------------------------------------------------- #
# What arrives in the thread (AC-26b, AC-27b, AC-69, AC-81b)
# --------------------------------------------------------------------------- #


def send_event(broker_instance, seq, method, params):
    return call(
        broker_instance,
        {
            "token": "s3cret",
            "method": method,
            "session_id": "cp_discord:a",
            "seq": seq,
            "params": params,
        },
    )


def test_report_mode_shows_a_status_line_then_the_report(broker, gateway):
    """AC-26b: during work only the status line; at the wait point, the report."""
    register(broker)

    send_event(broker, 2, "state", {"state": "working", "message": "coding…"})
    send_event(broker, 3, "report", {"chunks": ["letzte Antwort", "-> run_shell"]})
    send_event(
        broker,
        4,
        "state",
        {
            "state": "blocked",
            "message": "wartet auf deine Freigabe",
            "remote_resolvable": True,
        },
    )

    bodies = gateway.bodies_for("cp_discord:a")
    assert bodies == [
        "coding…",
        "letzte Antwort",
        "-> run_shell",
        "wartet auf deine Freigabe",
    ]


def test_report_chunks_arrive_intact_and_in_order(broker, gateway):
    """AC-81b: the content C7 built reaches the thread unchanged."""
    register(broker)
    chunks = [f"chunk {index}" for index in range(5)]

    send_event(broker, 2, "report", {"chunks": chunks})

    assert gateway.bodies_for("cp_discord:a") == chunks


def test_stream_deltas_arrive_live(broker, gateway):
    """AC-27b: in ``stream`` mode the activity line keeps updating."""
    register(broker)

    for index, text in enumerate(
        ["schreibt a", "schreibt ab", "schreibt abc"], start=2
    ):
        send_event(broker, index, "state", {"state": "working", "message": text})

    assert gateway.bodies_for("cp_discord:a") == [
        "schreibt a",
        "schreibt ab",
        "schreibt abc",
    ]


def test_a_locally_answerable_block_is_marked_as_such(broker, gateway):
    """AC-69 / INV-C23: a ``/agent`` menu cannot be answered from the phone.

    Posting it like a gate would leave somebody tapping at a message that is
    never going to respond.

    The message deliberately arrives WITHOUT the marker.  C3's own
    ``BLOCKED_LOCALLY`` text already contains it, so asserting against that
    would pass no matter what the broker did -- measured: the assertion
    survived deleting the labelling outright.  The authority is the
    ``remote_resolvable`` FLAG, not somebody else's wording.
    """
    from cp_discord import broker_server

    register(broker)

    send_event(
        broker,
        2,
        "state",
        {
            "state": "blocked",
            "message": "wartet auf eine Eingabe",
            "remote_resolvable": False,
        },
    )

    posted = gateway.bodies_for("cp_discord:a")[-1]
    assert broker_server.LOCAL_ONLY_MARKER in posted


def test_an_already_marked_message_is_not_marked_twice(broker, gateway):
    """C3 words it itself; the broker must not stutter over it."""
    from cp_discord import broker_server
    from cp_discord.reporter import BLOCKED_LOCALLY

    register(broker)

    send_event(
        broker,
        2,
        "state",
        {
            "state": "blocked",
            "message": BLOCKED_LOCALLY,
            "remote_resolvable": False,
        },
    )

    posted = gateway.bodies_for("cp_discord:a")[-1]
    assert posted.count(broker_server.LOCAL_ONLY_MARKER) == 1


def test_a_gate_block_is_not_marked_as_local(broker, gateway):
    from cp_discord import broker_server

    register(broker)

    send_event(
        broker,
        2,
        "state",
        {
            "state": "blocked",
            "message": "wartet auf deine Freigabe",
            "remote_resolvable": True,
        },
    )

    posted = gateway.bodies_for("cp_discord:a")[-1]
    assert broker_server.LOCAL_ONLY_MARKER not in posted


# --------------------------------------------------------------------------- #
# Activation warnings reach the channel (AC-60b, AC-71b)
# --------------------------------------------------------------------------- #


def test_configuration_warnings_are_posted_into_the_channel(bridge_dir, gateway):
    """AC-60b / INV-C15: not silent -- somebody is staring at a quiet thread."""
    from cp_discord import broker_server

    broker_instance = broker_server.Broker(
        gateway,
        token="s3cret",
        notices=("no identities are configured",),
    )
    broker_instance.start()
    try:
        broker_instance.announce_notices()
    finally:
        broker_instance.stop()

    assert any("no identities are configured" in body for body in gateway.channel_posts)


def test_an_activation_failure_is_posted_into_the_channel(bridge_dir, gateway):
    """AC-71b: the ``emit_error`` half is W6's; the channel half is the broker's.

    The person who needs this message is not looking at the terminal -- that is
    the entire premise of the feature.
    """
    from cp_discord import broker_server

    broker_instance = broker_server.Broker(
        gateway,
        token="s3cret",
        notices=("layer C4 (approvals) failed to start: slot taken",),
    )
    broker_instance.start()
    try:
        broker_instance.announce_notices()
    finally:
        broker_instance.stop()

    assert any("failed to start" in body for body in gateway.channel_posts)


def test_notices_are_announced_once(bridge_dir, gateway):
    """A re-election must not replay every old warning into the channel."""
    from cp_discord import broker_server

    broker_instance = broker_server.Broker(
        gateway, token="s3cret", notices=("eine Warnung",)
    )
    broker_instance.start()
    try:
        broker_instance.announce_notices()
        broker_instance.announce_notices()
    finally:
        broker_instance.stop()

    assert len(gateway.channel_posts) == 1


# --------------------------------------------------------------------------- #
# The return channel: broker -> session (§3.2a, AC-85d)
# --------------------------------------------------------------------------- #


class FakeSession:
    """A session's inbound listener: loopback, token-checked (INV-C18).

    ``reject_tokens`` makes it behave like a session that has not healed yet;
    ``heal_after`` is how many rejections it takes before it accepts, which is
    what lets the retry rule be tested for real rather than by counting calls.
    """

    def __init__(self, *, token="s3cret", heal_after=None):
        self.token = token
        self.heal_after = heal_after
        self.rejections = 0
        self.received = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self.port = self._server.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._server.settimeout(0.2)
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            with connection:
                try:
                    self._handle(connection)
                except OSError:
                    pass

    def _handle(self, connection):
        connection.settimeout(2.0)
        with connection.makefile("rwb") as stream:
            line = stream.readline()
            if not line:
                return
            try:
                payload = json.loads(line.decode("utf-8"))
            except ValueError:
                payload = {}
            if payload.get("token") != self.token:
                self.rejections += 1
                if self.heal_after is not None and self.rejections >= self.heal_after:
                    self.token = payload.get("token")
                answer = {"ok": False, "error": "unauthorized"}
            else:
                self.received.append(payload)
                answer = {"ok": True}
            stream.write((json.dumps(answer) + "\n").encode("utf-8"))
            stream.flush()

    def close(self):
        self._stop.set()
        self._thread.join(2)
        self._server.close()


@pytest.fixture
def session_listener():
    listeners = []

    def make(**kwargs):
        listener = FakeSession(**kwargs)
        listeners.append(listener)
        return listener

    yield make
    for listener in listeners:
        listener.close()


def test_a_resolution_carries_gate_id_decision_and_sender(broker, session_listener):
    """§3.2a: the APPROVER check happens in the SESSION, so the sender rides along.

    Freezing the frame without ``discord_user_id`` would leave W4 unable to
    authorize anything -- and unable to change the frame by then.
    """
    listener = session_listener()
    register(broker, inbound_port=listener.port)

    delivered = broker.deliver_resolution("cp_discord:a", "gate-1", "approve", 424242)

    assert delivered is True
    assert len(listener.received) == 1
    params = listener.received[0]["params"]
    assert params["gate_id"] == "gate-1"
    assert params["decision"] == "approve"
    assert params["discord_user_id"] == 424242


def test_delivery_is_fast(broker, session_listener):
    """INV-C17: the return channel is actively delivered, not heartbeat-coupled."""
    listener = session_listener()
    register(broker, inbound_port=listener.port)

    started = time.monotonic()
    broker.deliver_resolution("cp_discord:a", "gate-1", "approve", 1)
    elapsed = time.monotonic() - started

    assert elapsed < 0.1


def test_a_token_rejection_is_retried_until_the_session_heals(
    broker, session_listener, monkeypatch
):
    """AC-85d: three attempts, one second apart -- the second finds the new token.

    Without the retry the phone tap is simply gone: the CAS lives in the
    session, a discarded delivery never sets it, and after 120 s the Discord
    branch is dead (INV-C10).
    """
    from cp_discord import broker_server

    monkeypatch.setattr(broker_server, "RETRY_DELAY", 0.01)
    listener = session_listener(token="stale", heal_after=1)
    register(broker, inbound_port=listener.port)

    delivered = broker.deliver_resolution("cp_discord:a", "gate-1", "approve", 1)

    assert delivered is True
    assert listener.rejections == 1
    assert len(listener.received) == 1


def test_a_rejected_delivery_never_marks_the_session_dead(
    broker, gateway, session_listener, monkeypatch
):
    """AC-85d: "token invalid" PROVES somebody is alive over there.

    Reading it as a failed connection would archive a LIVE session's thread
    (INV-C14, AC-15).
    """
    from cp_discord import broker_server

    monkeypatch.setattr(broker_server, "RETRY_DELAY", 0.01)
    listener = session_listener(token="stale")
    register(broker, inbound_port=listener.port)

    delivered = broker.deliver_resolution("cp_discord:a", "gate-1", "approve", 1)

    assert delivered is False
    assert listener.rejections == broker_server.RETRY_ATTEMPTS
    # The MARK is what matters, and it is asserted directly: checking only
    # ``archived`` would pass while the session sat marked, because nothing
    # archives until the next sweep (measured -- the assertion survived
    # marking every refusal as dead).
    assert broker.is_marked_dead("cp_discord:a") is False
    assert gateway.archived == []
    assert broker.registry.get("cp_discord:a") is not None


def test_an_undeliverable_resolution_is_reported_in_the_thread(broker, gateway):
    """INV-C17: a failed delivery is announced, not swallowed."""
    dead_port = _closed_port()
    register(broker, inbound_port=dead_port)

    delivered = broker.deliver_resolution("cp_discord:a", "gate-1", "approve", 1)

    assert delivered is False
    assert any(
        "Zustellung fehlgeschlagen" in body
        for body in gateway.bodies_for("cp_discord:a")
    )


def _closed_port() -> int:
    """A port nothing listens on.  Bound and released, never guessed."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_delivering_to_an_unknown_session_is_refused(broker):
    assert broker.deliver_resolution("cp_discord:ghost", "g", "approve", 1) is False


def test_a_transport_failure_marks_the_session_dead(broker, gateway):
    """§3.2a: connect refused IS a dead session -- unlike a token rejection."""
    register(broker, inbound_port=_closed_port())

    broker.deliver_resolution("cp_discord:a", "gate-1", "approve", 1)

    assert broker.is_marked_dead("cp_discord:a") is True


def test_a_marked_session_whose_process_lives_is_still_not_archived(broker, gateway):
    """AC-15 guards this path too: the PID has the last word (INV-C13).

    A mark only shortens the grace period; it never overrules liveness, or a
    momentarily unreachable listener would take a working session's thread
    down with it.
    """
    register(broker, inbound_port=_closed_port())
    broker.deliver_resolution("cp_discord:a", "gate-1", "approve", 1)

    broker.sweep_dead_sessions()

    assert gateway.archived == []
    assert broker.registry.get("cp_discord:a") is not None


def test_a_marked_session_whose_process_died_is_archived_at_once(broker, gateway):
    """The mark's whole value: skip the 90-second wait when the PID is gone."""
    register(broker, pid=_unused_pid(), started_at=1.0, inbound_port=_closed_port())
    broker.deliver_resolution("cp_discord:a", "gate-1", "approve", 1)

    broker.sweep_dead_sessions()

    assert gateway.archived == ["cp_discord:a"]


def test_the_sweep_archives_sessions_whose_process_is_gone(broker, gateway):
    """AC-14 / AC-16: hard exit, then the broker cleans up after the grace."""
    register(broker, pid=_unused_pid(), started_at=1.0)
    broker.registry.touch("cp_discord:a", now=time.time() - threads.HEARTBEAT_GRACE - 1)

    broker.sweep_dead_sessions()

    assert gateway.archived == ["cp_discord:a"]
    assert broker.registry.get("cp_discord:a") is None


def test_the_sweep_leaves_live_sessions_alone(broker, gateway):
    """AC-15 again, from the sweep's side."""
    register(broker)
    broker.registry.touch("cp_discord:a", now=time.time() - threads.HEARTBEAT_GRACE - 1)

    broker.sweep_dead_sessions()

    assert gateway.archived == []


# --------------------------------------------------------------------------- #
# Election, re-election and thread liveness (AC-52, AC-53, AC-70, AC-85a)
# --------------------------------------------------------------------------- #


@pytest.fixture
def supervisor_factory(bridge_dir, gateway):
    from cp_discord import broker_activation

    made = []

    def make(**kwargs):
        instance = broker_activation.BrokerSupervisor(lambda: gateway, **kwargs)
        made.append(instance)
        return instance

    yield make
    for instance in made:
        instance.stop()


def test_the_first_candidate_becomes_the_broker(supervisor_factory, bridge_dir):
    supervisor = supervisor_factory()

    assert supervisor.try_elect() is True
    assert supervisor.is_broker is True
    assert election.read_portfile().port == supervisor.broker.address.port


def test_only_one_candidate_wins(supervisor_factory):
    """§3.1: Discord allows one gateway connection per bot token."""
    first = supervisor_factory()
    second = supervisor_factory()

    assert first.try_elect() is True
    assert second.try_elect() is False
    assert second.is_broker is False


def test_a_re_election_keeps_the_token(supervisor_factory, bridge_dir):
    """AC-85a(a): otherwise every established session goes deaf (INV-C18)."""
    first = supervisor_factory()
    first.try_elect()
    inherited = election.read_portfile().token
    first.stop()

    second = supervisor_factory()
    second.try_elect()

    assert election.read_portfile().token == inherited
    assert second.broker.token == inherited


def test_a_cold_start_mints_a_token(supervisor_factory, bridge_dir):
    """AC-85a(b): nothing to inherit, so rotate."""
    supervisor = supervisor_factory()
    supervisor.try_elect()

    assert supervisor.broker.token
    assert election.read_portfile().token == supervisor.broker.token


def test_a_new_broker_adopts_the_existing_register(supervisor_factory, gateway):
    """AC-53/AC-54: adopt, do not rebuild, and archive nothing."""
    first = supervisor_factory()
    first.try_elect()
    register(first.broker)
    first.stop()
    gateway.opened.clear()

    second = supervisor_factory()
    second.try_elect()

    assert second.broker.registry.get("cp_discord:a") is not None
    assert gateway.adopted == ["cp_discord:a"]
    assert gateway.opened == []
    assert gateway.archived == []


def test_a_new_broker_archives_orphaned_threads(supervisor_factory, gateway):
    """AC-16 / SS3.3: on takeover, archive the threads with no live session.

    Only those.  A broker that adopted everything would leave the threads of
    long-dead sessions in the active list forever, which is requirement 5
    ("stops nagging") failing quietly.
    """
    first = supervisor_factory()
    first.try_elect()
    register(first.broker, "cp_discord:live")
    register(first.broker, "cp_discord:dead", pid=_unused_pid(), started_at=1.0)
    first.broker.registry.touch(
        "cp_discord:dead", now=time.time() - threads.HEARTBEAT_GRACE - 1
    )
    first.stop()
    gateway.archived.clear()

    second = supervisor_factory()
    second.try_elect()

    assert gateway.archived == ["cp_discord:dead"]
    assert second.broker.registry.get("cp_discord:live") is not None


def test_a_dead_broker_thread_releases_the_lock(supervisor_factory):
    """AC-70 / INV-C22: the process keeps the lock the OS would have freed.

    Without this, every other session fails the non-blocking acquire FOREVER
    -- exactly the state the re-election exists to prevent.
    """
    holder = supervisor_factory()
    assert holder.try_elect() is True

    holder.simulate_broker_thread_death()
    assert holder.broker_thread_alive() is False

    holder.check_broker_health()

    rival = supervisor_factory()
    assert rival.try_elect() is True


def test_a_healthy_broker_keeps_its_lock(supervisor_factory):
    holder = supervisor_factory()
    holder.try_elect()

    holder.check_broker_health()

    assert holder.is_broker is True
    assert supervisor_factory().try_elect() is False


def test_the_holder_re_elects_itself_after_a_thread_death(supervisor_factory):
    """INV-C22's second half: the same process may take over again."""
    holder = supervisor_factory()
    holder.try_elect()
    port_before = holder.broker.address.port

    holder.simulate_broker_thread_death()
    holder.check_broker_health()

    assert holder.is_broker is False
    assert holder.try_elect() is True
    assert holder.broker.address.port != port_before


def test_a_failed_election_leaves_no_broker_behind(
    supervisor_factory, bridge_dir, monkeypatch, caplog
):
    """A half-finished election must not leave a serving thread nobody owns.

    ``start()`` binds the socket, and every step after it can throw --
    ``write_portfile`` is a disk write, so a full volume, a permission or (on
    Windows) a scanner is enough.  Falling out of ``try_elect`` without
    stopping that broker RELEASES the lock while the thread keeps answering:
    the next candidate wins, and two brokers serve at once -- the state SS3.1
    holds to be worse than no broker at all.  And it is unrecoverable, because
    ``self.broker`` was never assigned: ``_release``, ``check_broker_health``
    and ``stop`` all read it, so nothing can reach the orphan for the life of
    the process.
    """
    from cp_discord import broker_activation

    built = []
    order = []
    real_broker = broker_activation.Broker

    def recording_broker(*args, **kwargs):
        instance = real_broker(*args, **kwargs)
        real_stop = instance.stop

        def recording_stop():
            order.append("stop")
            real_stop()

        instance.stop = recording_stop
        built.append(instance)
        return instance

    def explode(_address):
        raise OSError("no space left on device")

    monkeypatch.setattr(broker_activation, "Broker", recording_broker)
    monkeypatch.setattr(broker_activation.election, "write_portfile", explode)

    supervisor = supervisor_factory()
    real_release = supervisor._lock.release
    monkeypatch.setattr(
        supervisor._lock,
        "release",
        lambda: (order.append("release"), real_release())[1],
    )
    with caplog.at_level(logging.DEBUG, logger="cp_discord.broker_activation"):
        assert supervisor.try_elect() is False

    assert supervisor.is_broker is False
    assert len(built) == 1, "the broker was built and started before the throw"
    assert built[0].is_alive() is False, "the serving thread outlived its owner"
    assert broker_activation.broker_is_reachable(built[0].address) is False
    # Order, not merely presence: releasing first opens the window in which a
    # rival wins the lock while this socket is still answering.
    assert order == ["stop", "release"]
    # Louder than the lost ``acquire()`` one line above, which is a normal
    # outcome.  This one is not: a broker that started and then fell over is
    # the only path that can strand a socket.
    levels = [
        record.levelno
        for record in caplog.records
        if record.name == "cp_discord.broker_activation"
    ]
    assert levels == [logging.WARNING]


def test_a_stale_portfile_does_not_block_the_election(
    supervisor_factory, bridge_dir, monkeypatch
):
    """AC-52: the holding tab died, so its address is still on disk -- and wrong.

    SS3.1 step 2 asks whether the broker is REACHABLE, not whether a portfile
    exists.  Reading existence as "somebody is serving" would make the file a
    tombstone: nobody would ever take over, and Discord would stay dead until
    the next cold start -- exactly what the re-election exists to prevent.
    """
    from cp_discord import broker_activation

    monkeypatch.setattr(broker_activation, "ELECTION_RETRY_DELAY", 0.0)
    election.write_portfile(
        election.BrokerAddress(port=_closed_port(), token="vom-toten-broker")
    )

    successor = supervisor_factory()

    assert successor.run_election_round() is True
    assert successor.is_broker is True


def test_a_stale_portfiles_token_is_still_adopted(
    supervisor_factory, bridge_dir, monkeypatch
):
    """AC-85a: unreachable is not the same as untrustworthy.

    The sessions that registered with the dead broker still hold that token;
    minting a new one would cut every one of them off (INV-C18).
    """
    from cp_discord import broker_activation

    monkeypatch.setattr(broker_activation, "ELECTION_RETRY_DELAY", 0.0)
    election.write_portfile(
        election.BrokerAddress(port=_closed_port(), token="vom-toten-broker")
    )

    successor = supervisor_factory()
    successor.run_election_round()

    assert successor.broker.token == "vom-toten-broker"


def test_a_reachable_broker_is_left_alone(supervisor_factory, monkeypatch):
    """The other half: do not depose a broker that is answering."""
    from cp_discord import broker_activation

    monkeypatch.setattr(broker_activation, "ELECTION_RETRY_DELAY", 0.0)
    holder = supervisor_factory()
    holder.try_elect()

    rival = supervisor_factory()

    assert rival.run_election_round() is False
    assert holder.is_broker is True


def test_giving_up_never_breaks_the_session(supervisor_factory, monkeypatch):
    """INV-C1 / §3.1 step 4: no broker just means no Discord."""
    from cp_discord import broker_activation

    monkeypatch.setattr(broker_activation, "ELECTION_RETRY_DELAY", 0.0)
    holder = supervisor_factory()
    holder.try_elect()

    loser = supervisor_factory()

    assert loser.run_election_round() is False
    assert loser.is_broker is False


def test_a_gateway_that_explodes_does_not_take_the_broker_down(
    bridge_dir, session_listener
):
    """INV-C1: the broker keeps serving even if every Discord call fails."""
    from cp_discord import broker_server

    class BrokenGateway:
        def adopt(self, records):
            raise RuntimeError("discord is down")

        def open_thread(self, session_id, title):
            raise RuntimeError("discord is down")

        def post(self, session_id, body):
            raise RuntimeError("discord is down")

        def post_channel(self, body):
            raise RuntimeError("discord is down")

        def archive(self, session_id):
            raise RuntimeError("discord is down")

    instance = broker_server.Broker(BrokenGateway(), token="s3cret")
    instance.start()
    try:
        assert register(instance)["ok"] is True
        assert send_event(instance, 2, "state", {"state": "working"})["ok"] is True
        instance.announce_notices()
        instance.sweep_dead_sessions()
    finally:
        instance.stop()


# --------------------------------------------------------------------------- #
# The Discord gateway: schedule, never wait (INV-C1, INV-C4)
# --------------------------------------------------------------------------- #


@pytest.fixture
def discord_gateway(channel):
    """A real :class:`DiscordGateway` driven by a real loop, no bot token."""
    instance = threads.DiscordGateway()
    instance.start_loop()
    instance.set_channel(channel)
    try:
        yield instance
    finally:
        instance.close()


def test_the_gateway_opens_a_thread(discord_gateway, channel):
    discord_gateway.open_thread("cp_discord:a", "cp_plugins/main")
    discord_gateway.wait_idle()

    assert [thread.name for thread in channel.threads] == ["cp_plugins/main"]


def test_the_gateway_posts_into_the_thread(discord_gateway, channel):
    discord_gateway.open_thread("cp_discord:a", "cp_plugins/main")
    discord_gateway.post("cp_discord:a", "coding…")
    discord_gateway.wait_idle()

    assert channel.threads[0].messages == ["coding…"]


def test_gateway_work_is_ordered(discord_gateway, channel):
    """§8b: the report is posted BEFORE the state edge that announces it.

    Scheduling without preserving order would shuffle the report behind the
    gate it belongs to, which is the one ordering the reader depends on.
    """
    discord_gateway.open_thread("cp_discord:a", "cp_plugins/main")
    for index in range(20):
        discord_gateway.post("cp_discord:a", f"m{index}")
    discord_gateway.wait_idle()

    assert channel.threads[0].messages == [f"m{index}" for index in range(20)]


def test_the_gateway_never_blocks_the_caller(discord_gateway, channel):
    """INV-C1/INV-C4: the broker thread must not inherit Discord's latency."""
    entered = threading.Event()
    release = threading.Event()

    async def slow_send(content):
        entered.set()
        release.wait()
        channel.threads[0].messages.append(content)

    discord_gateway.open_thread("cp_discord:a", "cp_plugins/main")
    discord_gateway.wait_idle()
    channel.threads[0].send = slow_send

    discord_gateway.post("cp_discord:a", "erste")
    assert entered.wait(5), "the slow send never started"

    started = time.monotonic()
    discord_gateway.post("cp_discord:a", "zweite")
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.1


def test_the_gateway_archives_never_deletes(discord_gateway, channel):
    discord_gateway.open_thread("cp_discord:a", "cp_plugins/main")
    discord_gateway.wait_idle()

    discord_gateway.archive("cp_discord:a")
    discord_gateway.wait_idle()

    assert channel.threads[0].archived is True
    assert channel.threads[0].deleted is False


def test_the_gateway_swallows_work_it_cannot_do_yet(channel):
    """INV-C1: a session may register before the bot has logged in."""
    instance = threads.DiscordGateway()
    instance.start_loop()
    try:
        instance.open_thread("cp_discord:a", "cp_plugins/main")
        instance.post("cp_discord:a", "anything")
        instance.post_channel("a warning")
        instance.archive("cp_discord:a")
        instance.wait_idle()
    finally:
        instance.close()

    assert channel.threads == []


def test_the_gateway_works_without_a_loop(channel):
    """Nothing scheduled, nothing raised -- the bot never came up."""
    instance = threads.DiscordGateway()

    instance.set_channel(channel)
    instance.open_thread("cp_discord:a", "cp_plugins/main")
    instance.post_channel("a warning")
    instance.close()

    assert channel.threads == []
    assert channel.messages == []


def test_the_gateway_adopts_registered_sessions(discord_gateway, channel):
    """AC-53: adoption resolves recorded thread ids without creating any."""
    existing = FakeThread(4242, "cp_plugins/main")
    channel.threads.append(existing)
    discord_gateway.set_thread_resolver(
        lambda thread_id: existing if thread_id == 4242 else None
    )

    discord_gateway.adopt([a_record(thread_id=4242)])
    discord_gateway.wait_idle()

    discord_gateway.post("cp_discord:a", "weiter geht es")
    discord_gateway.wait_idle()

    assert len(channel.threads) == 1
    assert existing.messages == ["weiter geht es"]


# --------------------------------------------------------------------------- #
# The C1 plugin surface (C6 drives this)
# --------------------------------------------------------------------------- #


class Config:
    def __init__(self, **kwargs):
        self.token = kwargs.get("token", "bot-token")
        self.channel_id = kwargs.get("channel_id", 12345)
        self.mode = kwargs.get("mode", "report")
        self.session_name = kwargs.get("session_name")


@pytest.fixture
def installed(bridge_dir, monkeypatch):
    """Install C1 with the Discord LOGIN stubbed out -- nothing else.

    The stub replaces exactly one function, the one that needs a bot token.
    Everything ``install`` does around it (election, supervision, teardown)
    is the shipping code path.
    """
    from cp_discord import broker_activation, broker_server

    monkeypatch.setattr(broker_activation, "connect_gateway", lambda config, gw: None)
    yield broker_server
    broker_server.uninstall()


def test_install_returns_immediately(installed):
    """INV-C1: ``startup`` must not wait for an election or for Discord."""
    started = time.monotonic()
    installed.install(Config())
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert installed.active_supervisor() is not None


def test_install_is_idempotent(installed):
    installed.install(Config())
    first = installed.active_supervisor()

    installed.install(Config())

    assert installed.active_supervisor() is not first


def test_uninstall_releases_everything(installed, bridge_dir):
    installed.install(Config())
    installed.uninstall()

    assert installed.active_supervisor() is None
    assert election.SingleOwnerLock().acquire() is True


def test_uninstall_without_install_is_harmless(installed):
    installed.uninstall()


def test_refresh_token_from_portfile_is_exported():
    """W2 calls this by name in its error path (AC-85c); keep the seam."""
    from cp_discord import broker_activation, broker_server

    assert broker_server.refresh_token_from_portfile is (
        election.refresh_token_from_portfile
    )
    assert broker_activation.refresh_token_from_portfile is (
        election.refresh_token_from_portfile
    )


def test_c6_addresses_this_layer_as_broker_server():
    """``COMPONENTS[0].module`` decides which module C6 calls ``install`` on.

    If the name drifted, activation would fail at the FIRST layer and the
    whole bridge would roll back -- with a message about an import, not about
    the broker.
    """
    from cp_discord import broker_server, register_callbacks

    assert register_callbacks.COMPONENTS[0].module == "broker_server"
    assert callable(broker_server.install)
    assert callable(broker_server.uninstall)
