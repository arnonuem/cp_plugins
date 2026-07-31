# wmux

Reports Code Puppy's state to the [wmux](https://wmux.dev) pane it is
running in, so the wmux sidebar can answer **"which of my ten sessions
needs me?"** without guessing from the screen.

Outside a wmux pane the plugin is completely inert: no worker thread, no
pipe handle, and not a single callback registered.

## What you see

Code Puppy declares three facts; wmux derives the state word from them.

| Pane state | When | Meaning |
|---|---|---|
| **working** | a run is in flight (`runDepth > 0`) | busy, no action needed |
| **blocked** | Code Puppy is asking you something | **this one needs you** |
| **idle** | control is yours | waiting for your next prompt |
| **unknown** | the process released the pane, or died | no ghost state |

A blocked pane carries a reason, e.g. `permission: run_shell_command`.
Model, token count and context percentage ride along as pane metadata
after each interactive turn, and tool activity is reported separately via
`agent.activity`.

On exit the pane is released, so a dead session shows `unknown` rather
than a ghost `working`.

## Activation

Automatic, and there is nothing to configure. wmux injects the variables
into every shell it spawns.

| Variable | Required | Default |
|---|---|---|
| `WMUX` | yes, must be `1` | — |
| `WMUX_SURFACE_ID` | yes | — |
| `WMUX_PIPE` | no | `\\.\pipe\wmux` |
| `WMUX_PIPE_TOKEN` | no (see below) | — |
| `WMUX_INSTANCE` | no | — |

The auth token is resolved exactly the way the wmux CLI resolves it:
`WMUX_PIPE_TOKEN` first, else
`%APPDATA%\wmux[-<WMUX_INSTANCE>]\pipe-token`. With neither the plugin
still activates, but logs **one warning** — an unauthenticated report is
rejected in a way that looks exactly like success at the transport level,
so silence there would mean a plugin that is 100 % dead with zero symptom.

Windows only: wmux's control channel is a Windows named pipe. On any other
platform the transport reports nothing rather than pretending to work.

## Deploy

```powershell
cd C:\Projekte_prv\arnonuem\cp_plugins
.\deploy.ps1 wmux
```

Code Puppy loads plugins at startup, so **restart it after deploying**.

## Honest limits

Three of these are core-level and cannot be fixed from a plugin. They are
listed rather than hidden, because a state display you cannot trust is
worse than none.

1. **A `/fork` started at an idle prompt leaves the pane on `idle`.**
   Sub-agent runs do not fire `agent_run_start` — the sole call site
   (`agents/_runtime.py:911`) is bypassed by
   `tools/subagent_invocation.py:519` — so nothing raises `runDepth`
   while the fork works. A fork started *during* a run is invisible for
   the same reason, but the pane is already `working`, so it does not
   show wrong.
2. **With a Discord or ACP approval backend installed, the pane stays
   `working` while a human is asked out-of-band.** Those backends return
   from `tools/common.py:1441-1449` (async) / `:1244-1250` (sync) BEFORE
   reaching the `set_awaiting_user_input` choke-point (`:1502` / `:1303`),
   so no blocked edge exists to observe.
3. **Metadata is refreshed on the interactive turn boundary only.** A
   headless Discord/ACP deployment therefore reports state but never
   metadata: `interactive_turn_end` fires from `cli_runner.py:1191` alone,
   which those deployments never reach.

### The blocked reason is inferred, and says so

The choke-point Code Puppy exposes passes a **bool** — the reason is
discarded before any plugin can see it. The reason is therefore
reconstructed from the tools in flight at that moment, at three tiers of
honesty:

| In-flight tools | Reported reason |
|---|---|
| exactly 1 | `permission: <tool_name>` |
| more than 1 | `permission: 1 of N tools` (approvals are lock-serialized; the mapping is genuinely ambiguous) |
| 0, and no run active | nothing at all — a menu at an idle prompt is not a request for you |
| 0, but a run IS active | `permission: unknown` — a real block whose tool key was lost |

`notify=False` is deliberately **not** used as the menu discriminator:
exactly one call site in the entire Code Puppy tree passes it
(`command_line/model_picker_completion.py:591`), so it cannot separate
menus from agent approvals.

A tool key can leak when a plugin blocks a tool call, because
`post_tool_call` is then never fired (`pydantic_patches.py:393` returns
before the `finally` that emits it). Such a key expires after five
minutes. Within that window a real block may report the generic
`permission: 1 of N tools` instead of naming the tool — a wrong reason
string, never a suppressed block.

## Tests

Tests need Code Puppy's dependencies, so run them from that checkout:

```powershell
cd C:\Projekte_prv\arnonuem\code_puppy
uv run pytest C:\Projekte_prv\arnonuem\cp_plugins\wmux\tests --no-cov -q
```

The reply-deadline tests stand up a real named-pipe server
(`_winapi.CreateNamedPipe`) rather than stubbing the transport: the
deadline lives *inside* the transport, so a stub could only prove the stub
works.

## Layout

| File | Responsibility |
|---|---|
| `register_callbacks.py` | activation guard + the twelve hooks |
| `client.py` | delivery policy: worker thread, lanes, seq, retry, release |
| `wire.py` | wire mechanics: pipe I/O, the pipe-path guard, reply verdicts |
| `reporter.py` | run tracking, reason inference, the TTL sweep |
| `sources.py` | fail-soft adapters into Code Puppy internals |
| `diagnostics.py` | one-shot warnings for the failures that would be silent |

## Diagnostics: why some failures warn

Code Puppy's core installs no logging configuration, so Python's
`lastResort` handler applies — and that handler is fixed at `WARNING`.
**Every `logger.debug` this plugin emits is therefore discarded** (measured:
`isEnabledFor(DEBUG)` is false and a debug marker produces no output on any
stream).

So the failures that mean *the plugin is dead* or *the pane is now wrong*
emit a real warning instead — each **once per process**, never once per
report, because these causes are persistent and repeating them would bury
your terminal:

| You will see a warning when | Meaning |
|---|---|
| no pipe token could be resolved | reports are unauthenticated and will be rejected |
| `WMUX_PIPE` is not a named-pipe path | the plugin disabled itself (see below) |
| `WMUX_INSTANCE` is not a plain name | the token file was not read |
| the pane **rejected** a report | the pane is showing a STALE state — check your token |
| a report failed unexpectedly | that one report was dropped |

Everything else — a superseded report, a suppressed menu edge, an expired
run id, a transient timeout — stays at debug, because none of those leaves
the pane wrong.

## Security note: `WMUX_PIPE` is validated

The plugin refuses to open anything but the **local** named-pipe device
path — `\\.\pipe\<name>`, where the host is the literal `.` — and
**deactivates** rather than falling back to the default. `WMUX_INSTANCE` is
likewise restricted to a plain instance name.

This is not tidiness, and it guards two separate disasters. Every envelope
carries your pipe token, and `open(path, "r+b")` does not reject a regular
file — pointing `WMUX_PIPE` at one would destroy that file's contents *and*
leave the token on disk in cleartext.

A **remote** UNC path is worse, because it leaves the box: `\\<host>\pipe\x`
is resolved by the SMB redirector, which authenticates *implicitly*, so a
hostile `WMUX_PIPE` would ship your NetNTLMv2 response to whoever answered
along with the token. `localhost` and `127.0.0.1` are rejected for the same
reason — only the literal `.` reaches the local NPFS device without a
redirector, and it is the only form wmux itself emits.

The reply is bounded too: it is abandoned after 64 KB — whether or not it
ever sends a newline, so a squatter cannot buy an unbounded line for the
price of one trailing `\n` — a reply that defeats the JSON parser is retried
rather than counted as delivered, and the server's own error text is escaped
and truncated before it is ever written to your terminal. That last applies
to the error CODE as well as the message: both come off the reply JSON, so
both are treated as hostile.

Named-pipe squatting by a hostile local process remains outside the threat
model (single-user dev workstation), as does anything requiring code already
running as you.
