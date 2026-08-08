# cp_plugins

Personal plugins for [Code Puppy](https://github.com/mpfaffenberger/code_puppy),
kept in one repo and deployed selectively into `~/.code_puppy/plugins/`.

## Plugins

| Plugin | What it does |
|--------|--------------|
| [`cp_discord`](cp_discord/) | Puts every terminal session in its own Discord thread: watch it work, approve gates from your phone, and steer it mid-run by just typing. One bot, any number of sessions -- one of them holds the Discord connection and the others attach; if it goes away another takes over within 30 s. Needs `py-cord` ([setup](cp_discord/README.md)) |
| [`user_msg_style`](user_msg_style/) | Restyles the echo of your own message -- color, attributes, prefix -- via `/set` |
| [`wmux`](wmux/) | Reports agent state (working / blocked / idle) to the [wmux](https://github.com/amirlehmam/wmux) multiplexer, so a session parked on you is visible at a glance instead of looking identical to a finished one. Windows named pipe; inert outside a wmux pane |

## Deploy

```powershell
.\deploy.ps1 cp_discord               # one plugin
.\deploy.ps1 wmux,user_msg_style      # several -- comma-separated, not spaces
.\deploy.ps1 -All                     # every plugin in the repo
.\deploy.ps1 cp_discord -WhatIf       # dry run, touches nothing
```

Target is `$env:USERPROFILE\.code_puppy\plugins\<name>\`.

Code Puppy loads plugins at startup, so **restart it after deploying**.

> With `cp_discord`, restart **every** session, not just one. Any session can
> be the one holding the Discord connection, and a broker still running the
> old code breaks the path even when all the others are current.

What gets copied, and what does not:

- copied: top-level `*.py` and `README.md`
- not copied: `tests/` and any other subfolder, `__pycache__`
- removed in the target: `*.py` files that no longer exist in the source,
  plus a stale `__pycache__`

The last point is deliberate. The target directory *is* the import path --
a file left behind after a rename keeps getting loaded, which is a bug
that costs hours to find.

A folder without a `register_callbacks.py` is rejected with a clear
message and nothing is copied.

## Layout

Each plugin is one folder, matching the layout Code Puppy's user-tier
loader expects:

```
<plugin_name>/
├── register_callbacks.py   # required -- the loader's entry point
├── <module>.py             # optional helper modules
├── README.md               # what it does and how to configure it
└── tests/                  # not deployed
```

**Flat, and that is load-bearing:** only top-level `*.py` is deployed, so a
module tucked into a subpackage simply never arrives -- and the plugin fails
by going quiet rather than by crashing.

Code Puppy discovers user plugins by looking for `register_callbacks.py`
in each subdirectory of `~/.code_puppy/plugins/`; there is no manifest and
no registration step.

## Conventions

These follow `AGENTS.md` in the Code Puppy repo:

1. **Plugins over core** -- if a hook exists for it, use it.
2. **One `register_callbacks.py` per plugin**, registering at module scope.
3. **600-line cap per file** -- split into submodules.
4. **Fail gracefully** -- a plugin must never crash the app.
5. **Return `None` from commands you do not own.**
6. **Lint before committing:** `ruff check --fix`, `ruff format`.

Two more that this repo adds:

7. **Keep the pure logic out of `register_callbacks.py`.** A module with no
   `code_puppy` import is testable without booting the app.
8. **Never patch at module scope.** Plugins are imported partway through
   `cli_runner`'s own import, so the thing you want to patch may not exist
   yet. Install patches from the `startup` hook.

## Tests

Tests need Code Puppy's dependencies, so run them from that checkout:

```powershell
cd C:\Projekte_prv\arnonuem\code_puppy
uv run pytest C:\Projekte_prv\arnonuem\cp_plugins\<plugin>\tests --no-cov -q
```

`cp_discord` additionally needs `py-cord` in that environment:

```powershell
uv pip install --python "$env:APPDATA\uv\tools\code-puppy\Scripts\python.exe" "py-cord>=2.8.1,<3"
```
