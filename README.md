# claude_telegram_interface

A Telegram bot that bridges your phone to local `claude` CLI sessions on this
machine. Each Telegram conversation maps to a persistent Claude Code session;
partial responses stream live via the Telegram Bot API's `sendMessageDraft`
(animated drafts), and the final answer is committed as a real message. Each
new session opens its own forum topic in the bot DM.

## How it works

- **Two transports, one bot token.** Telethon (MTProto) handles inbound updates
  and ordinary sends; a thin `aiohttp` client to `api.telegram.org/bot<TOKEN>/`
  is used for `sendMessageDraft` and `createForumTopic` in private chats —
  both Bot-API-only, with no MTProto equivalents.
- **Persistent subprocess per active session** running
  `claude --print --input-format stream-json --output-format stream-json
  --include-partial-messages --resume <session-id>`. Idle sessions are killed
  after `IDLE_SUSPEND_SEC`; the next user turn respawns with `--resume` and
  Claude remembers prior context.
- **Live login.** `/login` drives `claude auth login` in a PTY, captures the
  OAuth URL, sends it to you, and pipes your verification code reply back in.
- **Threading.** `/new` calls `createForumTopic` so each session gets its own
  topic in the DM. Subsequent messages in that topic are routed to the topic's
  Claude session; messages outside any topic go to your active session.

## Requirements

- Python 3.11+
- `claude` CLI installed (Claude Code) — defaults to `/root/.local/bin/claude`
- A Telegram bot token from @BotFather
- Telethon API credentials (`API_ID`, `API_HASH`) from <https://my.telegram.org/apps>

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
# fill in API_ID, API_HASH, BOT_TOKEN, ALLOWED_USER_IDS
.venv/bin/python -m bot
```

## Run as a systemd service

Three units in `deploy/systemd/`:

- `tg-interface.service` — the bot itself (`Restart=always`, env from `/opt/tg_interface/.env`).
- `tg-interface-watch.path` — watches `bot/` and `bot/locales/` for changes.
- `tg-interface-watch.service` — oneshot, sleeps 3 s for debounce, then restarts the bot. A burst of file changes during the sleep collapses to one restart, so editing rapidly doesn't thrash the process.

Install and enable:

```sh
sudo deploy/install.sh
journalctl -u tg-interface -f
```

### Environment variables

| Var | Required | Purpose |
| --- | --- | --- |
| `API_ID`, `API_HASH` | yes | Telethon credentials |
| `BOT_TOKEN` | yes | BotFather token, used by both transports |
| `ALLOWED_USER_IDS` | yes | Comma-separated Telegram user_ids permitted to use the bot |
| `CLAUDE_BIN` | no | Path to `claude` CLI (default `/root/.local/bin/claude`) |
| `DEFAULT_CWD` | no | Initial working directory for new sessions |
| `IDLE_SUSPEND_SEC` | no | Idle timeout before subprocesses are killed (default 600) |
| `ALLOWED_TOOLS` | no | Space-separated tool allow-list (default `Bash Read Write Edit Glob Grep`) |
| `DB_PATH` | no | SQLite path (default `state.db`) |

## Bot commands

| Command | Effect |
| --- | --- |
| `/start`, `/help` | Welcome / help text in your language |
| `/new` | Open a new forum topic and start a fresh Claude session in it |
| `/cd <abs-path>` | Set the working directory used for new sessions |
| `/login` | Start an interactive Claude OAuth login from inside Telegram |
| `/perm <bypass\|default\|plan\|acceptedits>` | Set permission mode for new sessions |
| `/lang <en\|ru\|uk>` | Override the auto-detected UI language |

Plain text outside any topic goes to your active session; plain text inside a
session topic stays in that session.

## Adding a language

Drop `bot/locales/<code>.toml` next to `en.toml` and restart. The boot-time
key-parity check warns if any keys are missing. `pick_lang` maps Telegram
`User.language_code` (e.g. `ru-RU`) to the matching short code, falling back
to English.

## Tests

```sh
.venv/bin/python -m pytest -q
```

Covers: i18n key parity, message chunking, draft-streamer throttling +
finalize, and the stream-json codec.

## Layout

```
bot/
  main.py              entrypoint
  config.py            env-driven Settings
  bot_api.py           aiohttp wrapper for sendMessageDraft, createForumTopic, sendMessage
  i18n.py              TOML loader, t(), pick_lang()
  state.py             aiosqlite DAO (users, sessions, drafts; WAL)
  registry.py          in-memory ClaudeSession registry + idle sweeper
  streaming.py         debounced DraftStreamer
  render.py            message chunking and tool-summary formatting
  claude_session.py    subprocess + stream-json codec + watchdog
  permissions.py       --permission-mode aliases
  login_pty.py         PTY-driven `claude auth login`
  handlers.py          Telegram event routing
  locales/             en.toml ru.toml uk.toml
tests/                 pytest tests
```
