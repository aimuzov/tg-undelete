"""Rich Console singletons and thin status helpers.

`STDOUT_CONSOLE` is the user-facing console (colour, emoji, auto-detected TTY).
`get_file_console(path)` opens a separate Console writing plain text into a
log file — so ANSI escapes never end up in `resender_errors.log` &c.

Use `info` / `warn` / `error` / `success` for one-line status lines. `error`
can mirror its message into a log file via `log_path=`; the file copy goes
through a colourless Console so the on-disk text stays grep-friendly.
"""

from __future__ import annotations

import atexit
import re
from typing import Optional, TextIO

from rich.console import Console
from rich.text import Text


STDOUT_CONSOLE: Console = Console(highlight=False, soft_wrap=False)


_FILE_HANDLES: dict[str, TextIO] = {}
_FILE_CONSOLES: dict[str, Console] = {}


def _close_handles() -> None:
    for fh in _FILE_HANDLES.values():
        try:
            fh.close()
        except Exception:
            pass


atexit.register(_close_handles)


def get_file_console(path: str) -> Console:
    """Return (and cache) a Console that appends plain text to `path`."""
    cached = _FILE_CONSOLES.get(path)
    if cached is not None:
        return cached
    fh = open(path, "a", encoding="utf-8")
    _FILE_HANDLES[path] = fh
    console = Console(
        file=fh,
        no_color=True,
        force_terminal=False,
        emoji=False,
        highlight=False,
        soft_wrap=True,
        width=200,
    )
    _FILE_CONSOLES[path] = console
    return console


def _prefixed(label: str, label_style: str, message: str) -> Text:
    """Renders ``[<label>] <message>`` — brackets dim, label in `label_style`."""
    body = Text.from_markup(message)
    out = Text()
    out.append("[", style="dim")
    out.append(label, style=label_style)
    out.append("]", style="dim")
    out.append(" ")
    out.append_text(body)
    return out


def info(message: str) -> None:
    STDOUT_CONSOLE.print(_prefixed("info", "blue", message))


def success(message: str) -> None:
    STDOUT_CONSOLE.print(_prefixed("ok", "green", message))


def warn(message: str) -> None:
    STDOUT_CONSOLE.print(_prefixed("warn", "yellow", message))


def error(message: str, *, log_path: Optional[str] = None) -> None:
    STDOUT_CONSOLE.print(_prefixed("error", "red", message))
    if log_path:
        # File copy stays plain — strip rich markup before writing.
        get_file_console(log_path).print(
            f"[error] {Text.from_markup(message).plain}"
        )


_PROMPT_RE = re.compile(r"^(?P<body>.*?)(?P<hint>\s*\([^)]*\))?(?P<colon>\s*:)?\s*$")


def prompt_text(prompt: str) -> str:
    """Coloured replacement for ``input(prompt)``.

    The body of the prompt is rendered in bright cyan, while any ``(...)``
    hint inside it is dimmed to grey. The trailing colon (if any) keeps the
    cyan colour. Use everywhere instead of plain ``input(...)`` so all
    interactive questions look uniform.

    >>> prompt_text("Enter the minimum message ID (0 to start from the first):")
    # ↑ "Enter the minimum message ID" cyan, "(0 to start from the first)" dim, ":" cyan
    """
    match = _PROMPT_RE.match(prompt)
    if match:
        body = match.group("body")
        hint = match.group("hint") or ""
        colon = match.group("colon") or ""
        STDOUT_CONSOLE.print(
            f"[bright_cyan]{body}[/bright_cyan]"
            f"[dim]{hint}[/dim]"
            f"[bright_cyan]{colon}[/bright_cyan] ",
            end="",
        )
    else:
        STDOUT_CONSOLE.print(f"[bright_cyan]{prompt}[/bright_cyan] ", end="")
    # Empty input() prompt: rich already painted everything, builtin is just
    # the line-reader.
    return input("")
