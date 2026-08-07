"""C1e — the RUECKKANAL for chat: a thread message becomes agent input (§6.0).

The seventh method, and the second thing that travels broker -> session.  It
looks like :mod:`.broker_gates`' resolution push and is deliberately NOT in
that module, for two reasons that both bite:

* that file answers *what a gate looks like in Discord and where a click
  goes* (``broker_gates.py:21-23``).  A steer is not an approval: no widget,
  no claim, no button.  Putting ``steer_frame`` beside ``resolution_frame``
  would turn that module docstring into a false statement and send the next
  reader looking for this code everywhere but here;
* **the two paths have OPPOSITE rules about a failed delivery**, which is the
  substantial half.  ``Broker._push_resolution`` marks a silent session
  ``_unreachable`` -- the sweep then archives its Discord thread, history and
  all -- and posts ``UNDELIVERABLE`` where every thread reader sees it.
  Neither is allowed here (§4.3b):

  - a steer is **not a liveness probe**.  One failed push is no evidence that
    a session died; heartbeat and sweep are what answer that question, and
    letting a CHAT MESSAGE archive a live session's thread would be the most
    destructive thing in this feature;
  - a post into the thread after a stranger's message would confirm that the
    session exists -- INV-6 again, this time around the back.

  Laid side by side, the two would be read as one pattern and re-aligned by
  the next person to touch them.

**It knows nothing about a broker** (AC-B35).  Token, port and session id
arrive as arguments, so the import chain stays
``broker_server -> broker_steer -> broker_gates`` and never closes.

**Nothing here may learn who wrote** (INV-2).  The frame carries
``external_id`` through; the authorization happens in the session, which owns
the database.  What comes back is one coarse word, and the log records that
word and the session id -- never the text, never the sender.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

from . import broker_gates, constants

logger = logging.getLogger(__name__)

#: The seventh method: a chat message, on its way into a running session.
M_STEER = "steer"

#: "Your message got through."  A reaction rather than a reply: it needs no
#: extra intent, it does not push the thread around, and it lands on the
#: message it answers.
REACTION_TAKEN = "\N{WHITE HEAVY CHECK MARK}"

#: "Arrived, but nothing happened with it" -- empty, or the steering queue
#: refused it.  Distinct from :data:`REACTION_TAKEN` because the difference is
#: the only thing the sender can act on.
REACTION_DROPPED = "\N{HEAVY EXCLAMATION MARK SYMBOL}"

#: How an answer maps to a reaction (§4.6c).  Keyed on the ANSWER, never on
#: the sender: whether somebody is authorized is a fact the broker does not
#: have (INV-2), so this table is the only thing it can decide on.
#:
#: Two words are absent on purpose, and both silences are load-bearing:
#: ``refused`` (a stranger must learn nothing, INV-6) and the missing answer
#: altogether -- with no frame there is no ``steer`` field, so the sender's
#: standing is UNKNOWN, and in doubt we do not confirm that the session is
#: there.  ``duplicate`` is acknowledged exactly like a delivery (§4.4b): its
#: normal cause is a first attempt that LANDED and lost its answer on the way
#: back, so anything else would simply be untrue.
_REACTIONS: Dict[str, str] = {
    constants.STEER_DELIVERED: REACTION_TAKEN,
    constants.STEER_DUPLICATE: REACTION_TAKEN,
    constants.STEER_EMPTY: REACTION_DROPPED,
    constants.STEER_UNDELIVERED: REACTION_DROPPED,
}


def steer_frame(
    token: str,
    session_id: str,
    external_id: Any,
    text: Any,
    message_id: Any,
) -> Dict[str, Any]:
    """The frame a chat message travels in (§4.3).

    ``external_id`` is stringified HERE, in the broker, and that one call is
    what makes the whole path work: py-cord hands over ``message.author.id``
    as an ``int``, JSON carries it as a number, and the session fail-closes on
    anything that is not a ``str`` (``inbound.py:233``).  Every legitimate
    sender would have been rejected as ``UNKNOWN_SENDER`` -- and INV-6
    prescribes silence for that, so the feature would have been dead without
    a single symptom.  ``approvals.py:415`` does the same thing for the same
    reason.

    ``message_id`` rides along for the deduplication window (§4.4a): the push
    retries, and a repeated frame would otherwise steer the same instruction
    into the agent two or three times over.
    """
    return {
        "token": token,
        "method": M_STEER,
        "session_id": session_id,
        "params": {
            "external_id": str(external_id),
            "text": text,
            "message_id": message_id,
        },
    }


def push_steer(port: int, frame: Dict[str, Any]) -> Optional[str]:
    """Hand one steer frame to a session.  Answers what became of it.

    One of :data:`constants.STEER_DELIVERED` / ``_EMPTY`` / ``_UNDELIVERED`` /
    ``_REFUSED`` / ``_DUPLICATE``, or ``None`` when nothing answered at all.
    Shaped like :func:`broker_gates.push` -- port and frame -- because it IS
    that push, with a different reading of the failure.

    A failure is **only** logged (§4.3b): ``_unreachable`` is not touched and
    nothing is posted into the thread -- see this module's docstring for why
    both would be worse than the lost message.

    The log line carries the session id and the outcome and **nothing else**.
    ``inbound.py:201`` puts it word for word: an unauthorized message is *not
    logged with its content*.  A well-meant diagnostic here would write a
    stranger's text into the broker's process log -- exactly the place it was
    discarded to stay out of.
    """
    outcome, answer = broker_gates.push(port, frame)
    steer = answer.get("steer") if isinstance(answer, dict) else None
    logger.debug(
        "cp_discord: steer for %s: %s",
        frame.get("session_id"),
        steer if isinstance(steer, str) else outcome.name,
    )
    return steer if isinstance(steer, str) else None


def no_inbound_port(session_id: str) -> None:
    """The one branch of the steer chain that used to be mute (review R1).

    A registered session without an ``inbound_port`` is a real, operator-
    fixable state -- registration through, listener not up yet.  Without this
    line the message vanished with no record anywhere in the process: the user
    types from a phone, never gets a reaction, and nothing says why.

    Session id only.  That is the same INV-6 frame :func:`push_steer` keeps,
    and it is why this is a broker-side log and not a thread post.

    Lives here rather than inline because ``broker_server.py`` is three lines
    under its cap; ``AC-B20`` fails the moment that stops being respected.
    """
    logger.debug("cp_discord: no inbound port for %s", session_id)
    return None


def reaction_for(steer: Optional[str]) -> Optional[str]:
    """Which reaction an answer earns, or ``None`` for silence (§4.6c)."""
    return _REACTIONS.get(steer) if isinstance(steer, str) else None


__all__: Sequence[str] = (
    "M_STEER",
    "REACTION_DROPPED",
    "REACTION_TAKEN",
    "push_steer",
    "reaction_for",
    "steer_frame",
)
