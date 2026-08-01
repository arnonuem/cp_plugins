"""L2 transport — per-channel agents and the turn lifecycle.

Every Discord channel is an independent conversation: its own agent instance,
its own message history, its own cancellable run task.  The module is split in
two halves that can be tested apart:

* the **channel registry + turn lifecycle** (`handle_message`, `cancel_channel`),
  which is pure asyncio and knows nothing about py-cord, and
* the **py-cord client** (`run_gateway`), a thin adapter that translates
  ``on_message`` into an :class:`IncomingMessage` and hands it over.

That split is what lets AC-15/16/55 be tested without a Discord connection.

Three properties here were measured against the running system rather than
inferred, because each of them silently breaks the layer if assumed wrong:

``run_with_mcp`` does NOT write history back (Fakt A4, mirrored by ACP at
``plugins/acp/session.py:172-191``).  A caller that forgets
``set_message_history`` gets a channel that forgets every turn instantly.

A cancelled ``run_with_mcp`` **returns ``None`` instead of raising**:
``_run_with_mcp_impl`` catches ``asyncio.CancelledError``
(``agents/_runtime.py:1086-1092``) and falls through to an implicit ``return
None``.  Measured: the awaiting caller sees ``None`` and ``task.cancelled()``
is ``False``.  Cancellation is therefore tracked with an explicit per-channel
flag, and a ``None`` result is never written into history.

``_AGENT_CANCEL_CB`` is a one-slot global (Fakt D10, ``command_runner.py:134``)
that only the FIRST concurrent run occupies and that is cleared when that run
ends (measured).  It cannot express "cancel channel A", so each channel's task
is held here instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import concurrency
from .session_ids import SESSION_PREFIX, session_id_for

logger = logging.getLogger(__name__)

#: INV-1 lives in :mod:`.session_ids` -- one format, one parser.  Both names are
#: re-exported here because this is where the rest of the plugin looks for them.
_ = (SESSION_PREFIX, session_id_for)


class TurnStatus(enum.Enum):
    """How a turn ended.  Every path through ``handle_message`` yields one."""

    COMPLETED = "completed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class IncomingMessage:
    """A channel message, decoupled from py-cord's own types.

    Keeping this plain makes the lifecycle testable without a gateway
    connection and keeps the py-cord surface confined to :func:`run_gateway`.
    """

    channel_id: int
    author_id: int
    content: str


@dataclass(frozen=True)
class TurnOutcome:
    """The result of one turn, for the caller and for L5's output routing."""

    status: TurnStatus
    session_id: str
    principal: Optional[str] = None
    result: Any = None
    detail: Optional[str] = None


@dataclass
class ChannelAgent:
    """One channel's agent plus the state needed to run and cancel its turns."""

    channel_id: int
    session_id: str
    agent: Any
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: Optional[asyncio.Task] = None
    cancel_requested: bool = False


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_CHANNELS: Dict[int, ChannelAgent] = {}

#: The live py-cord client and the loop it runs on.  L4 needs the loop to hand
#: a gate from an executor thread back onto the gateway (and to detect that it
#: is ALREADY on that loop, which would deadlock); L5 needs the client to reach
#: a channel.  Both are set once at connect and cleared at teardown.
_CLIENT: Any = None
_LOOP: Optional[asyncio.AbstractEventLoop] = None

#: Set by L3 (W3).  ``None`` means "no authorizer installed", which is a
#: DENIAL, not a bypass — INV-3 is fail-closed and this is the network edge.
_AUTHORIZER: Optional[Callable[[IncomingMessage], Optional[str]]] = None

#: Set by L5 (W5) to receive turn results.  Absent, turns still run.
_OUTCOME_SINK: Optional[Callable[[TurnOutcome], Any]] = None


def set_connection(client: Any, loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Register (or clear, with ``None``) the live client and its event loop."""
    global _CLIENT, _LOOP
    _CLIENT = client
    _LOOP = loop


def get_client() -> Any:
    """The py-cord client, or ``None`` when the gateway is not connected."""
    return _CLIENT


def allowed_mentions() -> Any:
    """An ``AllowedMentions`` that pings nobody.  Used on EVERY send.

    py-cord leaves ``Client.allowed_mentions`` at ``None``, which means
    Discord's own permissive default applies.  This plugin forwards agent
    output, shell stdout, file diffs and approval previews verbatim, so an
    ``@everyone`` sitting in a repo file would otherwise notify the whole
    server the moment the agent echoes it.

    Set on the client AND passed per message: the client default is
    process-wide state another frontend could change, and the messages that
    quote attacker-influenced text must not depend on it.  Lives here because
    this module owns the py-cord surface for both L4 and L5.
    """
    import discord

    return discord.AllowedMentions.none()


def get_loop() -> Optional[asyncio.AbstractEventLoop]:
    """The gateway's event loop, or ``None`` when not connected.

    L4's approval backend runs in an executor thread and bounces onto this loop
    via ``run_coroutine_threadsafe``.  It must also compare this against the
    running loop: if they are the same, blocking on the future would deadlock
    the gateway, so the gate is refused instead (SPEC-L4 §4.2).
    """
    return _LOOP


def set_authorizer(
    authorizer: Optional[Callable[[IncomingMessage], Optional[str]]],
) -> None:
    """Install L3's rule check (INV-4: it runs before any model contact).

    The callable returns the resolved principal for an allowed sender, or
    ``None`` to discard the message (L3/R2 — an unknown sender's text must
    never reach the model, it is prompt-injection surface).
    """
    global _AUTHORIZER
    _AUTHORIZER = authorizer


def set_outcome_sink(sink: Optional[Callable[[TurnOutcome], Any]]) -> None:
    """Install L5's consumer for finished turns (may be a coroutine function)."""
    global _OUTCOME_SINK
    _OUTCOME_SINK = sink


def get_channel_agent(channel_id: int) -> Optional[ChannelAgent]:
    """The channel's agent record, or ``None`` if it has never run a turn."""
    return _CHANNELS.get(channel_id)


def active_channels() -> List[int]:
    """Channels with a turn in flight right now."""
    return [
        cid
        for cid, channel in _CHANNELS.items()
        if channel.task is not None and not channel.task.done()
    ]


def reset_state() -> None:
    """Drop all channel state and hooks.  Used by shutdown and by tests.

    L3's session ownership and L4's sub-agent claims are released alongside our
    own: a principal left behind would let a later run in that channel inherit
    an owner nobody authorized, and any gate still open for it would outlive
    its run.  Fail-closed means forgetting, not remembering.
    """
    from . import approvals, authz

    for channel in list(_CHANNELS.values()):
        if channel.task is not None and not channel.task.done():
            channel.task.cancel()
        concurrency.release_session(channel.session_id)
        authz.release_session(channel.session_id)
        approvals.release_session(channel.session_id)
    _CHANNELS.clear()
    set_authorizer(None)
    set_outcome_sink(None)
    set_connection(None, None)


def _new_agent() -> Any:
    """Build a fresh agent instance for one channel.

    ``load_agent`` (not ``get_current_agent``) — the singleton ``_CURRENT_AGENT``
    is shared process-wide (Fakt D6) and would make every channel the same
    conversation.  Same reasoning as ACP's ``plugins/acp/agent.py:406-410``.
    """
    from code_puppy.agents.agent_manager import get_current_agent_name, load_agent

    return load_agent(get_current_agent_name())


def _channel_for(channel_id: int) -> ChannelAgent:
    """Return the channel's record, creating its agent on first use."""
    channel = _CHANNELS.get(channel_id)
    if channel is None:
        channel = ChannelAgent(
            channel_id=channel_id,
            session_id=session_id_for(channel_id),
            agent=_new_agent(),
        )
        _CHANNELS[channel_id] = channel
    return channel


def _authorize(message: IncomingMessage) -> Optional[str]:
    """Resolve the sender to a principal, or ``None`` to discard the message.

    Any failure is a denial (INV-3): an authorizer that cannot answer must not
    become an open door.
    """
    authorizer = _AUTHORIZER
    if authorizer is None:
        logger.warning(
            "Discord: no authorizer installed; discarding message from %s",
            message.author_id,
        )
        return None
    try:
        return authorizer(message)
    except Exception:
        logger.exception("Discord: authorizer failed; discarding message")
        return None


def authz_authorizer(message: IncomingMessage) -> Optional[str]:
    """The production authorizer: L3's R2 rule, and nothing else.

    Answers exactly one question — *may this sender's text be processed at
    all?* — and returns the principal behind it.  Unknown senders are
    discarded here, before the text can reach the model (INV-4).

    It deliberately does NOT take ownership of the channel.  Authorization
    happens before the channel lock (it must: an unauthorized message may not
    even queue behind a running turn), and binding ownership out there is what
    let a second talker rebind a channel mid-run — stamping their name on every
    gate the first talker's still-running turn opened afterwards.  Ownership is
    taken inside the lock instead, in :func:`_run_turn`.

    Installed by boot; tests substitute their own authorizer.
    """
    from . import authz

    decision = authz.check_message(SESSION_PREFIX, str(message.author_id))
    if not decision.allowed or decision.principal is None:
        logger.info(
            "Discord: refused message from %s:%s (%s)",
            SESSION_PREFIX,
            message.author_id,
            decision.reason.value if decision.reason else "unknown",
        )
        return None
    return decision.principal


async def _emit_outcome(outcome: TurnOutcome) -> None:
    """Hand the outcome to L5.  A broken sink must never fail the turn."""
    sink = _OUTCOME_SINK
    if sink is None:
        return
    try:
        result = sink(outcome)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.exception("Discord: outcome sink failed")


def _absorb_history(channel: ChannelAgent, result: Any) -> None:
    """Fold the finished run's messages back into the channel's agent.

    ``run_with_mcp`` does not do this itself (Fakt A4); without it the channel
    starts every turn from an empty history.  A ``None`` result (the cancelled
    path) is deliberately ignored — overwriting the history there would erase
    the conversation.
    """
    if result is None:
        return
    try:
        messages = result.all_messages()
    except Exception:
        logger.debug("Discord: could not read run messages", exc_info=True)
        return
    if messages:
        channel.agent.set_message_history(list(messages))


async def handle_message(message: IncomingMessage) -> TurnOutcome:
    """Run one turn for one channel.

    The order below is binding, per SPEC-L2 §2.4 and INV-4:

    1. authorize — completely, before any model/history/agent structure is
       touched;
    2. take the channel lock, and only THEN claim ownership of the session:
       binding the principal outside the lock let a second talker rebind a
       channel whose turn was still running (R1);
    3. enter ``session_scope`` so L1 can attribute output, gates and locks;
    4. start the run as a task **we hold**, so this channel alone can be
       cancelled;
    5. write the history back;
    6. release L1's, L3's and L4's session state together.
    """
    principal = _authorize(message)
    if principal is None:
        outcome = TurnOutcome(
            status=TurnStatus.DENIED,
            session_id=session_id_for(message.channel_id),
            detail="sender is not authorized",
        )
        await _emit_outcome(outcome)
        return outcome

    channel = _channel_for(message.channel_id)
    # Serialise turns WITHIN a channel (one conversation, one turn at a time);
    # different channels never contend for this lock.
    async with channel.lock:
        outcome = await _run_turn(channel, message, principal)
    await _emit_outcome(outcome)
    return outcome


async def _run_turn(
    channel: ChannelAgent, message: IncomingMessage, principal: str
) -> TurnOutcome:
    """Execute one authorized turn inside the channel's session scope.

    Ownership is claimed here, under the channel lock, and frozen for the whole
    turn by ``authz.session_turn``: a gate reads its requester from that map
    when it opens, so an owner who can change mid-run means gates that name the
    wrong person (R1).  The claim is dropped again in ``finally`` — a stale
    principal would let the channel's NEXT run inherit an owner nobody
    authorized.
    """
    from . import authz

    channel.cancel_requested = False
    result: Any = None
    detail: Optional[str] = None
    status = TurnStatus.COMPLETED

    try:
        authz.bind_session_principal(channel.session_id, principal)
    except authz.AuthzError as error:
        # Only reachable if a run outlives its lock; refusing is fail-closed.
        logger.warning("Discord: %s", error)
        return TurnOutcome(
            status=TurnStatus.DENIED,
            session_id=channel.session_id,
            principal=principal,
            detail=str(error),
        )

    try:
        with (
            authz.session_turn(channel.session_id),
            concurrency.session_scope(channel.session_id),
        ):
            channel.task = asyncio.ensure_future(
                channel.agent.run_with_mcp(message.content)
            )
            try:
                result = await channel.task
            except asyncio.CancelledError:
                # Either our own cancel_channel, or this coroutine itself was
                # cancelled.  Only the former is a normal per-channel cancel;
                # the latter must propagate so we don't outlive our caller.
                if not channel.cancel_requested:
                    raise
                status = TurnStatus.CANCELLED
            else:
                if channel.cancel_requested or result is None:
                    # A cancelled run returns None rather than raising
                    # (measured); a None result is never a usable turn.
                    status = (
                        TurnStatus.CANCELLED
                        if channel.cancel_requested
                        else TurnStatus.FAILED
                    )
                    detail = (
                        None if channel.cancel_requested else "run produced no result"
                    )
                    result = None
                else:
                    _absorb_history(channel, result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Discord: agent run failed for %s", channel.session_id)
        status = TurnStatus.FAILED
        detail = str(exc)
    finally:
        channel.task = None
        channel.cancel_requested = False
        concurrency.release_session(channel.session_id)
        # L3's ownership belongs to THIS turn (R1/S9), and so do L4's
        # child-session claims: kept beyond it, a child id another channel
        # later reuses would look ambiguous forever.
        authz.release_session(channel.session_id)
        from . import approvals

        approvals.release_session(channel.session_id)

    return TurnOutcome(
        status=status,
        session_id=channel.session_id,
        principal=principal,
        result=result,
        detail=detail,
    )


def cancel_channel(channel_id: int) -> bool:
    """Cancel the in-flight run of ONE channel.  Returns False when idle.

    Deliberately does not touch ``_AGENT_CANCEL_CB`` or
    ``kill_all_running_shell_processes``: both are process-wide and would reach
    into other channels' runs (AC-55).
    """
    channel = _CHANNELS.get(channel_id)
    if channel is None:
        return False
    task = channel.task
    if task is None or task.done():
        return False
    channel.cancel_requested = True
    task.cancel()
    return True


# ---------------------------------------------------------------------------
# py-cord adapter
# ---------------------------------------------------------------------------
async def run_gateway(discord_module: Any, token: str) -> int:
    """Connect to Discord and serve channel messages until disconnected.

    *discord_module* is the py-cord module, already identity-checked by
    :func:`register_callbacks.load_pycord` — this function never imports it, so
    the transport stays testable and the import guard stays in one place.
    """
    intents = discord_module.Intents.default()
    # Without message_content the bot receives empty message bodies; that is a
    # privileged intent and must also be enabled in the Discord developer
    # portal, otherwise login fails loudly rather than silently going deaf.
    intents.message_content = True
    client = discord_module.Client(
        intents=intents,
        # py-cord defaults this to None, i.e. Discord's permissive behaviour.
        # Everything this bot posts is text it did not author.
        allowed_mentions=discord_module.AllowedMentions.none(),
    )
    tasks: set[asyncio.Task] = set()
    # Publish the connection BEFORE serving: L4's approval backend and L5's
    # output routing both resolve it lazily, and a gate can be raised by the
    # very first message we handle.
    set_connection(client, asyncio.get_running_loop())

    @client.event
    async def on_message(message: Any) -> None:  # pragma: no cover - needs a socket
        if message.author.id == client.user.id:
            return
        incoming = IncomingMessage(
            channel_id=message.channel.id,
            author_id=message.author.id,
            content=message.content or "",
        )
        # Fire and hold: a slow turn must not block the gateway's event loop,
        # and a dropped reference would let Python garbage-collect the task.
        task = asyncio.ensure_future(handle_message(incoming))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        await client.start(token)
    except discord_module.LoginFailure:
        logger.error("Discord: login failed — check the bot token")
        return 1
    finally:
        for task in list(tasks):
            task.cancel()
        for task in list(tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if not client.is_closed():
            await client.close()
        set_connection(None, None)
    return 0
