"""The Discord half of the approval bridge: widgets, text, and clicks.

:mod:`.approvals` decides *whether an operation may proceed*.  This module
decides *what the human sees* and *what a button press means* -- the
presentation seam, kept apart because the policy half is where the
concurrency lives and neither concern should have to be read to understand
the other.

**What changed in the rebuild** (SPEC §2 import matrix, W4's UPDATE dossier).
This module used to reach into ``gateway`` for the Discord client
(``get_client()``, ``allowed_mentions()``) and into ``authz`` for
``authorize_resolution``.  ``gateway.py`` is deleted, and INV-C25 rules
``authorize_resolution`` out: it demands a session principal
(``authz.py:228-230``) that a session started at a TERMINAL never has, so
every gate would have been refused.

So both went, and what stayed is the part that was always worth keeping: the
button/view structure and the interaction handling.  Two consequences shape
the rest:

* **the connection is not ours any more.**  The Discord connection lives in
  the BROKER (C1), possibly in another process entirely.  This module never
  looks a channel up; it builds a view and hands it over.
* **authorization moved to the SESSION** (§3.2a).  A click travels back over
  the return channel, and the APPROVER check happens where the gate lives.
  The callback here therefore reports a decision -- it does not make one.

Two rendering rules are security properties, not cosmetics:

* **the command is escaped** (:func:`inline_code`).  The gate message is the
  only thing the human bases the decision on, so a backtick in the command
  must not be able to close the code span and inject text -- an approval UI
  the requester can forge is not an approval UI;
* **mentions are suppressed** (:func:`allowed_mentions`).  Gate text quotes
  attacker-influenced content verbatim; an ``@everyone`` inside it would ping
  the whole server.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional, Sequence

from . import authz

logger = logging.getLogger(__name__)

APPROVE_LABEL = "Approve"
DENY_LABEL = "Deny"

#: What a click MEANS on the wire (§3.2a's ``decision`` field).  Named here
#: because the buttons are what produce it; :mod:`.approvals` imports these
#: rather than comparing against a second pair of literals.
DECISION_APPROVE = "approve"
DECISION_DENY = "deny"

#: Kept identical to L3's, so a gate and its widget die at the same moment.
#: Imported rather than repeated: ``authz.py:42-49`` carries the reason the
#: number is 120 (Discord's interaction token), and a copy here would lose it.
GATE_TIMEOUT_SECONDS = authz.GATE_TIMEOUT_SECONDS

#: Longest preview inlined into a gate.  Discord caps a body at 2000
#: characters, so one huge diff must not push the buttons off the message.
PREVIEW_LIMIT = 1200

#: Replies to a click that resolves nothing.  Both are ephemeral: they answer
#: the person who clicked, not the channel.
ALREADY_DECIDED = "This request has already been decided."
NOT_DELIVERED = "The session did not take this decision — please try again."

#: What a click reports back.  ``(decision, discord_user_id) -> reply or None``;
#: ``None`` means "taken, nothing to say".
ClickReporter = Callable[[str, str], Awaitable[Optional[str]]]


def allowed_mentions() -> Any:
    """An ``AllowedMentions`` that pings nobody.  Used on EVERY gate send.

    py-cord leaves ``Client.allowed_mentions`` at ``None``, which means
    Discord's own permissive default applies.  A gate quotes the command
    verbatim, so an ``@everyone`` sitting in a repository file would otherwise
    notify the whole server the moment it reaches a gate message.

    Lives here rather than on the gateway because the message that needs it is
    built here, and a per-message value cannot be switched off by process-wide
    state somebody else owns.
    """
    import discord

    return discord.AllowedMentions.none()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def inline_code(text: str) -> str:
    """Render *text* as an inline code span it cannot escape from.

    Discord closes a span at the first matching run of backticks, so a command
    containing one would otherwise end the span and let the rest render as
    markdown -- forging the very message the approval decision rests on.

    The fix is Discord's (and CommonMark's) own rule: fence with a run of
    backticks longer than any run inside the text, and pad with spaces so a
    leading or trailing backtick cannot merge with the fence.  Newlines are
    flattened because an inline span does not survive them.
    """
    flattened = " ".join(str(text).split())
    if not flattened:
        return "``` ```"
    longest = 0
    run = 0
    for character in flattened:
        run = run + 1 if character == "`" else 0
        longest = max(longest, run)
    fence = "`" * (longest + 1)
    return f"{fence} {flattened} {fence}"


def gate_text(
    title: str,
    message: str,
    preview: Optional[str] = None,
    *,
    remote_resolvable: bool = True,
) -> str:
    """The full gate body: what it is, what it touches, how it can be answered.

    The closing line differs by design.  A gate with buttons says how long it
    lives; a gate WITHOUT them says so plainly (INV-C23) instead of inviting
    somebody to wait for an answer that can only be given at the machine.
    """
    lines = [f"**{title}**", message or ""]
    if preview:
        # Fenced so a diff stays legible; truncated so one huge edit cannot
        # push the message past Discord's 2000-character limit.
        body = (
            preview
            if len(preview) <= PREVIEW_LIMIT
            else preview[:PREVIEW_LIMIT] + "\n…"
        )
        lines.append(f"```\n{body}\n```")
    if remote_resolvable:
        lines.append(
            f"Approve or deny below — this request expires in "
            f"{GATE_TIMEOUT_SECONDS:.0f} s."
        )
    else:
        from .reporter import LOCAL_ONLY_MARKER

        lines.append(f"— {LOCAL_ONLY_MARKER}")
    return "\n".join(part for part in lines if part)


# --------------------------------------------------------------------------- #
# The widget
# --------------------------------------------------------------------------- #


def build_gate_view(gate_id: str, report: ClickReporter) -> Any:
    """Two buttons that REPORT a decision for *gate_id*.

    Must be called with a running event loop: py-cord's ``View`` grabs one in
    its constructor (``discord/ui/core.py:79``, measured).  That single fact is
    why the broker hands a view FACTORY to the gateway instead of a view --
    only the gateway knows which loop Discord is on.

    The decorator form cannot be used: every gate needs its own callbacks, so
    the items are constructed and their ``callback`` assigned per gate.

    *report* answers what happened, and the answer goes to the CLICKER only
    (ephemeral): whether a click was authorized is not the channel's business.
    """
    import discord

    view = discord.ui.View(timeout=GATE_TIMEOUT_SECONDS, store=False)
    for label, style, decision in (
        (APPROVE_LABEL, discord.ButtonStyle.success, DECISION_APPROVE),
        (DENY_LABEL, discord.ButtonStyle.danger, DECISION_DENY),
    ):
        button = discord.ui.Button(
            label=label, style=style, custom_id=f"cp-gate:{gate_id}:{decision}"
        )
        button.callback = _make_callback(decision, report)
        view.add_item(button)
    return view


def _make_callback(decision: str, report: ClickReporter):
    async def callback(interaction: Any) -> None:
        await on_click(decision, report, interaction)

    return callback


async def on_click(decision: str, report: ClickReporter, interaction: Any) -> None:
    """Handle one button press.  The ORDER here is binding.

    ``defer()`` first — Discord drops an interaction that is not acknowledged
    within 3 s — then report, and only then answer.  Missing the deadline
    costs us the reply, never the decision, so the defer failure is swallowed.
    """
    try:
        await interaction.response.defer()
    except Exception:
        logger.debug("cp_discord: defer() failed on a gate click", exc_info=True)

    user_id = str(getattr(getattr(interaction, "user", None), "id", ""))
    try:
        reply = await report(decision, user_id)
    except Exception:
        logger.debug("cp_discord: reporting a gate click failed", exc_info=True)
        reply = NOT_DELIVERED
    if reply:
        await _reply(interaction, reply)


async def _reply(interaction: Any, text: str) -> None:
    """Answer the clicker privately.  A failure here must not break the gate."""
    try:
        await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        logger.debug("cp_discord: could not send an ephemeral reply", exc_info=True)


def disable(view: Any) -> None:
    """Take a resolved gate's buttons out of service.  Never raises.

    A live button under a decided gate is worse than no button: it invites a
    click that can only be answered with \"too late\".
    """
    if view is None:
        return
    try:
        view.disable_all_items()
        view.stop()
    except Exception:
        logger.debug("cp_discord: could not disable a gate view", exc_info=True)


__all__: Sequence[str] = (
    "ALREADY_DECIDED",
    "APPROVE_LABEL",
    "DECISION_APPROVE",
    "DECISION_DENY",
    "DENY_LABEL",
    "GATE_TIMEOUT_SECONDS",
    "NOT_DELIVERED",
    "PREVIEW_LIMIT",
    "allowed_mentions",
    "build_gate_view",
    "disable",
    "gate_text",
    "inline_code",
    "on_click",
)
