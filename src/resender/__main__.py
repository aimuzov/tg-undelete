"""
Re-upload previously rescued messages back into the SAME forum-supergroup
and the SAME topics.

Reads `backup_will_be_inside_me/dump.json` produced by `src.backup`, sorts
messages chronologically, groups albums (`grouped_id`), and posts every
message into the original topic (`reply_to.reply_to_top_id` or, if absent,
`reply_to.reply_to_msg_id` when `forum_topic=True`).

Since Telegram does not allow posting on behalf of another user, the original
author and date are placed into the prefix of the message/caption:

    <blockquote><b>Имя Фамилия • 11:55 • 2025 May 12</b></blockquote>

    <тело сообщения>

State is persisted between runs in
`backup_will_be_inside_me/resender_state.json`, so the script can be safely
restarted — already-sent messages will be skipped.
"""

import argparse
import asyncio
import glob
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import (
    ChannelParticipantsBanned,
    ChannelParticipantsKicked,
    DocumentAttributeAnimated,
    DocumentAttributeVideo,
    InputGeoPoint,
    InputMediaGeoPoint,
    PeerChannel,
)

from src.common.backup_layout import chat_layout, prompt_chat_id
from src.common.checkbox import undelete_checkbox
from src.common.console import (
    STDOUT_CONSOLE,
    error,
    info,
    prompt_text,
    success,
    warn,
)
from src.common.entity_render import render_entities_to_text
from src.common.message_view import (
    RenderedMessage,
    render_message_oneline,
    render_message_panel,
    render_summary_table,
)
from src.common.progress import make_lot_progress


# All on-disk paths are derived per chat from --output-folder + chat-id; see
# `src.common.backup_layout`. Only the *file names* are fixed here.
DEFAULT_OUTPUT_FOLDER: str = "backups"
STATE_FILENAME: str = "resender_state.json"
USERS_CACHE_FILENAME: str = "users_cache.json"
MISSING_LOG_FILENAME: str = "resender_missing_media.log"
ERROR_LOG_FILENAME: str = "resender_errors.log"

DEFAULT_SESSION_NAME: str = "session_name"

# Pacing — be gentle with the API. FloodWaitError will be honoured explicitly
# on top of these base delays. Text-only messages can be sent more rapidly
# than media uploads.
SLEEP_AFTER_TEXT: float = 0.3
SLEEP_AFTER_MEDIA: float = 1.0
CAPTION_LIMIT: int = 1024

# Preview truncation lengths for quoted/embedded text. The short variant is
# used inside blockquote chips next to message bodies; the regular one — for
# fuller reply previews.
PREVIEW_LEN_SHORT: int = 80
PREVIEW_LEN: int = 200

# All timestamps in the dump are stored in UTC. We display them in Samara
# local time (UTC+4) — no timezone suffix, just the wall-clock value.
DISPLAY_TZ = timezone(timedelta(hours=4))


# ---------- dump loading & album grouping ----------


def load_dump(messages_path: str) -> list[dict]:
    """Parses the comma-separated stream of Message objects written by backup."""
    with open(messages_path, "r", encoding="utf-8") as f:
        raw = f.read().strip().rstrip(",")
    data = json.loads("[" + raw + "]")
    messages = [m for m in data if m.get("_") == "Message"]
    messages.sort(key=lambda m: (m["date"], m["id"]))
    return messages


def group_albums(messages: list[dict]) -> list[list[dict]]:
    """
    Collapses runs of messages sharing a non-empty `grouped_id` (in our
    chronological order) into a single lot. Standalone messages become
    one-element lots.
    """
    lots: list[list[dict]] = []
    current: list[dict] = []
    current_gid: Optional[int] = None
    for m in messages:
        gid = m.get("grouped_id")
        if gid is not None and gid == current_gid:
            current.append(m)
            continue
        if current:
            lots.append(current)
        current = [m]
        current_gid = gid
    if current:
        lots.append(current)
    return lots


# ---------- state persistence ----------


def load_state(state_path: str) -> dict[str, Any]:
    """Loads resender state ``{"id_map": {old_id: new_id}}`` from disk.

    Returns an empty skeleton when the file is missing. Keys/values in
    ``id_map`` are coerced back to ``int`` because JSON serialises them
    as strings.
    """
    if not os.path.exists(state_path):
        return {"id_map": {}}
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("id_map", {})
    state["id_map"] = {int(k): int(v) for k, v in state["id_map"].items()}
    return state


def save_state(state_path: str, state: dict[str, Any]) -> None:
    """Writes ``state`` atomically (``.tmp`` + ``os.replace``).

    The atomic swap guarantees the original file stays intact if the
    process is killed mid-flush, so a crashed resender run can resume
    from the previous checkpoint without losing the id mapping.
    """
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {"id_map": {str(k): v for k, v in state["id_map"].items()}},
            f,
            ensure_ascii=False,
        )
    os.replace(tmp, state_path)


def load_users_cache(users_cache_path: str) -> dict[int, str]:
    """Loads the ``{user_id: display_name}`` cache from disk.

    Returns ``{}`` on any I/O or JSON error (and warns) so a corrupted
    cache never blocks a run — names will simply be rebuilt by
    :func:`prefetch_chat_participants` on the next start.
    """
    if not os.path.exists(users_cache_path):
        return {}
    try:
        with open(users_cache_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}
    except (ValueError, OSError) as exc:
        warn(f"users_cache.json unreadable, ignoring: {exc}")
        return {}


def save_users_cache(users_cache_path: str, cache: dict[int, str]) -> None:
    """Writes the user-name cache atomically (``.tmp`` + ``os.replace``)."""
    tmp = users_cache_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {str(k): v for k, v in cache.items()}, f, ensure_ascii=False
        )
    os.replace(tmp, users_cache_path)


def append_line(path: str, line: str) -> None:
    """Appends ``line`` plus a trailing newline to ``path`` (used by logs)."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- topic / reply resolution ----------


def resolve_topic_and_reply(
    msg: dict, id_map: dict[int, int], dump_index: dict[int, dict]
) -> tuple[Optional[int], Optional[int]]:
    """
    Returns (topic_root, reply_target).
    - topic_root: id of the forum topic's root message; None for General.
    - reply_target: id to attach the reply to.
      * topic_root, if there is no parent or the parent IS the topic root;
      * the new id from id_map, if we have already re-sent the parent;
      * the ORIGINAL parent id, if the parent is not in the dump (i.e. it
        was never deleted and is still alive in the chat — Telegram will
        resolve it on its own);
      * topic_root, if the parent was deleted (in the dump) but we have
        not re-sent it yet — fallback to at least land in the right topic.
    """
    topic_root = topic_of(msg)
    rt = msg.get("reply_to") or {}
    parent = rt.get("reply_to_msg_id")
    if parent is None or parent == topic_root:
        return topic_root, topic_root
    mapped = id_map.get(parent)
    if mapped is not None:
        return topic_root, mapped
    if parent not in dump_index:
        # Parent never got deleted — it's still alive in the chat under
        # its original id. Telegram will resolve the reply correctly.
        return topic_root, parent
    return topic_root, topic_root


def make_reply_to(
    topic_root: Optional[int], reply_target: Optional[int]
) -> Optional[int]:
    """
    Telethon's `send_message`/`send_file` accept `reply_to` only as an int
    (or a Message). Passing a low-level InputReplyToMessage raises
    'Invalid message type'. Plain int is enough: when the target is a
    message inside a forum topic, Telethon discovers `top_msg_id` from
    the parent automatically.
    """
    return reply_target


# ---------- caption building ----------


def format_datetime_parts(iso: str) -> tuple[str, str]:
    """Returns (date_str, time_str) in display TZ, e.g. ('2025 May 12', '11:55')."""
    dt = datetime.fromisoformat(iso).astimezone(DISPLAY_TZ)
    return dt.strftime("%Y %b %d"), dt.strftime("%H:%M")


def _user_label_html(user: Any) -> str:
    """
    Renders one chat participant as an HTML fragment for the prefix:
    a clickable link wrapping the display name. Falls back to @username,
    then to user{id}. Username links use https://t.me/<username>; if there
    is no username we use the tg://user?id=<id> deep-link, which the official
    Telegram clients resolve to the user's profile.
    """
    full_name = " ".join(
        filter(
            None,
            [
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
            ],
        )
    )
    username = getattr(user, "username", None)
    uid = getattr(user, "id", None)

    if full_name:
        label = html.escape(full_name)
    elif username:
        label = f"@{html.escape(username)}"
    elif uid is not None:
        label = f"user{uid}"
    else:
        return "Unknown User"

    if username:
        return f'<a href="https://t.me/{username}">{label}</a>'
    if uid is not None:
        return f'<a href="tg://user?id={uid}">{label}</a>'
    return label


async def prefetch_chat_participants(
    client: TelegramClient, entity: Any, cache: dict[int, str]
) -> None:
    """
    Pre-populates user_cache from current chat participants. This is the
    ONLY source we trust for author names — a bare `client.get_entity(int)`
    is known to return the logged-in user as a silent fallback when the id
    is not in the session cache, which corrupts every prefix in the dump.
    """
    print("Prefetching chat participants for name resolution...")
    sample: list[str] = []

    async def absorb(filter_obj: Any, label: str) -> int:
        """Pulls one participant slice (active/kicked/banned) into ``cache``."""
        local = 0
        try:
            async for user in client.iter_participants(entity, filter=filter_obj):
                rendered = _user_label_html(user)
                # Don't overwrite a fresher record from the "active" pass.
                cache.setdefault(user.id, rendered)
                local += 1
                if len(sample) < 5:
                    sample.append(f"{user.id}={rendered}")
        except RPCError as exc:
            print(f"  ! iter_participants({label}) failed: {exc}")
        return local

    active = await absorb(None, "active")
    # Former members: for chat admins Telegram exposes "kicked" and "banned"
    # filters, which covers users who left or were removed.
    kicked = await absorb(ChannelParticipantsKicked(""), "kicked")
    banned = await absorb(ChannelParticipantsBanned(""), "banned")
    print(
        f"  cached {len(cache)} participants "
        f"(active={active}, kicked={kicked}, banned={banned}). sample: {sample}"
    )


def _seed_fwd_names(messages: list[dict], cache: dict[int, str]) -> int:
    """
    For imported/forwarded messages Telegram sometimes ships a plain-string
    `fwd_from.from_name` alongside the numeric id (or instead of it). Use that
    name as a fallback label when the id is not in the participant cache.
    Returns the number of ids seeded.
    """
    seeded = 0
    for m in messages:
        fwd = m.get("fwd_from")
        if not fwd:
            continue
        from_name = fwd.get("from_name")
        if not from_name:
            continue
        fid = (fwd.get("from_id") or {}).get("user_id")
        if fid is None or fid in cache:
            continue
        # No link — we only have the plain-text name, no username/access_hash.
        cache[fid] = html.escape(from_name)
        seeded += 1
    return seeded


def get_author_label(from_id: Optional[int], cache: dict[int, str]) -> str:
    """
    Returns an HTML-ready label for `<b>...</b>`. For known users (prefetched
    from the chat) — a clickable link. For unknown ids — a profile deep-link
    by id so the user is still reachable in the official client.
    """
    if from_id is None:
        return "Unknown User"
    cached = cache.get(from_id)
    if cached:
        return cached
    return f'<a href="tg://user?id={from_id}">user{from_id}</a>'


def _entity_tags(entity: dict) -> Optional[tuple[str, str]]:
    """
    Maps a Telegram MessageEntity dict to (open_tag, close_tag) in Telegram-
    flavoured HTML. Returns None for entity types that don't need rendering
    (Telegram autodetects urls/mentions/hashtags etc. from plain text).
    """
    t = entity.get("_")
    if t == "MessageEntityBold":
        return "<b>", "</b>"
    if t == "MessageEntityItalic":
        return "<i>", "</i>"
    if t == "MessageEntityUnderline":
        return "<u>", "</u>"
    if t == "MessageEntityStrike":
        return "<s>", "</s>"
    if t == "MessageEntityCode":
        return "<code>", "</code>"
    if t == "MessageEntityPre":
        lang = entity.get("language") or ""
        if lang:
            return (
                f'<pre><code class="language-{html.escape(lang)}">',
                "</code></pre>",
            )
        return "<pre>", "</pre>"
    if t == "MessageEntityBlockquote":
        return "<blockquote>", "</blockquote>"
    if t == "MessageEntitySpoiler":
        return "<tg-spoiler>", "</tg-spoiler>"
    if t == "MessageEntityTextUrl":
        url = entity.get("url") or ""
        return f'<a href="{html.escape(url, quote=True)}">', "</a>"
    if t == "MessageEntityMentionName":
        uid = entity.get("user_id")
        if uid is not None:
            return f'<a href="tg://user?id={uid}">', "</a>"
    if t == "MessageEntityCustomEmoji":
        doc_id = entity.get("document_id")
        if doc_id is not None:
            return f'<tg-emoji emoji-id="{doc_id}">', "</tg-emoji>"
    return None  # url/mention/hashtag/email/phone — Telegram parses them on its own


def render_entities(text: str, entities: list[dict]) -> str:
    """
    Re-applies Telegram MessageEntity ranges to text and produces HTML.

    Telegram entity offsets are measured in UTF-16 code units, not Python
    characters, so we slice the text via its utf-16-le bytes. Nested entities
    are supported as long as they don't overlap (standard Telegram output).
    Open tags at the same offset are emitted longest-range-first; close tags
    at the same offset are emitted in reverse (LIFO) so the resulting HTML
    is well-nested.
    """
    if not text:
        return ""
    if not entities:
        return html.escape(text)

    utf16 = text.encode("utf-16-le")

    opens: dict[int, list[str]] = {}
    closes: dict[int, list[str]] = {}
    sorted_e = sorted(
        entities, key=lambda e: (e.get("offset") or 0, -(e.get("length") or 0))
    )
    for e in sorted_e:
        offset = e.get("offset") or 0
        length = e.get("length") or 0
        if length <= 0:
            continue
        tags = _entity_tags(e)
        if tags is None:
            continue
        open_tag, close_tag = tags
        opens.setdefault(offset, []).append(open_tag)
        # Close tags emitted in reverse order of opening — prepend.
        closes.setdefault(offset + length, []).insert(0, close_tag)

    n_units = len(utf16) // 2
    out: list[str] = []
    cursor = 0
    positions = sorted(set(opens.keys()) | set(closes.keys()))
    for pos in positions:
        if pos > cursor:
            chunk = utf16[cursor * 2 : pos * 2].decode("utf-16-le", errors="replace")
            out.append(html.escape(chunk))
            cursor = pos
        for tag in closes.get(pos, []):
            out.append(tag)
        for tag in opens.get(pos, []):
            out.append(tag)
    if cursor < n_units:
        tail = utf16[cursor * 2 :].decode("utf-16-le", errors="replace")
        out.append(html.escape(tail))
    return "".join(out)


def build_body_html(
    message_text: str, entities: list[dict], quote_text: Optional[str]
) -> str:
    """Assembles the caption body: entity-styled text plus an optional reply quote.

    ``quote_text`` (when present) is HTML-escaped and wrapped in
    ``<blockquote>``, then prepended with a blank line before the body
    so the original message visually nests under the reply preview.
    """
    body_html = render_entities(message_text, entities) if message_text else ""
    if quote_text:
        block = f"<blockquote>{html.escape(quote_text)}</blockquote>"
        body_html = block + ("\n\n" + body_html if body_html else "")
    return body_html


def lot_has_web_preview(lot: list[dict]) -> bool:
    """Returns ``True`` if any message in ``lot`` carries a web-page preview.

    Drives ``link_preview=True`` in :func:`_send_text_only` so Telegram
    re-renders the link card the original message had.
    """
    for m in lot:
        media = m.get("media") or {}
        if media.get("_") == "MessageMediaWebPage":
            return True
    return False


def lot_has_geo(lot: list[dict]) -> bool:
    """Returns ``True`` if any message in ``lot`` is a geolocation point.

    Geo lots take the ``_send_geo`` branch in :func:`send_lot` because
    Telegram has no "send geo with album" endpoint — they always go solo.
    """
    for m in lot:
        media = m.get("media") or {}
        if media.get("_") in ("MessageMediaGeo", "MessageMediaGeoLive"):
            return True
    return False


def collect_chosen_reactions(msg: dict) -> list[str]:
    """
    Returns emojis the logged-in user ticked on this message in the original
    chat — those are the ones we replay via `send_reaction`. Custom/paid
    reactions are skipped (we can't replay them faithfully).
    """
    r = msg.get("reactions") or {}
    results = r.get("results") or []
    out: list[str] = []
    for res in results:
        if res.get("chosen_order") is None:
            continue
        reac = res.get("reaction") or {}
        if reac.get("_") != "ReactionEmoji":
            continue
        emo = reac.get("emoticon")
        if emo:
            out.append(emo)
    return out


def render_reactions(msg: dict) -> str:
    """
    Renders reactions as `❤️×3 👍×2 ⭐×1` — but only the ones we can NOT
    replay through `send_reaction`. The logged-in user's own reactions are
    re-applied for real later (see `_post_send_actions`), so we subtract
    them from the count here to avoid double-printing them in the caption.
    Custom and paid reactions always go textual (we can't replay them
    faithfully), collapsed under ⭐.
    """
    r = msg.get("reactions") or {}
    results = r.get("results") or []
    parts: list[str] = []
    for res in results:
        cnt = res.get("count") or 0
        reac = res.get("reaction") or {}
        kind = reac.get("_")
        chosen = res.get("chosen_order") is not None
        # Standard emoji reactions are replayed for real if we placed them;
        # only the foreign portion stays in the text.
        if kind == "ReactionEmoji" and chosen:
            cnt -= 1
        if cnt <= 0:
            continue
        if kind == "ReactionEmoji":
            emoji = reac.get("emoticon") or "?"
        elif kind in ("ReactionCustomEmoji", "ReactionPaid"):
            emoji = "⭐"
        else:
            continue
        parts.append(f"{emoji}×{cnt}")
    if not parts:
        return ""
    return "<i>" + html.escape(" ").join(parts) + "</i>"


def short_media_kind_ru(msg: dict) -> str:
    """Returns a one-word Russian label for the message's media type.

    Used both for the textual placeholder shown when a media file is
    missing on disk and for :attr:`RenderedMessage.media_summary` in
    dry-run previews.
    """
    media = msg.get("media") or {}
    t = media.get("_")
    if t == "MessageMediaPhoto":
        return "фото"
    if t == "MessageMediaDocument":
        kind = media_kind(msg)
        return {
            "voice": "голосовое",
            "video_note": "кружок",
            "gif": "гиф",
            "sticker": "стикер",
            "document": "файл",
        }.get(kind or "document", "файл")
    if t == "MessageMediaPoll":
        return "опрос"
    if t == "MessageMediaGeo" or t == "MessageMediaGeoLive":
        return "геолокация"
    if t == "MessageMediaStory":
        return "story"
    if t == "MessageMediaDice":
        return "кубик"
    if t == "MessageMediaWebPage":
        return "ссылка"
    return "медиа"


def build_reply_preview(
    msg: dict,
    dump_index: dict[int, dict],
    user_cache: dict[int, str],
    id_map: dict[int, int],
) -> str:
    """
    When the reply target lives in our dump but has not been re-sent yet
    (e.g. the user picked a topic filter that excluded it), render a small
    quoted preview of that parent so context isn't lost.
    """
    rt = msg.get("reply_to") or {}
    parent_id = rt.get("reply_to_msg_id")
    if parent_id is None:
        return ""
    topic_root = topic_of(msg)
    if parent_id == topic_root or parent_id in id_map:
        return ""
    parent = dump_index.get(parent_id)
    if parent is None:
        return ""
    author, _, _ = resolve_original_author_and_date(parent, user_cache)
    text = (parent.get("message") or "").strip().replace("\n", " ")
    if not text:
        text = f"[{short_media_kind_ru(parent)}]"
    if len(text) > PREVIEW_LEN_SHORT:
        text = text[:PREVIEW_LEN_SHORT] + "…"
    return f"<blockquote>↪ {author}: {html.escape(text)}</blockquote>"


def pick_caption_source(lot: list[dict]) -> dict:
    """The 'carrier' message inside an album: first one with text or quote."""
    for m in lot:
        if (m.get("message") or "").strip():
            return m
        rt = m.get("reply_to") or {}
        if rt.get("quote") and (rt.get("quote_text") or "").strip():
            return m
    return lot[0]


def resolve_original_author_and_date(
    msg: dict, user_cache: dict[int, str]
) -> tuple[str, str, str]:
    """
    For Telegram Import / forwarded messages the real author and date live
    inside `fwd_from`, while top-level `from_id`/`date` describe the import
    operation. We prefer fwd_from when present.

    Returns (author_html, date_str, time_str).
    """
    fwd = msg.get("fwd_from") or None
    if fwd:
        fwd_user_id = (fwd.get("from_id") or {}).get("user_id")
        if fwd_user_id is not None:
            author = get_author_label(fwd_user_id, user_cache)
        elif fwd.get("from_name"):
            author = html.escape(fwd["from_name"])
        else:
            author = "Unknown User"
        date_iso = fwd.get("date") or msg["date"]
    else:
        from_user_id = (msg.get("from_id") or {}).get("user_id")
        author = get_author_label(from_user_id, user_cache)
        date_iso = msg["date"]
    date_str, time_str = format_datetime_parts(date_iso)
    return author, date_str, time_str


def build_caption(
    lot: list[dict],
    user_cache: dict[int, str],
    dump_index: dict[int, dict],
    id_map: dict[int, int],
    extra_note: Optional[str] = None,
) -> tuple[str, dict]:
    """
    Returns (caption_html, source_msg). source_msg — the lot element whose
    text/author/date were used. Caption format:

        <blockquote><b>Имя • 11:55 • 2025 May 12 (ред.)</b></blockquote>
        <blockquote>↪ Парент: фрагмент</blockquote>          (if needed)
        <body>
        <i>❤️×3 👍×2</i>                                     (if any)
    """
    src = pick_caption_source(lot)
    author, date, t = resolve_original_author_and_date(src, user_cache)
    rt = src.get("reply_to") or {}
    quote_text = rt.get("quote_text") if rt.get("quote") else None
    body_html = build_body_html(
        src.get("message") or "", src.get("entities") or [], quote_text
    )
    if extra_note:
        body_html = (body_html + "\n\n" + extra_note) if body_html else extra_note

    edit_marker = " (ред.)" if src.get("edit_date") else ""
    reply_preview = build_reply_preview(src, dump_index, user_cache, id_map)
    # Reactions are best collected across the whole album — in the source
    # they attach to the "carrier" message, but if ``src`` was picked from a
    # media-only element the reactions may live on a different one.
    reactions_str = ""
    for m in lot:
        rendered = render_reactions(m)
        if rendered:
            reactions_str = rendered
            break

    header = (
        f"<blockquote><b>{author} • {t} • {date}{edit_marker}</b></blockquote>"
    )
    parts: list[str] = [header]
    if reply_preview:
        parts.append(reply_preview)
    if body_html:
        parts.append(body_html)
    if reactions_str:
        parts.append("")  # blank line before reactions
        parts.append(reactions_str)
    return "\n".join(parts), src


def _clean_author_label(label: str) -> str:
    """Strip the `<a href=…>…</a>` wrapper that `get_author_label` puts
    around unknown users so the name shows up plain in the terminal."""
    if "<a " in label and "</a>" in label:
        try:
            inner = label.split(">", 1)[1].rsplit("<", 1)[0]
            return html.unescape(inner)
        except IndexError:
            pass
    return html.unescape(label)


def build_rendered_for_lot(
    lot: list[dict],
    user_cache: dict[int, str],
    dump_index: dict[int, dict],
    id_map: dict[int, int],
    *,
    idx: int,
    total: int,
    reveal_spoilers: bool,
    extra_note: Optional[str] = None,
    status: str = "normal",
) -> RenderedMessage:
    """Builds a display-ready snapshot for one lot — the rich equivalent of
    `build_caption`. Reuses the same source-message picker so the panel and
    the actual caption stay in sync."""
    src = pick_caption_source(lot)
    author_html, date_str, time_str = resolve_original_author_and_date(
        src, user_cache
    )
    author = _clean_author_label(author_html)

    body = render_entities_to_text(
        src.get("message") or "",
        src.get("entities") or [],
        reveal_spoilers=reveal_spoilers,
    )

    reply_author: Optional[str] = None
    reply_text: Optional[str] = None
    rt = src.get("reply_to") or {}
    if rt.get("quote") and (rt.get("quote_text") or "").strip():
        reply_text = (rt["quote_text"] or "").strip().replace("\n", " ")
        if len(reply_text) > PREVIEW_LEN:
            reply_text = reply_text[:PREVIEW_LEN] + "…"
    else:
        parent_id = rt.get("reply_to_msg_id")
        topic_root = topic_of(src)
        if (
            parent_id is not None
            and parent_id != topic_root
            and parent_id not in id_map
        ):
            parent = dump_index.get(parent_id)
            if parent is not None:
                pa_html, _, _ = resolve_original_author_and_date(
                    parent, user_cache
                )
                reply_author = _clean_author_label(pa_html)
                t = (parent.get("message") or "").strip().replace("\n", " ")
                if not t:
                    t = f"[{short_media_kind_ru(parent)}]"
                if len(t) > PREVIEW_LEN:
                    t = t[:PREVIEW_LEN] + "…"
                reply_text = t

    media_summary: list[str] = []
    for m in lot:
        if m.get("media") is None:
            continue
        media_summary.append(short_media_kind_ru(m))

    reactions_plain: Optional[str] = None
    for m in lot:
        parts: list[str] = []
        for res in (m.get("reactions") or {}).get("results") or []:
            cnt = res.get("count") or 0
            if cnt <= 0:
                continue
            reac = res.get("reaction") or {}
            kind = reac.get("_")
            if kind == "ReactionEmoji":
                parts.append(f"{reac.get('emoticon') or '?'}×{cnt}")
            elif kind in ("ReactionCustomEmoji", "ReactionPaid"):
                parts.append(f"⭐×{cnt}")
        if parts:
            reactions_plain = " ".join(parts)
            break

    edited = bool(src.get("edit_date"))
    when = f"{time_str} · {date_str}"
    lot_ids = [m["id"] for m in lot]

    return RenderedMessage(
        author=author,
        when=when,
        edited=edited,
        body=body,
        reply_author=reply_author,
        reply_text=reply_text,
        media_summary=media_summary,
        reactions=reactions_plain,
        note=extra_note,
        lot_ids=lot_ids,
        progress_label=f"{idx}/{total}",
        status=status,  # type: ignore[arg-type]
    )


# ---------- media file lookup ----------


def find_media_file(media_folder: str, old_id: int) -> Optional[str]:
    """Locates the downloaded media file for a backed-up message id.

    Matches ``<media_folder>/<old_id>.*`` and skips the ``.json`` sidecar
    written by :mod:`src.backup`. Returns the first match or ``None``
    when the file was never downloaded (or was deleted out-of-band).
    """
    matches = glob.glob(os.path.join(media_folder, f"{old_id}.*"))
    matches = [p for p in matches if not p.endswith(".json")]
    return matches[0] if matches else None


def extract_video_attribute(msg: dict) -> Optional[DocumentAttributeVideo]:
    """
    Rebuilds the original `DocumentAttributeVideo` from the dump so Telegram
    renders a proper video player with thumbnail instead of a bare document
    icon. Without this attribute non-mp4 containers (e.g. `.webm`, `.mov`)
    are uploaded as plain files.
    """
    media = msg.get("media") or {}
    if media.get("_") != "MessageMediaDocument":
        return None
    attrs = (media.get("document") or {}).get("attributes") or []
    for a in attrs:
        if a.get("_") != "DocumentAttributeVideo":
            continue
        return DocumentAttributeVideo(
            duration=a.get("duration") or 0,
            w=a.get("w") or 0,
            h=a.get("h") or 0,
            round_message=bool(a.get("round_message")),
            supports_streaming=bool(a.get("supports_streaming")),
            nosound=bool(a.get("nosound")),
        )
    return None


def media_kind(msg: dict) -> Optional[str]:
    """
    Classifies the media attached to a message. We use the result to pass
    the right flag to `send_file` so video notes stay video notes, voice
    notes stay voice notes, etc.

    Returns one of: 'photo', 'voice', 'video_note', 'gif', 'sticker',
    'document'. Returns None for media types that have no file on disk
    (web previews, polls, geo, stories, dice).
    """
    media = msg.get("media") or {}
    t = media.get("_")
    if t == "MessageMediaPhoto":
        return "photo"
    if t != "MessageMediaDocument":
        return None  # web preview / poll / geo / story / dice — no file
    attrs = (media.get("document") or {}).get("attributes") or []
    is_voice = False
    is_round = False
    is_sticker = False
    is_animated = False
    for a in attrs:
        kind = a.get("_")
        if kind == "DocumentAttributeAudio" and a.get("voice"):
            is_voice = True
        elif kind == "DocumentAttributeVideo" and a.get("round_message"):
            is_round = True
        elif kind == "DocumentAttributeSticker":
            is_sticker = True
        elif kind == "DocumentAttributeAnimated":
            is_animated = True
    if is_voice:
        return "voice"
    if is_round:
        return "video_note"
    if is_animated:
        return "gif"
    if is_sticker:
        return "sticker"
    return "document"


def collect_lot_files(
    lot: list[dict], media_folder: str,
) -> tuple[list[tuple[str, str, dict]], list[int]]:
    """
    Returns (existing_files, missing_old_ids) where existing_files is a list
    of (path, kind, msg) tuples. The carrier `msg` is needed so the caller
    can pull DocumentAttributeVideo out of it. Text-only elements
    (media=None) are skipped. Messages whose media has no associated file
    (polls/geo/stories/etc.) are treated as missing so the caller can fall
    back to a text-only message with the standard caption.
    """
    files: list[tuple[str, str, dict]] = []
    missing: list[int] = []
    for m in lot:
        if m.get("media") is None:
            continue
        old_id = m["id"]
        path = find_media_file(media_folder, old_id)
        if path is None:
            missing.append(old_id)
            continue
        kind = media_kind(m) or "document"
        files.append((path, kind, m))
    return files, missing


# ---------- sending ----------


async def _post_send_actions(
    client: TelegramClient,
    entity: Any,
    new_id: int,
    src_for_pin: dict,
    src_for_reactions: dict,
) -> None:
    """Pin the new message if the original was pinned, and replay reactions
    the logged-in user originally placed. Failures are logged but never
    abort the run."""
    if src_for_pin.get("pinned"):
        try:
            await client.pin_message(entity, new_id, notify=False)
        except Exception as exc:  # noqa: BLE001
            warn(f"pin_message({new_id}) failed: {exc}")
    chosen = collect_chosen_reactions(src_for_reactions)
    if chosen:
        try:
            await client.send_reaction(entity, new_id, chosen)
        except Exception as exc:  # noqa: BLE001
            warn(f"send_reaction({new_id}, {chosen}) failed: {exc}")


async def send_lot(
    client: Optional[TelegramClient],
    entity: Any,
    lot: list[dict],
    id_map: dict[int, int],
    user_cache: dict[int, str],
    dump_index: dict[int, dict],
    *,
    media_folder: str,
    missing_log: str,
    error_log: str,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """
    Sends one lot (single message or album). Returns (success, kind) where
    `kind` is 'text' or 'media' — the caller uses it to pick a per-lot
    delay. Updates id_map in place.

    In dry_run the function still computes the caption, file list and the
    reply target (so we exercise the full pipeline), but no API calls are
    made and id_map gets synthetic negative ids so reply-rendering keeps
    behaving as it would for real.
    """
    topic_root, reply_target = resolve_topic_and_reply(
        lot[0], id_map, dump_index
    )
    reply_to = make_reply_to(topic_root, reply_target)

    files, missing = collect_lot_files(lot, media_folder)
    for mid in missing:
        append_line(missing_log, f"{mid}\tmedia file not found in backup folder")

    note: Optional[str] = None
    if missing and not files:
        kind_label = short_media_kind_ru(lot[0])
        note = f"[медиа недоступно: {kind_label}]"
    elif missing:
        note = f"[медиа частично недоступно: {len(missing)} файл(ов)]"

    caption, src = build_caption(
        lot, user_cache, dump_index, id_map, extra_note=note
    )
    caption_too_long = len(caption) > CAPTION_LIMIT

    lot_ids = [m["id"] for m in lot]
    kind_for_pacing = "media" if files else "text"

    if dry_run:
        # Negative ids ensure subsequent reply previews / mapping work the
        # same way they would after a real send.
        synthetic = -lot_ids[0]
        for oid in lot_ids:
            id_map.setdefault(oid, synthetic)
        return True, kind_for_pacing

    use_link_preview = lot_has_web_preview(lot)
    geo_msg = next(
        (m for m in lot if (m.get("media") or {}).get("_")
         in ("MessageMediaGeo", "MessageMediaGeoLive")),
        None,
    )

    while True:
        try:
            if geo_msg is not None and not files:
                return await _send_geo(
                    client, entity, geo_msg, lot, lot_ids, id_map,
                    caption=caption, reply_to=reply_to,
                )

            if not files:
                return await _send_text_only(
                    client, entity, lot, lot_ids, id_map,
                    caption=caption, reply_to=reply_to,
                    use_link_preview=use_link_preview,
                )

            # We have files — send as media / album.
            if caption_too_long:
                text_msg = await client.send_message(
                    entity,
                    message=caption,
                    parse_mode="html",
                    reply_to=reply_to,
                    silent=True,
                    link_preview=use_link_preview,
                )
                id_map[src["id"]] = text_msg.id
                media_caption: Optional[str] = None
            else:
                media_caption = caption

            if len(files) == 1:
                return await _send_single_media(
                    client, entity, files[0], lot_ids, id_map,
                    caption=media_caption, reply_to=reply_to,
                )

            return await _send_album(
                client, entity, lot, files, lot_ids, id_map, src,
                caption=media_caption, reply_to=reply_to,
                media_folder=media_folder,
            )

        except FloodWaitError as fw:
            wait = fw.seconds + 1
            warn(
                f"floodwait: sleeping {wait}s, retrying lot starting at "
                f"id={lot_ids[0]}"
            )
            await asyncio.sleep(wait)
            continue

        except RPCError as exc:
            reason = f"{type(exc).__name__}: {exc}"
            error(
                f"rpc error for lot starting at id={lot_ids[0]}: {reason}",
                log_path=error_log,
            )
            for oid in lot_ids:
                append_line(error_log, f"{oid}\t{reason}")
            return False, kind_for_pacing

        except Exception as exc:  # noqa: BLE001 — last-resort logger; don't kill the run
            reason = f"{type(exc).__name__}: {exc}"
            error(
                f"unexpected error for lot starting at id={lot_ids[0]}: {reason}",
                log_path=error_log,
            )
            for oid in lot_ids:
                append_line(error_log, f"{oid}\t{reason}")
            return False, kind_for_pacing


async def _send_geo(
    client: TelegramClient,
    entity: Any,
    geo_msg: dict,
    lot: list[dict],
    lot_ids: list[int],
    id_map: dict[int, int],
    *,
    caption: str,
    reply_to: Optional[int],
) -> tuple[bool, str]:
    """Sends a pure geolocation point (no attached media file)."""
    geo = (geo_msg.get("media") or {}).get("geo") or {}
    media = InputMediaGeoPoint(
        geo_point=InputGeoPoint(
            lat=float(geo.get("lat") or 0.0),
            long=float(geo.get("long") or 0.0),
            accuracy_radius=geo.get("accuracy_radius"),
        )
    )
    sent = await client.send_file(
        entity,
        file=media,
        caption=caption,
        parse_mode="html",
        reply_to=reply_to,
        silent=True,
    )
    new_id = sent.id
    for oid in lot_ids:
        id_map[oid] = new_id
    await _post_send_actions(client, entity, new_id, lot[0], lot[0])
    return True, "media"


async def _send_text_only(
    client: TelegramClient,
    entity: Any,
    lot: list[dict],
    lot_ids: list[int],
    id_map: dict[int, int],
    *,
    caption: str,
    reply_to: Optional[int],
    use_link_preview: bool,
) -> tuple[bool, str]:
    """Sends a lot that has no media files attached (text or text-fallback)."""
    sent = await client.send_message(
        entity,
        message=caption,
        parse_mode="html",
        reply_to=reply_to,
        silent=True,
        link_preview=use_link_preview,
    )
    new_id = sent.id
    for oid in lot_ids:
        id_map[oid] = new_id
    await _post_send_actions(client, entity, new_id, lot[0], lot[0])
    return True, "text"


async def _send_single_media(
    client: TelegramClient,
    entity: Any,
    file_entry: tuple,
    lot_ids: list[int],
    id_map: dict[int, int],
    *,
    caption: Optional[str],
    reply_to: Optional[int],
) -> tuple[bool, str]:
    """Sends a lot with exactly one media file, preserving its kind (voice,
    video note, animated GIF, video attributes)."""
    path, kind, carrier = file_entry
    extra_attrs: list[Any] = []
    video_attr = extract_video_attribute(carrier)
    if video_attr is not None:
        extra_attrs.append(video_attr)
    if kind == "gif":
        extra_attrs.append(DocumentAttributeAnimated())
    sent = await client.send_file(
        entity,
        file=path,
        caption=caption,
        parse_mode="html" if caption else None,
        reply_to=reply_to,
        silent=True,
        voice_note=(kind == "voice"),
        video_note=(kind == "video_note"),
        attributes=extra_attrs or None,
        # Force "not a document" when we have a video attribute: without
        # this Telethon falls back to force_document for .webm/.mov.
        force_document=False if video_attr else None,
    )
    new_id = sent.id
    for oid in lot_ids:
        id_map.setdefault(oid, new_id)
    await _post_send_actions(client, entity, new_id, carrier, carrier)
    return True, "media"


async def _send_album(
    client: TelegramClient,
    entity: Any,
    lot: list[dict],
    files: list[tuple],
    lot_ids: list[int],
    id_map: dict[int, int],
    src: dict,
    *,
    caption: Optional[str],
    reply_to: Optional[int],
    media_folder: str,
) -> tuple[bool, str]:
    """Sends a multi-file album. Maps each original id to its new server-side
    id, falling back to the album's first id when a member's media is missing
    or the API returned fewer messages than expected."""
    sent_list = await client.send_file(
        entity,
        file=[p for p, _, _ in files],
        caption=caption,
        parse_mode="html" if caption else None,
        reply_to=reply_to,
        silent=True,
    )
    if not isinstance(sent_list, list):
        sent_list = [sent_list]
    first_new_id = sent_list[0].id

    media_iter = iter(sent_list)
    for m in lot:
        old_id = m["id"]
        if m.get("media") is None:
            id_map.setdefault(old_id, first_new_id)
            continue
        if find_media_file(media_folder, old_id) is None:
            id_map.setdefault(old_id, first_new_id)
            continue
        try:
            new = next(media_iter)
        except StopIteration:
            id_map.setdefault(old_id, first_new_id)
            continue
        id_map.setdefault(old_id, new.id)

    # For an album, pin and react on the "carrier" message (``src``),
    # which is usually the first in the group too.
    await _post_send_actions(client, entity, first_new_id, src, src)
    return True, "media"


# ---------- main ----------


def detect_channel_id(messages: list[dict]) -> Optional[int]:
    """Returns the unique ``peer_id.channel_id`` seen in ``messages``, or ``None``.

    The dump should always come from a single channel; returning ``None``
    when the set has zero or more than one value forces :func:`main` to
    ask the user instead of silently picking a wrong target.
    """
    ids = {
        (m.get("peer_id") or {}).get("channel_id")
        for m in messages
        if (m.get("peer_id") or {}).get("channel_id") is not None
    }
    if len(ids) == 1:
        return ids.pop()
    return None


GENERAL_TOPIC_KEY: str = "general"


def topic_of(msg: dict) -> Optional[int]:
    """Returns the forum-topic root id of ``msg``, or ``None`` for the General chat."""
    rt = msg.get("reply_to") or {}
    if not rt.get("forum_topic"):
        return None
    return rt.get("reply_to_top_id") or rt.get("reply_to_msg_id")


def summarize_topics(messages: list[dict]) -> dict[Optional[int], int]:
    """Counts messages per topic id (``None`` key = General chat)."""
    counts: dict[Optional[int], int] = {}
    for m in messages:
        t = topic_of(m)
        counts[t] = counts.get(t, 0) + 1
    return counts


def parse_topic_selection(
    raw: str, available: dict[Optional[int], int]
) -> Optional[set[Optional[int]]]:
    """
    'all' / empty -> None (no filter).
    Comma-separated ints -> set of topic ids.
    'general' (case-insensitive) -> None topic (General chat).
    Unknown topic ids raise ValueError.
    """
    s = raw.strip().lower()
    if s == "" or s == "all":
        return None
    selected: set[Optional[int]] = set()
    for token in s.split(","):
        token = token.strip()
        if not token:
            continue
        if token == GENERAL_TOPIC_KEY:
            selected.add(None)
            continue
        try:
            tid = int(token)
        except ValueError as exc:
            raise ValueError(f"Bad topic id: {token!r}") from exc
        if tid not in available:
            raise ValueError(
                f"Topic id {tid} is not present in dump. "
                f"Available: {sorted(t for t in available if t is not None)}"
            )
        selected.add(tid)
    return selected or None


async def prompt_topic_selection(
    topic_counts: dict[Optional[int], int],
) -> Optional[set[Optional[int]]]:
    """Interactive checkbox picker over topics found in the dump.

    All topics are checked by default — pressing Enter restores everything.
    Returns ``None`` when *every* topic is selected (semantic of "all"), or
    a ``set`` of selected ids (``None`` element = General topic). An empty
    selection re-prompts. Falls back to plain text input if questionary is
    unavailable.
    """
    try:
        import questionary
    except ImportError:
        warn(
            "questionary is not installed; falling back to text input. "
            "run `pip install questionary` for the checkbox UI."
        )
        raw = prompt_text(
            "Topics to restore (comma-separated ids, 'general' for General, "
            "empty/'all' for everything):"
        )
        return parse_topic_selection(raw, topic_counts)

    # General first, then numeric topics ascending. Each row carries the
    # message-count tag in dim grey via the "selected"/"text" style.
    sorted_topics = sorted(
        topic_counts.items(),
        key=lambda x: (0 if x[0] is None else 1, x[0] or 0),
    )
    choices = []
    label_width = max(
        (len("general") if t is None else len(str(t)))
        for t in topic_counts
    )
    for topic_id, count in sorted_topics:
        label = "general" if topic_id is None else str(topic_id)
        title = f"{label:<{label_width}}   {count} message(s)"
        choices.append(
            questionary.Choice(title, value=topic_id, checked=True)
        )

    while True:
        selected = await undelete_checkbox(
            "Topics to restore?",
            choices=choices,
        )
        if selected is None:  # Esc / Ctrl+C
            info("cancelled by user")
            sys.exit(0)
        if not selected:
            warn("pick at least one topic.")
            continue
        # Picking everything == 'all' — collapse to None so downstream code
        # skips the filtering pass entirely.
        if len(selected) == len(topic_counts):
            return None
        return set(selected)


def parse_cli_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Builds the ``argparse`` schema for ``python -m src.resender``.

    Every option is optional: :func:`main` falls back to interactive
    prompts (api credentials, chat id picker, topic checkbox) for
    anything not supplied. Boolean flags (``--dry-run``, ``--yes``,
    ``--no-color``, ``--reveal-spoilers``, ``--compact``) default to off.
    """
    p = argparse.ArgumentParser(
        prog="python -m src.resender",
        description=(
            "Re-upload rescued messages (produced by `src.backup`) into the "
            "original forum-supergroup topics. Reads from "
            "<output-folder>/<chat-id>/messages.json. Missing options are "
            "asked interactively — without --chat-id you'll see a picker "
            "with existing backups."
        ),
    )
    p.add_argument("--api-id", type=int)
    p.add_argument("--api-hash")
    p.add_argument(
        "--chat-id",
        type=int,
        help=(
            "Source/target channel id. If omitted, an interactive picker "
            "shows existing chat backups under --output-folder."
        ),
    )
    p.add_argument(
        "--output-folder",
        default=DEFAULT_OUTPUT_FOLDER,
        help=(
            f"Root folder where backups live; the actual chat data is read "
            f"from <output-folder>/<chat-id>/messages.json "
            f"(default: {DEFAULT_OUTPUT_FOLDER})."
        ),
    )
    p.add_argument(
        "--session-name",
        default=DEFAULT_SESSION_NAME,
        help=(
            f"Telethon session file basename (default: {DEFAULT_SESSION_NAME})."
        ),
    )
    p.add_argument(
        "--topics",
        help="Comma-separated topic ids, 'general' for non-topic messages, "
        "'all' or empty for everything.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Build captions and walk lots, but do not call Telegram. "
        "Prints sample captions and a summary.",
    )
    p.add_argument(
        "--limit",
        type=int,
        help="Stop after sending this many lots (useful for smoke tests).",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the [y/N] confirmation prompt before sending.",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colours (also honoured via NO_COLOR=1).",
    )
    p.add_argument(
        "--reveal-spoilers",
        action="store_true",
        help="Render <tg-spoiler> contents visible in previews.",
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="Render each lot as a single coloured line instead of a panel.",
    )
    return p.parse_args(argv)


def format_eta(seconds: float) -> str:
    """Formats a duration as ``MM:SS`` (or ``H:MM:SS`` past one hour).

    Duplicates :func:`src.backup.__main__.format_eta` on purpose — the two
    CLIs are standalone entry points and we keep ``src/common`` free of
    one-off helpers each module touches at most twice.
    """
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def summarise_pending(
    lots: list[list[dict]], id_map: dict[int, int], media_folder: str,
) -> dict:
    """Counts what a real run would do, given the current id_map state."""
    stats = {
        "lots_total": len(lots),
        "skipped": 0,
        "text": 0,
        "media_single": 0,
        "media_album": 0,
        "media_missing": 0,
    }
    for lot in lots:
        ids = [m["id"] for m in lot]
        if all(oid in id_map for oid in ids):
            stats["skipped"] += 1
            continue
        files, missing = collect_lot_files(lot, media_folder)
        if not files and not missing:
            stats["text"] += 1
        elif not files and missing:
            stats["media_missing"] += 1
        elif len(files) == 1:
            stats["media_single"] += 1
        else:
            stats["media_album"] += 1
    return stats


def _print_topic_summary(topic_counts: dict[Optional[int], int]) -> None:
    """Logs a per-topic message-count breakdown for the loaded dump."""
    info(f"topics in dump: [bold]{len(topic_counts)}[/bold] unique.")
    for t, c in sorted(
        topic_counts.items(),
        key=lambda x: (-x[1], -1 if x[0] is None else x[0]),
    ):
        label = "general" if t is None else str(t)
        STDOUT_CONSOLE.print(
            f"  [dim]topic[/dim] [cyan]{label:>10}[/cyan]: {c} message(s)"
        )


async def _select_topics(
    args: argparse.Namespace,
    messages: list[dict],
    topic_counts: dict[Optional[int], int],
) -> Optional[list[dict]]:
    """Asks the user (or parses --topics) which topics to restore, then
    returns the filtered message list. ``None`` means the user wants to
    abort the run (nothing matched after filtering)."""
    if args.topics is None:
        # Interactive checkbox over the topics actually present in the dump.
        selected_topics = await prompt_topic_selection(topic_counts)
    else:
        try:
            selected_topics = parse_topic_selection(args.topics, topic_counts)
        except ValueError as exc:
            raise SystemExit(str(exc))

    if selected_topics is None:
        return messages

    before = len(messages)
    filtered = [m for m in messages if topic_of(m) in selected_topics]
    labels = ", ".join(
        "general" if t is None else str(t)
        for t in sorted(selected_topics, key=lambda x: -1 if x is None else x)
    )
    info(
        f"filtered by topics \\[{labels}]: "
        f"[bold]{len(filtered)}/{before}[/bold] messages selected."
    )
    if not filtered:
        warn("nothing to send after filtering.")
        return None
    return filtered


async def _setup_client(
    args: argparse.Namespace,
    chat_id: int,
    api_id: int,
    api_hash: str,
    messages: list[dict],
    user_cache: dict[int, str],
    users_cache_path: str,
) -> tuple[TelegramClient, Any]:
    """Starts the Telethon client, resolves the target channel, prefetches
    participants and seeds forward-author names from the dump."""
    client = TelegramClient(args.session_name, api_id, api_hash)
    await client.start()
    entity = await client.get_entity(PeerChannel(int(chat_id)))
    await prefetch_chat_participants(client, entity, user_cache)
    seeded = _seed_fwd_names(messages, user_cache)
    if seeded:
        info(f"+{seeded} name(s) seeded from fwd_from.from_name.")
    save_users_cache(users_cache_path, user_cache)
    return client, entity


async def _run_send_loop(
    args: argparse.Namespace,
    client: Optional[TelegramClient],
    entity: Any,
    lots: list[list[dict]],
    id_map: dict[int, int],
    user_cache: dict[int, str],
    dump_index: dict[int, dict],
    state: dict,
    *,
    state_path: str,
    media_folder: str,
    missing_log: str,
    error_log: str,
) -> dict[str, int]:
    """Iterates over lots, dispatches each to `send_lot`, paces between
    lots, and (in dry-run) prints a preview of what *would* have gone out.
    Returns ``{"sent": .., "skipped": .., "failed": ..}``."""
    total = len(lots)
    sent_count = 0
    skipped_count = 0
    failed_count = 0

    with make_lot_progress() as progress:
        task_id = progress.add_task("Processing lots", total=total)
        for idx, lot in enumerate(lots, 1):
            lot_ids = [m["id"] for m in lot]
            if all(oid in id_map for oid in lot_ids):
                skipped_count += 1
                progress.update(task_id, advance=1)
                continue

            if args.limit and sent_count >= args.limit:
                warn(f"reached --limit {args.limit}, stopping.")
                break

            ok, kind = await send_lot(
                client, entity, lot, id_map, user_cache, dump_index,
                media_folder=media_folder,
                missing_log=missing_log,
                error_log=error_log,
                dry_run=args.dry_run,
            )
            if ok:
                sent_count += 1
                if not args.dry_run:
                    save_state(state_path, state)
                if args.dry_run:
                    _print_dry_run_preview(
                        lot, idx, total, user_cache, dump_index, id_map,
                        media_folder=media_folder,
                        compact=args.compact,
                        reveal_spoilers=args.reveal_spoilers,
                        console=progress.console,
                    )
            else:
                failed_count += 1

            progress.update(task_id, advance=1)

            if not args.dry_run:
                delay = SLEEP_AFTER_MEDIA if kind == "media" else SLEEP_AFTER_TEXT
                await asyncio.sleep(delay)

    return {"sent": sent_count, "skipped": skipped_count, "failed": failed_count}


def _print_dry_run_preview(
    lot: list[dict],
    idx: int,
    total: int,
    user_cache: dict[int, str],
    dump_index: dict[int, dict],
    id_map: dict[int, int],
    *,
    media_folder: str,
    compact: bool,
    reveal_spoilers: bool,
    console: Any,
) -> None:
    """Renders a Rich preview of one lot during --dry-run (no network calls)."""
    files, missing = collect_lot_files(lot, media_folder)
    note: Optional[str] = None
    status = "normal"
    if missing and not files:
        note = f"[медиа недоступно: {short_media_kind_ru(lot[0])}]"
        status = "warning"
    elif missing:
        note = f"[медиа частично недоступно: {len(missing)} файл(ов)]"
        status = "warning"
    rendered = build_rendered_for_lot(
        lot,
        user_cache,
        dump_index,
        id_map,
        idx=idx,
        total=total,
        reveal_spoilers=reveal_spoilers,
        extra_note=note,
        status=status,
    )
    if compact:
        console.print(render_message_oneline(rendered))
    else:
        console.print(render_message_panel(rendered))


def _print_run_report(
    counts: dict[str, int],
    elapsed: float,
    *,
    state_path: str,
    missing_log: str,
    error_log: str,
    dry_run: bool,
) -> None:
    """Final per-run summary table for `main`."""
    footer: list[str] = []
    if not dry_run:
        footer.append(f"State: {state_path}")
    if os.path.exists(missing_log):
        footer.append(f"Missing media log: {missing_log}")
    if os.path.exists(error_log):
        footer.append(f"Errors log: {error_log}")
    STDOUT_CONSOLE.print(
        render_summary_table(
            title=f"Done in {format_eta(elapsed)}",
            rows=[
                ("Sent", str(counts["sent"]), "bold green"),
                ("Skipped", str(counts["skipped"]), "dim"),
                (
                    "Failed",
                    str(counts["failed"]),
                    "bold red" if counts["failed"] else "dim",
                ),
            ],
            footer=footer or None,
        )
    )


async def main(argv: Optional[list[str]] = None) -> None:
    """Async entry point for ``python -m src.resender``.

    Picks a backup folder (existing-backups picker if ``--chat-id`` is
    omitted), loads ``messages.json``, groups it into album lots, asks
    the user which topics to restore, resumes the persisted ``id_map``,
    sets up Telethon (skipped in ``--dry-run``) and dispatches to
    :func:`_run_send_loop`. The client is always disconnected via
    ``try/finally``; the persisted users cache is flushed on the way out.
    """
    args = parse_cli_args(argv)

    if args.no_color:
        STDOUT_CONSOLE.no_color = True

    # Step 1: pick chat (and therefore the on-disk backup folder) BEFORE
    # loading anything — the picker shows a menu of existing backups when
    # --chat-id is omitted.
    chat_id: Optional[int] = args.chat_id
    if chat_id is None:
        chat_id = prompt_chat_id(
            args.output_folder, action="restore", require_existing=True
        )

    layout = chat_layout(args.output_folder, chat_id)
    if not os.path.exists(layout.messages):
        warn(
            f"no messages.json found at [bold]{layout.messages}[/bold]\n"
            f"       back it up first:\n"
            f"       [bold cyan]python -m src.backup --chat-id {chat_id}[/bold cyan]"
        )
        sys.exit(1)
    state_path = os.path.join(layout.chat_dir, STATE_FILENAME)
    users_cache_path = os.path.join(layout.chat_dir, USERS_CACHE_FILENAME)
    missing_log = os.path.join(layout.chat_dir, MISSING_LOG_FILENAME)
    error_log = os.path.join(layout.chat_dir, ERROR_LOG_FILENAME)
    info(f"restore folder: [bold]{layout.chat_dir}[/bold]")

    all_messages = load_dump(layout.messages)
    if not all_messages:
        warn(f"{layout.messages} пуст — нечего отправлять.")
        return
    info(
        f"loaded [bold]{len(all_messages)}[/bold] messages from "
        f"{layout.messages}."
    )

    topic_counts = summarize_topics(all_messages)
    _print_topic_summary(topic_counts)

    # Sanity-check: warn if the chat id encoded in the dump differs from
    # what we picked (e.g. someone renamed the folder).
    detected = detect_channel_id(all_messages)
    if detected and detected != chat_id:
        warn(
            f"folder name says chat_id={chat_id}, but messages.json carries "
            f"peer_id.channel_id={detected}. continuing with {chat_id}."
        )

    api_id = args.api_id
    if api_id is None:
        api_id = int(prompt_text("Enter your api_id:").strip())
    api_hash = args.api_hash or prompt_text("Enter your api_hash:").strip()

    messages = await _select_topics(args, all_messages, topic_counts)
    if messages is None:
        return

    # Index by old_id — needed for the dropped-reply preview, so build it
    # over the entire dump (including messages from topics we did not pick).
    dump_index: dict[int, dict] = {m["id"]: m for m in all_messages}
    lots = group_albums(messages)
    info(f"grouped into [bold]{len(lots)}[/bold] lots (albums collapsed).")

    state = load_state(state_path)
    id_map = state["id_map"]
    info(f"state loaded: [bold]{len(id_map)}[/bold] already-mapped messages.")

    stats = summarise_pending(lots, id_map, layout.media)
    STDOUT_CONSOLE.print(
        render_summary_table(
            title="Plan summary",
            rows=[
                ("text-only", str(stats["text"]), "green"),
                ("single media", str(stats["media_single"]), "green"),
                ("media albums", str(stats["media_album"]), "green"),
                (
                    "media unavailable (text fallback)",
                    str(stats["media_missing"]),
                    "yellow" if stats["media_missing"] else "dim",
                ),
                (
                    "already sent (skipped)",
                    str(stats["skipped"]),
                    "dim",
                ),
                ("total lots", str(stats["lots_total"]), "bold"),
            ],
            footer=(
                [f"will stop after {args.limit} sent lots (--limit)."]
                if args.limit
                else None
            ),
        )
    )

    if args.dry_run:
        info("[bold yellow]dry-run mode[/bold yellow]: no API calls will be made.")
    elif not args.yes:
        # Default is YES — pressing Enter accepts. Type "n" to abort.
        answer = prompt_text("Proceed? (Y/n)").strip().lower()
        if answer and answer not in ("y", "yes", "д", "да"):
            warn("aborted.")
            return

    client: Optional[TelegramClient] = None
    entity: Any = None
    user_cache: dict[int, str] = load_users_cache(users_cache_path)
    if user_cache:
        info(f"loaded [bold]{len(user_cache)}[/bold] cached names from previous run.")

    if not args.dry_run:
        client, entity = await _setup_client(
            args, chat_id, api_id, api_hash, messages, user_cache, users_cache_path
        )

    try:
        start_ts = time.monotonic()
        counts = await _run_send_loop(
            args, client, entity, lots, id_map, user_cache, dump_index, state,
            state_path=state_path,
            media_folder=layout.media,
            missing_log=missing_log,
            error_log=error_log,
        )
        _print_run_report(
            counts,
            time.monotonic() - start_ts,
            state_path=state_path,
            missing_log=missing_log,
            error_log=error_log,
            dry_run=args.dry_run,
        )
    finally:
        if client is not None:
            await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Land on a clean line after the "^C" Ctrl+C prints, then exit
        # with the conventional 128+SIGINT(2) status code — no traceback.
        STDOUT_CONSOLE.print()
        warn("aborted by user (Ctrl+C).")
        sys.exit(130)
