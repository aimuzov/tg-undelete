# tg-undelete

> 🇷🇺 [Русский](README.ru.md)

[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Pylint](https://img.shields.io/github/actions/workflow/status/aimuzov/tg-undelete/pylint.yml?branch=main&label=pylint&logo=github)](.github/workflows/pylint.yml)
[![Telethon](https://img.shields.io/badge/Telethon-%E2%89%A51.37-2CA5E0?logo=telegram&logoColor=white)](https://docs.telethon.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)]()
[![Language: EN | RU](https://img.shields.io/badge/lang-EN%20%7C%20RU-blue.svg)](README.ru.md)

Rescue and re-post deleted Telegram messages of a supergroup you administer.

The tool comes in two stages:

1. **`src.backup`** — walks the Telegram **admin log** of a supergroup,
   collects every *delete-message* event together with the original text and
   media, and writes them to a local folder.
2. **`src.resender`** — replays that local dump back into the **same**
   supergroup, preserving forum topics, albums, replies, reactions and pin
   state. Because Telegram does not allow posting as another user, the
   original author and timestamp are prepended to each message body:

   ```
   <blockquote><b>First Last • 11:55 • 2025 May 12</b></blockquote>

   <message body>
   ```

   > **Tip:** enable *Remain Anonymous* on the admin account that runs
   > the resender. Restored posts will then appear as sent by the group
   > itself rather than by a personal account — visually much cleaner.

Both scripts persist run state, so they resume cleanly after Ctrl+C or a
crash — already-saved / already-resent events are skipped.

## Requirements

- Python 3.10+ (CI tests 3.10 – 3.13)
- Telegram **api_id** and **api_hash** — get them from <https://my.telegram.org>
- Your account must be an **admin** of the supergroup (the deletion admin
  log is only visible to admins)

## Install

```bash
python -m venv venv
source venv/bin/activate           # fish: source venv/bin/activate.fish
pip install -r requirements.txt
```

## Quick start

### 1. Back up deleted messages

```bash
python -m src.backup
```

Any missing option is asked interactively — `api_id`, `api_hash`, the
target `chat_id`, and the export mode (text / media / both). You can
also pass everything on the command line:

```bash
python -m src.backup \
    --api-id 123 --api-hash abc \
    --chat-id -1001234567890 \
    --mode 1 --yes
```

On first launch Telethon will ask for your phone number, login code and
2FA password (if enabled) and store the session in `session_name.session`.

See `python -m src.backup --help` for every flag (`--min-id`, `--max-id`,
`--limit`, `--dry-run`, `--reset-session`, `--verbose`, …).

### 2. Re-upload the rescued messages

```bash
python -m src.resender
```

Without `--chat-id` you get an interactive picker over existing backups
under `./backups/`. The resender then asks which forum topics to restore
(checkbox UI), shows a summary, and on confirmation starts sending.

```bash
python -m src.resender \
    --api-id 123 --api-hash abc \
    --chat-id -1001234567890 \
    --topics all --yes
```

Useful flags: `--dry-run` (build captions and print previews without
calling Telegram), `--compact` (one line per lot instead of a Rich
panel), `--reveal-spoilers`, `--limit`, `--yes`. Run with `--help` for
the full list.

## On-disk layout

A single chat backup is fully self-contained — copy or move the
`<chat_id>/` folder and you've moved everything (state, logs, media):

```
backups/
└── <chat_id>/
    ├── messages.json              # append-mode JSON stream of Message objects
    ├── media/                     # downloaded photos, videos, audio, files
    ├── backup_state.json          # backup resume state (saved_ids + permanent failures)
    ├── backup_errors.log
    ├── backup_missing_media.log
    ├── resender_state.json        # resender resume state (id_map)
    ├── resender_errors.log
    ├── resender_missing_media.log
    └── users_cache.json           # participant display-name cache
```

## Project layout

```
src/
├── backup/__main__.py     # python -m src.backup       — admin-log export
├── resender/__main__.py   # python -m src.resender     — replay into the chat
└── common/                # shared utilities
    ├── backup_layout.py   #   path resolution + chat-id picker
    ├── checkbox.py        #   questionary checkbox UI patches
    ├── console.py         #   rich Console singletons + status helpers
    ├── entity_render.py   #   Telegram MessageEntity → rich.Text
    ├── message_view.py    #   RenderedMessage + Panel/one-line renderers
    ├── progress.py        #   rich.Progress factory
    └── telethon_adapter.py # live Telethon Message → RenderedMessage
```

Public APIs are documented as PEP 257 docstrings inside each module —
`python -c "import src.backup.__main__; help(src.backup.__main__)"` etc.

## Caveats

- The script only sees deletions that **happened while you were an
  admin** — Telegram's admin log retention is ~48 h for some channels,
  longer for others.
- The CLI is bilingual: prompts and logs are in English, but a few
  user-facing labels (media kind: "фото", "видео", … and the
  `(ред.)` "edited" marker) are in Russian because that's what gets
  re-posted into the chat.
- `*.session` files contain login state — they're in `.gitignore`; do
  not commit or share them.

## License

[MIT](LICENSE) © 2026 Aleksey Imuzov
