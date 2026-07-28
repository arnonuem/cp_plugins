"""Pure render logic for the user_msg_style plugin.

This module deliberately imports NOTHING from ``code_puppy`` -- it is a
plain function over strings, so it can be unit-tested standalone and it
cannot drag the core into an import cycle at plugin-load time.

The contract it must preserve is the core's own echo builder
(``code_puppy/cli_runner.py:603-611``)::

    style = f"bold {prompt_color}" if prompt_color else "bold"
    return Text(f"\\n> {task}", style=style)

With no configuration supplied, :func:`build_echo` reproduces that output
byte for byte.

Two behaviors are load-bearing and must not be "simplified" away:

* the leading ``\\n`` is LAYOUT, not a prefix -- it is always emitted and is
  not configurable, otherwise the echo glues itself to the previous output;
* the text is built with ``rich.text.Text``, never Rich markup, so square
  brackets typed by the user stay literal (the core explains this choice at
  ``cli_runner.py:872-877``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from rich.color import Color
from rich.style import Style
from rich.text import Text

#: Prefix used by the core today (``cli_runner.py:611``).
DEFAULT_PREFIX = "> "

#: Attributes used by the core today (``cli_runner.py:610``).
DEFAULT_ATTRS = "bold"

#: The newline is layout, not style -- always present, never configurable.
_LEADING_NEWLINE = "\n"

__all__ = [
    "DEFAULT_ATTRS",
    "DEFAULT_PREFIX",
    "EchoResult",
    "build_echo",
    "unquote",
]


@dataclass(frozen=True)
class EchoResult:
    """A rendered echo plus an optional one-line complaint about the config.

    The warning is data, not a side effect: this module has no messaging
    channel, so the caller decides whether (and how often) to surface it.
    """

    text: Text
    warning: Optional[str] = None


def unquote(value: str) -> str:
    """Strip one layer of matching quotes from a config value.

    ``puppy.cfg`` is a ``configparser`` file, and ``configparser`` strips
    surrounding whitespace on read -- verified: writing ``"> "`` and reading
    it back yields ``">"``. Quoting is therefore the ONLY way to express a
    trailing space (or an intentionally empty value) in a config key, so
    ``/set user_msg_prefix="| "`` must survive the round trip intact.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _as_text(value: Any) -> Optional[str]:
    """Return ``value`` as a config string, or ``None`` if it carries nothing.

    Anything that is not a string is treated as absent rather than coerced:
    a non-string here means the config layer handed us something unexpected,
    and guessing at its meaning is worse than falling back to the default.
    """
    if not isinstance(value, str):
        return None
    return value if value.strip() else None


def _resolve(raw: Any, default: str) -> str:
    """Resolve one config value, honoring quotes and falling back to *default*."""
    text = _as_text(raw)
    if text is None:
        return default
    return unquote(text.strip())


def _is_valid_color(value: str) -> bool:
    try:
        Color.parse(value)
    except Exception:
        return False
    return True


def _is_valid_attrs(value: str) -> bool:
    """True if *value* parses as a Rich style definition.

    Colors are deliberately NOT rejected here. ``Style.parse`` accepts them,
    so ``user_msg_style=red`` works -- and because :func:`_compose_style`
    appends the color AFTER the attributes, and Rich lets the last color in
    a style string win (verified: ``Style.parse("red cyan").color`` is
    ``cyan``), ``user_msg_color`` still takes precedence. Rejecting the
    value would break a harmless usage for no gain.
    """
    if not value:
        return True
    try:
        Style.parse(value)
    except Exception:
        return False
    return True


def _resolve_color(raw: Any, prompt_color: Any) -> Tuple[str, Optional[str]]:
    """Resolve the foreground color and describe any problem found.

    Precedence is ``user_msg_color`` -> the ambient prompt color -> no color.
    An invalid ``user_msg_color`` therefore degrades to the color the CORE
    would have used, not to nothing -- otherwise a themed prompt would lose
    its color over an unrelated typo. Only an ambient color that is itself
    unusable leaves the style colorless.

    The ambient color is validated ONLY as a fallback candidate, never on
    its own. With no ``user_msg_color`` set, this plugin must be invisible
    (AC-1), so an unparseable prompt color is passed straight through the
    way the core passes it through -- Rich discards it at render time (it
    drops the WHOLE style, so ``bold`` goes with it, and reproducing that
    exactly is the point). Rejecting it here would both diverge from the
    core's rendering and print a complaint naming ``user_msg_color``, a key
    the user never set.

    Returns:
        ``(color, complaint)``. At most ONE complaint is produced per render:
        it names the value that was actually rejected, so the message never
        blames a key the user did not set.
    """
    ambient_raw = _as_text(prompt_color)
    ambient = ambient_raw.strip() if ambient_raw else ""

    explicit = _resolve(raw, "")
    if not explicit:
        return ambient, None

    if _is_valid_color(explicit):
        return explicit, None

    if ambient and _is_valid_color(ambient):
        return ambient, (
            f"user_msg_color={explicit!r} is not a valid Rich color; "
            f"falling back to the prompt color {ambient!r}"
        )
    return "", (
        f"user_msg_color={explicit!r} is not a valid Rich color; "
        "falling back to the default"
    )


def _compose_style(attrs: str, color: str) -> str:
    """Join attributes and color the way the core does: ``"<attrs> <color>"``."""
    parts: List[str] = [part for part in (attrs, color) if part]
    return " ".join(parts)


def build_echo(
    task: Any,
    *,
    prefix: Any = None,
    attrs: Any = None,
    color: Any = None,
    prompt_color: Any = None,
) -> EchoResult:
    """Build the styled echo of the user's own message.

    Args:
        task: The user's message. Coerced with ``str`` so a non-string
            payload can never break the render path.
        prefix: ``user_msg_prefix``; unset falls back to ``"> "``.
        attrs: ``user_msg_style``; unset falls back to ``"bold"``.
        color: ``user_msg_color``; unset falls back to *prompt_color*,
            which is what the core uses today.
        prompt_color: The ambient prompt foreground
            (``callbacks.on_prompt_text_color()``).

    Returns:
        An :class:`EchoResult`. Invalid values never raise -- they degrade
        to the corresponding default and are reported in ``warning``.
    """
    complaints: List[str] = []

    resolved_prefix = _resolve(prefix, DEFAULT_PREFIX)

    resolved_attrs = _resolve(attrs, DEFAULT_ATTRS)
    if not _is_valid_attrs(resolved_attrs):
        complaints.append(
            f"user_msg_style={resolved_attrs!r} is not a valid Rich style; "
            f"using {DEFAULT_ATTRS!r}"
        )
        resolved_attrs = DEFAULT_ATTRS

    resolved_color, color_complaint = _resolve_color(color, prompt_color)
    if color_complaint:
        complaints.append(color_complaint)

    style = _compose_style(resolved_attrs, resolved_color)
    text = Text(f"{_LEADING_NEWLINE}{resolved_prefix}{task}", style=style)
    return EchoResult(text=text, warning="; ".join(complaints) or None)
