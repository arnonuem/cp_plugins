"""Unit tests for the pure render logic (no code_puppy import required).

Acceptance criteria covered here: AC-1, AC-3, AC-4, AC-6, AC-7.
"""

from io import StringIO

import pytest
from rich.console import Console
from rich.text import Text

from user_msg_style.style_builder import (
    DEFAULT_ATTRS,
    DEFAULT_PREFIX,
    build_echo,
)


# --------------------------------------------------------------------------
# AC-1: with nothing configured the output is byte-identical to the core
#       implementation at cli_runner.py:603-611:
#           style = f"bold {prompt_color}" if prompt_color else "bold"
#           return Text(f"\n> {task}", style=style)
# --------------------------------------------------------------------------


def _core_reference(task: str, prompt_color: str | None) -> Text:
    """Verbatim copy of cli_runner.py:609-611 (the behavior we must preserve)."""
    style = f"bold {prompt_color}" if prompt_color else "bold"
    return Text(f"\n> {task}", style=style)


def test_ac1_defaults_match_core_without_prompt_color():
    reference = _core_reference("hello world", None)
    result = build_echo("hello world")

    assert result.warning is None
    assert result.text.plain == reference.plain
    assert str(result.text.style) == str(reference.style)
    assert result.text.plain == "\n> hello world"
    assert str(result.text.style) == "bold"


def test_ac1_defaults_match_core_with_prompt_color():
    reference = _core_reference("hi", "#7dd3fc")
    result = build_echo("hi", prompt_color="#7dd3fc")

    assert result.text.plain == reference.plain
    assert str(result.text.style) == str(reference.style)
    assert str(result.text.style) == "bold #7dd3fc"


def test_ac1_defaults_are_the_documented_constants():
    assert DEFAULT_PREFIX == "> "
    assert DEFAULT_ATTRS == "bold"


def test_ac1_blank_config_values_are_treated_as_unset():
    # configparser hands back whitespace-only strings for `key =    `.
    result = build_echo("hi", prefix=None, attrs="   ", color="  ")
    assert result.text.plain == "\n> hi"
    assert str(result.text.style) == "bold"
    assert result.warning is None


# --------------------------------------------------------------------------
# R2-2: AC-1 promises BYTE-IDENTICAL output, and "output" means what the
# terminal receives -- not the style string. Rich resolves a style only at
# RENDER time and silently discards one it cannot parse, so two different
# style strings can render identically and, worse, two renders can differ
# while the style strings look fine. Comparing style strings is therefore
# blind to exactly the class of divergence R2-1 was.
# Measured: Text("\n> hi", style="bold alsobad") renders as '\n> hi\n'
# (Rich drops the WHOLE style, not just the bad color), while style="bold"
# renders as '\n\x1b[1m> hi\x1b[0m\n'.
# --------------------------------------------------------------------------


def _render(text: Text) -> str:
    """Render *text* to the exact bytes a truecolor terminal would receive."""
    console = Console(
        file=StringIO(),
        force_terminal=True,
        color_system="truecolor",
        width=200,
    )
    console.print(text)
    return console.file.getvalue()


@pytest.mark.parametrize(
    "prompt_color",
    [None, "cyan", "#7dd3fc", "alsobad", ""],
    ids=["no-ambient", "named", "hex", "invalid-ambient", "empty-ambient"],
)
@pytest.mark.parametrize(
    "task",
    ["hello world", "", "[bold]not markup[/bold]", "line one\nline two", "   "],
    ids=["plain", "empty", "markup-looking", "multi-line", "whitespace"],
)
def test_r2_2_unconfigured_render_is_byte_identical_to_the_core(task, prompt_color):
    result = build_echo(task, prompt_color=prompt_color)

    assert _render(result.text) == _render(_core_reference(task, prompt_color))


def test_r2_2_an_invalid_ambient_color_alone_stays_silent():
    """An unconfigured install must not complain about the AMBIENT color.

    The core does not validate it -- it passes the value to Rich, which
    discards it. Warning here would name ``user_msg_color``, a key the user
    never set, and would fire on a perfectly working (if oddly themed)
    install. AC-1: installing without configuring changes NOTHING.
    """
    result = build_echo("hi", prompt_color="alsobad")

    assert result.warning is None


# --------------------------------------------------------------------------
# AC-2 support: an explicit color overrides the SHARED prompt color, so the
# user message can differ from the agent response without touching the
# prompt_text_color channel.
# --------------------------------------------------------------------------


def test_ac2_explicit_color_overrides_prompt_color():
    result = build_echo("hi", color="magenta", prompt_color="#7dd3fc")
    assert str(result.text.style) == "bold magenta"


def test_ac2_user_msg_color_wins_over_a_color_in_user_msg_style():
    # Rich lets the LAST color in a style string win, and the color is
    # appended after the attributes -- so the dedicated key stays in charge.
    from rich.style import Style

    result = build_echo("hi", attrs="red", color="cyan")
    assert str(Style.parse(str(result.text.style)).color.name) == "cyan"


# --------------------------------------------------------------------------
# AC-3: attributes
# --------------------------------------------------------------------------


def test_ac3_attrs_replace_the_default_bold():
    assert str(build_echo("hi", attrs="bold italic").text.style) == "bold italic"
    assert str(build_echo("hi", attrs="dim").text.style) == "dim"
    assert str(build_echo("hi", attrs="italic").text.style) == "italic"


def test_ac3_attrs_combine_with_color():
    result = build_echo("hi", attrs="dim italic", color="#7dd3fc")
    assert str(result.text.style) == "dim italic #7dd3fc"


def test_ac3_empty_attrs_means_no_attributes():
    # Explicitly set to empty (quoted) -> plain text, only the color applies.
    result = build_echo("hi", attrs='""', color="cyan")
    assert str(result.text.style) == "cyan"
    assert result.warning is None


# --------------------------------------------------------------------------
# AC-4: prefix
# --------------------------------------------------------------------------


def test_ac4_prefix_replaces_the_default_and_newline_survives():
    result = build_echo("hi", prefix='"| "')
    assert result.text.plain == "\n| hi"


def test_ac4_prefix_may_be_emptied():
    result = build_echo("hi", prefix='""')
    assert result.text.plain == "\nhi"


def test_ac4_unquoted_prefix_is_used_verbatim():
    result = build_echo("hi", prefix="user:")
    assert result.text.plain == "\nuser:hi"


def test_ac4_leading_newline_is_never_configurable():
    # Even a prefix that itself contains newlines keeps the leading one.
    result = build_echo("hi", prefix='"\n>> "')
    assert result.text.plain.startswith("\n")
    assert result.text.plain == "\n\n>> hi"


# --------------------------------------------------------------------------
# AC-6: garbage in -> default out, never an exception
# --------------------------------------------------------------------------


def test_ac6_invalid_color_falls_back_and_warns():
    result = build_echo("hi", color="notacolor")
    assert str(result.text.style) == "bold"
    assert result.warning is not None
    assert "notacolor" in result.warning


def test_ac6_invalid_attrs_fall_back_to_default_attrs():
    result = build_echo("hi", attrs="zzz", color="cyan")
    assert str(result.text.style) == "bold cyan"
    assert result.warning is not None
    assert "zzz" in result.warning


def test_ac6_both_invalid_falls_back_to_plain_bold():
    result = build_echo("hi", attrs="zzz", color="alsobad")
    assert str(result.text.style) == "bold"
    assert result.warning is not None


def test_ac6_invalid_color_does_not_discard_valid_attrs():
    result = build_echo("hi", attrs="dim", color="notacolor")
    assert str(result.text.style) == "dim"


# --------------------------------------------------------------------------
# R1-3: an invalid user_msg_color must degrade to the CORE's output, which
# means keeping the ambient prompt color -- not dropping all color.
# --------------------------------------------------------------------------


def test_r1_3_invalid_color_falls_back_to_the_ambient_prompt_color():
    reference = _core_reference("hi", "#7dd3fc")
    result = build_echo("hi", color="notacolor", prompt_color="#7dd3fc")

    assert str(result.text.style) == str(reference.style)
    assert str(result.text.style) == "bold #7dd3fc"


def test_r1_3_the_warning_names_the_color_actually_used():
    result = build_echo("hi", color="notacolor", prompt_color="#7dd3fc")

    assert result.warning is not None
    assert "notacolor" in result.warning
    assert "#7dd3fc" in result.warning


def test_r1_3_invalid_color_keeps_ambient_and_custom_attrs():
    result = build_echo("hi", attrs="dim italic", color="zzz", prompt_color="cyan")
    assert str(result.text.style) == "dim italic cyan"


def test_r1_3_invalid_ambient_leaves_no_color_and_warns_once():
    result = build_echo("hi", color="notacolor", prompt_color="alsobad")

    assert str(result.text.style) == "bold"
    assert result.warning is not None
    # One render, one complaint about the color -- not two.
    assert result.warning.count("is not a valid Rich color") == 1
    assert "alsobad" not in result.warning


def test_r2_1_invalid_ambient_alone_is_passed_through_like_the_core():
    """Inverted in round 2 (was: reported as the prompt color).

    The old contract validated the ambient color even when the user had set
    NO plugin key, and so broke AC-1 in one branch: it rendered without the
    ``bold`` the core still shows, and complained about ``user_msg_color``.
    Validation of the ambient color survives only where the user DID set
    ``user_msg_color`` -- see the R1-3 fallback tests above.
    """
    reference = _core_reference("hi", "alsobad")
    result = build_echo("hi", prompt_color="alsobad")

    assert str(result.text.style) == str(reference.style)
    assert str(result.text.style) == "bold alsobad"
    assert result.warning is None


def test_ac6_non_string_config_values_do_not_raise():
    result = build_echo("hi", prefix=object(), attrs=42, color=[1, 2])
    assert result.text.plain == "\n> hi"
    assert str(result.text.style) == "bold"


def test_ac6_non_string_task_is_coerced():
    result = build_echo(12345)
    assert result.text.plain == "\n> 12345"


# --------------------------------------------------------------------------
# AC-7: Rich markup in user input must NOT be interpreted
#       (preserves the deliberate core behavior at cli_runner.py:872-877)
# --------------------------------------------------------------------------


def test_ac7_markup_in_input_stays_literal():
    result = build_echo("[bold]not markup[/bold]", color="cyan")
    assert result.text.plain == "\n> [bold]not markup[/bold]"
    # A markup-parsed Text would carry extra spans; a styled Text carries none.
    assert result.text.spans == []


def test_ac7_markup_in_prefix_stays_literal():
    result = build_echo("hi", prefix='"[red]> "')
    assert result.text.plain == "\n[red]> hi"
    assert result.text.spans == []


def test_ac7_unbalanced_brackets_do_not_raise():
    result = build_echo("[/nope")
    assert result.text.plain == "\n> [/nope"
