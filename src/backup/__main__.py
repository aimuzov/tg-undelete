"""
Export deleted Telegram messages and media using Telethon.

Modes (the two "save text" / "save media" checkboxes are orthogonal):
    1 — text + media: append every event to messages.json AND download files
    2 — media only:   download media files; back-fill messages.json with the
                      records of those media events if they aren't there yet
    3 — text only:    append every event to messages.json (including full
                      media metadata for media events), but don't download
                      any files

On-disk layout per chat::

    backups/
      <chat_id>/
        messages.json            # append-mode JSON stream of Message objects
        media/                   # downloaded photo / video / audio / files
        backup_state.json        # saved_ids + permanent_failures (resume info)
        backup_errors.log        # RPCError + permanent download failures
        backup_missing_media.log # one line per permanently missing media file

Run state is persisted to ``backup_state.json`` (``saved_ids`` and
``permanent_failures``), so the script can be restarted safely —
already-saved events are skipped, which also prevents duplicate JSON
objects in the append-mode dump file.

CLI usage (any missing option is asked interactively):

    python -m src.backup --api-id 123 --api-hash abc \\
        --chat-id 456 --mode 1 --min-id 0 --max-id 0 --yes

See ``python -m src.backup --help`` for the full list of options.
"""

import argparse
import asyncio
import contextlib
import glob
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import questionary

from telethon import TelegramClient
from telethon.tl.types import PeerChannel
from telethon.errors import RPCError, FileReferenceExpiredError

from src.common.backup_layout import ChatLayout, chat_layout, prompt_chat_id
from src.common.checkbox import undelete_checkbox
from src.common.console import (
    STDOUT_CONSOLE,
    error,
    info,
    prompt_text,
    success,
    warn,
)
from src.common.message_view import (
    render_message_oneline,
    render_message_panel,
    render_summary_table,
)
from src.common.progress import make_lot_progress
from src.common.telethon_adapter import TelethonRenderCtx, build_rendered_from_telethon
from src.common.types import (
    DownloadRequest,
    ExportCounters,
    ExportFlags,
    ExportPaths,
    ExportRange,
    FinalReportContext,
)


# ---------- defaults & constants ----------

DEFAULT_OUTPUT_FOLDER: str = "backups"
DEFAULT_SESSION_NAME: str = "session_name"
STATE_FILENAME: str = "backup_state.json"
ERROR_LOG_FILENAME: str = "backup_errors.log"
MISSING_LOG_FILENAME: str = "backup_missing_media.log"

# Save state every N processed events so a kill -9 loses at most that many ids.
STATE_FLUSH_EVERY: int = 25

# Number of admin-log events per Telegram API request. 100 is the cap that
# `iter_admin_log` accepts before silently downgrading.
ADMIN_LOG_BATCH_SIZE: int = 100

USER_NAME_CACHE: dict[int, str] = {}


# Permanent errors — retrying won't help (expired file reference, self-destructing
# media, etc.). Recorded once and excluded from the retry queue.
DOWNLOAD_PERMANENT_ERRORS = (FileReferenceExpiredError,)

# Transient errors we treat as "skip and retry later" instead of fatal.
# NOTE: keep this AFTER the permanent tuple in except clauses, since
# FileReferenceExpiredError is also an RPCError.
DOWNLOAD_RETRYABLE_ERRORS = (
    RPCError,
    TimeoutError,
    asyncio.TimeoutError,
    ValueError,
    OSError,
)


# ---------- state persistence ----------


def load_state(path: str) -> dict[str, Any]:
    """Loads run state from disk; returns an empty skeleton if absent."""
    if not os.path.exists(path):
        return {"saved_ids": [], "permanent_failures": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (ValueError, OSError) as exc:
        warn(f"state file unreadable, starting fresh: {exc}")
        return {"saved_ids": [], "permanent_failures": []}
    state.setdefault("saved_ids", [])
    state.setdefault("permanent_failures", [])
    return state


def save_state(path: str, state: dict[str, Any]) -> None:
    """Writes state atomically (tmp + rename)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


def append_line(path: str, line: str) -> None:
    """Appends one line to a log file (creates if missing)."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def format_eta(seconds: float) -> str:
    """Formats a duration as ``MM:SS`` or ``H:MM:SS``."""
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ---------- media download with retries ----------


async def safe_download_media(
    client: TelegramClient,
    request: DownloadRequest,
) -> Optional[str]:
    """
    Downloads media, surviving Telegram timeouts and other transient errors.

    Telethon retries internally up to 5 times and then raises
    ``ValueError('Request was unsuccessful 6 time(s)')``. Without protection,
    one broken file aborts the whole export. Failures are logged and, if
    the corresponding list is provided, recorded for a later retry pass
    (``request.failed``) or reported as unrecoverable (``request.permanent``).

    Skips the download if a file at ``<file_path>.<ext>`` already exists on
    disk — useful when re-running with mode=1 to capture media metadata into
    the dump without re-downloading the bytes.
    """
    existing = glob.glob(request.file_path + ".*")
    if existing:
        info(f"skipping download, already on disk: [dim]{existing[0]}[/dim]")
        return existing[0]
    try:
        return await client.download_media(request.media, request.file_path)
    except DOWNLOAD_PERMANENT_ERRORS as exc:
        error(
            f"permanently failed for {request.file_path}: {exc}. not retrying.",
            log_path=request.error_log,
        )
        if request.permanent is not None:
            request.permanent.append((request.file_path, str(exc)))
        return None
    except DOWNLOAD_RETRYABLE_ERRORS as exc:
        warn(f"failed to download media for {request.file_path}: {exc}. skipping.")
        if request.failed is not None:
            request.failed.append((request.media, request.file_path))
        return None


@dataclass
class _RetrySettings:
    """Tunables and sinks for :func:`retry_failed_downloads`."""

    permanent: Optional[list] = None
    error_log: Optional[str] = None
    max_rounds: int = 3
    base_delay: float = 2.0


@dataclass
class _RetryRound:
    """Per-iteration metadata for :func:`_run_one_retry_round`."""

    round_num: int
    max_rounds: int
    permanent: Optional[list]
    error_log: Optional[str]


async def _run_one_retry_round(
    client: TelegramClient,
    failed: list,
    round_: _RetryRound,
) -> list:
    """Retries each pending download once; returns items still failing."""
    next_failed: list = []
    total = len(failed)
    with make_lot_progress() as progress:
        task = progress.add_task(
            f"retry pass {round_.round_num}/{round_.max_rounds}", total=total
        )
        for idx, (media, file_path) in enumerate(failed, 1):
            try:
                await client.download_media(media, file_path)
                success(f"\\[{idx}/{total}] recovered: {file_path}")
            except DOWNLOAD_PERMANENT_ERRORS as exc:
                error(
                    f"\\[{idx}/{total}] permanently failed for {file_path}: "
                    f"{exc}. dropping from retry queue.",
                    log_path=round_.error_log,
                )
                if round_.permanent is not None:
                    round_.permanent.append((file_path, str(exc)))
            except DOWNLOAD_RETRYABLE_ERRORS as exc:
                warn(f"\\[{idx}/{total}] still failing for {file_path}: {exc}")
                next_failed.append((media, file_path))
            progress.update(task, advance=1)
            await asyncio.sleep(0.3)  # Be gentle with the API between items
    return next_failed


async def retry_failed_downloads(
    client: TelegramClient,
    failed: list,
    settings: _RetrySettings,
) -> list:
    """
    Retries previously failed downloads in several rounds with exponential
    backoff between rounds. Permanent errors encountered during retries are
    moved to ``settings.permanent`` (if provided) and removed from the retry
    queue. Returns the list of items that still failed transiently.
    """
    for round_num in range(1, settings.max_rounds + 1):
        if not failed:
            return []
        delay = settings.base_delay * (2 ** (round_num - 1))
        info(
            f"retry pass [bold]{round_num}/{settings.max_rounds}[/bold]: "
            f"{len(failed)} item(s) to retry (sleeping {delay:.0f}s first)..."
        )
        await asyncio.sleep(delay)
        failed = await _run_one_retry_round(
            client,
            failed,
            _RetryRound(
                round_num=round_num,
                max_rounds=settings.max_rounds,
                permanent=settings.permanent,
                error_log=settings.error_log,
            ),
        )
    return failed


# ---------- core export ----------


@dataclass
class _ExportRequest:
    """Inputs that don't change during one export: ids, files, flags, state."""

    target_group_id: int
    paths: ExportPaths
    flags: ExportFlags
    range_: ExportRange
    state: dict[str, Any]


@dataclass
class _TelethonHandles:
    """Live Telethon objects threaded through the per-event pipeline."""

    client: TelegramClient
    group: Any
    progress: Any
    dump: Optional[Any] = None


@dataclass
class _PassContext:
    """Shared state for one main-pass loop over admin-log events."""

    handles: _TelethonHandles
    flags: ExportFlags
    counters: ExportCounters
    paths: ExportPaths
    range_: ExportRange
    state: dict[str, Any]
    events_seen: int = 0


def _init_counters(state: dict[str, Any]) -> ExportCounters:
    """Build :class:`ExportCounters` seeded from persisted resume state."""
    return ExportCounters(
        saved_ids=set(state.get("saved_ids", [])),
        permanent_failures=list(state.get("permanent_failures", [])),
    )


def _flush_state(state: dict[str, Any], counters: ExportCounters, state_path: str) -> None:
    """Persist ``counters`` into ``state`` and write it atomically."""
    state["saved_ids"] = sorted(counters.saved_ids)
    state["permanent_failures"] = counters.permanent_failures
    save_state(state_path, state)


def _open_dump_if_needed(
    stack: contextlib.ExitStack,
    paths: ExportPaths,
    *,
    dry_run: bool,
) -> Optional[Any]:
    """Open ``messages.json`` for append/create unless this is a dry-run."""
    if dry_run:
        return None
    file_mode: str = "a" if os.path.exists(paths.messages) else "w"
    return stack.enter_context(open(paths.messages, file_mode, encoding="utf-8"))


async def _run_export_passes(
    client: TelegramClient,
    group: Any,
    dump: Optional[Any],
    request: _ExportRequest,
    counters: ExportCounters,
) -> list:
    """Drive the main + retry passes; return items still failing transiently."""
    with make_lot_progress() as progress:
        ctx = _PassContext(
            handles=_TelethonHandles(
                client=client, group=group, progress=progress, dump=dump
            ),
            flags=request.flags,
            counters=counters,
            paths=request.paths,
            range_=request.range_,
            state=request.state,
        )
        await _run_main_pass(ctx)
    return await _run_retry_pass(
        client,
        counters,
        request.paths.error_log,
        dry_run=request.flags.dry_run,
    )


async def export_messages(
    client: TelegramClient,
    request: _ExportRequest,
) -> None:
    """
    Exports deleted messages from a Telegram group or channel.

    The two checkbox flags inside ``request.flags`` are orthogonal:

    * ``save_text`` (mode 1 or 3) — write every deleted message into
      ``messages.json`` (with full media metadata when present, but without
      downloading the file).
    * ``save_media`` (mode 1 or 2) — download the media file. If the message
      isn't yet in ``messages.json``, it gets appended too — so a media-only
      run "back-fills" the JSON for the media events it touches.

    Resume: ``request.state["saved_ids"]`` lists message ids already in the
    JSON. A re-run never appends a duplicate JSON object, but media downloads
    are re-attempted only when the file is missing on disk
    (``safe_download_media`` skips if a file at ``<id>.<ext>`` already exists).

    With ``request.flags.dry_run=True`` no on-disk side effects happen: the
    dump file stays untouched, media is not downloaded, but every event still
    gets a Rich preview.
    """
    group = await client.get_entity(PeerChannel(request.target_group_id))
    counters = _init_counters(request.state)
    permanent_count_before = len(counters.permanent_failures)
    start_ts = time.monotonic()
    still_failed: list = []

    with contextlib.ExitStack() as stack:
        dump = _open_dump_if_needed(stack, request.paths, dry_run=request.flags.dry_run)
        try:
            still_failed = await _run_export_passes(
                client, group, dump, request, counters
            )
            _log_permanent_failures(
                counters.permanent_failures,
                permanent_count_before,
                request.paths.missing_log,
            )
        finally:
            if not request.flags.dry_run:
                _flush_state(request.state, counters, request.paths.state_path)

    _render_final_report(
        FinalReportContext(
            counters=counters,
            still_failed=still_failed,
            paths=request.paths,
            elapsed=time.monotonic() - start_ts,
            dry_run=request.flags.dry_run,
        )
    )


async def _fetch_admin_log_batch(ctx: _PassContext) -> list:
    """Pull one page of deleted-message admin-log events."""
    return [
        event
        async for event in ctx.handles.client.iter_admin_log(
            ctx.handles.group,
            min_id=ctx.range_.min_id or 0,
            max_id=ctx.range_.max_id or 0,
            limit=ADMIN_LOG_BATCH_SIZE,
            delete=True,
        )
    ]


def _limit_reached(counters: ExportCounters, limit: Optional[int]) -> bool:
    """True if ``--limit`` would be exceeded by the next event."""
    if limit is None:
        return False
    return (counters.counts["messages"] + counters.counts["media"]) >= limit


async def _run_main_pass(ctx: _PassContext) -> None:
    """Pulls deletion events page-by-page and dispatches each to the
    per-event handler. Stops on empty page, reaching min_id, or hitting
    ``flags.limit``."""
    task_id = ctx.handles.progress.add_task("Exporting deleted events", total=None)
    try:
        while True:
            events = await _fetch_admin_log_batch(ctx)
            if not events:
                success("loading complete, no new messages.")
                return

            for event in events:
                ctx.handles.progress.update(task_id, advance=1)
                await _process_event(event, ctx)
                if _limit_reached(ctx.counters, ctx.flags.limit):
                    warn(f"reached --limit {ctx.flags.limit}, stopping.")
                    return

            # Exclude last received event from next request.
            ctx.range_.max_id = events[-1].id - 1
            if ctx.range_.max_id < ctx.range_.min_id:
                info("reached the lower message id limit.")
                return
    except RPCError as exc:
        reason = f"{type(exc).__name__}: {exc}"
        error(f"an error occurred: {reason}", log_path=ctx.paths.error_log)
        append_line(ctx.paths.error_log, reason)


def _event_actions(event: Any, ctx: _PassContext) -> tuple[bool, bool]:
    """Return ``(write_json, download_media)`` for this event."""
    has_media = event.old.media is not None
    json_eligible = ctx.flags.save_text or (ctx.flags.save_media and has_media)
    write_json = json_eligible and event.old.id not in ctx.counters.saved_ids
    download_media = ctx.flags.save_media and has_media
    return write_json, download_media


def _write_event_json(event: Any, ctx: _PassContext) -> str:
    """Append the event to the dump and update counters. Returns a note bit."""
    if not ctx.flags.dry_run and ctx.handles.dump is not None:
        ctx.handles.dump.write(event.old.to_json() + ",")
    ctx.counters.counts["messages"] += 1
    ctx.counters.saved_ids.add(event.old.id)
    verb = "preview" if ctx.flags.dry_run else "saved"
    return f"{verb} #{ctx.counters.counts['messages']}"


async def _download_event_media(event: Any, ctx: _PassContext) -> tuple[Optional[str], str]:
    """Download (or fake-download in dry-run) media; return path and note bit."""
    if ctx.flags.dry_run:
        ctx.counters.counts["media"] += 1
        return None, f"preview media #{ctx.counters.counts['media']}"
    download_path = await safe_download_media(
        ctx.handles.client,
        DownloadRequest(
            media=event.old.media,
            file_path=os.path.join(ctx.paths.media_folder, str(event.old.id)),
            failed=ctx.counters.failed_downloads,
            permanent=ctx.counters.permanent_failures,
            error_log=ctx.paths.error_log,
        ),
    )
    if download_path is None:
        return None, ""
    ctx.counters.counts["media"] += 1
    return download_path, f"media #{ctx.counters.counts['media']}"


async def _render_event(event: Any, ctx: _PassContext, note_bits: list[str]) -> None:
    """Paint a Rich preview line/panel for the processed event."""
    rendered = await build_rendered_from_telethon(
        event.old,
        TelethonRenderCtx(
            client=ctx.handles.client,
            user_cache=USER_NAME_CACHE,
            note=" + ".join(note_bits) if note_bits else None,
        ),
    )
    if ctx.flags.verbose:
        ctx.handles.progress.console.print(render_message_panel(rendered))
    else:
        ctx.handles.progress.console.print(render_message_oneline(rendered))


async def _process_event(event: Any, ctx: _PassContext) -> None:
    """Processes one admin-log event: appends to JSON dump, downloads media,
    prints a preview. Mutates ``ctx`` in place."""
    if not (event.deleted_message and event.old.id >= ctx.range_.min_id):
        return

    write_json, download_media = _event_actions(event, ctx)
    if not write_json and not download_media:
        ctx.counters.counts["skipped"] += 1
        return

    note_bits: list[str] = []
    if write_json:
        note_bits.append(_write_event_json(event, ctx))

    download_path: Optional[str] = None
    if download_media:
        download_path, note = await _download_event_media(event, ctx)
        if note:
            note_bits.append(note)

    await _render_event(event, ctx, note_bits)

    if download_path is not None:
        success(
            f"saved media #{ctx.counters.counts['media']} (id: {event.old.id}, "
            f"file: [dim]{download_path}[/dim])"
        )

    if write_json:
        ctx.events_seen += 1
        if not ctx.flags.dry_run and ctx.events_seen % STATE_FLUSH_EVERY == 0:
            _flush_state(ctx.state, ctx.counters, ctx.paths.state_path)

    await asyncio.sleep(0.1)  # Short pause to avoid flooding API


async def _run_retry_pass(
    client: TelegramClient,
    counters: ExportCounters,
    error_log: str,
    *,
    dry_run: bool,
) -> list:
    """Retries any media that hit a transient error during the main pass.
    Prints a summary of what remains broken. No-op in dry-run."""
    failed = counters.failed_downloads
    if not failed or dry_run:
        return []
    warn(
        f"main pass finished with [bold]{len(failed)}[/bold] "
        f"transient failure(s). starting retry pass."
    )
    still_failed = await retry_failed_downloads(
        client,
        failed,
        _RetrySettings(
            permanent=counters.permanent_failures,
            error_log=error_log,
        ),
    )
    if still_failed:
        warn(f"{len(still_failed)} item(s) still failing transiently:")
        for _, file_path in still_failed:
            STDOUT_CONSOLE.print(f"  [yellow]-[/yellow] {file_path}")
    else:
        success("all transient failures recovered.")
    return still_failed


def _log_permanent_failures(
    permanent_failures: list,
    permanent_count_before: int,
    missing_log: str,
) -> None:
    """Mirrors *new* permanent failures into the missing-media log; entries
    that were already on disk from previous runs are not re-logged."""
    if not permanent_failures:
        return
    error(
        f"{len(permanent_failures)} item(s) permanently unavailable "
        f"(expired references or self-destructing media):"
    )
    for file_path, reason in permanent_failures:
        STDOUT_CONSOLE.print(f"  [red]-[/red] {file_path}: [dim]{reason}[/dim]")
    for file_path, reason in permanent_failures[permanent_count_before:]:
        append_line(missing_log, f"{file_path}\t{reason}")


def _build_report_footer(ctx: FinalReportContext) -> list[str]:
    """Compose the per-run footer of the summary table."""
    footer: list[str] = []
    if not ctx.dry_run:
        footer.append(f"State: {ctx.paths.state_path}")
    if os.path.exists(ctx.paths.missing_log):
        footer.append(f"Missing media log: {ctx.paths.missing_log}")
    if os.path.exists(ctx.paths.error_log):
        footer.append(f"Errors log: {ctx.paths.error_log}")
    return footer


def _render_final_report(ctx: FinalReportContext) -> None:
    """Renders the run summary table at the end of :func:`export_messages`."""
    footer = _build_report_footer(ctx)
    counts = ctx.counters.counts
    title_prefix = "Backup dry-run complete" if ctx.dry_run else "Backup complete"
    STDOUT_CONSOLE.print(
        render_summary_table(
            title=f"{title_prefix} in {format_eta(ctx.elapsed)}",
            rows=[
                ("Saved messages", str(counts["messages"]), "bold green"),
                ("Saved media", str(counts["media"]), "bold green"),
                (
                    "Skipped (already in state)",
                    str(counts["skipped"]),
                    "yellow" if counts["skipped"] else "dim",
                ),
                (
                    "Transient failures (unrecovered)",
                    str(len(ctx.still_failed)),
                    "yellow" if ctx.still_failed else "dim",
                ),
                (
                    "Permanent failures",
                    str(len(ctx.counters.permanent_failures)),
                    "red" if ctx.counters.permanent_failures else "dim",
                ),
            ],
            footer=footer or None,
        )
    )


# ---------- CLI ----------


async def prompt_export_mode() -> int:
    """Interactive checkbox picker — returns 1 (all), 2 (media only), 3 (text only).

    Both items checked → mode 1; only media → 2; only text → 3. An empty
    selection re-prompts.
    """
    while True:
        choices = await undelete_checkbox(
            "What to export from deleted messages?",
            choices=[
                questionary.Choice(
                    "Text messages   (saved into messages.json)",
                    value="text",
                    checked=True,
                ),
                questionary.Choice(
                    "Media files     (downloaded into media/)",
                    value="media",
                    checked=True,
                ),
            ],
        )

        if choices is None:  # user pressed Ctrl+C / Esc
            info("cancelled by user")
            sys.exit(0)

        selected = set(choices)
        if "text" in selected and "media" in selected:
            return 1
        if "media" in selected:
            return 2
        if "text" in selected:
            return 3
        warn("pick at least one option.")


def parse_cli_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Builds the ``argparse`` schema for ``python -m src.backup``.

    Every option may be omitted on the command line: :func:`main` falls
    back to interactive prompts (api credentials, chat id, export mode)
    for whatever is missing. Boolean flags (``--dry-run``, ``--yes``,
    ``--reset-session``, ``--no-color``, ``--verbose``) default to off and
    are also picked up from the ``TG_BACKUP_VERBOSE`` env var where noted.
    """
    p = argparse.ArgumentParser(
        prog="python -m src.backup",
        description=(
            "Export deleted Telegram messages and media into a local folder. "
            "Missing options are asked interactively."
        ),
    )
    p.add_argument("--api-id", type=int)
    p.add_argument("--api-hash")
    p.add_argument(
        "--chat-id",
        type=int,
        help="Target channel/supergroup id (the group whose deleted messages to export).",
    )
    p.add_argument(
        "--mode",
        type=int,
        choices=[1, 2, 3],
        help="1 - all, 2 - media only, 3 - text only.",
    )
    p.add_argument(
        "--min-id",
        type=int,
        help="Minimum message id to export (0 = from the very first). Default: 0.",
    )
    p.add_argument(
        "--max-id",
        type=int,
        help="Maximum message id to export (0 = no upper bound). Default: 0.",
    )
    p.add_argument(
        "--limit",
        type=int,
        help="Stop after saving this many messages (smoke-test helper).",
    )
    p.add_argument(
        "--output-folder",
        default=DEFAULT_OUTPUT_FOLDER,
        help=(
            f"Root folder for backups; the actual chat data lands in "
            f"<output-folder>/<chat-id>/{{messages.json, media/, *.log}} "
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
        "--reset-session",
        action="store_true",
        help="Delete the existing .session file before starting (forces re-login).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Walk the admin log and render previews, but do not write "
            "messages.json or download media."
        ),
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the [y/N] confirmation prompt before starting.",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colours (also honoured via NO_COLOR=1).",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Render every event as a Rich panel instead of a one-liner "
            "(env TG_BACKUP_VERBOSE=1 also enables this)."
        ),
    )
    return p.parse_args(argv)


@dataclass
class _ResolvedRun:
    """User-supplied or interactively-resolved inputs for one backup run."""

    api_id: int
    api_hash: str
    chat_id: int
    mode: int
    range_: ExportRange = field(default_factory=ExportRange)
    verbose: bool = False


def _resolve_verbose(args: argparse.Namespace) -> bool:
    """Resolve verbose mode from CLI flag + ``TG_BACKUP_VERBOSE`` env var."""
    env = os.environ.get("TG_BACKUP_VERBOSE", "").strip()
    return args.verbose or env not in ("", "0", "false", "False")


def _resolve_credentials(args: argparse.Namespace) -> tuple[int, str]:
    """Prompt for api credentials if missing on the command line."""
    api_id = args.api_id
    if api_id is None:
        api_id = int(prompt_text("Enter your api_id:").strip())
    api_hash = args.api_hash or prompt_text("Enter your api_hash:").strip()
    return api_id, api_hash


def _resolve_range(args: argparse.Namespace) -> ExportRange:
    """Prompt for min/max id range bounds if missing on the command line."""
    min_id = args.min_id
    if min_id is None:
        raw = prompt_text(
            "Enter the minimum message ID (0 to start from the first):"
        ).strip()
        min_id = int(raw) if raw else 0
    max_id = args.max_id
    if max_id is None:
        raw = prompt_text(
            "Enter the maximum message ID (0 to retrieve all):"
        ).strip()
        max_id = int(raw) if raw else 0
    return ExportRange(min_id=min_id, max_id=max_id)


async def _resolve_inputs(args: argparse.Namespace) -> _ResolvedRun:
    """Collect every interactive prompt up-front so the export call is pure."""
    api_id, api_hash = _resolve_credentials(args)
    chat_id = args.chat_id
    if chat_id is None:
        chat_id = prompt_chat_id(args.output_folder, action="backup")
    mode = args.mode
    if mode is None:
        mode = await prompt_export_mode()
    return _ResolvedRun(
        api_id=api_id,
        api_hash=api_hash,
        chat_id=chat_id,
        mode=mode,
        range_=_resolve_range(args),
        verbose=_resolve_verbose(args),
    )


def _build_paths(layout: ChatLayout) -> ExportPaths:
    """Materialise per-chat file paths into an :class:`ExportPaths` bundle."""
    return ExportPaths(
        messages=layout.messages,
        media_folder=layout.media,
        state_path=os.path.join(layout.chat_dir, STATE_FILENAME),
        error_log=os.path.join(layout.chat_dir, ERROR_LOG_FILENAME),
        missing_log=os.path.join(layout.chat_dir, MISSING_LOG_FILENAME),
    )


def _build_flags(args: argparse.Namespace, mode: int, verbose: bool) -> ExportFlags:
    """Translate CLI/interactive answers into an :class:`ExportFlags` bundle."""
    return ExportFlags(
        save_text=mode in (1, 3),
        save_media=mode in (1, 2),
        dry_run=args.dry_run,
        verbose=verbose,
        limit=args.limit,
    )


def _log_state_summary(state: dict[str, Any]) -> None:
    """Print resume-state highlights so the user sees what's preloaded."""
    if state["saved_ids"]:
        info(
            f"state loaded: [bold]{len(state['saved_ids'])}[/bold] "
            f"messages already saved."
        )
    if state["permanent_failures"]:
        info(
            f"state loaded: [bold]{len(state['permanent_failures'])}[/bold] "
            f"permanent failures recorded."
        )


def _confirm_or_abort(args: argparse.Namespace) -> bool:
    """Return ``True`` if the export may proceed, ``False`` if user aborted."""
    if args.dry_run:
        info(
            "[bold yellow]dry-run mode[/bold yellow]: messages.json will not be "
            "written, no media will be downloaded."
        )
        return True
    if args.yes:
        return True
    # Default is YES — pressing Enter accepts. Type "n" to abort.
    answer = prompt_text("Proceed? (Y/n)").strip().lower()
    if answer and answer not in ("y", "yes", "д", "да"):
        warn("aborted.")
        return False
    return True


def _maybe_reset_session(args: argparse.Namespace) -> None:
    """Delete the session file when ``--reset-session`` was passed."""
    session_file = f"{args.session_name}.session"
    if args.reset_session and os.path.exists(session_file):
        os.remove(session_file)
        info(f"existing session file removed: [bold]{session_file}[/bold]")


async def main(argv: Optional[list[str]] = None) -> None:
    """Async entry point for ``python -m src.backup``.

    Parses CLI flags, prompts for anything the user omitted (api creds,
    chat id, export mode), resolves the on-disk :class:`ChatLayout`,
    starts a Telethon session and delegates the actual export to
    :func:`export_messages`. The Telethon client is always disconnected
    via ``try/finally`` so the session file stays consistent on Ctrl+C.
    """
    args = parse_cli_args(argv)
    if args.no_color:
        STDOUT_CONSOLE.no_color = True

    run = await _resolve_inputs(args)

    layout = chat_layout(args.output_folder, run.chat_id)
    layout.ensure()
    paths = _build_paths(layout)
    info(f"backup folder: [bold]{layout.chat_dir}[/bold]")

    _maybe_reset_session(args)
    state = load_state(paths.state_path)
    _log_state_summary(state)

    if not _confirm_or_abort(args):
        return

    flags = _build_flags(args, run.mode, run.verbose)
    client = TelegramClient(args.session_name, run.api_id, run.api_hash)
    await client.start()
    try:
        await export_messages(
            client,
            _ExportRequest(
                target_group_id=run.chat_id,
                paths=paths,
                flags=flags,
                range_=run.range_,
                state=state,
            ),
        )
    finally:
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
