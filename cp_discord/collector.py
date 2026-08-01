"""C7 — collects a run's output into the report the phone actually reads.

``report`` is the DEFAULT mode (§4.4), so this is the feature's main path, not
a garnish: while the agent works, Discord sees one status line, and at the
next wait point it gets a report -- the last assistant message, the tools that
ran since the previous wait point, and (added by C4) the question or gate.

**Why not ``output.py``.**  The old module bound its channel resolver at
import time and pushed everything it could not resolve into a system channel;
it was written for "Discord started this run", which is the direction this
rebuild reverses.  So C7 hangs on the same three core hooks it did --
``stream_event``, ``pre_tool_call``, ``post_tool_call`` -- and writes into its
own sink.  The pure functions stay shared: :mod:`.rendering` decides what a
line looks like, :mod:`.chunking` decides where a message is cut.  Neither
knows about sessions, so both are reusable as they are.

**The buffer is a ring, and its overflow is visible** (§8b): at most 50
entries or 8 KB, whichever bites first.  Dropping quietly would be worse than
dropping loudly -- a report that silently omits the destructive command is
exactly the report somebody approves without reading.  So an overflow says
``… (n weitere)`` and a truncated message says so too.

**Nothing here waits.**  The hooks run on the agent's own path; they append to
a bounded buffer and return ``None``, which is what keeps the collector from
being able to block, transform or fail a tool call (INV-C1, INV-C4).
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Deque, List, Optional, Sequence, Tuple

from . import rendering
from .chunking import chunk_message
from .reporter import (
    MODE_REPORT,
    MODE_STREAM,
    Mailbox,
    ReportEvent,
    StateReporter,
)

logger = logging.getLogger(__name__)

#: Ring bounds (§8b).  Whichever is reached first ends the ride.
MAX_ENTRIES = 50
MAX_BYTES = 8 * 1024

#: What an overflow looks like.  It is CONTENT, not a log line: it goes into
#: the message so the reader knows the list is incomplete.
OVERFLOW_TEMPLATE = "… ({count} weitere)"

#: The same courtesy for a message whose beginning had to go.
TRUNCATION_MARKER = "… (Anfang gekuerzt)"

#: How much of the running answer ``stream`` mode shows in the status line.
#: The slot is latest-wins, so this is a window on the newest text, not a
#: transcript -- a transcript is what the report at the wait point is for.
STREAM_TAIL = 200


def _byte_size(text: str) -> int:
    return len(text.encode("utf-8", "replace"))


class ReportCollector:
    """Buffers one run's output and posts a report at every wait point.

    Owns no thread and no socket: it posts into C3's mailbox, so the report
    rides the same critical lane as the state edge that announces the wait
    point, and arrives just before it (AC-24).
    """

    def __init__(
        self,
        mailbox: Mailbox,
        reporter: StateReporter,
        *,
        mode: str = MODE_REPORT,
    ) -> None:
        self._mailbox = mailbox
        self._reporter = reporter
        self._mode = mode if mode in (MODE_REPORT, MODE_STREAM) else MODE_REPORT
        # (text, size) pairs, oldest first.  Two rings, one shared budget.
        self._entries: Deque[Tuple[str, int]] = deque()
        self._message: Deque[Tuple[str, int]] = deque()
        self._bytes = 0
        self._dropped_entries = 0
        self._message_truncated = False
        # A ``part_start`` begins a new part, so the next visible text replaces
        # the answer instead of extending it.  Resolving that LAZILY -- on the
        # next non-empty text rather than immediately -- is what keeps a
        # thinking part from wiping a finished answer: thinking text is never
        # visible, so it never triggers the replacement.
        self._new_message_pending = False
        reporter.add_wait_point_observer(self.flush)

    # -- observation ----------------------------------------------------

    @property
    def buffered_entries(self) -> int:
        return len(self._entries)

    @property
    def buffered_bytes(self) -> int:
        return self._bytes

    # -- hooks ----------------------------------------------------------

    def on_stream_event(
        self, event_type: str, event_data: Any, agent_session_id: Any = None
    ) -> None:
        """Visible assistant text.  Thinking is not forwarded (see rendering)."""
        try:
            if event_type == "part_start":
                self._new_message_pending = True
            text = rendering.stream_text(event_type, event_data)
            if not text:
                return
            if self._new_message_pending:
                self._reset_message()
                self._new_message_pending = False
            self._append_message(text)
            if self._mode == MODE_STREAM:
                self._reporter.on_activity(self._assistant_text()[-STREAM_TAIL:])
        except Exception:
            logger.debug("cp_discord: collecting a stream event failed", exc_info=True)
        return None

    def on_pre_tool_call(
        self, tool_name: Any, tool_args: Any, context: Any = None
    ) -> None:
        """A tool is about to run.

        Recorded HERE and not only on completion: a tool that opens an
        approval gate has not completed when the report is built, and the
        command being approved is the single most important line in it.
        """
        try:
            self._append_entry(rendering.tool_start_text(str(tool_name), tool_args))
        except Exception:
            logger.debug("cp_discord: collecting a tool call failed", exc_info=True)
        return None

    def on_post_tool_call(
        self,
        tool_name: Any,
        tool_args: Any,
        result: Any,
        duration_ms: Any,
        context: Any = None,
    ) -> None:
        try:
            self._append_entry(
                rendering.tool_end_text(str(tool_name), _as_duration(duration_ms))
            )
        except Exception:
            logger.debug("cp_discord: collecting a tool result failed", exc_info=True)
        return None

    # -- the report -----------------------------------------------------

    def flush(self) -> None:
        """Build and post the report, then start the next one.

        Called at every wait point.  An empty buffer posts NOTHING: a report
        that says nothing is the failure mode C7 exists to prevent, and an
        empty one on the wire looks like content that got lost.
        """
        body = self._render()
        self._clear()
        if not body:
            return
        chunks = chunk_message(body)
        if chunks:
            self._mailbox.post_report(ReportEvent(tuple(chunks)))

    def _render(self) -> str:
        sections: List[str] = []
        message = self._assistant_text()
        if message.strip():
            if self._message_truncated:
                message = f"{TRUNCATION_MARKER}\n{message}"
            sections.append(message)
        lines = [text for text, _size in self._entries]
        if self._dropped_entries:
            lines.insert(0, OVERFLOW_TEMPLATE.format(count=self._dropped_entries))
        if lines:
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

    def _assistant_text(self) -> str:
        return "".join(text for text, _size in self._message)

    def _clear(self) -> None:
        self._entries.clear()
        self._message.clear()
        self._bytes = 0
        self._dropped_entries = 0
        self._message_truncated = False
        self._new_message_pending = False

    # -- the ring -------------------------------------------------------

    def _reset_message(self) -> None:
        self._bytes -= sum(size for _text, size in self._message)
        self._message.clear()
        self._message_truncated = False

    def _append_message(self, text: str) -> None:
        self._message.append((text, _byte_size(text)))
        self._bytes += _byte_size(text)
        self._enforce_bounds()

    def _append_entry(self, text: str) -> None:
        self._entries.append((text, _byte_size(text)))
        self._bytes += _byte_size(text)
        self._enforce_bounds()

    def _enforce_bounds(self) -> None:
        """Make room, oldest first, and remember what that cost.

        Entries go before message fragments: the answer is what the reader
        came for, the tool list is the context around it.  Both losses stay
        visible -- that is the difference between a bounded buffer and a
        buffer that lies.
        """
        while len(self._entries) > MAX_ENTRIES:
            self._drop_oldest_entry()
        while self._bytes > MAX_BYTES:
            if self._entries:
                self._drop_oldest_entry()
            elif self._message:
                _text, size = self._message.popleft()
                self._bytes -= size
                self._message_truncated = True
            else:  # pragma: no cover - both rings empty means _bytes is 0
                return

    def _drop_oldest_entry(self) -> None:
        _text, size = self._entries.popleft()
        self._bytes -= size
        self._dropped_entries += 1


def _as_duration(duration_ms: Any) -> float:
    """A duration we can format, whatever the caller passed."""
    try:
        return float(duration_ms)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# Plugin surface (C6 drives this)
# --------------------------------------------------------------------------- #

_collector: Optional[ReportCollector] = None

_HOOKS: Tuple[Tuple[str, str], ...] = (
    ("stream_event", "_on_stream_event"),
    ("pre_tool_call", "_on_pre_tool_call"),
    ("post_tool_call", "_on_post_tool_call"),
)


def active_collector() -> Optional[ReportCollector]:
    return _collector


def _on_stream_event(*args: Any, **kwargs: Any) -> None:
    if _collector is not None:
        _collector.on_stream_event(*args, **kwargs)
    return None


def _on_pre_tool_call(*args: Any, **kwargs: Any) -> None:
    if _collector is not None:
        _collector.on_pre_tool_call(*args, **kwargs)
    return None


def _on_post_tool_call(*args: Any, **kwargs: Any) -> None:
    if _collector is not None:
        _collector.on_post_tool_call(*args, **kwargs)
    return None


def install(config: Any) -> None:
    """Bring C7 up.  Requires C3, which ``COMPONENTS`` starts first.

    Refusing loudly when the reporter is missing is deliberate: a collector
    with nowhere to post would leave ``report`` mode -- the DEFAULT mode --
    delivering an empty shell, and the layer would look installed while the
    feature was gone.
    """
    global _collector

    from code_puppy.callbacks import register_callback

    from . import reporter as reporter_module

    mailbox = reporter_module.active_mailbox()
    state_reporter = reporter_module.active_reporter()
    if mailbox is None or state_reporter is None:
        raise RuntimeError(
            "the report collector (C7) needs the state reporter (C3), which is "
            "not installed"
        )

    if _collector is not None:
        uninstall()

    _collector = ReportCollector(
        mailbox, state_reporter, mode=getattr(config, "mode", MODE_REPORT)
    )
    for phase, handler in _HOOKS:
        register_callback(phase, globals()[handler])
    logger.debug("cp_discord: C7 collector installed (mode=%s)", _collector._mode)


def uninstall() -> None:
    """Take C7 down.  Never raises: teardown must reach every layer."""
    global _collector

    from code_puppy.callbacks import unregister_callback

    for phase, handler in _HOOKS:
        unregister_callback(phase, globals()[handler])
    _collector = None


__all__: Sequence[str] = (
    "MAX_BYTES",
    "MAX_ENTRIES",
    "OVERFLOW_TEMPLATE",
    "STREAM_TAIL",
    "TRUNCATION_MARKER",
    "ReportCollector",
    "active_collector",
    "install",
    "uninstall",
)
