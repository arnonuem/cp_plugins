"""Turn Code Puppy's internal messages into Discord-ready text.

Pure functions only: no session state, no Discord objects, no I/O.  Splitting
them out of :mod:`.output` keeps the router about *where* text goes and this
module about *what* it looks like -- and makes the wording testable without a
gateway.

Rendering choices worth knowing:

* shell output is fenced, everything else is plain -- a code block inside a
  code block is unreadable, and :mod:`.chunking` has to reason about fences;
* ANSI escapes are stripped: Discord shows them as literal ``ESC[0m`` noise;
* a ``ShellOutputMessage`` always shows its exit code, even when both streams
  are empty.  "It ran and returned 0" is information; silence is not.
"""

from __future__ import annotations

import re
from typing import Any

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

#: Longest tool/prompt fragment we inline before truncating.
_SUMMARY_LIMIT = 120

#: Tool argument names worth showing, most informative first.
_SUMMARY_KEYS = ("command", "path", "file_path", "directory", "agent_name", "query")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences, which Discord renders as noise."""
    return _ANSI_RE.sub("", text or "")


def fenced(text: str) -> str:
    return f"```\n{text}\n```"


def _short(value: Any) -> str:
    return str(value).replace("\n", " ").strip()[:_SUMMARY_LIMIT]


def stream_text(event_type: str, event_data: Any) -> str:
    """Visible assistant text in a stream event; thinking is not forwarded.

    ``part_start`` carries the opening content and ``part_delta`` the
    additions.  The two are disjoint, so forwarding both reconstructs the
    message exactly once -- forwarding only deltas drops the first token
    whenever the model front-loads it into the start event.
    """
    data = event_data if isinstance(event_data, dict) else {}
    if event_type == "part_start":
        if "Thinking" in (data.get("part_type") or ""):
            return ""
        return getattr(data.get("part"), "content", "") or ""
    if event_type == "part_delta":
        if "Thinking" in (data.get("delta_type") or ""):
            return ""
        return getattr(data.get("delta"), "content_delta", "") or ""
    return ""


def tool_start_text(tool_name: str, tool_args: Any) -> str:
    """``-> read_file . x.py`` -- what the tool is about to do."""
    summary = ""
    if isinstance(tool_args, dict):
        for key in _SUMMARY_KEYS:
            if tool_args.get(key):
                summary = _short(tool_args[key])
                break
    return f"-> {tool_name} ({summary})" if summary else f"-> {tool_name}"


def tool_end_text(tool_name: str, duration_ms: float) -> str:
    return f"<- {tool_name} ({duration_ms:.0f} ms)"


def bus_message_text(message: Any) -> str:
    """One Discord-ready block for a structured message, or ``""`` to skip."""
    from code_puppy.messaging.messages import (
        DiffMessage,
        ShellLineMessage,
        ShellOutputMessage,
        ShellStartMessage,
        SubAgentInvocationMessage,
        TextMessage,
    )

    if isinstance(message, ShellOutputMessage):
        # The single most useful artefact of a shell run: the summarised
        # streams together with the exit code (AC-54).
        body = "\n".join(
            part
            for part in (strip_ansi(message.stdout), strip_ansi(message.stderr))
            if part.strip()
        )
        head = f"$ {message.command}"
        tail = f"exit {message.exit_code} ({message.duration_seconds:.2f}s)"
        return f"{head}\n{fenced(body)}\n{tail}" if body else f"{head}\n{tail}"
    if isinstance(message, ShellStartMessage):
        return f"$ {message.command}"
    if isinstance(message, ShellLineMessage):
        line = strip_ansi(message.line)
        return f"! {line}" if message.stream == "stderr" else line
    if isinstance(message, DiffMessage):
        adds = sum(1 for d in message.diff_lines if d.type == "add")
        removes = sum(1 for d in message.diff_lines if d.type == "remove")
        return f"diff: {message.path} ({message.operation}, +{adds}/-{removes})"
    if isinstance(message, SubAgentInvocationMessage):
        return f"invoke_agent: {message.agent_name} - {_short(message.prompt)}"
    if isinstance(message, TextMessage):
        return f"[{message.level.value}] {message.text}"
    return ""


def legacy_message_text(message: Any) -> str:
    """Render a legacy queue entry, which carries no session information."""
    content = getattr(message, "content", None)
    if content is None:
        return ""
    kind = getattr(getattr(message, "type", None), "value", "info")
    return f"[{kind}] {strip_ansi(str(content))}"


def result_text(result: Any) -> str:
    """Display text of a pydantic-ai run result: ``output``, then ``data``."""
    for attribute in ("output", "data"):
        value = getattr(result, attribute, None)
        if value:
            return str(value)
    return ""
