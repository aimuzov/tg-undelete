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
from typing import Any, Optional

from telethon import TelegramClient
from telethon.tl.types import PeerChannel
from telethon.errors import RPCError, FileReferenceExpiredError

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
from src.common.message_view import (
    render_message_oneline,
    render_message_panel,
    render_summary_table,
)
from src.common.progress import make_lot_progress
from src.common.telethon_adapter import build_rendered_from_telethon


# ---------- defaults & constants ----------

DEFAULT_OUTPUT_FOLDER: str = "backups"
DEFAULT_SESSION_NAME: str = "session_name"
STATE_FILENAME: str = "backup_state.json"
ERROR_LOG_FILENAME: str = "backup_errors.log"
MISSING_LOG_FILENAME: str = "backup_missing_media.log"

# Save state every N processed events so a kill -9 loses at most that many ids.
STATE_FLUSH_EVERY: int = 25

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
    media,
    file_path: str,
    failed: Optional[list] = None,
    permanent: Optional[list] = None,
    error_log: Optional[str] = None,
) -> Optional[str]:
    """
    Downloads media, surviving Telegram timeouts and other transient errors.

    Telethon retries internally up to 5 times and then raises
    ``ValueError('Request was unsuccessful 6 time(s)')``. Without protection,
    one broken file aborts the whole export. Failures are logged and, if
    the corresponding list is provided, recorded for a later retry pass
    (``failed``) or reported as unrecoverable (``permanent``).

    Skips the download if a file at ``<file_path>.<ext>`` already exists on
    disk — useful when re-running with mode=1 to capture media metadata into
    the dump without re-downloading the bytes.
    """
    existing = glob.glob(file_path + ".*")
    if existing:
        info(f"skipping download, already on disk: [dim]{existing[0]}[/dim]")
        return existing[0]
    try:
        return await client.download_media(media, file_path)
    except DOWNLOAD_PERMANENT_ERRORS as exc:
        error(
            f"permanently failed for {file_path}: {exc}. not retrying.",
            log_path=error_log,
        )
        if permanent is not None:
            permanent.append((file_path, str(exc)))
        return None
    except DOWNLOAD_RETRYABLE_ERRORS as exc:
        warn(f"failed to download media for {file_path}: {exc}. skipping.")
        if failed is not None:
            failed.append((media, file_path))
        return None


async def retry_failed_downloads(
    client: TelegramClient,
    failed: list,
    permanent: Optional[list] = None,
    error_log: Optional[str] = None,
    max_rounds: int = 3,
    base_delay: float = 2.0,
) -> list:
    """
    Retries previously failed downloads in several rounds with exponential
    backoff between rounds. Permanent errors encountered during retries are
    moved to ``permanent`` (if provided) and removed from the retry queue.
    Returns the list of items that still failed transiently.
    """
    for round_num in range(1, max_rounds + 1):
        if not failed:
            return []

        delay = base_delay * (2 ** (round_num - 1))
        info(
            f"retry pass [bold]{round_num}/{max_rounds}[/bold]: "
            f"{len(failed)} item(s) to retry (sleeping {delay:.0f}s first)..."
        )
        await asyncio.sleep(delay)

        next_failed: list = []
        total = len(failed)
        with make_lot_progress() as progress:
            task = progress.add_task(f"retry pass {round_num}/{max_rounds}", total=total)
            for idx, (media, file_path) in enumerate(failed, 1):
                try:
                    await client.download_media(media, file_path)
                    success(f"\\[{idx}/{total}] recovered: {file_path}")
                except DOWNLOAD_PERMANENT_ERRORS as exc:
                    error(
                        f"\\[{idx}/{total}] permanently failed for {file_path}: "
                        f"{exc}. dropping from retry queue.",
                        log_path=error_log,
                    )
                    if permanent is not None:
                        permanent.append((file_path, str(exc)))
                except DOWNLOAD_RETRYABLE_ERRORS as exc:
                    warn(f"\\[{idx}/{total}] still failing for {file_path}: {exc}")
                    next_failed.append((media, file_path))
                progress.update(task, advance=1)
                await asyncio.sleep(0.3)  # Be gentle with the API between items

        failed = next_failed

    return failed


# ---------- core export ----------


async def export_messages(
    client: TelegramClient,
    target_group_id: int,
    mode: int,
    *,
    messages_path: str,
    media_folder: str,
    state: dict[str, Any],
    state_path: str,
    error_log: str,
    missing_log: str,
    min_id: int = 0,
    max_id: int = 0,
    dry_run: bool = False,
    limit: Optional[int] = None,
    verbose: bool = False,
) -> None:
    """
    Exports deleted messages from a Telegram group or channel.

    The two checkbox flags are orthogonal:

    * ``save_text`` (mode 1 or 3) — write every deleted message into
      ``messages.json`` (with full media metadata when present, but without
      downloading the file).
    * ``save_media`` (mode 1 or 2) — download the media file. If the message
      isn't yet in ``messages.json``, it gets appended too — so a media-only
      run "back-fills" the JSON for the media events it touches.

    Resume: ``state["saved_ids"]`` lists message ids already in the JSON.
    A re-run never appends a duplicate JSON object, but media downloads are
    re-attempted only when the file is missing on disk
    (``safe_download_media`` skips if a file at ``<id>.<ext>`` already exists).

    With ``dry_run=True`` no on-disk side effects happen: the dump file stays
    untouched, media is not downloaded, but every event still gets a Rich
    preview.
    """
    group: PeerChannel = await client.get_entity(PeerChannel(target_group_id))

    save_text = mode in (1, 3)
    save_media = mode in (1, 2)

    saved_ids: set[int] = set(state.get("saved_ids", []))
    permanent_failures: list = list(state.get("permanent_failures", []))
    failed_downloads: list = []
    permanent_count_before = len(permanent_failures)

    counts: dict[str, int] = {"messages": 0, "media": 0, "skipped": 0}
    start_ts = time.monotonic()
    still_failed: list = []

    with contextlib.ExitStack() as stack:
        dump: Optional[Any] = None
        if not dry_run:
            # Either flag may write into messages.json — text writes everything,
            # media-only back-fills records for downloaded media — so always open
            # the file when not in dry-run.
            file_mode: str = "a" if os.path.exists(messages_path) else "w"
            dump = stack.enter_context(
                open(messages_path, file_mode, encoding="utf-8")
            )

        try:
            with make_lot_progress() as progress:
                await _run_main_pass(
                    client,
                    group,
                    progress,
                    save_text=save_text,
                    save_media=save_media,
                    saved_ids=saved_ids,
                    failed_downloads=failed_downloads,
                    permanent_failures=permanent_failures,
                    counts=counts,
                    dump=dump,
                    state=state,
                    state_path=state_path,
                    media_folder=media_folder,
                    error_log=error_log,
                    min_id=min_id,
                    max_id=max_id,
                    dry_run=dry_run,
                    limit=limit,
                    verbose=verbose,
                )

            still_failed = await _run_retry_pass(
                client,
                failed_downloads,
                permanent_failures,
                error_log,
                dry_run=dry_run,
            )

            _log_permanent_failures(
                permanent_failures, permanent_count_before, missing_log
            )
        finally:
            if not dry_run:
                state["saved_ids"] = sorted(saved_ids)
                state["permanent_failures"] = permanent_failures
                save_state(state_path, state)

    elapsed = time.monotonic() - start_ts
    _render_final_report(
        counts=counts,
        permanent_failures=permanent_failures,
        still_failed=still_failed,
        state_path=state_path,
        missing_log=missing_log,
        error_log=error_log,
        elapsed=elapsed,
        dry_run=dry_run,
    )


# Number of admin-log events per Telegram API request. 100 is the cap that
# `iter_admin_log` accepts before silently downgrading.
ADMIN_LOG_BATCH_SIZE: int = 100


async def _run_main_pass(
    client: TelegramClient,
    group: Any,
    progress: Any,
    *,
    save_text: bool,
    save_media: bool,
    saved_ids: set[int],
    failed_downloads: list,
    permanent_failures: list,
    counts: dict[str, int],
    dump: Optional[Any],
    state: dict[str, Any],
    state_path: str,
    media_folder: str,
    error_log: str,
    min_id: int,
    max_id: int,
    dry_run: bool,
    limit: Optional[int],
    verbose: bool,
) -> None:
    """Pulls deletion events page-by-page and dispatches each to the per-event
    handler. Stops on empty page, reaching min_id, or hitting --limit."""
    task_id = progress.add_task("Exporting deleted events", total=None)
    events_seen = 0  # Events that produced a write (drives state-flush cadence)

    try:
        while True:
            events = [
                event
                async for event in client.iter_admin_log(
                    group,
                    min_id=min_id or 0,
                    max_id=max_id or 0,
                    limit=ADMIN_LOG_BATCH_SIZE,
                    delete=True,  # Interested only in deleted messages
                )
            ]

            if not events:
                success("loading complete, no new messages.")
                return

            stop = False
            for event in events:
                progress.update(task_id, advance=1)
                events_seen = await _process_event(
                    event,
                    client=client,
                    progress=progress,
                    save_text=save_text,
                    save_media=save_media,
                    saved_ids=saved_ids,
                    failed_downloads=failed_downloads,
                    permanent_failures=permanent_failures,
                    counts=counts,
                    events_seen=events_seen,
                    dump=dump,
                    state=state,
                    state_path=state_path,
                    media_folder=media_folder,
                    error_log=error_log,
                    min_id=min_id,
                    dry_run=dry_run,
                    verbose=verbose,
                )
                if limit is not None and (counts["messages"] + counts["media"]) >= limit:
                    warn(f"reached --limit {limit}, stopping.")
                    stop = True
                    break

            if stop:
                return

            max_id = events[-1].id - 1  # Exclude last received event from next request
            if max_id < min_id:
                info("reached the lower message id limit.")
                return

    except RPCError as exc:
        reason = f"{type(exc).__name__}: {exc}"
        error(f"an error occurred: {reason}", log_path=error_log)
        append_line(error_log, reason)


async def _process_event(
    event: Any,
    *,
    client: TelegramClient,
    progress: Any,
    save_text: bool,
    save_media: bool,
    saved_ids: set[int],
    failed_downloads: list,
    permanent_failures: list,
    counts: dict[str, int],
    events_seen: int,
    dump: Optional[Any],
    state: dict[str, Any],
    state_path: str,
    media_folder: str,
    error_log: str,
    min_id: int,
    dry_run: bool,
    verbose: bool,
) -> int:
    """Processes one admin-log event: appends to JSON dump, downloads media,
    prints a preview. Returns the updated `events_seen` counter so the caller
    keeps flush cadence in sync."""
    if not (event.deleted_message and event.old.id >= min_id):
        return events_seen

    has_media = event.old.media is not None

    # Eligibility (what *would* happen ignoring resume state):
    #   text-checkbox → write everything
    #   media-checkbox → also write media events (so the JSON
    #                    learns about them when text wasn't run)
    json_eligible = save_text or (save_media and has_media)
    write_json = json_eligible and event.old.id not in saved_ids
    download_media = save_media and has_media

    if not write_json and not download_media:
        counts["skipped"] += 1
        return events_seen

    note_bits: list[str] = []

    if write_json:
        if not dry_run and dump is not None:
            dump.write(event.old.to_json() + ",")
        counts["messages"] += 1
        saved_ids.add(event.old.id)
        note_bits.append(f"{'preview' if dry_run else 'saved'} #{counts['messages']}")

    download_path: Optional[str] = None
    if download_media:
        if not dry_run:
            download_path = await safe_download_media(
                client,
                event.old.media,
                os.path.join(media_folder, str(event.old.id)),
                failed=failed_downloads,
                permanent=permanent_failures,
                error_log=error_log,
            )
            if download_path is not None:
                counts["media"] += 1
                note_bits.append(f"media #{counts['media']}")
        else:
            counts["media"] += 1
            note_bits.append(f"preview media #{counts['media']}")

    rendered = await build_rendered_from_telethon(
        event.old,
        client=client,
        user_cache=USER_NAME_CACHE,
        note=" + ".join(note_bits) if note_bits else None,
    )
    if verbose:
        progress.console.print(render_message_panel(rendered))
    else:
        progress.console.print(render_message_oneline(rendered))

    if download_path is not None:
        success(
            f"saved media #{counts['media']} (id: {event.old.id}, "
            f"file: [dim]{download_path}[/dim])"
        )

    if write_json:
        events_seen += 1
        if not dry_run and events_seen % STATE_FLUSH_EVERY == 0:
            state["saved_ids"] = sorted(saved_ids)
            state["permanent_failures"] = permanent_failures
            save_state(state_path, state)

    await asyncio.sleep(0.1)  # Short pause to avoid flooding API
    return events_seen


async def _run_retry_pass(
    client: TelegramClient,
    failed_downloads: list,
    permanent_failures: list,
    error_log: str,
    *,
    dry_run: bool,
) -> list:
    """Retries any media that hit a transient error during the main pass.
    Prints a summary of what remains broken. No-op in dry-run."""
    if not failed_downloads or dry_run:
        return []

    warn(
        f"main pass finished with [bold]{len(failed_downloads)}[/bold] "
        f"transient failure(s). starting retry pass."
    )
    still_failed = await retry_failed_downloads(
        client,
        failed_downloads,
        permanent=permanent_failures,
        error_log=error_log,
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


def _render_final_report(
    *,
    counts: dict[str, int],
    permanent_failures: list,
    still_failed: list,
    state_path: str,
    missing_log: str,
    error_log: str,
    elapsed: float,
    dry_run: bool,
) -> None:
    """Renders the run summary table at the end of `export_messages`."""
    footer: list[str] = []
    if not dry_run:
        footer.append(f"State: {state_path}")
    if os.path.exists(missing_log):
        footer.append(f"Missing media log: {missing_log}")
    if os.path.exists(error_log):
        footer.append(f"Errors log: {error_log}")

    title_prefix = "Backup dry-run complete" if dry_run else "Backup complete"
    STDOUT_CONSOLE.print(
        render_summary_table(
            title=f"{title_prefix} in {format_eta(elapsed)}",
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
                    str(len(still_failed)),
                    "yellow" if still_failed else "dim",
                ),
                (
                    "Permanent failures",
                    str(len(permanent_failures)),
                    "red" if permanent_failures else "dim",
                ),
            ],
            footer=footer or None,
        )
    )


# ---------- CLI ----------


async def prompt_export_mode() -> int:
    """Interactive checkbox picker — returns 1 (all), 2 (media only), 3 (text only).

    Both items checked → mode 1; only media → 2; only text → 3. An empty
    selection re-prompts. Falls back to plain numeric input if `questionary`
    is unavailable.
    """
    try:
        import questionary
    except ImportError:
        warn(
            "questionary is not installed; falling back to text input. "
            "run `pip install questionary` for the checkbox UI."
        )
        return _prompt_export_mode_text()

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


def _prompt_export_mode_text() -> int:
    """Plain-input fallback for environments without questionary."""
    while True:
        raw = prompt_text(
            "Enter export mode (1 - all, 2 - media only, 3 - text only):"
        ).strip()
        try:
            value = int(raw)
        except ValueError:
            STDOUT_CONSOLE.print(f"  [red]Bad input '{raw}', expected 1-3.[/red]")
            continue
        if value in (1, 2, 3):
            return value
        STDOUT_CONSOLE.print(f"  [red]Out of range, expected 1-3 (got {value}).[/red]")


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

    verbose = args.verbose or os.environ.get(
        "TG_BACKUP_VERBOSE", ""
    ).strip() not in ("", "0", "false", "False")

    api_id = args.api_id
    if api_id is None:
        api_id = int(prompt_text("Enter your api_id:").strip())
    api_hash = args.api_hash or prompt_text("Enter your api_hash:").strip()

    chat_id = args.chat_id
    if chat_id is None:
        # Offer existing backups first so the user can resume into one of
        # them; falls back to free-text entry for a brand-new chat.
        chat_id = prompt_chat_id(args.output_folder, action="backup")

    mode = args.mode
    if mode is None:
        mode = await prompt_export_mode()

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

    # Layout: <output-folder>/<chat-id>/{messages.json, media/, *.log, *.json}
    layout = chat_layout(args.output_folder, chat_id)
    layout.ensure()

    state_path = os.path.join(layout.chat_dir, STATE_FILENAME)
    error_log = os.path.join(layout.chat_dir, ERROR_LOG_FILENAME)
    missing_log = os.path.join(layout.chat_dir, MISSING_LOG_FILENAME)
    info(f"backup folder: [bold]{layout.chat_dir}[/bold]")

    session_file = f"{args.session_name}.session"
    if args.reset_session and os.path.exists(session_file):
        os.remove(session_file)
        info(f"existing session file removed: [bold]{session_file}[/bold]")

    state = load_state(state_path)
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

    if args.dry_run:
        info(
            "[bold yellow]dry-run mode[/bold yellow]: messages.json will not be "
            "written, no media will be downloaded."
        )
    elif not args.yes:
        # Default is YES — pressing Enter accepts. Type "n" to abort.
        answer = prompt_text("Proceed? (Y/n)").strip().lower()
        if answer and answer not in ("y", "yes", "д", "да"):
            warn("aborted.")
            return

    client: TelegramClient = TelegramClient(args.session_name, api_id, api_hash)
    await client.start()
    try:
        await export_messages(
            client,
            chat_id,
            mode,
            messages_path=layout.messages,
            media_folder=layout.media,
            state=state,
            state_path=state_path,
            error_log=error_log,
            missing_log=missing_log,
            min_id=min_id,
            max_id=max_id,
            dry_run=args.dry_run,
            limit=args.limit,
            verbose=verbose,
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
