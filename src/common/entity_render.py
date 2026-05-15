"""Render Telegram message text + entities as a styled `rich.text.Text`.

Mirrors the algorithm of `resender.render_entities` (which produces HTML),
but applies `Text.stylize(...)` instead of emitting tags. The same UTF-16
offset arithmetic is preserved so entity ranges line up byte-for-byte with
what Telegram sees.
"""

from __future__ import annotations

from typing import Optional

from rich.text import Text


# Visual style per Telegram MessageEntity type. Tweak in one place to retheme.
ENTITY_STYLES: dict[str, str] = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strike",
    "MessageEntityCode": "bold cyan on grey15",
    "MessageEntityPre": "bold cyan on grey15",
    "MessageEntityBlockquote": "grey70",
    "MessageEntitySpoiler": "grey23 on grey23",
    "MessageEntityTextUrl": "underline blue",
    "MessageEntityUrl": "underline blue",
    "MessageEntityMention": "bold blue",
    "MessageEntityMentionName": "bold blue",
    "MessageEntityHashtag": "blue",
    "MessageEntityCashtag": "blue",
    "MessageEntityEmail": "underline blue",
    "MessageEntityPhone": "blue",
    "MessageEntityBotCommand": "magenta",
    "MessageEntityCustomEmoji": "dim",
}


def _utf16_to_char_index(text: str) -> list[int]:
    """Map each UTF-16 code unit boundary to a Python character index.

    Returns a list `m` of length `n_utf16_units + 1` such that `m[k]` is the
    Python string index right after the k-th UTF-16 code unit. Characters
    outside the BMP occupy two UTF-16 units; this lets us translate
    Telegram's UTF-16 offsets to Python char positions without re-decoding
    the bytes repeatedly.
    """
    mapping: list[int] = [0]
    for i, ch in enumerate(text):
        units = 2 if ord(ch) > 0xFFFF else 1
        for _ in range(units):
            mapping.append(i + 1)
    return mapping


def render_entities_to_text(
    text: str,
    entities: Optional[list[dict]],
    *,
    reveal_spoilers: bool = False,
    style_overrides: Optional[dict[str, str]] = None,
) -> Text:
    """Build a `rich.text.Text` with Telegram entity styles applied.

    Unsupported entity types (those absent from `ENTITY_STYLES`) leave the
    underlying text untouched — mirroring how `_entity_tags` returns `None`
    for url/mention/hashtag/etc. that Telegram autodetects.
    """
    if not text:
        return Text()
    result = Text(text)
    if not entities:
        return result

    styles = dict(ENTITY_STYLES)
    if style_overrides:
        styles.update(style_overrides)
    if reveal_spoilers:
        styles["MessageEntitySpoiler"] = "on grey50"

    u2c = _utf16_to_char_index(text)
    n_units = len(u2c) - 1

    # Apply outermost (longest) first so inner stylize calls layer on top.
    sorted_e = sorted(
        entities,
        key=lambda e: (e.get("offset") or 0, -(e.get("length") or 0)),
    )
    for e in sorted_e:
        t = e.get("_")
        style = styles.get(t)
        if style is None:
            continue
        offset = e.get("offset") or 0
        length = e.get("length") or 0
        if length <= 0:
            continue
        u_start = max(0, min(offset, n_units))
        u_end = max(u_start, min(offset + length, n_units))
        c_start = u2c[u_start]
        c_end = u2c[u_end]
        if c_end <= c_start:
            continue
        # For TextUrl, also embed an OSC-8 hyperlink — clickable in modern
        # terminals (iTerm2, recent VS Code, kitty, wezterm).
        if t == "MessageEntityTextUrl":
            url = e.get("url") or ""
            if url:
                result.stylize(f"underline blue link {url}", c_start, c_end)
                continue
        result.stylize(style, c_start, c_end)
    return result
