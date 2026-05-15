"""Convert live Telethon `Message` objects into our display model.

The `backup` script operates on Telethon TLObjects (no `dump.json` in
between), so we need a small adapter that produces the same
`RenderedMessage` the resender constructs from JSON dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Union

from telethon.errors import RPCError

from .entity_render import render_entities_to_text
from .message_view import RenderedMessage


def _format_when(dt: Any) -> str:
    """Formats a ``datetime`` as ``HH:MM · YYYY Mon DD`` (passthrough otherwise)."""
    if isinstance(dt, datetime):
        return dt.strftime("%H:%M · %Y %b %d")
    return str(dt)


def _entities_to_dicts(entities: Optional[list[Any]]) -> list[dict]:
    """Flattens Telethon entity TLObjects into the dict shape used by
    :func:`render_entities_to_text` (``_`` is the class name)."""
    if not entities:
        return []
    out: list[dict] = []
    for e in entities:
        d: dict = {"_": type(e).__name__}
        for attr in ("offset", "length", "url", "user_id", "language", "document_id"):
            v = getattr(e, attr, None)
            if v is not None:
                d[attr] = v
        out.append(d)
    return out


def _document_kinds(media: Any) -> list[str]:
    """Russian labels for the attributes of ``MessageMediaDocument``."""
    doc = getattr(media, "document", None)
    attrs = getattr(doc, "attributes", None) or []
    kinds: list[str] = []
    for a in attrs:
        an = type(a).__name__
        if an == "DocumentAttributeAudio":
            kinds.append("голосовое" if getattr(a, "voice", False) else "аудио")
        elif an == "DocumentAttributeVideo":
            kinds.append("кружок" if getattr(a, "round_message", False) else "видео")
        elif an == "DocumentAttributeAnimated":
            kinds.append("гиф")
        elif an == "DocumentAttributeSticker":
            kinds.append("стикер")
    return kinds or ["файл"]


_MediaLabel = Union[list[str], Callable[[Any], list[str]]]

_MEDIA_LABELS: dict[str, _MediaLabel] = {
    "MessageMediaPhoto": ["фото"],
    "MessageMediaDocument": _document_kinds,
    "MessageMediaPoll": ["опрос"],
    "MessageMediaGeo": ["геолокация"],
    "MessageMediaGeoLive": ["геолокация"],
    "MessageMediaWebPage": ["ссылка"],
    "MessageMediaDice": ["кубик"],
    "MessageMediaStory": ["story"],
}


def _media_summary(media: Any) -> list[str]:
    """Returns short Russian labels describing the media (``["фото"]``, etc.)."""
    if media is None:
        return []
    name = type(media).__name__
    label = _MEDIA_LABELS.get(name)
    if label is None:
        return [name]
    if callable(label):
        return label(media)
    return list(label)


def _format_reactions(reactions: Any) -> Optional[str]:
    """Renders ``MessageReactions`` as ``"❤️×3 👍×2"`` (custom/paid → ``⭐``)."""
    if reactions is None:
        return None
    results = getattr(reactions, "results", None) or []
    parts: list[str] = []
    for res in results:
        cnt = getattr(res, "count", 0) or 0
        if cnt <= 0:
            continue
        reac = getattr(res, "reaction", None)
        if reac is None:
            continue
        rn = type(reac).__name__
        if rn == "ReactionEmoji":
            emo = getattr(reac, "emoticon", None) or "?"
        elif rn in ("ReactionCustomEmoji", "ReactionPaid"):
            emo = "⭐"
        else:
            continue
        parts.append(f"{emo}×{cnt}")
    return " ".join(parts) if parts else None


def _extract_sender_id(msg: Any) -> Optional[int]:
    """Pull the sender user id from ``msg`` (prefers ``fwd_from``)."""
    fwd = getattr(msg, "fwd_from", None)
    if fwd is not None:
        from_id = getattr(fwd, "from_id", None)
        sender_id = getattr(from_id, "user_id", None) if from_id is not None else None
        if sender_id is not None:
            return sender_id
    sender_id = getattr(msg, "sender_id", None)
    if sender_id is not None:
        return sender_id
    from_id = getattr(msg, "from_id", None)
    return getattr(from_id, "user_id", None) if from_id is not None else None


async def _resolve_author(client: Any, msg: Any, cache: dict[int, str]) -> str:
    """Resolves the author display name, with ``cache`` keyed by user id.

    Prefers the ``fwd_from`` sender (for forwarded posts) over the actual
    message sender. Falls back to ``"user{id}"`` when ``client`` is ``None``
    or :meth:`get_entity` fails.
    """
    fwd = getattr(msg, "fwd_from", None)
    sender_id = _extract_sender_id(msg)
    if sender_id is None:
        if fwd is not None:
            name = getattr(fwd, "from_name", None)
            if name:
                return name
        return "Unknown User"
    cached = cache.get(sender_id)
    if cached:
        return cached
    if client is None:
        label = f"user{sender_id}"
        cache[sender_id] = label
        return label
    try:
        ent = await client.get_entity(sender_id)
    except (RPCError, ValueError, TypeError):
        label = f"user{sender_id}"
        cache[sender_id] = label
        return label
    parts = [getattr(ent, "first_name", "") or "", getattr(ent, "last_name", "") or ""]
    label = " ".join(p for p in parts if p).strip()
    if not label:
        username = getattr(ent, "username", None)
        label = f"@{username}" if username else f"user{sender_id}"
    cache[sender_id] = label
    return label


@dataclass
class TelethonRenderCtx:
    """Resolution helpers and display flags for :func:`build_rendered_from_telethon`."""

    client: Any = None
    user_cache: Optional[dict[int, str]] = None
    reveal_spoilers: bool = False
    status: str = "normal"
    note: Optional[str] = None


async def build_rendered_from_telethon(
    msg: Any,
    ctx: TelethonRenderCtx,
) -> RenderedMessage:
    """Adapts a live Telethon ``Message`` to a :class:`RenderedMessage`.

    The author is resolved through ``ctx.client.get_entity`` and memoised in
    ``ctx.user_cache`` to avoid repeated RPCs across a backup run. Entities,
    media and reactions are converted via the private helpers in this
    module. When ``ctx.client`` is ``None`` (e.g. dry-run/preview),
    name resolution degrades to ``"user{id}"`` without raising.
    """
    cache = ctx.user_cache if ctx.user_cache is not None else {}
    author = await _resolve_author(ctx.client, msg, cache)
    text = getattr(msg, "message", None) or ""
    entities = _entities_to_dicts(getattr(msg, "entities", None))
    body = render_entities_to_text(text, entities, reveal_spoilers=ctx.reveal_spoilers)
    media = _media_summary(getattr(msg, "media", None))
    reactions = _format_reactions(getattr(msg, "reactions", None))
    when = _format_when(getattr(msg, "date", None))
    edited = bool(getattr(msg, "edit_date", None))
    return RenderedMessage(
        author=author,
        when=when,
        edited=edited,
        body=body,
        media_summary=media,
        reactions=reactions,
        note=ctx.note,
        lot_ids=(getattr(msg, "id", 0),),
        status=ctx.status,  # type: ignore[arg-type]
    )
