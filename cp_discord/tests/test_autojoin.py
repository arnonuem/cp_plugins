"""Auto-joining the approvers to a fresh session thread (SPEC §8a).

Discord shows a thread in the sidebar -- and pushes a notification for it --
only to members who have JOINED it.  A thread the bot creates and posts into
is therefore INVISIBLE to the human it was created for: it sits in the thread
overview and never says a word.

That breaks the one use case this bridge exists for.  "Start at the PC, leave
the house, carry on from the phone" only works if the phone rings when the
agent hits an approval gate; without a join it does not, and the user would
have to go looking on their own.

The recipients are already known: the APPROVER principals in the
authorization database, which is where ``DISCORD_APPROVERS`` was loaded at
startup.  Re-parsing the configuration here would be a second reader of the
same fact, and the two could drift.

Three properties are load-bearing and each has its own test below:

* **a failed join is a blemish, never a defect** (INV-C1) -- the thread
  stands, the bridge runs, the session never learns about it;
* **one user must not take the others down** -- catching per CALL instead of
  per USER would let a single ex-member block every approver;
* **the switch works** -- somebody running many parallel sessions has to be
  able to turn the notifications off.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Set

import pytest

from cp_discord import (
    bindings,
    broker_autojoin,
    broker_threads,
    constants,
    register_callbacks,
)

WAYNE = "wayne"
WAYNE_ID = "123456789"
MARY = "mary"
MARY_ID = "987654321"
TIM = "tim"
TIM_ID = "555555555"

SESSION = "cp_discord:a"
TITLE = "cp_plugins/main"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeThread:
    """A Discord thread with the surface this feature touches."""

    def __init__(self, thread_id: int, name: str, refuse: Set[int]) -> None:
        self.id = thread_id
        self.name = name
        self.archived = False
        self.added: List[int] = []
        self._refuse = refuse

    async def add_user(self, user: Any) -> None:
        user_id = int(user.id)
        if user_id in self._refuse:
            raise RuntimeError("Missing Permissions")
        self.added.append(user_id)

    async def send(self, content):
        return object()

    async def edit(self, **kwargs):
        if "archived" in kwargs:
            self.archived = bool(kwargs["archived"])


class FakeChannel:
    """A text channel that hands out :class:`FakeThread`."""

    def __init__(self) -> None:
        self.threads: List[FakeThread] = []
        self.refuse: Set[int] = set()
        self._next_id = 2000

    async def create_thread(self, *, name, auto_archive_duration=None, **_kwargs):
        self._next_id += 1
        thread = FakeThread(self._next_id, name, self.refuse)
        self.threads.append(thread)
        return thread

    async def send(self, content):
        return object()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Own bindings database: the real one holds the operator's own roles."""
    monkeypatch.setenv(bindings.DB_PATH_ENV, str(tmp_path / "authz.db"))
    bindings.forget_initialized_paths()
    yield
    bindings.forget_initialized_paths()


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    """Neither the environment nor the developer's puppy.cfg may leak in."""
    monkeypatch.delenv(register_callbacks.AUTOJOIN_ENV_VAR, raising=False)
    monkeypatch.setattr(register_callbacks, "_get_config_value", lambda _key: None)


@pytest.fixture(autouse=True)
def _fresh_warning():
    """The "warn once" flag is session state -- every test gets a new session."""
    broker_autojoin.reset_state()
    yield
    broker_autojoin.reset_state()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def manager(channel) -> broker_threads.ThreadManager:
    return broker_threads.ThreadManager(lambda: channel)


@pytest.fixture
def approvers() -> None:
    """Two approvers and one talker-only principal, as configured at startup."""
    bindings.bind(constants.AUTHZ_CHANNEL, WAYNE_ID, WAYNE)
    bindings.bind(constants.AUTHZ_CHANNEL, MARY_ID, MARY)
    bindings.bind(constants.AUTHZ_CHANNEL, TIM_ID, TIM)
    bindings.grant(WAYNE, bindings.Role.APPROVER)
    bindings.grant(MARY, bindings.Role.APPROVER)
    bindings.grant(TIM, bindings.Role.TALKER)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# The point of the whole thing: the thread reaches the phone
# --------------------------------------------------------------------------- #


def test_a_new_thread_pulls_the_approvers_in(manager, channel, approvers):
    run(manager.ensure_thread(SESSION, TITLE))

    assert channel.threads[0].added == [int(MARY_ID), int(WAYNE_ID)]


def test_a_talker_is_not_pulled_in(manager, channel, approvers):
    """The two role axes stay independent (AC-18): talking is not approving."""
    run(manager.ensure_thread(SESSION, TITLE))

    assert int(TIM_ID) not in channel.threads[0].added


def test_only_discord_identities_are_used(manager, channel):
    """INV-C28: the pair is keyed on a CHANNEL, and only ours belongs here."""
    bindings.bind("slack", "U0001", WAYNE)
    bindings.grant(WAYNE, bindings.Role.APPROVER)

    run(manager.ensure_thread(SESSION, TITLE))

    assert channel.threads[0].added == []


def test_an_adopted_thread_is_left_alone(manager, channel, approvers):
    """AC-53: a re-election must stay invisible -- no fresh notifications."""
    manager.adopt(SESSION, FakeThread(77, TITLE, set()))

    run(manager.ensure_thread(SESSION, TITLE))

    assert channel.threads == []


def test_the_ids_are_read_from_the_database_not_the_configuration(approvers):
    """One reader for one fact: the config was consumed at startup."""
    assert broker_autojoin.approver_ids() == [MARY_ID, WAYNE_ID]


# --------------------------------------------------------------------------- #
# The switch (SPEC §8a) -- default ON
# --------------------------------------------------------------------------- #


def test_the_switch_defaults_to_on(manager, channel, approvers):
    assert register_callbacks.autojoin_enabled() is True
    run(manager.ensure_thread(SESSION, TITLE))

    assert channel.threads[0].added


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_the_switch_turns_the_joining_off(
    manager, channel, approvers, monkeypatch, value
):
    """Many parallel sessions are exactly the case for switching this off."""
    monkeypatch.setenv(register_callbacks.AUTOJOIN_ENV_VAR, value)

    assert register_callbacks.autojoin_enabled() is False
    run(manager.ensure_thread(SESSION, TITLE))

    assert channel.threads[0].added == []


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_switch_takes_the_usual_truthy_words(monkeypatch, value):
    monkeypatch.setenv(register_callbacks.AUTOJOIN_ENV_VAR, value)

    assert register_callbacks.autojoin_enabled() is True


def test_the_switch_can_be_set_in_puppy_cfg(manager, channel, approvers, monkeypatch):
    """The env var is not the only surface -- §8a always offers both."""
    monkeypatch.setattr(
        register_callbacks,
        "_get_config_value",
        lambda key: "0" if key == register_callbacks.AUTOJOIN_CONFIG_KEY else None,
    )

    run(manager.ensure_thread(SESSION, TITLE))

    assert channel.threads[0].added == []


# --------------------------------------------------------------------------- #
# INV-C1 -- a failed join is a blemish, not a defect
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "refused, expected", [(WAYNE_ID, MARY_ID), (MARY_ID, WAYNE_ID)]
)
def test_one_refused_user_does_not_cost_the_others(
    manager, channel, approvers, refused, expected
):
    """Catching per CALL instead of per USER would be a silent regression.

    An ex-member, or somebody who never joined the guild, is the ordinary
    case -- and it must not decide whether everyone else gets notified.

    BOTH positions are exercised on purpose.  Refusing only the LAST approver
    proves nothing: a loop that aborts on the first error has already added
    everyone before it, so the assertion would hold for the broken version
    too.  This was caught by mutation, not by reading.
    """
    channel.refuse.add(int(refused))

    run(manager.ensure_thread(SESSION, TITLE))

    assert channel.threads[0].added == [int(expected)]


def test_the_thread_survives_a_total_join_failure(manager, channel, approvers):
    channel.refuse.update({int(WAYNE_ID), int(MARY_ID)})

    thread_id = run(manager.ensure_thread(SESSION, TITLE))

    assert thread_id == channel.threads[0].id
    assert manager.has_thread(SESSION)


def test_a_thread_without_add_user_is_survivable(manager, channel, approvers):
    """py-cord's forum/news objects need not carry the method we want."""

    async def create_thread(*, name, auto_archive_duration=None, **_kwargs):
        return _ThreadWithoutAddUser(4711, name)

    channel.create_thread = create_thread

    assert run(manager.ensure_thread(SESSION, TITLE)) == 4711


class _ThreadWithoutAddUser:
    def __init__(self, thread_id: int, name: str) -> None:
        self.id = thread_id
        self.name = name
        self.archived = False


def test_an_unreadable_database_is_survivable(manager, channel, monkeypatch):
    """Whom to add is a lookup, and a lookup can fail (INV-C1)."""

    def explode(_role):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(bindings, "principals_with_role", explode)

    assert run(manager.ensure_thread(SESSION, TITLE)) == channel.threads[0].id


def test_a_non_numeric_external_id_only_costs_its_own_user(manager, channel):
    """Discord ids are numeric; a hand-edited row must not take the rest out."""
    bindings.bind(constants.AUTHZ_CHANNEL, "not-a-snowflake", WAYNE)
    bindings.bind(constants.AUTHZ_CHANNEL, MARY_ID, MARY)
    bindings.grant(WAYNE, bindings.Role.APPROVER)
    bindings.grant(MARY, bindings.Role.APPROVER)

    run(manager.ensure_thread(SESSION, TITLE))

    assert channel.threads[0].added == [int(MARY_ID)]


# --------------------------------------------------------------------------- #
# Audible once, not once per thread
# --------------------------------------------------------------------------- #


def test_a_failed_join_is_audible(manager, channel, approvers, caplog):
    """A silent degradation is what cost 40 minutes of bisecting last time."""
    channel.refuse.add(int(WAYNE_ID))

    with caplog.at_level(logging.WARNING, logger="cp_discord.broker_autojoin"):
        run(manager.ensure_thread(SESSION, TITLE))

    assert [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and WAYNE_ID in record.getMessage()
    ], "a user who could not be added must be audible"


def test_the_warning_names_the_switch_that_turns_it_off(
    manager, channel, approvers, caplog
):
    """The way out belongs in the message, and it has to be the REAL key.

    A hint carrying a hand-typed copy of the key is a hint that goes stale at
    the first rename -- and stale exactly where somebody is following it.
    """
    channel.refuse.add(int(WAYNE_ID))

    with caplog.at_level(logging.WARNING, logger="cp_discord.broker_autojoin"):
        run(manager.ensure_thread(SESSION, TITLE))

    assert register_callbacks.AUTOJOIN_CONFIG_KEY in caplog.records[0].getMessage()


def test_the_warning_comes_once_per_session_not_once_per_thread(
    manager, channel, approvers, caplog
):
    """A missing permission is missing for EVERY thread; one word is enough."""
    channel.refuse.update({int(WAYNE_ID), int(MARY_ID)})

    with caplog.at_level(logging.WARNING, logger="cp_discord.broker_autojoin"):
        run(manager.ensure_thread("cp_discord:a", TITLE))
        run(manager.ensure_thread("cp_discord:b", TITLE))
        run(manager.ensure_thread("cp_discord:c", TITLE))

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and record.name == "cp_discord.broker_autojoin"
    ]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"


def test_a_restarted_bridge_may_say_it_again(manager, channel, approvers, caplog):
    """ "Once per session" has to mean the session that is running NOW.

    C1 coming up again is a new session by every other module's reckoning
    (``inbound.py:330`` resets on install for the same reason), and an
    operator who just restarted the bridge to see whether their permission
    fix took hold is owed the answer.
    """
    from cp_discord import broker_activation

    channel.refuse.add(int(WAYNE_ID))
    run(manager.ensure_thread("cp_discord:a", TITLE))

    # Teardown, not startup: ``uninstall`` needs no election, no lock file and
    # no threads, so the test exercises the real seam without booting a broker.
    broker_activation.uninstall()

    # ``caplog.records`` accumulates over the WHOLE test, so the first
    # warning is already in there -- only what comes AFTER the restart counts.
    before = len(caplog.records)
    with caplog.at_level(logging.WARNING, logger="cp_discord.broker_autojoin"):
        run(manager.ensure_thread("cp_discord:b", TITLE))

    assert [
        record
        for record in caplog.records[before:]
        if record.name == "cp_discord.broker_autojoin"
    ], "a bridge that came up again must be allowed to say it once more"


# --------------------------------------------------------------------------- #
# AC-83b -- the channel key is the SHARED constant
# --------------------------------------------------------------------------- #


def test_ac83b_the_autojoin_path_carries_no_second_discord_literal():
    import ast
    from pathlib import Path

    path = Path(broker_autojoin.__file__)
    source = path.read_text(encoding="utf-8")

    assert "AUTHZ_CHANNEL" in source
    assert [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and node.value == "discord"
    ] == [], "a second 'discord' literal is exactly what INV-C28 forbids"
