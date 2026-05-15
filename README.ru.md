# tg-undelete

> 🇬🇧 [English](README.md)

[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Pylint](https://img.shields.io/github/actions/workflow/status/aimuzov/tg-undelete/pylint.yml?branch=main&label=pylint&logo=github)](.github/workflows/pylint.yml)
[![Telethon](https://img.shields.io/badge/Telethon-%E2%89%A51.37-2CA5E0?logo=telegram&logoColor=white)](https://docs.telethon.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)]()
[![Language: EN | RU](https://img.shields.io/badge/lang-EN%20%7C%20RU-blue.svg)](README.md)

Восстановление и повторная отправка удалённых сообщений Telegram в супергруппе, которой вы администрируете.

Инструмент состоит из двух этапов:

1. **`src.backup`** — проходит по **админ-логу** супергруппы в Telegram,
   собирает каждое событие *удаления сообщения* вместе с исходным текстом
   и медиа и сохраняет их в локальной папке.
2. **`src.resender`** — воспроизводит этот локальный дамп обратно в **ту же**
   супергруппу, сохраняя форумные топики, альбомы, ответы, реакции и
   закрепы. Поскольку Telegram не позволяет публиковать от имени другого
   пользователя, имя оригинального автора и время добавляются в начало
   каждого тела сообщения:

   ```
   <blockquote><b>Имя Фамилия • 11:55 • 2025 May 12</b></blockquote>

   <тело сообщения>
   ```

   > **Совет:** включите режим *«Анонимный администратор»* для
   > аккаунта, от имени которого работает ресендер. Тогда
   > восстановленные сообщения будут выглядеть как отправленные от
   > лица самой группы, а не от личного аккаунта — визуально намного
   > аккуратнее.

Оба скрипта сохраняют состояние выполнения, поэтому корректно
продолжают работу после Ctrl+C или сбоя — уже сохранённые / уже
переотправленные события пропускаются.

## Требования

- Python 3.10+ (CI тестирует на 3.10 – 3.13)
- Telegram **api_id** и **api_hash** — получить можно на <https://my.telegram.org>
- Ваш аккаунт должен быть **администратором** супергруппы (админ-лог
  удалений виден только администраторам)

## Установка

```bash
python -m venv venv
source venv/bin/activate           # fish: source venv/bin/activate.fish
pip install -r requirements.txt
```

## Быстрый старт

### 1. Резервное копирование удалённых сообщений

```bash
python -m src.backup
```

Любая отсутствующая опция запрашивается интерактивно — `api_id`,
`api_hash`, целевой `chat_id` и режим экспорта (текст / медиа / оба).
Можно также передать всё через командную строку:

```bash
python -m src.backup \
    --api-id 123 --api-hash abc \
    --chat-id -1001234567890 \
    --mode 1 --yes
```

При первом запуске Telethon запросит номер телефона, код входа и
пароль 2FA (если включён) и сохранит сессию в `session_name.session`.

Полный список флагов смотрите в `python -m src.backup --help`
(`--min-id`, `--max-id`, `--limit`, `--dry-run`, `--reset-session`,
`--verbose`, …).

### 2. Повторная отправка восстановленных сообщений

```bash
python -m src.resender
```

Без `--chat-id` вам будет предложен интерактивный выбор существующих
бэкапов в каталоге `./backups/`. Затем ресендер спросит, какие
форумные топики восстанавливать (чекбокс-интерфейс), покажет сводку и
после подтверждения начнёт отправку.

```bash
python -m src.resender \
    --api-id 123 --api-hash abc \
    --chat-id -1001234567890 \
    --topics all --yes
```

Полезные флаги: `--dry-run` (собирает подписи и печатает превью без
обращений к Telegram), `--compact` (одна строка на лот вместо
Rich-панели), `--reveal-spoilers`, `--limit`, `--yes`. Полный список —
по `--help`.

## Структура на диске

Бэкап одного чата полностью самодостаточен — скопируйте или переместите
папку `<chat_id>/`, и вы перенесли всё (состояние, логи, медиа):

```
backups/
└── <chat_id>/
    ├── messages.json              # JSON-поток объектов Message в режиме append
    ├── media/                     # скачанные фото, видео, аудио, файлы
    ├── backup_state.json          # состояние возобновления бэкапа (saved_ids + постоянные сбои)
    ├── backup_errors.log
    ├── backup_missing_media.log
    ├── resender_state.json        # состояние возобновления ресендера (id_map)
    ├── resender_errors.log
    ├── resender_missing_media.log
    └── users_cache.json           # кэш отображаемых имён участников
```

## Структура проекта

```
src/
├── backup/__main__.py     # python -m src.backup       — экспорт админ-лога
├── resender/__main__.py   # python -m src.resender     — повторная отправка в чат
└── common/                # общие утилиты
    ├── backup_layout.py   #   разрешение путей + выбор chat-id
    ├── checkbox.py        #   патчи чекбокс-интерфейса questionary
    ├── console.py         #   синглтоны rich Console + хелперы статуса
    ├── entity_render.py   #   Telegram MessageEntity → rich.Text
    ├── message_view.py    #   RenderedMessage + рендереры Panel / одной строкой
    ├── progress.py        #   фабрика rich.Progress
    └── telethon_adapter.py # живой Telethon Message → RenderedMessage
```

Публичные API задокументированы PEP 257 docstring'ами внутри каждого
модуля — `python -c "import src.backup.__main__; help(src.backup.__main__)"` и т. д.

## Оговорки

- Скрипт видит только те удаления, которые **произошли, пока вы были
  администратором** — Telegram хранит админ-лог около 48 ч для одних
  каналов и дольше для других.
- Интерфейс CLI двуязычный: подсказки и логи на английском, но
  несколько пользовательских надписей (типы медиа: «фото», «видео», …
  и пометка `(ред.)` для «отредактировано») оставлены на русском —
  именно они попадают в чат при повторной отправке.
- Файлы `*.session` содержат данные авторизации — они в `.gitignore`;
  не коммитьте их и не передавайте третьим лицам.

## Лицензия

[MIT](LICENSE) © 2026 Aleksey Imuzov
