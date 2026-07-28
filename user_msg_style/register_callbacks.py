"""Restyles the echo of YOUR OWN message -- color, attributes and prefix -- without touching the agent's output. Three keys, all set with /set and all re-read on every render, so a change applies to your NEXT message with no restart. Unset = stock Code Puppy behavior. ......... [1] /set user_msg_color=VALUE -- the foreground. Four notations: hex #c5fc7d | name cyan, bright_magenta, dark_sea_green, grey54, red | palette color(42) (0-255) | rgb(197,252,125). Also 'default' (your terminal's own foreground); case is ignored. NOT accepted here: a background like 'on red' -- that belongs in user_msg_style. ......... [2] /set user_msg_style=VALUE -- attributes, space-separated. All thirteen: bold, dim, italic, underline, blink, blink2, reverse, strike, underline2, frame, encircle, overline, conceal. Safe nearly everywhere: bold, dim, italic, underline, reverse, strike -- the rest are emitted correctly but many terminals ignore them. Combine freely: 'bold italic', 'dim underline'. Empty means no attributes, color only. Negate with not: 'not bold italic'. BACKGROUNDS live here via Rich's on-syntax: 'bold on #303030', 'on blue'. A foreground put here works too but LOSES to user_msg_color (both end up in one style string and Rich takes the last color). ......... [3] /set user_msg_prefix=VALUE -- the text before your message, default "> ". Any text: "| ", "-> ", "you: ", or "" for none. QUOTE IT -- puppy.cfg strips whitespace, so an unquoted "> " loses its trailing space and your text sticks to the marker. The leading newline is layout and is not configurable. ......... Bad values never break anything: they fall back (style to bold, color to the prompt color, i.e. what the core would show) and warn ONCE, never per message. Full reference incl. worked examples: README.md next to this file.

The first paragraph above is deliberately ONE long line, and it is long on
purpose: the /plugins TUI is the ONLY place a user discovers this plugin's
settings, and the description is the only field we can fill. It reads no
README, and config keys are not one of its "Contributes" categories.

Format constraints, measured (``plugin_list/plugin_meta.py:38-41``):
the TUI takes the FIRST paragraph and collapses newlines into spaces
(``" ".join(line.strip() for line in first_para.splitlines())``), so a
blank line TRUNCATES everything after it and manual line breaks are lost.
Lists, tables and paragraphs are therefore impossible -- the "........."
runs are the only available visual separator, and the pane wraps the rest
itself (``plugins_menu_render.py:284-287``).

Restyles the echo of the USER'S OWN message -- color, attributes, prefix --
via three ``puppy.cfg`` keys, with no change to the Code Puppy core.

Why a monkey-patch and not a hook: the closest existing hook,
``prompt_text_color``, is a SHARED channel. It also colors the agent
response (``rich_renderer.py:1010-1012``), the input line
(``prompt_toolkit_completion.py:998-999``), the bottom bar
(``bar_painters.py:88-96``) and the streaming writer
(``event_stream_handler.py:209``), so using it would repaint far more than
the user's message. The echo itself is built in exactly one place,
``cli_runner._prompt_echo_text`` (``cli_runner.py:603-611``), and both call
sites (``cli_runner.py:866`` and ``:878``) resolve it through the module
namespace -- so rebinding that attribute changes the echo and nothing else.

WHEN the patch is installed is the critical detail. Plugins are loaded at
``cli_runner.py:53``, DURING the import of ``cli_runner`` and 550 lines
BEFORE ``_prompt_echo_text`` exists at line 603. Installing at module scope
would therefore silently do nothing. The ``startup`` hook fires later, at
``cli_runner.py:403``, when the module is complete -- so the patch goes
there. This mirrors the in-repo precedent, ``plugins/prompt_newline``.

Config keys, all read PER RENDER so ``/set`` takes effect on the next
message with no restart:

===================  ================================  =================
Key                  Meaning                           Default
===================  ================================  =================
``user_msg_color``   Rich color (``cyan``, ``#7dd3fc``) inherit prompt
``user_msg_style``   Rich attributes (``bold italic``)  ``bold``
``user_msg_prefix``  Literal prefix                     ``"> "``
===================  ================================  =================
"""

from __future__ import annotations

from typing import Any, Optional, Set

from code_puppy.callbacks import register_callback

from user_msg_style.style_builder import build_echo

#: Where the untouched original is parked, and the idempotency sentinel.
_PATCH_ATTR = "_user_msg_style_original"

#: The plugin's own name, as the loader derives it from the directory
#: (``plugins/__init__.py:135``: ``plugin_name = item.name``). This is the
#: key ``/plugins disable`` writes, so it must stay in sync with the folder.
PLUGIN_NAME = "user_msg_style"

#: Returned by :func:`_extract_task` for a call shape this plugin does not
#: understand. A dedicated sentinel, because ``None`` is a legal argument.
_UNKNOWN_CALL = object()

#: Warnings already surfaced, so a bad config key complains once, not once
#: per message. Bad config is persistent -- repeating it every render would
#: bury the transcript the plugin exists to make readable.
_WARNED: Set[str] = set()


def _reset_warning_dedupe() -> None:
    """Clear the warn-once memory (used by tests)."""
    _WARNED.clear()


def _emit_error(message: str) -> None:
    from code_puppy.messaging import emit_error

    emit_error(message)


def _emit_warning(message: str) -> None:
    from code_puppy.messaging import emit_warning

    emit_warning(message)


def _import_cli_runner():
    """Return the ``code_puppy.cli_runner`` module (own seam for testing)."""
    from code_puppy import cli_runner

    return cli_runner


def _get_value(key: str) -> Any:
    from code_puppy.config import get_value

    return get_value(key)


def _prompt_color() -> Optional[str]:
    from code_puppy.callbacks import on_prompt_text_color

    return on_prompt_text_color()


def _is_disabled() -> bool:
    """True if the user switched this plugin off at runtime.

    The core's disable only filters CALLBACK DISPATCH
    (``callbacks.py:228-243``), while ``plugins/config.py:5-9`` promises the
    user that "toggling takes effect immediately without a restart". This
    plugin does not work through a callback at render time: it REBINDS
    ``cli_runner._prompt_echo_text`` once at startup, and a rebinding is
    invisible to callback filtering -- the core would keep calling the
    replacement forever. The gate therefore has to sit INSIDE the patched
    function; there is nowhere else it could take effect.

    No in-repo precedent for this: grepping ``code_puppy/plugins`` for
    ``is_plugin_disabled`` finds only its definition (``config.py:69``) and
    the project-tier loader's LOAD-TIME gate (``__init__.py:408``, which
    skips loading a disabled project plugin outright). No PLUGIN gates on it
    at render time -- this one is the first. The other monkey-patching
    plugins have no equivalent check (``prompt_newline:76-79`` gates on its
    OWN feature toggle; ``context_indicator:58-63`` is a "nothing to render"
    guard, ``if not info`` / ``if usage is None``).

    Any failure to answer the question is read as "not disabled": an
    unanswerable config lookup must not cost the user their render.
    """
    try:
        from code_puppy.plugins.config import is_plugin_disabled

        return bool(is_plugin_disabled(PLUGIN_NAME))
    except Exception:
        return False


def _extract_task(args: tuple, kwargs: dict) -> Any:
    """Return the message argument, or :data:`_UNKNOWN_CALL` if unsure.

    The patched function must not pin today's core signature. Binding a
    fixed parameter list raises ``TypeError`` at CALL time -- before any
    ``try`` inside the body can help -- and the REPL loop
    (``cli_runner.py:903-914``) catches only ``KeyboardInterrupt``,
    ``asyncio.CancelledError`` and ``EOFError``, so such a ``TypeError``
    would escape and kill the process on every message. Accepting anything
    and recognising only the shape we can style keeps an added core
    parameter a no-op instead of a crash.
    """
    if not kwargs and len(args) == 1:
        return args[0]
    if not args and tuple(kwargs) == ("task",):
        return kwargs["task"]
    return _UNKNOWN_CALL


def _warn_once(message: str) -> None:
    if message in _WARNED:
        return
    _WARNED.add(message)
    try:
        _emit_warning(f"user_msg_style: {message}")
    except Exception:
        # The message queue is not worth a crashed render.
        pass


def _make_patched(original):
    """Build the replacement for ``cli_runner._prompt_echo_text``.

    *original* is captured so that ANY failure -- a broken config file, a
    Rich version that rejects something, a missing callback -- degrades to
    today's exact output instead of taking the REPL down with it.
    """

    def patched(*args, **kwargs):
        if _is_disabled():
            return original(*args, **kwargs)

        task = _extract_task(args, kwargs)
        if task is _UNKNOWN_CALL:
            return original(*args, **kwargs)

        try:
            result = build_echo(
                task,
                prefix=_get_value("user_msg_prefix"),
                attrs=_get_value("user_msg_style"),
                color=_get_value("user_msg_color"),
                prompt_color=_prompt_color(),
            )
        except Exception as exc:
            _warn_once(f"could not build the styled echo ({exc}); using the default")
            return original(*args, **kwargs)

        if result.warning:
            _warn_once(result.warning)
        return result.text

    return patched


def _install_patch() -> None:
    """Rebind ``cli_runner._prompt_echo_text``. Idempotent.

    Raises:
        AttributeError: if the target is missing -- i.e. the core renamed or
            removed it. The caller turns that into a message; the REPL keeps
            its original behavior.
    """
    cli_runner = _import_cli_runner()

    if getattr(cli_runner, _PATCH_ATTR, None) is not None:
        return  # Already patched

    original = getattr(cli_runner, "_prompt_echo_text", None)
    if original is None:
        raise AttributeError(
            "code_puppy.cli_runner._prompt_echo_text is missing -- the core "
            "may have renamed it"
        )

    setattr(cli_runner, _PATCH_ATTR, original)
    cli_runner._prompt_echo_text = _make_patched(original)


def _on_startup() -> None:
    try:
        _install_patch()
    except Exception as exc:
        # Plugins must fail gracefully -- never crash the app.
        _emit_error(f"user_msg_style: failed to install the echo patch -- {exc}")


# Registered at module scope; INSTALLED in the callback (see the module
# docstring -- at import time the target does not exist yet).
register_callback("startup", _on_startup)


__all__ = ["PLUGIN_NAME", "_install_patch", "_on_startup"]
