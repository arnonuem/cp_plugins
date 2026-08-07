"""C1's election and activation: WHETHER this session runs the broker.

Separate from :mod:`.broker_server` because it answers a different question.
The server answers *how a broker serves*; this answers *whether this session
runs one at all, and how it reaches Discord*.  Both halves of that question
are here -- :class:`BrokerSupervisor` (election, re-election, thread health)
and the plugin surface (:func:`install`, :func:`uninstall`, the login) --
because they share one lifecycle: whoever wins the election is exactly who
needs a Discord connection and who has to give both up again.

``install`` RETURNS IMMEDIATELY (INV-C1).  It starts two daemon threads (the
supervisor and the Discord client) and gets out of the way.  A session that
loses the election, or finds no Discord at all, is a session without Discord;
it is never a session that failed to start.

Flat file, no ``broker/`` package: ``deploy.ps1:234`` copies only top-level
``*.py``, so a sub-package would be silently left behind and the installed
plugin would be broken while the unit tests stayed green.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Optional, Sequence, Tuple

from . import broker_autojoin
from . import broker_election as election
from . import broker_steer, broker_threads
from .broker_server import (
    ELECTION_ATTEMPTS,
    ELECTION_RETRY_DELAY,
    SUPERVISION_INTERVAL,
    Broker,
    broker_is_reachable,
)

logger = logging.getLogger(__name__)


class BrokerSupervisor:
    """Decides whether this session runs the broker, and keeps deciding.

    Three questions, on a 30-second tick (§3.1a):

    1. *Is the broker reachable?*  If not, run the election again -- the tab
       that held it may simply be gone (AC-52).
    2. *If I hold it, is my broker THREAD still alive?*  A dead thread inside
       a live process keeps the OS lock, and everyone else then fails forever
       (INV-C22, AC-70).
    3. *If I won, did I inherit a token?*  Adopt it (AC-85a) -- minting a new
       one silently cuts off every established session.
    """

    def __init__(self, gateway_provider: Callable[[], Any], *, notices=()) -> None:
        self._gateway_provider = gateway_provider
        self._notices = tuple(notices)
        self._lock = election.SingleOwnerLock()
        self.broker: Optional[Broker] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- observation ----------------------------------------------------

    @property
    def is_broker(self) -> bool:
        return self.broker is not None

    def broker_thread_alive(self) -> bool:
        return self.broker is not None and self.broker.is_alive()

    def simulate_broker_thread_death(self) -> None:
        """Kill the serving thread, keeping the lock -- the INV-C22 state."""
        if self.broker is not None:
            self.broker.kill_thread_for_test()

    # -- election -------------------------------------------------------

    def try_elect(self) -> bool:
        """One attempt at becoming the broker.  Returns immediately.

        The token is read BEFORE the portfile is overwritten and while the
        lock is already held, so nothing can write in between (§3.1).
        """
        if self.broker is not None:
            return True
        if not self._lock.acquire():
            return False
        # Bound BEFORE the try so the except branch can reach a broker that was
        # already started: every step after ``start()`` can throw, and a local
        # assigned inside the try would be unbound exactly then.
        broker: Optional[Broker] = None
        try:
            token = election.adopt_or_mint_token()
            broker = Broker(
                self._gateway_provider(), token=token, notices=self._notices
            )
            broker.start()
            election.write_portfile(broker.address)
            # Adopt FIRST, then sweep.  The order is the invariant: adoption
            # is what keeps live sessions' threads (INV-C14, AC-53), and only
            # then may the leftovers be archived (AC-16).  Sweeping first
            # would be judging sessions we have not taken responsibility for.
            broker.adopt_registered_sessions()
            broker.sweep_dead_sessions()
            broker.announce_notices()
            self.broker = broker
            return True
        except Exception:
            # Louder than the lost ``acquire()`` above: THAT is a normal outcome
            # (another session serves), while a broker that started and then
            # fell over is the only path that can strand a listening socket.
            logger.warning("cp_discord: could not start the broker", exc_info=True)
            # Stop BEFORE releasing, never after.  Releasing first opens a
            # window in which a rival wins the lock while this socket is still
            # answering -- two brokers at once, which SS3.1 holds to be worse
            # than no broker at all.  ``self.broker`` was never assigned, so
            # nothing else could ever reach this one again.
            if broker is not None:
                broker.stop()
            self._lock.release()
            return False

    def run_election_round(self) -> bool:
        """§3.1 steps 1-4: try, back off, give up quietly.

        Giving up is a normal outcome, not an error: another session holds the
        broker, and this one simply registers with it.  All of this runs on a
        daemon thread, so the session's start-up time is untouched either way
        (INV-C1).
        """
        for attempt in range(ELECTION_ATTEMPTS):
            if not self._lock.held and broker_is_reachable(election.read_portfile()):
                # Somebody is genuinely serving; C2 will register with them.
                return False
            if self.try_elect():
                return True
            if attempt + 1 < ELECTION_ATTEMPTS:
                time.sleep(ELECTION_RETRY_DELAY)
        logger.info(
            "cp_discord: no broker could be elected; continuing without Discord"
        )
        return False

    def check_broker_health(self) -> None:
        """INV-C22: release the lock if our own broker thread died (AC-70).

        Without this the process keeps a lock the OS would have released on
        its death, and every other session fails the non-blocking acquire for
        as long as this process lives.
        """
        if self.broker is None or self.broker.is_alive():
            return
        logger.warning("cp_discord: the broker thread died; releasing the lock")
        self._release()

    def sweep(self) -> None:
        """One housekeeping pass, if we are the broker."""
        if self.broker is not None:
            self.broker.sweep_dead_sessions()

    # -- the supervision thread -----------------------------------------

    def start(self) -> None:
        """Run election and supervision on a daemon thread (INV-C1)."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="cp_discord-election", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        self.run_election_round()
        while not self._stop.wait(SUPERVISION_INTERVAL):
            try:
                self.check_broker_health()
                if self.broker is None:
                    self.run_election_round()
                else:
                    self.sweep()
            except Exception:
                logger.debug("cp_discord: supervision round failed", exc_info=True)

    def stop(self) -> None:
        """Stand down: stop supervising, stop serving, release the lock."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(2.0)
        self._release()

    def _release(self) -> None:
        broker, self.broker = self.broker, None
        if broker is not None:
            broker.stop()
        self._lock.release()


_supervisor: Optional[BrokerSupervisor] = None
_gateway: Optional[broker_threads.DiscordGateway] = None

#: W2's seam (AC-85c).  Re-exported by NAME rather than reimplemented: the
#: session's error path calls this after a token rejection, and a second
#: reader of the portfile would be a second place for the token rules to
#: drift apart.
refresh_token_from_portfile = election.refresh_token_from_portfile


def active_supervisor() -> Optional[BrokerSupervisor]:
    return _supervisor


def _active_broker() -> Optional[Broker]:
    """The broker this session runs, or ``None`` -- checking BOTH steps.

    Two ``Optional``s, and skipping either one raises inside the Discord event
    loop.  The state is genuinely reachable: the client's thread is a daemon
    whose ``run()`` never returns, so after :func:`uninstall` has set
    ``_supervisor`` to ``None`` the loop keeps delivering messages -- and the
    next one would hit ``AttributeError`` on ``None.broker``.  An unhandled
    error in that loop is INV-C1 broken by teardown.

    The second step is the ordinary case rather than the exotic one: a session
    that LOST the election has a supervisor and no broker.
    """
    supervisor = active_supervisor()
    if supervisor is None:
        return None
    return supervisor.broker


async def _on_message(message: Any, *, broker: Optional[Broker]) -> None:
    """One Discord message on its way into the session that owns its thread.

    MODULE-GLOBAL on purpose (§4.6a).  The obvious home is inside
    :func:`connect_gateway`'s ``run()``, but that closure is marked
    ``no cover`` and is reachable from outside only through
    ``inspect.getsource`` -- a handler locked in there could be asserted about
    only as a STRING, and "the source mentions ``to_thread``" is not a
    measurement of anything.  Out here it can be awaited with a fake broker
    and a fake message, which is what AC-B5 needs.

    Four ways a message ends here, all of them SILENTLY:

    * from a bot -- our own reports are posted by one, and answering them
      would turn every report into an instruction.  ``message.author.bot``
      filters foreign bots too, which is intended: no bot has business
      steering an agent;
    * without a broker -- no token and no port, so the message cannot even be
      evaluated.  A reaction would confirm the session to a possible stranger
      without any authorization having happened (INV-6);
    * from a thread that belongs to no session -- guessing "the first one"
      would steer a stranger's words into whichever session sorted first;
    * with an answer that says ``refused``, or with no answer at all -- see
      :func:`broker_steer.reaction_for`.

    The delivery goes OFF the loop.  ``push`` is a blocking socket round trip
    that retries for up to three seconds; on the gateway's loop that stalls
    every other session's posts, every gate widget and the connection's own
    heartbeat.  Same move as ``broker_gates.view_factory``.

    Never raises: this IS the event loop's callback, and a failed chat
    delivery must not become the session's problem (INV-C1).
    """
    try:
        if broker is None or getattr(message.author, "bot", False):
            return
        session_id = broker.registry.session_for_thread(
            getattr(message.channel, "id", None)
        )
        if session_id is None:
            return
        steer = await asyncio.to_thread(
            broker.deliver_steer,
            session_id,
            external_id=message.author.id,
            text=message.content,
            message_id=message.id,
        )
        reaction = broker_steer.reaction_for(steer)
        if reaction is not None:
            await message.add_reaction(reaction)
    except Exception:
        # No text and no sender: this may be an unauthorized message, and its
        # content must not reach a log (``inbound.py:201``).
        logger.debug("cp_discord: an inbound chat message failed", exc_info=True)


def active_gateway() -> Optional[broker_threads.DiscordGateway]:
    return _gateway


def connect_gateway(config: Any, gateway: broker_threads.DiscordGateway) -> None:
    """Log the bot in and hand its channel and event loop to *gateway*.

    Runs the client on its own thread: ``client.run`` never returns.  The
    gateway only becomes useful once ``on_ready`` fires, and until then it
    holds queued work rather than dropping it -- a session may well register
    before the bot has finished logging in.

    **The client is BUILT on that thread, not merely started there.**
    ``discord.Client.__init__`` pins ``self.loop`` at construction time
    (``discord/client.py:253`` -> ``_get_event_loop()``), and ``run()`` later
    drives exactly that object (``:852``).  This function is called from
    ``on_startup``, which the core awaits inside its OWN running loop, so
    constructing here handed the worker the MAIN loop: ``run()`` then tried to
    ``run_forever()`` a loop that was already running and, while cleaning up,
    to ``close()`` it.  Discord stayed dark for the whole session.

    Measured against py-cord 2.8.1: built inside a running loop the client
    adopts that loop; built on a thread that first calls ``set_event_loop`` it
    adopts the thread's own; built on a thread with no loop at all it raises.
    The ``set_event_loop`` below is therefore load-bearing.

    Failure is logged, never raised (INV-C1).
    """
    import asyncio
    import threading

    import discord

    # ``message_content`` IS privileged, and it is on because §6.0's
    # ``on_message`` now exists: without it that handler would receive an
    # empty ``message.content`` and the chat path would be silently dead.  It
    # is the ONLY addition -- ``members`` and ``presences``, the other two
    # privileged intents, stay off, and a test pins the whole object so a
    # future edit has to come past it rather than past a reviewer.
    intents = discord.Intents.default()
    intents.message_content = True

    # Suppression belongs to the CONNECTION, not to each call site.  py-cord
    # folds this into every outgoing message (``discord/abc.py:1623-1630``), so
    # a send that passes nothing still cannot ping -- reports quote agent
    # output verbatim, and a new send path must not be able to forget this.
    def run() -> None:  # pragma: no cover - needs a live bot
        # Give this thread its own loop BEFORE building the client, so the
        # client pins ours instead of the core's (see the docstring).
        asyncio.set_event_loop(asyncio.new_event_loop())

        try:
            client = discord.Client(
                intents=intents, allowed_mentions=discord.AllowedMentions.none()
            )

            @client.event
            async def on_message(message: Any) -> None:
                await _on_message(message, broker=_active_broker())

            @client.event
            async def on_ready() -> None:
                gateway.attach_loop(asyncio.get_running_loop())
                gateway.set_thread_resolver(client.get_channel)
                channel = client.get_channel(config.channel_id)
                if channel is None:
                    try:
                        channel = await client.fetch_channel(config.channel_id)
                    except Exception:
                        logger.warning(
                            "cp_discord: channel %s is not reachable",
                            config.channel_id,
                        )
                        return
                gateway.set_channel(channel)

            client.run(config.token)
        except Exception:
            # A rejected token surfaces here as LoginFailure -- the single most
            # likely reason the bridge stays dark, so it is named rather than
            # buried in a generic "connection ended".
            logger.warning(
                "cp_discord: the Discord connection ended -- the bridge stays "
                "offline for this session (check DISCORD_BOT_TOKEN)",
                exc_info=True,
            )

    threading.Thread(target=run, name="cp_discord-discord", daemon=True).start()


def activation_notices() -> Tuple[str, ...]:
    """The warnings C6 collected, for the channel (AC-60b, AC-71b).

    Read lazily and defensively: C1 is installed FIRST, so this has to work
    even while the module that produced them is still coming up.
    """
    try:
        from . import register_callbacks

        return tuple(register_callbacks.activation_warnings())
    except Exception:
        logger.debug("cp_discord: no activation warnings available", exc_info=True)
        return ()


def install(config: Any) -> None:
    """Bring C1 up and return at once (INV-C1)."""
    global _supervisor, _gateway

    if _supervisor is not None:
        uninstall()

    # "Warn once per session" is scoped to the bridge that is RUNNING, and a
    # fresh install IS a fresh session -- an operator who restarted to check
    # whether their permission fix took hold is owed the answer again.
    broker_autojoin.reset_state()

    gateway = broker_threads.DiscordGateway()
    supervisor = BrokerSupervisor(lambda: gateway, notices=activation_notices())
    _gateway = gateway
    _supervisor = supervisor

    try:
        connect_gateway(config, gateway)
    except Exception:
        # No Discord is a degraded session, not a broken one.  The broker
        # still comes up: it serves the local sessions either way, and the
        # gateway starts posting the moment a connection appears.
        logger.warning("cp_discord: could not connect to Discord", exc_info=True)

    supervisor.start()
    logger.debug("cp_discord: C1 broker installed")


def uninstall() -> None:
    """Take C1 down.  Never raises: teardown has to reach every layer."""
    global _supervisor, _gateway

    supervisor, _supervisor = _supervisor, None
    gateway, _gateway = _gateway, None
    if supervisor is not None:
        try:
            supervisor.stop()
        except Exception:
            logger.debug("cp_discord: stopping the supervisor failed", exc_info=True)
    if gateway is not None:
        try:
            gateway.close()
        except Exception:
            logger.debug("cp_discord: closing the gateway failed", exc_info=True)
    broker_autojoin.reset_state()


__all__: Sequence[str] = (
    "BrokerSupervisor",
    "_on_message",
    "activation_notices",
    "active_gateway",
    "active_supervisor",
    "connect_gateway",
    "install",
    "refresh_token_from_portfile",
    "uninstall",
)
