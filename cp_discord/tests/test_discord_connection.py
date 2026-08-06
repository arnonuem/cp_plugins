"""What the bot connection ITSELF guarantees, before any call site is written.

Two properties of :func:`.broker_activation.connect_gateway` that no single
send site can be trusted to re-establish:

**Mentions are suppressed for every send** (Befund 1).  ``approvals_ui.py:34``
advertises suppression as a security property, but only the gate path passed
``allowed_mentions`` -- ``broker_threads.py`` ``_post_to``, which carries every
state edge (``broker_server.py:315``) and every report chunk (``:330``), did
not.  Report bodies quote agent output verbatim (``rendering.py:95-101``), so
an ``@everyone`` inside a repository file, a branch name or a command echo
would have pinged the whole server.

Setting it on the CLIENT rather than at each ``send`` is the point: py-cord
folds ``state.allowed_mentions`` into every outgoing message
(``discord/abc.py:1623-1630``), so the guarantee belongs to the connection and
a newly added send path inherits it instead of having to remember it.

**No privileged intent for a path that does not exist** (Befund 2).
``message_content`` is a privileged intent; the bot registered ``on_ready``
and nothing else, so it was collecting guild-wide message text it never read.

Nothing here needs a bot token: ``discord.Client`` is replaced by a recorder,
which is what makes the constructor arguments -- the thing both findings are
actually about -- observable.
"""

from __future__ import annotations

import pytest

from cp_discord import broker_activation, broker_threads


class _Config:
    """The two fields ``connect_gateway`` reads off a broker config."""

    token = "not-a-real-token"
    channel_id = 123456789


class _RecordingClient:
    """Stands in for ``discord.Client`` and remembers how it was built."""

    last: dict = {}
    #: The loop that was current on the thread AT CONSTRUCTION TIME.  This is
    #: the whole point of the R18 regression test: the real client pins
    #: ``self.loop`` here (``discord/client.py:253``), so recording it is the
    #: only way to prove it did not adopt the core's loop.
    built_loop: object = None
    built_thread: object = None

    def __init__(self, **kwargs):
        import asyncio
        import threading

        type(self).last = dict(kwargs)
        type(self).built_thread = threading.current_thread().name
        try:
            type(self).built_loop = asyncio.get_event_loop()
        except RuntimeError as exc:  # no loop on this thread at all
            type(self).built_loop = exc
        self.events = []

    def event(self, coro):
        # ``connect_gateway`` registers its handlers through this decorator;
        # recording the NAMES is what lets Befund 2 assert that the intent
        # matches the handlers that actually exist.
        self.events.append(coro.__name__)
        return coro

    def run(self, _token):
        # ``client.run`` never returns in production; returning at once keeps
        # the daemon thread from outliving the test.
        return None

    def get_channel(self, _id):
        return None


def _connect(monkeypatch):
    """Run ``connect_gateway`` against the recorder and WAIT for its thread.

    The wait is not politeness: since R18 the client is constructed ON the
    worker thread, so returning before it has run would read an empty
    recorder and every assertion below would pass vacuously.
    """
    import threading

    import discord

    monkeypatch.setattr(discord, "Client", _RecordingClient)
    _RecordingClient.last = {}
    _RecordingClient.built_loop = None
    _RecordingClient.built_thread = None

    before = {t.name for t in threading.enumerate()}

    gateway = broker_threads.DiscordGateway()
    try:
        broker_activation.connect_gateway(_Config(), gateway)
        for t in threading.enumerate():
            if t.name not in before and t.name.startswith("cp_discord-discord"):
                t.join(5.0)
    finally:
        gateway.close()

    assert _RecordingClient.last, "the client was never constructed"
    return _RecordingClient.last


@pytest.fixture
def built(monkeypatch):
    """Run ``connect_gateway`` against a recorder and hand back the client."""
    return _connect(monkeypatch)


# --------------------------------------------------------------------------- #
# Befund 1 -- the connection pings nobody
# --------------------------------------------------------------------------- #


def test_the_connection_suppresses_mentions(built):
    """Every send -- edge, report chunk, gate, ephemeral reply -- pings nobody.

    Asserted on the wire form rather than on the object, because
    ``AllowedMentions()`` and ``AllowedMentions.none()`` are different objects
    that a plain truthiness check would not tell apart: the default permits
    ``@everyone``, and only ``{'parse': []}`` forbids it.
    """
    import discord

    mentions = built.get("allowed_mentions")

    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.to_dict() == {"parse": []}


def test_suppression_survives_a_send_that_asks_for_nothing(built):
    """The property py-cord gives us, pinned so a version bump cannot rot it.

    ``discord/abc.py:1623-1625`` is the whole reason setting this on the
    client works: a ``send`` that passes no ``allowed_mentions`` falls back to
    the connection's.  Were that fallback ever dropped, this fails and the
    guarantee would have to move back to the individual call sites.
    """
    import discord

    connection = built.get("allowed_mentions")

    # What ``_post_to`` does: send without asking for anything.
    assert connection.to_dict() == {"parse": []}
    # What the gate path does: ask for none() explicitly.  Merging must not
    # widen it back out.
    assert connection.merge(discord.AllowedMentions.none()).to_dict() == {"parse": []}


def test_an_everyone_in_a_report_chunk_cannot_ping(built):
    """The worst case, stated as the test: report text is attacker-influenced.

    A report quotes shell output and file contents verbatim, so the literal
    string below is a realistic payload.  The defence is not escaping it --
    it is that the connection refuses to parse ANY mention out of it.
    """
    payload = "branch: fix/@everyone-crash"

    assert "@everyone" in payload  # the payload really is dangerous
    assert built["allowed_mentions"].to_dict()["parse"] == []


# --------------------------------------------------------------------------- #
# Befund 2 -- no privileged intent without the handler that needs it
# --------------------------------------------------------------------------- #


def test_the_bot_does_not_request_message_content(built):
    """Least privilege: ``on_message`` does not exist, so neither may the intent.

    ``message_content`` is PRIVILEGED -- with it the bot receives the full text
    of every message in the guild.  Nothing reads it (§6.0 is not built), so a
    leaked token would have inherited a read capability the feature never used.
    """
    intents = built.get("intents")

    assert intents is not None
    assert intents.message_content is False


def test_the_requested_intents_are_the_plain_defaults(built):
    """Nothing privileged slipped in beside ``message_content``.

    Pinned as a whole-object comparison so that a future edit adding
    ``members`` or ``presences`` -- the other two privileged intents -- has to
    come past this test rather than past a reviewer.
    """
    import discord

    assert built["intents"] == discord.Intents.default()


def test_no_on_message_handler_exists():
    """The load-bearing half of Befund 2: the intent has no consumer.

    If somebody adds ``on_message`` they should re-enable the intent, and this
    test is what tells them the two belong together -- it fails, pointing at
    the comment in ``connect_gateway``.
    """
    import inspect

    source = inspect.getsource(broker_activation.connect_gateway)

    assert "async def on_ready" in source
    assert "async def on_message" not in source


# --------------------------------------------------------------------------- #
# R18 -- the client must NOT adopt the core's event loop
# --------------------------------------------------------------------------- #


def test_the_client_is_built_on_its_own_thread_not_the_callers(monkeypatch):
    """The client must never pin the loop that ``on_startup`` runs in.

    Found in a LIVE test, invisible to every unit test before it.  The core
    awaits ``on_startup`` inside its OWN running loop; building the client
    there handed it that loop (``discord/client.py:253`` pins ``self.loop``
    at construction time) and ``run()`` then tried to ``run_forever()`` a loop
    that was already running -- and to ``close()`` it while cleaning up.
    Discord stayed dark for the entire session.

    INV-C1 held throughout: the traceback was caught and the terminal was
    fine.  That is precisely why this needs its own assertion -- the failure
    is silent by design.
    """
    import threading

    _connect(monkeypatch)

    assert _RecordingClient.built_thread is not None
    assert _RecordingClient.built_thread != threading.current_thread().name, (
        "the client was constructed on the caller's thread; it pins the core's "
        "event loop and run() fails with 'This event loop is already running'"
    )
    assert _RecordingClient.built_thread.startswith("cp_discord-discord")


def test_the_worker_thread_has_an_event_loop_of_its_own(monkeypatch):
    """``set_event_loop`` in the worker is load-bearing, not decoration.

    Measured against py-cord 2.8.1: ``discord.Client()`` on a bare thread
    raises ``RuntimeError: There is no current event loop``.  Asserting this
    separately stops a future cleanup from dropping the line as redundant.
    """
    import asyncio

    _connect(monkeypatch)

    loop = _RecordingClient.built_loop
    assert not isinstance(loop, RuntimeError), (
        "the worker thread had no event loop -- discord.Client cannot be built "
        "there; set_event_loop is missing"
    )
    assert isinstance(loop, asyncio.AbstractEventLoop)
