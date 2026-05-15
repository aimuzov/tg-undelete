"""Render a Telegram message as a `rich.panel.Panel` that mimics how the
message looks in the Telegram client.

This module is intentionally decoupled from `resender` and `backup`: it
consumes a plain `RenderedMessage` dataclass and produces rich renderables.
Each caller (resender uses `dump.json` dicts; backup uses live Telethon
objects) is responsible for assembling that dataclass first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional, Sequence

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


MAX_BODY_LINES = 40

Status = Literal["normal", "warning", "error", "skipped"]

BORDER_BY_STATUS: dict[Status, str] = {
    "normal": "grey50",
    "warning": "yellow",
    "error": "red",
    "skipped": "grey35",
}


@dataclass
class MessageHeader:
    """Author + time + edited flag rendered on the panel title row."""

    author: str
    when: str  # "11:55 · 2025 May 12"
    edited: bool = False


@dataclass
class ReplyInfo:
    """Quoted reply shown above the body, if any."""

    author: Optional[str] = None
    text: Optional[str] = None


@dataclass
class MessageFooter:
    """Footer-area details: media line, reactions, status note, lot ids."""

    media_summary: list[str] = field(default_factory=list)
    reactions: Optional[str] = None  # plain like "❤️×3 👍×2"
    note: Optional[str] = None
    lot_ids: Sequence[int] = ()
    progress_label: Optional[str] = None  # "12/847"


@dataclass(init=False)
class RenderedMessage:
    """Display-ready snapshot of a message (or media album).

    Accepts the same flat ``author=..., when=..., body=..., ...`` keyword
    arguments as before for backwards compatibility with both callers.
    Internally fields are grouped into :class:`MessageHeader`,
    :class:`ReplyInfo` and :class:`MessageFooter`; the original flat
    attribute names are exposed as read-only properties.
    """

    header: MessageHeader
    body: Text
    reply: ReplyInfo
    footer: MessageFooter
    status: Status

    # Flat keyword-only constructor preserved so both callers (backup and
    # resender) can stay on the same `RenderedMessage(author=..., when=...)`
    # signature without learning the new nested dataclasses.
    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        author: str,
        when: str,
        body: Text,
        edited: bool = False,
        reply_author: Optional[str] = None,
        reply_text: Optional[str] = None,
        media_summary: Optional[list[str]] = None,
        reactions: Optional[str] = None,
        note: Optional[str] = None,
        lot_ids: Sequence[int] = (),
        progress_label: Optional[str] = None,
        status: Status = "normal",
    ) -> None:
        self.header = MessageHeader(author=author, when=when, edited=edited)
        self.body = body
        self.reply = ReplyInfo(author=reply_author, text=reply_text)
        self.footer = MessageFooter(
            media_summary=list(media_summary) if media_summary else [],
            reactions=reactions,
            note=note,
            lot_ids=lot_ids,
            progress_label=progress_label,
        )
        self.status = status

    @property
    def author(self) -> str:
        """Author display name (delegates to :attr:`header`)."""
        return self.header.author

    @property
    def when(self) -> str:
        """Formatted timestamp (delegates to :attr:`header`)."""
        return self.header.when

    @property
    def edited(self) -> bool:
        """Whether the message has been edited (delegates to :attr:`header`)."""
        return self.header.edited

    @property
    def reply_author(self) -> Optional[str]:
        """Quoted-reply author, if any (delegates to :attr:`reply`)."""
        return self.reply.author

    @property
    def reply_text(self) -> Optional[str]:
        """Quoted-reply text, if any (delegates to :attr:`reply`)."""
        return self.reply.text

    @property
    def media_summary(self) -> list[str]:
        """Short Russian labels for attached media (delegates to :attr:`footer`)."""
        return self.footer.media_summary

    @property
    def reactions(self) -> Optional[str]:
        """Plain reactions string (delegates to :attr:`footer`)."""
        return self.footer.reactions

    @property
    def note(self) -> Optional[str]:
        """Optional status note shown below the body (delegates to :attr:`footer`)."""
        return self.footer.note

    @property
    def lot_ids(self) -> Sequence[int]:
        """Telegram message ids covered by this rendering (delegates to :attr:`footer`)."""
        return self.footer.lot_ids

    @property
    def progress_label(self) -> Optional[str]:
        """Progress indicator like ``"12/847"`` (delegates to :attr:`footer`)."""
        return self.footer.progress_label


def _quoted(text: Text, color: str = "cyan") -> Text:
    """Prefix each line with a coloured `▎ ` bar — emulates a quote block."""
    out = Text()
    lines = text.split("\n", allow_blank=True)
    for i, line in enumerate(lines):
        if i:
            out.append("\n")
        out.append("▎ ", style=color)
        out.append_text(line)
    return out


def _clip(text: Text, max_lines: int = MAX_BODY_LINES) -> Text:
    """Truncate long bodies; report how many lines were dropped."""
    lines = text.split("\n", allow_blank=True)
    if len(lines) <= max_lines:
        return text
    kept = lines[:max_lines]
    out = Text()
    for i, line in enumerate(kept):
        if i:
            out.append("\n")
        out.append_text(line)
    out.append(f"\n… (+{len(lines) - max_lines} more lines)", style="dim italic")
    return out


def _title(rm: RenderedMessage) -> Text:
    t = Text()
    t.append("👤 ", style="bold")
    t.append(rm.header.author, style="bold cyan")
    t.append("  ·  ", style="dim")
    t.append(rm.header.when, style="dim")
    if rm.header.edited:
        t.append("  (ред.)", style="dim italic")
    return t


def _subtitle(rm: RenderedMessage) -> Optional[Text]:
    parts: list[Text] = []
    if rm.footer.progress_label:
        parts.append(Text(f"lot {rm.footer.progress_label}", style="dim"))
    if rm.footer.lot_ids:
        ids = ", ".join(str(i) for i in rm.footer.lot_ids)
        parts.append(Text(f"ids=[{ids}]", style="dim"))
    if not parts:
        return None
    sep = Text(" · ", style="dim")
    out = Text()
    for i, p in enumerate(parts):
        if i:
            out.append_text(sep)
        out.append_text(p)
    return out


def render_message_panel(rm: RenderedMessage) -> Panel:
    """Renders ``rm`` as a Rich ``Panel`` mimicking the Telegram client.

    Sections are stacked in the order they appear in chat: reply quote,
    body, media line, reactions, optional note. Border colour is driven by
    ``rm.status`` via :data:`BORDER_BY_STATUS`.
    """
    blocks: list[RenderableType] = []

    if rm.reply.author or rm.reply.text:
        head = Text("↪ ", style="cyan")
        if rm.reply.author:
            head.append(rm.reply.author, style="bold")
            head.append(": ", style="dim")
        if rm.reply.text:
            head.append(rm.reply.text, style="italic")
        blocks.append(_quoted(head, color="cyan"))
        blocks.append(Text())  # blank spacer

    body = _clip(rm.body)
    if len(body) > 0:
        blocks.append(body)

    if rm.footer.media_summary:
        media_line = Text()
        media_line.append("📎 ", style="bold")
        media_line.append(" · ".join(rm.footer.media_summary), style="magenta")
        if blocks:
            blocks.append(Text())
        blocks.append(media_line)

    if rm.footer.reactions:
        if blocks:
            blocks.append(Text())
        blocks.append(Text(rm.footer.reactions, style="dim"))

    if rm.footer.note:
        if blocks:
            blocks.append(Text())
        blocks.append(Text(rm.footer.note, style="yellow italic"))

    if not blocks:
        blocks.append(Text("[пустое сообщение]", style="dim italic"))

    border = BORDER_BY_STATUS.get(rm.status, "grey50")
    subtitle = _subtitle(rm)

    return Panel(
        Group(*blocks),
        title=_title(rm),
        title_align="left",
        subtitle=subtitle,
        subtitle_align="right",
        border_style=border,
        padding=(0, 1),
    )


_WS_RE = re.compile(r"\s+")


def _flatten_one_line(text: Text, limit: int = 80) -> str:
    raw = _WS_RE.sub(" ", text.plain).strip()
    if len(raw) > limit:
        raw = raw[: limit - 1].rstrip() + "…"
    return raw


def render_message_oneline(rm: RenderedMessage) -> Text:
    """A compact `[OK] id · time · author · "preview" · media` line."""
    badge_style = {
        "normal": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "skipped": "bold grey50",
    }.get(rm.status, "bold green")
    badge_label = {
        "normal": "[ OK ]",
        "warning": "[WARN]",
        "error": "[FAIL]",
        "skipped": "[SKIP]",
    }.get(rm.status, "[ OK ]")

    out = Text()
    out.append(badge_label, style=badge_style)
    if rm.footer.lot_ids:
        out.append(" #")
        out.append(",".join(str(i) for i in rm.footer.lot_ids), style="cyan")
    out.append(" · ", style="dim")
    out.append(rm.header.when, style="dim")
    out.append(" · ", style="dim")
    out.append(rm.header.author, style="bold")
    body_preview = _flatten_one_line(rm.body)
    if body_preview:
        out.append(' · "', style="dim")
        out.append(body_preview)
        out.append('"', style="dim")
    if rm.footer.media_summary:
        out.append(" · ", style="dim")
        out.append("📎 " + ",".join(rm.footer.media_summary), style="magenta")
    if rm.footer.note:
        out.append(" · ", style="dim")
        out.append(rm.footer.note, style="yellow")
    return out


def render_summary_table(
    *,
    title: str,
    rows: Iterable[tuple[str, str, Optional[str]]],
    footer: Optional[Sequence[str]] = None,
) -> Panel:
    """Two-column "metric → value" table wrapped in a Panel.

    Each row is (label, value, style_or_None) — colour applies to the value.
    `footer` lines (e.g. paths to log files) are appended below the table.
    """
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="bold")
    table.add_column()
    for label, value, style in rows:
        v = Text(value, style=style or "")
        table.add_row(Text(label, style="dim"), v)

    body: list[RenderableType] = [table]
    if footer:
        body.append(Text())
        for line in footer:
            body.append(Text(line, style="dim"))

    return Panel(
        Group(*body),
        title=Text(title, style="bold"),
        title_align="left",
        border_style="green",
        padding=(0, 1),
    )
