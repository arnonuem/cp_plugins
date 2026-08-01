"""C4b — the terminal branch: our own approval prompt, on a worker thread.

**Why we build an Application at all instead of calling the core helper.**
``arrow_select_async`` (``common.py:1027-1105``) builds its ``Application``
internally (``:1083``), takes no ``pre_run`` and returns no handle.  §5.2a
needs both: a point at which the app is demonstrably operable (that is where
the mark goes ``PENDING -> LIVE`` and the late-resolution check runs), and a
reference to call ``exit(result=...)`` on from the Discord branch.  Neither is
reachable through the helper, so the SHELL is rebuilt here -- and only the
shell.  Everything with behaviour in it is reused:

* ``_format_selector`` for the rendering,
* ``on_prompt_toolkit_style()`` for the style,
* ``suspended_key_listener()`` for stdin ownership,
* ``suspended_run_ui()`` for the renderer,
* the same key bindings (up/down/``c-p``/``c-n``/enter/``c-c``) and the same
  ``KeyboardInterrupt`` on cancel.

**``suspended_run_ui()`` is our job, not the core's** (R9-W4).  Unlike
``suspended_key_listener()`` -- which sits INSIDE ``arrow_select_async``
(``common.py:1099``) and would come along with it -- the core sets
``suspended_run_ui()`` up in the approval body, AFTER the backend early-return
(async ``:1503`` after ``:1442-1445``; sync ``:1304`` after ``:1246``).  We
stand before that.  The core assigns the duty explicitly to whoever takes the
terminal (``command_runner.py:314-317``); without it our Application and the
bottom bar write to the same terminal at the same time.

**Measured, not assumed** (SPIKE-R1 follow-up, W4's first task): a prompt runs
on a private loop in a worker thread while the main thread stays responsive,
``pre_run`` fires with ``is_running`` already true and ``future`` already set,
and ``exit(result=...)`` from a FOREIGN thread substitutes the result rather
than raising ``KeyboardInterrupt``.  That last one is AC-48: a bare abort
would mean "denied" even though Discord said approve.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

APPROVE_CHOICE = " Approve"
REJECT_CHOICE = " Reject"

#: The two answers our prompt offers.  Deliberately NOT the core's three: the
#: third is "reject with feedback", and feedback has no path back through a
#: Discord gate -- offering it here would promise something the other branch
#: cannot keep.
CHOICES = (APPROVE_CHOICE, REJECT_CHOICE)

#: "This branch produced no answer."  A distinct object rather than ``False``:
#: a prompt closed because the gate was already resolved has not REJECTED
#: anything, and returning ``False`` there would be a denial nobody uttered.
_NO_ANSWER = object()


class TerminalPrompt:
    """One approval prompt: built here, answered by a human or by Discord.

    *on_live* is called the moment the Application is genuinely operable and
    answers whether the prompt may go on.  ``False`` means the gate was
    resolved while we were building, and the app is closed again before a
    single key can reach it (§5.2a step 3a, AC-64b).
    """

    def __init__(self, title: str, message: str, preview: Optional[str] = None) -> None:
        self.title = title
        self.message = message
        self.preview = preview
        self._lock = threading.Lock()
        self._app: Optional[Any] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- what the Discord branch calls (from a FOREIGN thread) ----------

    def exit_with(self, approved: bool) -> bool:
        """Substitute the result and end the prompt.  Never raises.

        ``exit(result=...)`` rather than ``exit()``: an abort surfaces as
        ``KeyboardInterrupt`` and would be read as a rejection -- denied
        despite an approval on the phone (AC-48).
        """
        with self._lock:
            app, loop = self._app, self._loop
        if app is None or loop is None:
            return False
        try:
            loop.call_soon_threadsafe(self._exit_on_loop, app, approved)
        except RuntimeError:
            logger.debug("cp_discord: the prompt loop is gone", exc_info=True)
            return False
        return True

    @staticmethod
    def _exit_on_loop(app: Any, result: Any) -> None:
        # Guarded because the human may have answered in the meantime: the
        # future is then already resolved and ``exit`` raises.
        try:
            if app.is_running and app.future is not None and not app.future.done():
                app.exit(result=result)
        except Exception:
            logger.debug("cp_discord: could not end the prompt", exc_info=True)

    # -- what the backend calls (on the executor thread) ----------------

    def run(self, *, on_live: Callable[[], bool]) -> Optional[bool]:
        """Show the prompt and return the answer, or ``None`` if there is none.

        ``None`` means "this branch produced no winner": either the gate was
        already resolved when we went live, or the human pressed Ctrl+C.  It
        is NOT a rejection -- fail-closed is the backend's decision to make,
        once both branches are done (INV-C7, AC-33).
        """
        from code_puppy.messaging.run_ui import suspended_run_ui

        _flush()
        with suspended_run_ui():
            return asyncio.run(self._run_async(on_live))

    async def _run_async(self, on_live: Callable[[], bool]) -> Optional[bool]:
        from code_puppy.agents._key_listeners import suspended_key_listener

        app = self._build()
        with self._lock:
            self._app = app
            self._loop = asyncio.get_running_loop()

        def pre_run() -> None:
            # The app is operable here (measured: ``is_running`` is already
            # true and ``future`` is already set), so this is the only point
            # at which SS5.2a step 3a can run without a window.
            if not on_live():
                # NOT ``False``: closing an already-resolved gate's prompt is
                # "no answer from this branch", and answering ``False`` would
                # be a rejection nobody uttered.
                self._exit_on_loop(app, _NO_ANSWER)

        try:
            with suspended_key_listener():
                answer = await app.run_async(pre_run=pre_run)
        except KeyboardInterrupt:
            return None
        finally:
            with self._lock:
                self._app = None
                self._loop = None

        if answer is _NO_ANSWER:
            return None
        if isinstance(answer, bool):
            return answer
        if answer == APPROVE_CHOICE:
            return True
        if answer == REJECT_CHOICE:
            return False
        return None

    # -- the shell ------------------------------------------------------

    def _build(self) -> Any:
        from prompt_toolkit.application import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        from code_puppy.callbacks import on_prompt_toolkit_style
        from code_puppy.tools.common import _format_selector

        selected = [0]
        prompt_text = self._header()

        def formatted():
            return _format_selector(prompt_text, list(CHOICES), selected[0])

        bindings = KeyBindings()

        @bindings.add("up")
        @bindings.add("c-p")
        def _up(event):
            selected[0] = (selected[0] - 1) % len(CHOICES)
            event.app.invalidate()

        @bindings.add("down")
        @bindings.add("c-n")
        def _down(event):
            selected[0] = (selected[0] + 1) % len(CHOICES)
            event.app.invalidate()

        @bindings.add("enter")
        def _accept(event):
            event.app.exit(result=CHOICES[selected[0]])

        @bindings.add("c-c")
        def _cancel(event):
            # Same semantics as the core helper: a cancel is an EXCEPTION, not
            # a rejection, so nobody can mistake "I did not answer" for "no".
            event.app.exit(exception=KeyboardInterrupt)

        return Application(
            layout=Layout(Window(content=FormattedTextControl(formatted))),
            key_bindings=bindings,
            full_screen=False,
            style=on_prompt_toolkit_style(),
        )

    def _header(self) -> str:
        parts = [f"{self.title}: {self.message}".strip()]
        if self.preview:
            parts.append(self.preview)
        parts.append(" What would you like to do?")
        return "\n\n".join(part for part in parts if part)


def _flush() -> None:
    """Empty the streams before prompt_toolkit takes the terminal over."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass


def stdin_is_interactive() -> bool:
    """Whether stdin can host a prompt at all (INV-C19's stdin clause).

    The core checks this itself before prompting
    (``_stdin_supports_interactive_approval``, ``common.py:58-71``, used at
    ``:1250`` sync / ``:1449-1450`` async) -- but the backend runs BEFORE that
    check (``:1246``, ``:1442``), so skipping it here would BYPASS it.  In CI,
    pipes and ``--command`` runs stdin is regularly not a terminal; this is
    not an edge case.
    """
    from code_puppy.tools.common import _stdin_supports_interactive_approval

    return bool(_stdin_supports_interactive_approval())


__all__: Sequence[str] = (
    "APPROVE_CHOICE",
    "CHOICES",
    "REJECT_CHOICE",
    "TerminalPrompt",
    "stdin_is_interactive",
)
