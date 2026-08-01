"""The Discord half of the L4 approval bridge: widgets, text, and clicks.

:mod:`.approvals` decides *who answers for an operation* and *whether it may
proceed*.  This module decides *what the human sees* and *what a button press
does* -- the presentation seam, split out because the policy half had grown
past the project's 600-line cap with two unrelated concerns inside it.

Everything here runs ON the gateway loop.  Nothing here reads session context:
a gate arrives already attributed, which is what keeps attribution in exactly
one place.

Two rendering rules are security properties, not cosmetics:

* **the command is escaped** (:func:`inline_code`).  The gate message is the
  only thing the human bases the decision on, so a backtick in the command
  must not be able to close the code span and inject text -- an approval UI
  the requester can forge is not an approval UI;
* **mentions are suppressed** (:func:`allowed_mentions`).  Gate text quotes
  attacker-influenced content verbatim; an ``@everyone`` inside it would ping
  the whole server.  The client sets a default too, but that is process-wide
  state another frontend can change, so the message carries its own.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import authz, gateway

logger = logging.getLogger(__name__)

APPROVE_LABEL = "Approve"
DENY_LABEL = "Deny"

#: Longest preview inlined into a gate.  Discord caps a body at 2000
#: characters, so one huge diff must not push the buttons off the message.
PREVIEW_LIMIT = 1200


class ApprovalUIError(RuntimeError):
    """Raised when a gate cannot be shown at all (never a silent bypass)."""


@dataclass
class PendingGate:
    """One open gate: its authz record, the waiter, and the posted message."""

    gate: authz.Gate
    future: "asyncio.Future[bool]"
    message: Any = None
    view: Any = None


#: Gate id -> the gate awaiting a click.  Owned here because the click handler
#: is the only thing that resolves one.
_PENDING: Dict[str, PendingGate] = {}


def pending_gates() -> Dict[str, PendingGate]:
    """The gates currently awaiting a click.  Read-only view for tests/L5."""
    return dict(_PENDING)


def reset_state() -> None:
    """Cancel and forget every open gate.  Used at teardown and by tests."""
    for pending in list(_PENDING.values()):
        if not pending.future.done():
            pending.future.cancel()
    _PENDING.clear()


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


def gate_text(title: str, message: str, preview: Optional[str]) -> str:
    """The full gate body: what it is, what it touches, how long it lives."""
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
    lines.append(
        f"Approve or deny below — this request expires in "
        f"{authz.GATE_TIMEOUT_SECONDS:.0f} s."
    )
    return "\n".join(part for part in lines if part)


def build_view(pending: PendingGate) -> Any:
    """Two buttons wired to *pending*, built without a live client.

    The decorator form cannot be used: every gate needs its own callbacks, so
    the items are constructed and their ``callback`` assigned per gate.
    """
    import discord

    view = discord.ui.View(timeout=authz.GATE_TIMEOUT_SECONDS, store=False)
    gate_id = pending.gate.gate_id
    for label, style, allowed in (
        (APPROVE_LABEL, discord.ButtonStyle.success, True),
        (DENY_LABEL, discord.ButtonStyle.danger, False),
    ):
        button = discord.ui.Button(
            label=label,
            style=style,
            custom_id=f"cp-gate:{gate_id}:{'allow' if allowed else 'deny'}",
        )

        async def _callback(interaction: Any, _allowed: bool = allowed) -> None:
            await on_click(gate_id, _allowed, interaction)

        button.callback = _callback
        view.add_item(button)
    return view


# --------------------------------------------------------------------------- #
# Posting and resolving
# --------------------------------------------------------------------------- #


async def _channel_for(channel_id: int) -> Any:
    """The channel object, from cache or fetched.  Raises if unreachable."""
    client = gateway.get_client()
    if client is None:
        raise ApprovalUIError("the Discord gateway is not connected")
    channel = client.get_channel(channel_id)
    if channel is None:
        channel = await client.fetch_channel(channel_id)
    if channel is None:
        raise ApprovalUIError(f"channel {channel_id} is not reachable")
    return channel


async def post_gate(
    channel_id: int,
    gate: authz.Gate,
    title: str,
    message: str,
    preview: Optional[str],
) -> PendingGate:
    """Post *gate* into its channel and register it as awaiting a click.

    Raises on any Discord failure; the caller turns that into a refusal
    (INV-3) after undoing its own bookkeeping.
    """
    loop = asyncio.get_running_loop()
    pending = PendingGate(gate=gate, future=loop.create_future())
    _PENDING[gate.gate_id] = pending
    try:
        pending.view = build_view(pending)
        channel = await _channel_for(channel_id)
        pending.message = await channel.send(
            gate_text(title, message, preview),
            view=pending.view,
            # A gate quotes the command verbatim; it must ping nobody.
            allowed_mentions=gateway.allowed_mentions(),
        )
    except Exception:
        _PENDING.pop(gate.gate_id, None)
        raise
    return pending


def forget(gate_id: str) -> None:
    """Drop a resolved gate from the pending registry.  Idempotent."""
    _PENDING.pop(gate_id, None)


async def _reply(interaction: Any, text: str) -> None:
    """Answer the clicker privately.  A failure here must not break the gate."""
    try:
        await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        logger.debug("Discord: could not send an ephemeral reply", exc_info=True)


async def on_click(gate_id: str, allowed: bool, interaction: Any) -> None:
    """Handle one button press (SPEC-L4 §4.3a, order is binding).

    ``defer()`` first — Discord drops an interaction that is not acknowledged
    within 3 s — then authorize, and only then resolve.  Every refusal leaves
    the gate OPEN: an outsider's click must not consume somebody else's
    pending approval.
    """
    try:
        await interaction.response.defer()
    except Exception:
        # Missing the 3 s deadline costs us the reply, not the decision.
        logger.debug("Discord: defer() failed on a gate click", exc_info=True)

    pending = _PENDING.get(gate_id)
    if pending is None or pending.future.done():
        # Double-click, network retry, or a gate that already expired.  Both
        # must be idempotent: no second resolution, no second edit.
        await _reply(interaction, "This request has already been decided.")
        return

    external_id = str(getattr(interaction.user, "id", ""))
    decision = authz.authorize_resolution(gate_id, gateway.SESSION_PREFIX, external_id)
    if not decision.allowed:
        reason = decision.reason.value if decision.reason else "not_allowed"
        await _reply(interaction, f"You may not answer this request ({reason}).")
        return

    authz.close_gate(gate_id)
    if not pending.future.done():
        pending.future.set_result(allowed)

    verdict = "APPROVED" if allowed else "DENIED"
    await finish_message(
        pending, f"**{verdict}** by {decision.principal} — {pending.gate.title}"
    )


async def finish_message(pending: PendingGate, text: str) -> None:
    """Disable the buttons and write the outcome back.

    Wrapped: a Discord outage while closing must never turn a decision that was
    already made into a hung gate.
    """
    try:
        if pending.view is not None:
            pending.view.disable_all_items()
            pending.view.stop()
        if pending.message is not None:
            await pending.message.edit(content=text, view=pending.view)
    except Exception:
        logger.warning("Discord: could not finalise a gate message", exc_info=True)
