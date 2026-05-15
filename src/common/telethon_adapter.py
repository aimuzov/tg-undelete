"""Convert live Telethon `Message` objects into our display model.

The `backup` script operates on Telethon TLObjects (no `dump.json` in
between), so we need a small adapter that produces the same
`RenderedMessage` the resender constructs from JSON dicts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

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


def _media_summary(media: Any) -> list[str]:
    """Returns short Russian labels describing the media (``["фото"]``, etc.)."""
    if media is None:
        return []
    name = type(media).__name__
    if name == "MessageMediaPhoto":
        return ["фото"]
    if name == "MessageMediaDocument":
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
    if name == "MessageMediaPoll":
        return ["опрос"]
    if name in ("MessageMediaGeo", "MessageMediaGeoLive"):
        return ["геолокация"]
    if name == "MessageMediaWebPage":
        return ["ссылка"]
    if name == "MessageMediaDice":
        return ["кубик"]
    if name == "MessageMediaStory":
        return ["story"]
    return [name]


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


async def _resolve_author(client: Any, msg: Any, cache: dict[int, str]) -> str:
    """Resolves the author display name, with ``cache`` keyed by user id.

    Prefers the ``fwd_from`` sender (for forwarded posts) over the actual
    message sender. Falls back to ``"user{id}"`` when ``client`` is ``None``
    or :meth:`get_entity` fails.
    """
    sender_id: Optional[int] = None
    fwd = getattr(msg, "fwd_from", None)
    if fwd is not None:
        from_id = getattr(fwd, "from_id", None)
        sender_id = getattr(from_id, "user_id", None) if from_id is not None else None
        if sender_id is None:
            name = getattr(fwd, "from_name", None)
            if name:
                return name
    if sender_id is None:
        sender_id = getattr(msg, "sender_id", None)
        if sender_id is None:
            from_id = getattr(msg, "from_id", None)
            sender_id = getattr(from_id, "user_id", None) if from_id is not None else None
    if sender_id is None:
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
    except Exception:
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


async def build_rendered_from_telethon(
    msg: Any,
    *,
    client: Any = None,
    user_cache: Optional[dict[int, str]] = None,
    reveal_spoilers: bool = False,
    status: str = "normal",
    note: Optional[str] = None,
) -> RenderedMessage:
    """Adapts a live Telethon ``Message`` to a :class:`RenderedMessage`.

    The author is resolved through ``client.get_entity`` and memoised in
    ``user_cache`` to avoid repeated RPCs across a backup run. Entities,
    media and reactions are converted via the private helpers in this
    module. When ``client`` is ``None`` (e.g. dry-run/preview),
    name resolution degrades to ``"user{id}"`` without raising.
    """
    cache = user_cache if user_cache is not None else {}
    author = await _resolve_author(client, msg, cache)
    text = getattr(msg, "message", None) or ""
    entities = _entities_to_dicts(getattr(msg, "entities", None))
    body = render_entities_to_text(text, entities, reveal_spoilers=reveal_spoilers)
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
        note=note,
        lot_ids=(getattr(msg, "id", 0),),
        status=status,  # type: ignore[arg-type]
    )
