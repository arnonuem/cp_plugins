"""L5 output — route everything a run produces into the right channel.

Four independent sources feed one router (SPEC-L5 §5.1):

============================  ==========================================
source                        attribution
============================  ==========================================
``stream_event``              L1 ContextVar, else the callback's own id
``pre``/``post_tool_call``    L1 ContextVar
MessageBus messages           ``message.session_id`` (L1 patch D/E/F/G)
legacy ``emit_info`` queue    none at all -> system channel (B7)
============================  ==========================================

**The system channel is not optional** (SPEC-L5 §5.3): a message whose session
cannot be resolved -- a zombie reader thread emitting after its command was
abandoned, a legacy queue entry, another frontend's session id -- lands there.
Nothing is ever discarded silently; when even the system channel is
unreachable the text is recorded in :func:`undelivered` instead.

Three Discord facts shape the rest:

* a message body is capped at 2000 characters, so everything goes through
  :mod:`.chunking`, which also keeps code fences intact;
* editing a message once per token would be rate-limited into oblivion, so
  writes are coalesced to one per :data:`EDIT_INTERVAL_S`.  A state that is
  already superseded when the window opens is **dropped**, not sent late --
  only the newest text is ever written;
* Discord fails.  Every send and edit is contained: a dead channel costs its
  own output and nothing else, and never reaches the agent run (AC-32).

Two attribution limits are structural and deliberate, not oversights:

* ``SubAgentInvocationMessage.session_id`` is a *required* field carrying the
  SUB-AGENT's id (``messaging/messages.py:268-272``,
  ``tools/subagent_invocation.py:298-306``).  L1 patch D only fills a session
  id that is ``None`` (``plugins/discord/concurrency.py:_wrap_bus_emit``), so
  it can never re-tag this message and the bus copy goes to the system
  channel.  The channel still sees the invocation: it arrives through the
  ``pre_tool_call`` lane, which fires while the parent's session is still
  current.
* a sub-agent's own stream events carry the sub-agent session id for the same
  reason, and sub-agents emit no shell output at all
  (``silent=running_as_subagent``, ``command_runner.py:1304``).  Their
  internal chatter therefore lands in the system channel, by design.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

from code_puppy.callbacks import register_callback, unregister_callback

from . import chunking, concurrency, gateway, rendering
from .session_ids import channel_id_of

logger = logging.getLogger(__name__)

#: Coalescing window for writes to one channel.  Spec: ~1-2 s.
EDIT_INTERVAL_S = 1.5

#: How often the background pump drains the bus and flushes due channels.
PUMP_INTERVAL_S = 0.25

#: Where the system channel id comes from.
SYSTEM_CHANNEL_ENV_VAR = "DISCORD_SYSTEM_CHANNEL_ID"
SYSTEM_CHANNEL_CONFIG_KEY = "discord_system_channel_id"

#: Key of the catch-all buffer; not a session id, so it can never collide.
_SYSTEM_KEY = "\x00system"

#: Bounded record of text we could not deliver anywhere.
_UNDELIVERED_LIMIT = 200

_STREAM = "stream"
_BLOCK = "block"


# ---------------------------------------------------------------------------
# Per-channel buffering
# ---------------------------------------------------------------------------
@dataclass
class _Part:
    """One Discord message (or run of messages) we are still filling.

    A part is *live* while more text of the same kind may be appended to it.
    Once a part of a different kind starts, or the turn ends, it is sealed and
    never edited again -- which is what keeps stream text and discrete blocks
    in the order they happened.
    """

    kind: str
    text: str
    live: bool = True
    messages: List[Any] = field(default_factory=list)
    written: List[str] = field(default_factory=list)
    failed: bool = False

    def done(self) -> bool:
        """True when nothing more can or should be written for this part."""
        if self.failed:
            return True
        return not self.live and len(self.written) == len(
            chunking.chunk_message(self.text)
        )


class _ChannelBuffer:
    """Everything queued for one Discord channel, plus its write throttle."""

    def __init__(self, key: str, channel_id: Optional[int], now: float) -> None:
        self.key = key
        self.channel_id = channel_id
        self.parts: List[_Part] = []
        self.last_write = now
        self.streamed = False
        #: Held for the WHOLE of one flush.  A part only leaves ``parts``
        #: after its Discord write has been awaited, so a second flush
        #: entering during that await would see the same part again and post
        #: it twice.  Created lazily: buffers are built from the legacy
        #: queue's thread too, where there is no running loop to bind to.
        self._flush_lock: Optional[asyncio.Lock] = None

    def flush_lock(self) -> asyncio.Lock:
        """This buffer's flush lock, created on the loop that first flushes."""
        if self._flush_lock is None:
            self._flush_lock = asyncio.Lock()
        return self._flush_lock

    def append(self, kind: str, text: str) -> None:
        """Add *text*, starting a new message when the kind changes."""
        tail = self.parts[-1] if self.parts else None
        if tail is not None and tail.live and tail.kind == kind:
            separator = "" if kind == _STREAM else "\n"
            tail.text += separator + text
            return
        if tail is not None:
            tail.live = False
        self.parts.append(_Part(kind=kind, text=text))

    def seal(self) -> None:
        """Close the trailing part so later text starts a fresh message."""
        if self.parts:
            self.parts[-1].live = False

    def pending(self) -> bool:
        return any(not part.done() for part in self.parts)


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_BUFFERS: Dict[str, _ChannelBuffer] = {}
_UNDELIVERED: Deque[str] = deque(maxlen=_UNDELIVERED_LIMIT)
_GUARD = threading.Lock()  # the legacy queue calls us from its own thread

_INSTALLED = False
_SYSTEM_CHANNEL_ID: Optional[int] = None
_CLOCK: Callable[[], float] = time.monotonic
_TASKS: List[asyncio.Task] = []


def is_installed() -> bool:
    return _INSTALLED


def undelivered() -> List[str]:
    """Text that reached no channel at all -- the last resort of INV-6."""
    with _GUARD:
        return list(_UNDELIVERED)


def _record_undelivered(text: str) -> None:
    with _GUARD:
        _UNDELIVERED.append(text)
    logger.warning("Discord: could not deliver output: %s", text[:200])


# ---------------------------------------------------------------------------
# Session -> channel resolution
# ---------------------------------------------------------------------------
#: The Discord channel behind an INV-1 session id, or ``None``.
#:
#: Anything that is not ``discord:<ascii int>`` belongs to somebody else (an
#: ACP session, a sub-agent run) and must not be guessed into a channel.  This
#: is deliberately the SAME callable L4 routes gates with: when the two halves
#: parse differently, text reaches a channel whose gates cannot (P6).
_channel_id_for = channel_id_of


def _buffer_for(session_id: Optional[str]) -> _ChannelBuffer:
    """The buffer for *session_id*, falling back to the system channel."""
    channel_id = _channel_id_for(session_id)
    key = session_id if channel_id is not None else _SYSTEM_KEY
    with _GUARD:
        buffer = _BUFFERS.get(key)
        if buffer is None:
            buffer = _ChannelBuffer(
                key=key,
                channel_id=channel_id if channel_id is not None else _SYSTEM_CHANNEL_ID,
                now=_CLOCK(),
            )
            _BUFFERS[key] = buffer
        return buffer


def _emit(session_id: Optional[str], kind: str, text: str) -> None:
    """Queue *text* for the channel behind *session_id*.  Never raises."""
    if not text:
        return
    try:
        buffer = _buffer_for(session_id)
        with _GUARD:
            buffer.append(kind, text)
            if kind == _STREAM:
                buffer.streamed = True
    except Exception:
        logger.exception("Discord: failed to queue output")


# ---------------------------------------------------------------------------
# Flushing
# ---------------------------------------------------------------------------
def _resolve_channel(channel_id: Optional[int]) -> Any:
    if channel_id is None:
        return None
    client = gateway.get_client()
    if client is None:
        return None
    try:
        return client.get_channel(channel_id)
    except Exception:
        logger.debug("Discord: get_channel failed", exc_info=True)
        return None


async def _write_part(channel: Any, part: _Part) -> None:
    """Send new chunks and edit only the one that actually changed."""
    for index, chunk in enumerate(chunking.chunk_message(part.text)):
        if index < len(part.messages):
            if part.written[index] == chunk:
                continue
            await part.messages[index].edit(content=chunk)
            part.written[index] = chunk
        else:
            # Everything routed here is text the bot did not author -- agent
            # output, shell stdout, file diffs.  An @everyone inside it must
            # not ping the server.
            message = await channel.send(
                chunk, allowed_mentions=gateway.allowed_mentions()
            )
            part.messages.append(message)
            part.written.append(chunk)


async def _flush_buffer(buffer: _ChannelBuffer) -> None:
    """Write one channel's outstanding parts.  Never runs twice at once.

    The lock spans the whole flush, not just the bookkeeping: a part leaves
    ``buffer.parts`` only once its write has been awaited, so a concurrent
    flush entering mid-await would find it still queued and send it a second
    time (P1, reproduced against the real wiring).
    """
    async with buffer.flush_lock():
        channel = _resolve_channel(buffer.channel_id)
        with _GUARD:
            parts = [part for part in buffer.parts if not part.done()]
        for part in parts:
            if channel is None:
                _record_undelivered(part.text)
                part.failed = True
                continue
            try:
                await _write_part(channel, part)
            except Exception:
                # AC-32: a channel that refuses writes costs its own output and
                # nothing else.  Retrying forever would pin the buffer open.
                logger.exception(
                    "Discord: write to channel %s failed", buffer.channel_id
                )
                _record_undelivered(part.text)
                part.failed = True
        with _GUARD:
            buffer.parts = [part for part in buffer.parts if not part.done()]


async def flush_due(force: bool = False) -> None:
    """Write every channel whose throttle window has opened.

    Called by the pump and, with ``force``, whenever a turn ends -- the last
    words of a run must not wait for a timer.
    """
    now = _CLOCK()
    with _GUARD:
        buffers = list(_BUFFERS.values())
    for buffer in buffers:
        if not buffer.pending():
            continue
        if not force and now - buffer.last_write < EDIT_INTERVAL_S:
            continue
        buffer.last_write = now
        try:
            await _flush_buffer(buffer)
        except Exception:  # pragma: no cover - _flush_buffer contains its own
            logger.exception("Discord: flush failed for %s", buffer.key)


# ---------------------------------------------------------------------------
# Source 1 — stream events
# ---------------------------------------------------------------------------
def _active_session(agent_session_id: Optional[str] = None) -> Optional[str]:
    """The session this callback belongs to.

    The L1 ContextVar wins: it is per task, whereas *agent_session_id* comes
    from the process-wide bus context a concurrent channel can overwrite.
    """
    try:
        current = concurrency.current_session_id()
    except Exception:
        current = None
    return current or agent_session_id


async def on_stream_event(
    event_type: str, event_data: Any, agent_session_id: Optional[str] = None
) -> None:
    try:
        text = rendering.stream_text(event_type, event_data)
        if text:
            _emit(_active_session(agent_session_id), _STREAM, text)
    except Exception:
        logger.exception("Discord: stream event routing failed")


# ---------------------------------------------------------------------------
# Source 2 — tool activity
# ---------------------------------------------------------------------------
async def on_pre_tool_call(tool_name: str, tool_args: Any, context: Any = None) -> None:
    """Announce a starting tool call.  Returns ``None``: we never block."""
    try:
        _emit(
            _active_session(), _BLOCK, rendering.tool_start_text(tool_name, tool_args)
        )
    except Exception:
        logger.exception("Discord: pre_tool_call routing failed")
    return None


async def on_post_tool_call(
    tool_name: str,
    tool_args: Any,
    result: Any,
    duration_ms: float,
    context: Any = None,
) -> None:
    try:
        _emit(
            _active_session(), _BLOCK, rendering.tool_end_text(tool_name, duration_ms)
        )
    except Exception:
        logger.exception("Discord: post_tool_call routing failed")


# ---------------------------------------------------------------------------
# Source 3 — MessageBus
# ---------------------------------------------------------------------------
def route_bus_message(message: Any) -> None:
    """Queue one structured message.  Thread-safe; never raises."""
    if not _INSTALLED:
        return
    try:
        text = rendering.bus_message_text(message)
        if text:
            _emit(getattr(message, "session_id", None), _BLOCK, text)
    except Exception:
        logger.exception("Discord: bus message routing failed")


async def drain_bus() -> None:
    """Move everything queued on the global bus into the channel buffers."""
    from code_puppy.messaging.bus import get_message_bus

    bus = get_message_bus()
    for message in bus.get_buffered_messages():
        route_bus_message(message)
    bus.clear_buffer()
    while True:
        message = bus.get_message_nowait()
        if message is None:
            return
        route_bus_message(message)


# ---------------------------------------------------------------------------
# Source 4 — the legacy queue (no session field at all, B7)
# ---------------------------------------------------------------------------
def route_legacy_message(message: Any) -> None:
    """Queue one legacy ``emit_info``/``emit_warning`` entry.

    Called from the message queue's own thread, so it only touches the
    lock-protected buffers -- no asyncio, no Discord I/O.
    """
    if not _INSTALLED:
        return
    try:
        _emit(None, _BLOCK, rendering.legacy_message_text(message))
    except Exception:
        logger.exception("Discord: legacy message routing failed")


# ---------------------------------------------------------------------------
# Turn outcomes — the streaming-independent fallback (AC-33)
# ---------------------------------------------------------------------------
async def on_outcome(outcome: gateway.TurnOutcome) -> None:
    """Close the turn out: post whatever the channel has not seen yet.

    ``use_streaming`` may be off, in which case not a single delta fired and
    the final result is the ONLY thing the channel can be told (AC-33).  A
    denial is deliberately not echoed into the channel -- an unauthorized
    sender gets no feedback (L3/R2) -- but it is audited in the system channel.
    """
    try:
        if outcome.status is gateway.TurnStatus.DENIED:
            _emit(None, _BLOCK, f"denied ({outcome.session_id}): {outcome.detail}")
        else:
            buffer = _buffer_for(outcome.session_id)
            streamed = buffer.streamed
            with _GUARD:
                buffer.streamed = False
                buffer.seal()
            _emit(outcome.session_id, _BLOCK, _outcome_text(outcome, streamed))
        await flush_due(force=True)
    except Exception:
        logger.exception("Discord: outcome routing failed")


def _outcome_text(outcome: gateway.TurnOutcome, streamed: bool) -> str:
    if outcome.status is gateway.TurnStatus.COMPLETED:
        return "" if streamed else rendering.result_text(outcome.result)
    if outcome.status is gateway.TurnStatus.CANCELLED:
        return "[cancelled] the run was cancelled."
    detail = f": {outcome.detail}" if outcome.detail else "."
    return f"[failed] the run failed{detail}"


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------
_CALLBACKS = (
    ("stream_event", on_stream_event),
    ("pre_tool_call", on_pre_tool_call),
    ("post_tool_call", on_post_tool_call),
)


def resolve_system_channel_id() -> Optional[int]:
    """The configured system channel, from the environment or puppy.cfg."""
    raw = os.environ.get(SYSTEM_CHANNEL_ENV_VAR)
    if not raw:
        try:
            from code_puppy.config import get_value

            raw = get_value(SYSTEM_CHANNEL_CONFIG_KEY)
        except Exception:
            logger.debug("Discord: could not read the system channel", exc_info=True)
            return None
    try:
        return int(str(raw).strip()) if raw else None
    except ValueError:
        logger.warning(
            "Discord: %s is not a channel id: %r", SYSTEM_CHANNEL_ENV_VAR, raw
        )
        return None


async def _pump() -> None:
    """Drain the bus and flush due channels until cancelled."""
    while True:
        try:
            await drain_bus()
            await flush_due()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defence for a long-lived task
            logger.exception("Discord: output pump iteration failed")
        await asyncio.sleep(PUMP_INTERVAL_S)


def install(
    system_channel_id: Optional[int] = None,
    clock: Optional[Callable[[], float]] = None,
    start_tasks: bool = True,
) -> None:
    """Wire all four sources into the router.  Idempotent."""
    global _INSTALLED, _SYSTEM_CHANNEL_ID, _CLOCK

    if _INSTALLED:
        return

    from code_puppy.messaging.bus import get_message_bus
    from code_puppy.messaging.message_queue import get_global_queue

    _SYSTEM_CHANNEL_ID = system_channel_id
    _CLOCK = clock or time.monotonic
    _INSTALLED = True

    for phase, handler in _CALLBACKS:
        register_callback(phase, handler)

    # Without an active renderer the bus parks everything in its startup
    # buffer; we are the renderer now, so messages flow into the queue we
    # drain.  The legacy queue has no session field, hence a plain listener.
    get_message_bus().mark_renderer_active()
    get_global_queue().add_listener(route_legacy_message)

    if start_tasks:
        _TASKS.append(asyncio.ensure_future(_pump()))


def uninstall() -> None:
    """Undo :func:`install` completely.  Idempotent."""
    global _INSTALLED, _SYSTEM_CHANNEL_ID

    for task in _TASKS:
        task.cancel()
    _TASKS.clear()

    for phase, handler in _CALLBACKS:
        try:
            unregister_callback(phase, handler)
        except Exception:
            logger.debug("Discord: unregister failed for %s", phase, exc_info=True)

    try:
        from code_puppy.messaging.bus import get_message_bus
        from code_puppy.messaging.message_queue import get_global_queue

        get_global_queue().remove_listener(route_legacy_message)
        get_message_bus().mark_renderer_inactive()
    except Exception:
        logger.debug("Discord: messaging teardown failed", exc_info=True)

    gateway.set_outcome_sink(None)

    with _GUARD:
        _BUFFERS.clear()
        _UNDELIVERED.clear()
    _SYSTEM_CHANNEL_ID = None
    _INSTALLED = False
