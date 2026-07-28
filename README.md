# cp_plugins

Personal plugins for [Code Puppy](https://github.com/mpfaffenberger/code_puppy),
kept in one repo and deployed selectively into `~/.code_puppy/plugins/`.

## Plugins

| Plugin | What it does |
|--------|--------------|
| [`user_msg_style`](user_msg_style/) | Restyles the echo of your own message -- color, attributes, prefix -- via `/set` |

## Deploy

```powershell
.\deploy.ps1 user_msg_style          # one plugin
.\deploy.ps1 user_msg_style foo bar  # several
.\deploy.ps1 -All                    # every plugin in the repo
.\deploy.ps1 user_msg_style -WhatIf  # dry run, touches nothing
```

Target is `$env:USERPROFILE\.code_puppy\plugins\<name>\`.

Code Puppy loads plugins at startup, so **restart it after deploying**.

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
