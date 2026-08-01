"""Split text into Discord-sized messages without breaking code blocks.

Discord rejects any message body longer than 2000 characters, so every piece
of agent output has to be cut somewhere.  *Where* it is cut is the whole
problem:

* cutting mid-line turns shell output into gibberish, so we cut at line
  boundaries whenever a line fits at all;
* cutting inside a fenced code block is worse than gibberish — the opening
  ```` ``` ```` stays behind in message *n* and Discord renders **everything
  after it**, including the next agent turn, as code.  So a chunk that ends
  inside a block closes the fence, and the following chunk re-opens it with the
  same info string.

The module is deliberately pure: no Discord objects, no session state, no I/O.
That is what makes the fence behaviour testable on its own.
"""

from __future__ import annotations

from typing import List, Optional

#: Discord's hard limit for a message body.
DISCORD_LIMIT = 2000

FENCE = "```"

#: Room kept free for a synthetic closing fence (``"\n```"``).
_FENCE_RESERVE = len(FENCE) + 1


def _is_fence(line: str) -> bool:
    return line.lstrip().startswith(FENCE)


def _hard_split(line: str, width: int) -> List[str]:
    """Cut a single unbreakable line into pieces that fit.

    Only reached when one line is longer than a whole message (minified JS,
    a base64 blob, a stack trace with no newlines).  Losing the tail would be
    worse than an ugly cut.
    """
    if width <= 0:  # pathological limit; refuse to loop forever
        return [line]
    return [line[i : i + width] for i in range(0, len(line), width)]


def chunk_message(text: str, limit: int = DISCORD_LIMIT) -> List[str]:
    """Split *text* into message bodies of at most *limit* characters.

    Returns an empty list for empty or whitespace-only input, so callers can
    use the result directly without a separate "is there anything to send"
    check.  Every returned chunk contains an even number of fence markers.
    """
    if not text or not text.strip():
        return []

    chunks: List[str] = []
    current: List[str] = []
    size = 0
    fence: Optional[str] = None  # the opening fence line while inside a block

    def close_current() -> None:
        nonlocal current, size
        if not current:
            return
        body = "\n".join(current)
        if fence is not None and not body.rstrip().endswith(FENCE):
            body += "\n" + FENCE
        chunks.append(body)
        current = []
        size = 0

    def open_next() -> None:
        """Start a fresh chunk, re-opening the enclosing block if any."""
        nonlocal current, size
        current = []
        size = 0
        if fence is not None:
            current.append(fence)
            size = len(fence)

    for line in text.split("\n"):
        opens_fence = fence is None and _is_fence(line)
        # A chunk that is (or is about to be) inside a block must keep room
        # for the closing fence we may have to append -- and, if the line has
        # to be hard-split, for the opening fence the NEXT chunk re-emits.
        reserve = _FENCE_RESERVE if (fence is not None or opens_fence) else 0
        prefix = len(fence) + 1 if fence is not None else 0

        for piece in _hard_split(line, limit - reserve - prefix):
            cost = len(piece) + (1 if current else 0)
            if current and size + cost + reserve > limit:
                close_current()
                open_next()
                cost = len(piece) + (1 if current else 0)
            current.append(piece)
            size += cost

        if _is_fence(line):
            # Toggle AFTER placing the line: the opening marker belongs to the
            # chunk that starts the block, the closing one ends it.
            fence = None if fence is not None else line.strip()

    close_current()
    return chunks
