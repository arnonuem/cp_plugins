# cp_discord — your terminal sessions, on your phone

Every Code Puppy session gets its own Discord thread. Watch what the agent is
doing, approve gates with a tap, and steer it mid-run by just typing — from
the sofa or from the train.

**One bot, any number of sessions.** One session automatically holds the
Discord connection (the "broker"); the others attach to it. If it goes away,
another takes over within 30 seconds.

---

## 1. Discord server and channel

1. In Discord: **`+`** at the bottom left → **Create My Own** → *For me and my
   friends*. The name does not matter.
2. Inside the server: **`+`** next to *Text Channels* → create one, e.g.
   `#puppy`.

> A private server for yourself is the normal case. The bot creates **private
> threads** in this channel — one per session.

## 2. Create the bot

1. https://discord.com/developers/applications → **New Application**
2. **Bot** in the sidebar → **Add Bot**
3. **Reset Token** → copy it. **This is a password.** It is shown once.
4. Scroll to **Privileged Gateway Intents** →
   turn on **MESSAGE CONTENT INTENT** → *Save Changes*

> Without that switch every chat message arrives **empty**. The outbound
> direction (terminal → Discord) still works, typing back does not — and
> nothing tells you why.

## 3. Invite the bot

1. **OAuth2** in the sidebar → **URL Generator**
2. Scopes: **`bot`**
3. Bot Permissions:
   - Send Messages
   - Create Private Threads
   - Send Messages in Threads
   - Manage Threads
   - Read Message History
   - Add Reactions
4. Copy the generated URL at the bottom, open it, pick your server,
   **Authorize**.

## 4. Collect the IDs

First, once: **Settings → Advanced → Developer Mode** on.

| What | How |
|---|---|
| **Channel ID** | right-click `#puppy` → *Copy Channel ID* |
| **Your user ID** | right-click your own name → *Copy User ID* |
| **Bot token** | from step 2 |

## 5. Install py-cord

Code Puppy does not ship the Discord library:

```powershell
uv pip install --python "$env:APPDATA\uv\tools\code-puppy\Scripts\python.exe" "py-cord>=2.8.1,<3"
```

Without it Code Puppy starts normally and just says:
*"the Discord bridge needs py-cord"*.

## 6. Configure

In `~\.code_puppy\puppy.cfg`:

```ini
cp_discord_enabled    = 1
discord_bot_token     = YOUR_BOT_TOKEN
cp_discord_channel_id = 1234567890123456789

discord_approvers     = discord:9876543210987654321=yourname
discord_allow_from    = discord:9876543210987654321=yourname
```

**Format:** `discord:<your-user-id>=<any-name>`. Separate several with commas.

**The two roles are independent** — neither implies the other:

| Key | Role | May |
|---|---|---|
| `discord_approvers` | APPROVER | answer gates (Approve/Deny) |
| `discord_allow_from` | TALKER | send instructions to the agent |

> Miss out `discord_allow_from` and your chat messages are **discarded
> silently** — no check mark, no error. You want both lines.

### Optional

```ini
cp_discord_mode     = report     ; or: stream  (default: report)
cp_discord_autojoin = 1          ; pull approvers into new threads automatically
```

- **`report`** — one status line while it works, a report when it parks.
  Quiet, good on a phone.
- **`stream`** — follow the output as it happens.

Every value can also come from the environment (`CP_DISCORD`,
`DISCORD_BOT_TOKEN`, `CP_DISCORD_CHANNEL_ID`, `CP_DISCORD_MODE`,
`CP_DISCORD_AUTOJOIN`, `DISCORD_ALLOW_FROM`, `DISCORD_APPROVERS`) — the
environment wins.

## 7. Deploy the plugin

```powershell
cd C:\Projekte_prv\arnonuem\cp_plugins
powershell -ExecutionPolicy Bypass -File deploy.ps1 cp_discord
```

Copies the plugin to `~\.code_puppy\plugins\cp_discord\`.

## 8. Start

Restart Code Puppy. A thread named after your project appears in the channel.

**Check:** type "hello" into the thread. You should get a  and an answer.

---

## When something does not work

**Restart every session after a deploy.** Plugins load at startup only, and
*any* session can be the one holding the Discord connection — if that one
runs the old code the path is broken even though all the others are current.

| Symptom | Cause |
|---|---|
| No thread appears | `py-cord` missing (step 5), or wrong token / channel ID |
| Buttons say *"did not respond in time"* | The broker session runs old code → restart all sessions |
| Chat: nothing happens, no check mark | `discord_allow_from` missing (step 6) |
| Chat: check mark, but no reaction | You are typing in an **old** thread. A restarted session creates a **new** one |
| Message sits there while nothing runs | Needs Code Puppy ≥ the idle fix (`cdb4bf4a`) |

**Buttons are good for 120 seconds.** The message after 3 seconds means
something else: *nobody* answered — the press never arrived.

## Security

- The bot token is a **password**. Anyone holding it can post as your bot.
  Do not commit it.
- Threads are **private**: whoever was not added sees nothing and writes
  nothing.
- An **unauthorized** sender gets no reaction, no delivery, and their text
  reaches **no** log. That is deliberate.
- **TALKER is global**, not per session: a second name in
  `discord_allow_from` may steer **every** one of your sessions. Irrelevant
  for single-user setups, worth knowing with more people.
