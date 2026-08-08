# cp_discord — deine Terminal-Sitzungen auf dem Handy

Jede Code-Puppy-Sitzung bekommt einen eigenen Discord-Thread. Du siehst, was
der Agent tut, gibst Freigaben per Knopfdruck und schreibst ihm dazwischen —
vom Sofa oder von unterwegs.

**Ein Bot, beliebig viele Sitzungen.** Eine Sitzung übernimmt automatisch die
Discord-Verbindung (den „Broker"), die anderen hängen sich an. Fällt sie weg,
übernimmt binnen 30 Sekunden eine andere.

---

## 1. Discord-Server und Kanal

1. Discord öffnen → links unten **`+`** → **Server erstellen** → *Eigenen
   erstellen* → *Nur für mich*. Name egal.
2. Im Server: **`+`** neben *Textkanäle* → Kanal anlegen, z. B. `#puppy`.

> Ein privater Server für dich allein ist der Normalfall. Der Bot legt in
> diesem Kanal **private Threads** an — pro Sitzung einen.

## 2. Bot anlegen

1. https://discord.com/developers/applications → **New Application**
2. Links **Bot** → **Add Bot**
3. **Reset Token** → Token kopieren. **Das ist ein Passwort.** Er wird nur
   einmal angezeigt.
4. Runterscrollen zu **Privileged Gateway Intents** →
   **MESSAGE CONTENT INTENT einschalten** → *Save Changes*

> Ohne diesen Schalter kommt jede Chat-Nachricht **leer** beim Bot an. Der
> Rückweg (Terminal → Discord) funktioniert, das Schreiben nicht — ohne
> Fehlermeldung.

## 3. Bot auf den Server einladen

1. Links **OAuth2** → **URL Generator**
2. Scopes: **`bot`**
3. Bot Permissions:
   - Send Messages
   - Create Private Threads
   - Send Messages in Threads
   - Manage Threads
   - Read Message History
   - Add Reactions
4. Erzeugte URL unten kopieren, im Browser öffnen, Server auswählen,
   **Autorisieren**.

## 4. IDs einsammeln

Zuerst einmalig: **Einstellungen → Erweitert → Entwicklermodus** einschalten.

| Was | Wie |
|---|---|
| **Kanal-ID** | Rechtsklick auf `#puppy` → *Kanal-ID kopieren* |
| **Deine User-ID** | Rechtsklick auf deinen Namen → *Benutzer-ID kopieren* |
| **Bot-Token** | aus Schritt 2 |

## 5. py-cord nachinstallieren

Code Puppy bringt die Discord-Bibliothek nicht mit:

```powershell
uv pip install --python "$env:APPDATA\uv\tools\code-puppy\Scripts\python.exe" "py-cord>=2.8.1,<3"
```

Fehlt sie, startet Code Puppy normal weiter und meldet nur:
*„the Discord bridge needs py-cord"*.

## 6. Konfigurieren

In `~\.code_puppy\puppy.cfg`:

```ini
cp_discord_enabled    = 1
discord_bot_token     = DEIN_BOT_TOKEN
cp_discord_channel_id = 1234567890123456789

discord_approvers     = discord:9876543210987654321=deinname
discord_allow_from    = discord:9876543210987654321=deinname
```

**Format:** `discord:<deine-user-id>=<beliebiger-name>`. Mehrere Einträge mit
Komma trennen.

**Die zwei Rollen sind unabhängig** — eine folgt nicht aus der anderen:

| Schlüssel | Rolle | Darf |
|---|---|---|
| `discord_approvers` | APPROVER | Freigaben erteilen (Approve/Deny) |
| `discord_allow_from` | TALKER | dem Agenten schreiben |

> Fehlt `discord_allow_from`, werden deine Chat-Nachrichten **stumm
> verworfen** — grüner Haken bleibt aus, kein Fehler. Für den vollen
> Funktionsumfang brauchst du beide Zeilen.

### Optional

```ini
cp_discord_mode     = report     ; oder: stream  (Default: report)
cp_discord_autojoin = 1          ; Approver automatisch in neue Threads holen
```

- **`report`** — eine Statuszeile während der Arbeit, am Wartepunkt ein
  Bericht. Sparsam, gut fürs Handy.
- **`stream`** — laufende Ausgabe mit.

Alle Werte lassen sich auch per Umgebungsvariable setzen
(`CP_DISCORD`, `DISCORD_BOT_TOKEN`, `CP_DISCORD_CHANNEL_ID`,
`CP_DISCORD_MODE`, `CP_DISCORD_AUTOJOIN`, `DISCORD_ALLOW_FROM`,
`DISCORD_APPROVERS`) — die Umgebung gewinnt.

## 7. Plugin deployen

```powershell
cd C:\Projekte_prv\arnonuem\cp_plugins
powershell -ExecutionPolicy Bypass -File deploy.ps1 cp_discord
```

Kopiert das Plugin nach `~\.code_puppy\plugins\cp_discord\`.

## 8. Starten

Code Puppy neu starten. Im Kanal erscheint ein Thread mit dem Namen deines
Projekts.

**Probe:** Schreib „hallo" in den Thread. Es sollte ein  erscheinen und der
Agent antworten.

---

## Wenn etwas nicht geht

**Nach jedem Deploy alle Sitzungen neu starten.** Plugins werden nur beim
Start geladen, und der Broker kann in *jeder* Sitzung sitzen — läuft dort
alter Code, ist der Weg unterbrochen, obwohl alle anderen aktuell sind.

| Symptom | Ursache |
|---|---|
| Kein Thread erscheint | `py-cord` fehlt (Schritt 5) oder Token/Kanal-ID falsch |
| Buttons: *„hat nicht rechtzeitig reagiert"* | Der Broker-Prozess hat alten Code → alle Sitzungen neu starten |
| Chat: nichts passiert, kein Haken | `discord_allow_from` fehlt (Schritt 6) |
| Chat: Haken, aber keine Reaktion | Du schreibst in einen **alten** Thread. Nach einem Neustart legt die Sitzung einen **neuen** an |
| Nachricht im Leerlauf bleibt liegen | Braucht Code Puppy ≥ dem Leerlauf-Fix (`cdb4bf4a`) |

**Buttons haben 120 Sekunden.** Die Meldung nach 3 Sekunden ist etwas
anderes: Dann hat *niemand* geantwortet — der Klick ist gar nicht angekommen.

## Sicherheit

- Der Bot-Token ist ein **Passwort**. Wer ihn hat, kann als dein Bot posten.
  Nicht committen.
- Threads sind **privat**: Wer nicht hinzugefügt ist, sieht und schreibt
  nichts.
- Ein **unautorisierter** Absender bekommt keine Reaktion, keine Zustellung,
  und sein Text landet in **keinem** Log — bewusst so.
- **TALKER gilt global**, nicht pro Sitzung: Steht ein zweiter Name in
  `discord_allow_from`, darf er **jede** deiner Sitzungen steuern. Für den
  Einzelbetrieb egal, bei mehreren Personen zu bedenken.
