"""C1d — the HINWEG: a session's gate becomes a widget in its thread (§3.2b).

The sixth method, and the one that was missing until R15.  Everything else on
this wire travels session -> broker -> thread as TEXT; a gate has to travel as
a THING THAT CAN BE PRESSED, and that is a different problem:

* the buttons must reach back into the session that raised the gate, which is
  the return channel (§3.2a) -- so this module is where ``deliver_resolution``
  stops being dead code and becomes the second participant in C4's CAS;
* a py-cord ``View`` grabs the running event loop in its constructor
  (``discord/ui/core.py:79``, measured), so it can only be built ON the
  gateway's loop.  The broker therefore hands over a view FACTORY, not a view.
  That indirection is not ceremony: building the view on the broker's TCP
  thread raises ``RuntimeError: no running event loop``;
* a gate the phone CANNOT answer gets no widget at all (INV-C23, AC-91).  A
  button that resolves nothing is worse than no button -- somebody taps it,
  nothing happens, and they keep waiting.

Kept out of :mod:`.broker_server` for the same reason the registry is: that
module answers *how the broker serves a frame*, this one answers *what a gate
looks like in Discord and where a click goes*.  It also keeps both files under
the project's 600-line cap (AC-47).

**INV-C1 applies unchanged.**  A gate that cannot be posted -- no thread, no
Discord, no channel -- is simply a gate that cannot be answered from the
phone.  It is never an error for the session: the terminal prompt runs either
way, and :meth:`.SessionClient.submit_gate` returning ``False`` is
information, not a failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from . import approvals_ui
from .broker_election import LOOPBACK

logger = logging.getLogger(__name__)

#: ``status`` values a gate frame can carry.  TWO values on ONE method,
#: because opening and closing a gate are the same conversation about the same
#: message -- a separate method would let one arrive without the other and
#: leave live buttons under a decided gate.
GATE_OPEN = "open"
GATE_CLOSED = "closed"

#: How a resolution gets from a click back to the session.  Bound to
#: :meth:`.Broker.deliver_resolution` in production; a plain callable, so this
#: module never has to know what a broker is.  It answers ``None`` when the
#: session TOOK the decision, or a refusal to show the clicker.
ResolutionSender = Callable[[str, str, str, Any], Optional[str]]

#: The method that carries a decision back to a session (§3.2a).
M_RESOLVE = "resolve"

#: §3.2: connections are short-lived, so this is a latency budget rather than
#: a patience setting.  INV-C17 allows 100 ms for a delivery.
SOCKET_TIMEOUT = 0.5

#: A frame longer than this is not a frame (defence against a local process
#: feeding an endless line to either end).
MAX_FRAME_BYTES = 1024 * 1024

#: A rejected delivery is retried this often, this far apart (§3.1, AC-85d).
#: The session re-reads the portfile the moment it rejects one, so attempt two
#: already meets the healed token.
RETRY_ATTEMPTS = 3
RETRY_DELAY = 1.0

#: What an undeliverable resolution says in the thread (INV-C17).
UNDELIVERABLE = "Zustellung fehlgeschlagen — die Sitzung antwortet nicht."


class Outcome:
    """Three-valued delivery result; ``bool`` cannot express the middle one."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<{self.name}>"


#: Accepted.
DELIVERED = Outcome("delivered")
#: Answered, but not accepted -- somebody IS there (usually a stale token).
REFUSED = Outcome("refused")
#: Nobody answered: connect refused, timeout, closed.
TRANSPORT_FAILED = Outcome("transport_failed")

#: Builds the view, given a predicate that answers \"is this gate still mine?\".
#: Runs ON the gateway loop.
ViewFactory = Callable[[Callable[[], bool]], Any]


@dataclass(slots=True)
class PostedGate:
    """One gate that made it into a thread: its message, and its widget."""

    message: Any
    view: Any = None
    claimed: bool = False


class GateBoard:
    """Which gate owns which message, and whether it is still answerable.

    Small, and deliberately its own object: the claim is the only state a
    button press touches, and a press does NOT arrive on the thread that
    posted the gate -- it arrives from Discord.  A lock around three fields is
    cheaper to reason about than making the whole thread manager thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gates: Dict[Tuple[str, str], PostedGate] = {}

    def is_open(self, session_id: str, gate_id: str) -> bool:
        with self._lock:
            return (session_id, gate_id) in self._gates

    def remember(self, session_id: str, gate_id: str, posted: PostedGate) -> None:
        with self._lock:
            self._gates[(session_id, gate_id)] = posted

    def claim(self, session_id: str, gate_id: str) -> bool:
        """Take the gate, once.  ``False`` for a double click or a late one.

        The gate STAYS on the board after a successful claim: the session
        still has to close it, and that close is what edits the message and
        kills the buttons.  Dropping it here would leave a live widget behind.
        """
        with self._lock:
            posted = self._gates.get((session_id, gate_id))
            if posted is None or posted.claimed:
                return False
            posted.claimed = True
            return True

    def release(self, session_id: str, gate_id: str) -> None:
        """Give a claim back -- the session REFUSED the click (AC-74).

        Without this an outsider's click would consume somebody else's
        pending approval: the gate stays open in the session, but the widget
        would never accept a second press.
        """
        with self._lock:
            posted = self._gates.get((session_id, gate_id))
            if posted is not None:
                posted.claimed = False

    def take(self, session_id: str, gate_id: str) -> Optional[PostedGate]:
        """Remove and return a gate -- the session has decided it."""
        with self._lock:
            return self._gates.pop((session_id, gate_id), None)

    def forget_session(self, session_id: str) -> None:
        """Drop every gate of a session that went away."""
        with self._lock:
            for key in [key for key in self._gates if key[0] == session_id]:
                del self._gates[key]


def handle_gate(
    session_id: str,
    params: Dict[str, Any],
    *,
    gateway: Any,
    send_resolution: ResolutionSender,
    board: GateBoard,
) -> bool:
    """Turn one ``M_GATE`` frame into Discord work.

    Returns whether the frame was WELL-FORMED -- not whether Discord did
    anything.  The caller acks on ``True``: the decision comes back later over
    the return channel (§3.2a), because a human takes as long as a human takes
    and this socket is a 500 ms conversation.
    """
    gate_id = params.get("gate_id")
    if not isinstance(gate_id, str) or not gate_id:
        return False

    if params.get("status") == GATE_CLOSED:
        gateway.finish_gate(session_id, gate_id, closing_text(params))
        return True

    remote = bool(params.get("remote_resolvable", True))
    body = approvals_ui.gate_text(
        str(params.get("title") or ""),
        str(params.get("body") or ""),
        params.get("preview") or None,
        remote_resolvable=remote,
    )
    factory = (
        view_factory(session_id, gate_id, send_resolution, board) if remote else None
    )
    gateway.post_gate(session_id, gate_id, body, factory)
    return True


def resolution_frame(
    token: str, session_id: str, gate_id: str, decision: str, discord_user_id: Any
) -> Dict[str, Any]:
    """The frame a resolution travels back in (§3.2a).

    ``discord_user_id`` is carried because the APPROVER check happens in the
    SESSION, not in the broker: the authorization database belongs to the
    session, and the decision should fall where the gate lives.  Freezing this
    frame without the sender would have left C4 unable to add it later.
    """
    return {
        "token": token,
        "method": M_RESOLVE,
        "session_id": session_id,
        "params": {
            "gate_id": gate_id,
            "decision": decision,
            "discord_user_id": discord_user_id,
        },
    }


def push_once(
    port: int, frame: Dict[str, Any]
) -> Tuple[Outcome, Optional[Dict[str, Any]]]:
    """One attempt at handing *frame* to a session's listener on loopback.

    Returns the outcome AND the session's answer: an accepted frame can still
    carry a refusal of the CLICK, and that belongs to the person who clicked.
    """
    payload = (json.dumps(frame) + "\n").encode("utf-8")
    try:
        with socket.create_connection((LOOPBACK, port), timeout=SOCKET_TIMEOUT) as sock:
            sock.settimeout(SOCKET_TIMEOUT)
            sock.sendall(payload)
            with sock.makefile("rb") as stream:
                line = stream.readline(MAX_FRAME_BYTES)
    except OSError:
        return TRANSPORT_FAILED, None
    if not line:
        return TRANSPORT_FAILED, None
    try:
        answer = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # A reply we cannot read still proves somebody answered, so this is
        # not a transport failure -- but it is not an ack either.
        return REFUSED, None
    if isinstance(answer, dict) and answer.get("ok"):
        return DELIVERED, answer
    return REFUSED, answer if isinstance(answer, dict) else None


def push(
    port: int, frame: Dict[str, Any], *, sleep=time.sleep
) -> Tuple[Outcome, Optional[Dict[str, Any]]]:
    """Push *frame*, retrying a REFUSAL but never a silence (AC-85d).

    A refusal is retried because the session heals itself the moment it
    refuses -- it re-reads the portfile, so attempt two meets the new token.
    Without that the phone click is simply lost: the CAS lives in the session,
    a discarded delivery never sets it, and after 120 s the Discord branch is
    dead (INV-C10).

    Silence is NOT retried: nobody answered, and the caller has to treat that
    as "the session is gone" rather than spend three seconds hoping.
    """
    outcome: Outcome = TRANSPORT_FAILED
    answer: Optional[Dict[str, Any]] = None
    for attempt in range(RETRY_ATTEMPTS):
        outcome, answer = push_once(port, frame)
        if outcome is DELIVERED or outcome is TRANSPORT_FAILED:
            return outcome, answer
        if attempt + 1 < RETRY_ATTEMPTS:
            sleep(RETRY_DELAY)
    return outcome, answer


def closing_text(params: Dict[str, Any]) -> str:
    """What the finished gate message says.  Never empty.

    An empty outcome would leave a decided gate looking exactly like an open
    one -- with dead buttons, which is the worst of both.
    """
    outcome = str(params.get("outcome") or "").strip()
    title = str(params.get("title") or "").strip()
    if outcome and title:
        return f"**{title}** — {outcome}"
    return outcome or title or "entschieden"


def view_factory(
    session_id: str, gate_id: str, send_resolution: ResolutionSender, board: GateBoard
) -> ViewFactory:
    """The callable the gateway runs ON ITS OWN LOOP to build the widget.

    The claim is what makes a double click harmless and a late click honest
    (AC-39).  It is GIVEN BACK when the session refuses: an outsider's press
    must not consume the approval somebody else is still allowed to give
    (AC-74) -- the gate is still open, so the widget has to stay usable.
    """

    def make_view(claim: Callable[[], bool]) -> Any:
        async def report(decision: str, discord_user_id: str) -> Optional[str]:
            if not claim():
                return approvals_ui.ALREADY_DECIDED
            # Off the loop: delivery is a blocking socket round trip that
            # retries for up to three seconds (AC-85d).  On the Discord loop
            # that would stall every other session's posts.
            refusal = await asyncio.to_thread(
                send_resolution, session_id, gate_id, decision, discord_user_id
            )
            if refusal:
                board.release(session_id, gate_id)
            return refusal

        return approvals_ui.build_gate_view(gate_id, report)

    return make_view


__all__: Sequence[str] = (
    "DELIVERED",
    "GATE_CLOSED",
    "GATE_OPEN",
    "MAX_FRAME_BYTES",
    "M_RESOLVE",
    "REFUSED",
    "RETRY_ATTEMPTS",
    "RETRY_DELAY",
    "SOCKET_TIMEOUT",
    "TRANSPORT_FAILED",
    "UNDELIVERABLE",
    "GateBoard",
    "Outcome",
    "PostedGate",
    "ResolutionSender",
    "ViewFactory",
    "closing_text",
    "handle_gate",
    "push",
    "push_once",
    "resolution_frame",
    "view_factory",
)
