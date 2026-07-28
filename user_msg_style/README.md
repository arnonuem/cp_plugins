# user_msg_style

Restyles the echo of **your own** message in the Code Puppy transcript --
color, attributes, prefix -- without any change to the Code Puppy core.

Today the echo is hardcoded (`code_puppy/cli_runner.py:603-611`):

```python
style = f"bold {prompt_color}" if prompt_color else "bold"
return Text(f"\n> {task}", style=style)
```

Only the color is variable, and it comes from `prompt_text_color` -- a
**shared** channel that also paints the agent response, the input line,
the bottom bar and the streaming writer. Changing it recolors everything.
This plugin gives the user message its own three knobs instead.

> Every value and every rendered result in this document was verified
> against the Rich version this plugin runs on. Nothing here is a guess --
> where a value is rejected or behaves unexpectedly, it says so.

---

## Quick start

```
/set user_msg_color=#c5fc7d
```

That is the whole minimum. The change takes effect on your **next
message** -- no restart. With no keys set at all, the output is identical
to stock Code Puppy.

---

## The three keys

All three live in `~/.code_puppy/puppy.cfg` and are read **on every
render**, so any `/set` applies immediately.

| Key | Controls | Default |
|-----|----------|---------|
| `user_msg_color` | the foreground color | inherit the prompt color |
| `user_msg_style` | attributes (bold, italic, ...) and optionally a background | `bold` |
| `user_msg_prefix` | the text before your message | `"> "` |

---

## 1. `user_msg_color` -- the color

Four notations, all accepted:

| Form | Examples |
|------|----------|
| **Hex** | `#c5fc7d`, `#7dd3fc`, `#ff8800` |
| **Name** | `cyan`, `bright_magenta`, `dark_sea_green`, `grey54`, `red` |
| **Palette index** | `color(42)` -- any number 0-255 |
| **RGB** | `rgb(197,252,125)` |

Two more that work but are worth knowing about:

- `default` -- your terminal's own foreground, whatever that is.
- Case does not matter: `CYAN` parses and renders exactly like `cyan`.

**What does NOT work here:** a background specification. `on red` is a
*style* fragment, not a color -- `user_msg_color=on red` is rejected and
falls back (see the fallback table below). Put backgrounds in
`user_msg_style` instead; that is shown further down.

The full color-name list is Rich's standard palette (the 256 xterm names):
<https://rich.readthedocs.io/en/stable/appendix/colors.html>

---

## 2. `user_msg_style` -- attributes (and more)

### The attributes

Thirteen values are accepted, verified one by one:

| | | | |
|---|---|---|---|
| `bold` | `dim` | `italic` | `underline` |
| `blink` | `blink2` | `reverse` | `strike` |
| `underline2` | `frame` | `encircle` | `overline` |
| `conceal` | | | |

Combine them by separating with spaces:

```
/set user_msg_style="bold italic"
/set user_msg_style="dim underline"
/set user_msg_style="bold strike"
```

An empty value means *no attributes, color only*:

```
/set user_msg_style=""      ->  style is just the color
```

> **Terminal support varies.** `bold`, `dim`, `italic`, `underline`,
> `reverse` and `strike` are safe just about everywhere. `blink`,
> `blink2`, `frame`, `encircle`, `overline` and `conceal` are emitted
> correctly but many terminals ignore them silently. If nothing seems to
> happen, that is your terminal, not the plugin.

### Negation with `not`

Rich accepts a `not` prefix, which is useful for switching an attribute
back off inside a combination:

```
/set user_msg_style="not bold italic"    ->  renders italic, not bold
```

Verified: this renders as `\x1b[3m` (italic only), with no bold code.

### Background colors live here

This is the one thing that surprises people. `user_msg_color` cannot take
a background, but `user_msg_style` can, using Rich's `on` syntax:

```
/set user_msg_style="bold on #303030"    ->  bold, dark grey background
/set user_msg_style="on blue"            ->  blue background, no attributes
/set user_msg_style="white on blue"      ->  see the note below
```

Verified rendering for `user_msg_style="bold on #303030"` combined with
`user_msg_color=#c5fc7d`:

```
\x1b[1;38;2;197;252;125;48;2;48;48;48m
   ^bold ^foreground 197,252,125       ^background 48,48,48
```

Both keys cooperate: the attribute and background come from
`user_msg_style`, the foreground from `user_msg_color`.

### The precedence trap

You *can* put a foreground color in `user_msg_style` -- it is a valid Rich
style fragment. But `user_msg_color` still wins, and the reason is subtle:

The plugin builds the final style as `"<user_msg_style> <user_msg_color>"`,
and **Rich lets the last color in a style string win**. Verified:

```
Style.parse("red cyan").color  ->  cyan
```

So:

```
user_msg_style=red   user_msg_color=cyan   ->  final style "red cyan"  ->  renders CYAN
user_msg_style="bold red"   user_msg_color unset   ->  "bold red"  ->  renders RED
```

In other words: a color in `user_msg_style` acts as a *fallback* that
applies only while `user_msg_color` is unset. That is deliberate and
locked down by a test -- but if you set both and wonder why your
`user_msg_style` color is ignored, this is why.

---

## 3. `user_msg_prefix` -- the text before your message

Default `"> "`. Any text is allowed:

| Setting | Result |
|---------|--------|
| `/set user_msg_prefix="> "` | `> your message` (the default) |
| `/set user_msg_prefix="\| "` | `\| your message` |
| `/set user_msg_prefix="-> "` | `-> your message` |
| `/set user_msg_prefix="you: "` | `you: your message` |
| `/set user_msg_prefix=""` | `your message` (no prefix at all) |

### Quoting is not optional here

`puppy.cfg` is an INI file, and its parser **strips surrounding
whitespace**. Without quotes your trailing space is silently lost:

```
/set user_msg_prefix=>       ->  reads back as ">"    (space gone, text sticks to the marker)
/set user_msg_prefix="> "    ->  reads back as "> "   (correct)
```

One layer of matching `"` or `'` is stripped, so both quote styles work:

```
"> "    ->  "> "
'-> '   ->  "-> "
""      ->  ""      (an intentionally empty value)
plain   ->  "plain" (unquoted values are used verbatim)
```

Since almost every useful prefix ends in a space, **quote it by default.**

### The leading newline is not configurable

The echo always begins with `\n`. That is layout, not style -- without it
your message glues itself to whatever was printed before. `user_msg_prefix`
controls only what comes *after* that newline.

---

## Worked examples

Each row was rendered and checked:

| Goal | Settings | Result |
|------|----------|--------|
| Lime, bold | `color=#c5fc7d` `style=bold` | `> meine nachricht` in bold lime |
| Quiet grey | `color=grey54` `style="dim italic"` | dim italic grey |
| Color only, no weight | `color=cyan` `style=""` | plain cyan, not bold |
| Arrow marker | `color=#c5fc7d` `style=bold` `prefix="-> "` | `-> meine nachricht` |
| No marker at all | `color=cyan` `prefix=""` | `meine nachricht`, nothing before it |
| Highlighted block | `color=#c5fc7d` `style="bold on #303030"` | bold lime on a dark band |
| Named "you:" label | `color=bright_magenta` `prefix="you: "` | `you: meine nachricht` |

---

## What happens on bad input

Nothing breaks. An unparseable value falls back and emits **one** warning
(not one per message), and the REPL never sees an exception:

| Situation | Result | Warning |
|-----------|--------|---------|
| `user_msg_style` invalid | falls back to `bold` | names `user_msg_style` |
| `user_msg_color` invalid, prompt color valid | falls back to the **prompt color** -- exactly what stock Code Puppy would show | names both values |
| `user_msg_color` invalid, prompt color also unusable | no color | names `user_msg_color` |
| Only the prompt color is invalid, `user_msg_color` unset | passed through untouched, like the core | **none** -- an unconfigured plugin stays silent |

That last row matters: if you have configured nothing, this plugin never
complains about somebody else's setting.

If anything at all goes wrong during a render -- a corrupt config file, a
Rich version that rejects something -- the original core function is called
instead. That also covers a future Code Puppy calling the echo builder with
extra arguments: the plugin passes the call straight through rather than
failing.

---

## Rich markup you type is not interpreted

`[bold]hi[/bold]` shows up literally. The echo is built as a
`rich.text.Text` with an explicit style rather than parsed as markup --
matching what the core does today, and deliberately keeping bracket input
from crashing the render.

---

## Turning it off

```
/plugins disable user_msg_style
```

takes effect on your next message, no restart -- the echo goes back to the
core's. Code Puppy will nevertheless print its generic
*"Restart Code Puppy for this change to take effect."*
(`code_puppy/plugins/plugin_list/register_callbacks.py:150`, printed
unconditionally for every plugin); for this plugin you can ignore it,
because the disable is checked on every render rather than only at load
time.

`/plugins enable user_msg_style` brings the styling back the same way --
**as long as you have not restarted in the meantime.** If Code Puppy
starts up while the plugin is disabled, its `startup` callback is filtered
out, no patch is ever installed, and there is nothing for the enable to
switch back on: that case needs one more restart.

To go back to stock behavior without disabling the plugin, clear the keys:

```
/set user_msg_color=""
/set user_msg_style=""
/set user_msg_prefix=""
```

Note that empty `user_msg_style` and `user_msg_prefix` are *meaningful*
values (no attributes / no prefix), not "unset". To truly restore the
defaults, remove the lines from `~/.code_puppy/puppy.cfg`.

---

## Where these keys show up

`/set` autocompletes a key once it exists in `puppy.cfg` -- so the first
one you set has to be typed in full, and after that it is suggested.

The `/plugins` TUI shows this plugin's description (the first paragraph of
`register_callbacks.py`'s module docstring), which lists the three key
names. It does **not** show config keys as a category and does not read
this README -- there is no core mechanism for a plugin to advertise its
settings. This file is the complete reference.

---

## Install

```powershell
cd C:\Projekte_prv\arnonuem\cp_plugins
.\deploy.ps1 user_msg_style
```

Then restart Code Puppy -- plugins load at startup.

---

## How it works

`cli_runner._prompt_echo_text` is replaced in the `startup` callback.

The timing matters and is not obvious: plugins are loaded at
`cli_runner.py:53`, **during** the import of `cli_runner` and 550 lines
before `_prompt_echo_text` is defined at line 603. Patching at module
scope would silently do nothing. The `startup` hook fires later, at
`cli_runner.py:403`, when the module is complete. Same approach as the
builtin `prompt_newline` plugin.

Both call sites (`cli_runner.py:866` for queued input and `:878` for
typed input) resolve the function through the module namespace, so one
rebind covers both.

## Files

| File | Role |
|------|------|
| `register_callbacks.py` | Hook registration and patch installation |
| `style_builder.py` | Pure render logic -- no `code_puppy` import, unit-testable standalone |
| `tests/` | Unit tests; **not** deployed |

## Tests

```powershell
cd C:\Projekte_prv\arnonuem\code_puppy
uv run pytest C:\Projekte_prv\arnonuem\cp_plugins\user_msg_style\tests --no-cov -q
```

Run from the `code_puppy` checkout so the dependencies (`rich`, `pytest`)
resolve.
