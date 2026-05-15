"""On-disk layout shared by ``src.backup`` and ``src.resender``.

Per chat::

    <output-folder>/                     # default: "backups"
      <chat_id>/
        messages.json                    # append-mode JSON stream of messages
        media/                           # downloaded files, named <event_id>.<ext>
        backup_state.json                # backup resume state
        backup_errors.log                # backup logs
        backup_missing_media.log
        resender_state.json              # resender resume state
        resender_errors.log              # resender logs
        resender_missing_media.log
        users_cache.json                 # resender participant name cache

The same folder hosts both modules' state and logs so a chat is fully
self-contained — copy/move/delete the chat directory and you've moved the
whole backup with it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .console import STDOUT_CONSOLE, info, prompt_text


MESSAGES_FILENAME: str = "messages.json"
MEDIA_FOLDER_NAME: str = "media"


@dataclass(frozen=True)
class ChatLayout:
    """Resolved on-disk paths for a single chat backup."""

    chat_dir: str
    messages: str
    media: str

    def ensure(self) -> None:
        """Creates ``chat_dir`` and ``media`` on disk (idempotent)."""
        os.makedirs(self.chat_dir, exist_ok=True)
        os.makedirs(self.media, exist_ok=True)


def chat_layout(output_folder: str, chat_id: int) -> ChatLayout:
    """Builds (without creating) the standard layout for one chat."""
    chat_dir = os.path.join(output_folder, str(chat_id))
    return ChatLayout(
        chat_dir=chat_dir,
        messages=os.path.join(chat_dir, MESSAGES_FILENAME),
        media=os.path.join(chat_dir, MEDIA_FOLDER_NAME),
    )


def discover_chat_backups(output_folder: str) -> list[int]:
    """Returns sorted chat ids whose backups exist as numeric subfolders."""
    if not os.path.isdir(output_folder):
        return []
    out: list[int] = []
    for name in os.listdir(output_folder):
        full = os.path.join(output_folder, name)
        if not os.path.isdir(full):
            continue
        try:
            out.append(int(name))
        except ValueError:
            continue
    return sorted(out)


def _format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _describe_backup(output_folder: str, chat_id: int) -> str:
    """Short tag like '1.2 MB messages, 25 media' for the picker menu."""
    layout = chat_layout(output_folder, chat_id)
    bits: list[str] = []
    if os.path.exists(layout.messages):
        try:
            bits.append(f"{_format_bytes(os.path.getsize(layout.messages))} messages")
        except OSError:
            pass
    if os.path.isdir(layout.media):
        try:
            count = sum(1 for n in os.listdir(layout.media) if not n.startswith("."))
            if count:
                bits.append(f"{count} media file(s)")
        except OSError:
            pass
    return ", ".join(bits) or "empty"


def prompt_chat_id(
    output_folder: str,
    *,
    action: str,
    require_existing: bool = False,
) -> int:
    """Picks a chat id interactively.

    If ``output_folder`` already contains chat-id subfolders, prints them as
    a numbered menu and lets the user pick one. Item ``[0]`` falls back to
    free-text entry for a brand-new chat (suppressed when
    ``require_existing=True`` — used by resender, which can only restore
    from an existing backup).

    With no existing backups:
      * ``require_existing=False`` — falls back to free-text input.
      * ``require_existing=True`` — exits with a hint to run ``src.backup`` first.
    """
    candidates = discover_chat_backups(output_folder)
    if not candidates:
        if require_existing:
            raise SystemExit(
                f"No existing backups found in '{output_folder}'. "
                f"Run `python -m src.backup` first."
            )
        return _ask_chat_id_freetext()

    info(
        f"existing backups in [bold]{output_folder}[/bold] "
        f"({len(candidates)} chat(s)):"
    )
    for idx, cid in enumerate(candidates, 1):
        STDOUT_CONSOLE.print(
            f"  [bold cyan]\\[{idx}][/bold cyan] {cid} "
            f"[dim]— {_describe_backup(output_folder, cid)}[/dim]"
        )
    if not require_existing:
        STDOUT_CONSOLE.print(
            "  [bold cyan]\\[0][/bold cyan] Enter a different chat id manually"
        )

    range_hint = (
        f"1-{len(candidates)}" if require_existing else f"0-{len(candidates)}"
    )
    while True:
        raw = prompt_text(f"Pick chat to {action} ({range_hint}):").strip()
        if not raw:
            continue
        try:
            choice = int(raw)
        except ValueError:
            STDOUT_CONSOLE.print(
                f"  [red]Bad input '{raw}', expected a number.[/red]"
            )
            continue
        if choice == 0 and not require_existing:
            return _ask_chat_id_freetext()
        if 1 <= choice <= len(candidates):
            return candidates[choice - 1]
        STDOUT_CONSOLE.print(
            f"  [red]Out of range — pick {range_hint}.[/red]"
        )


def _ask_chat_id_freetext() -> int:
    """Re-prompts for a chat id until the user enters a valid integer."""
    while True:
        raw = prompt_text("Enter the group or channel ID:").strip()
        try:
            return int(raw)
        except ValueError:
            STDOUT_CONSOLE.print(
                f"  [red]Bad chat id '{raw}', expected an integer.[/red]"
            )


__all__ = [
    "ChatLayout",
    "MEDIA_FOLDER_NAME",
    "MESSAGES_FILENAME",
    "chat_layout",
    "discover_chat_backups",
    "prompt_chat_id",
]
