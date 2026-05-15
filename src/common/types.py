"""Shared dataclasses used to bundle the many parameters of ``src.backup``.

Each bundle groups attributes that always travel together so we can pass
one object instead of 10+ keyword arguments. Kept here (rather than inside
``src.backup``) because :func:`src.common.telethon_adapter` already lives
under ``src.common`` and a future ``src.resender`` refactor would reuse the
same shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ExportPaths:
    """File paths for one chat-export run."""

    messages: str
    media_folder: str
    state_path: str
    error_log: str
    missing_log: str


@dataclass
class ExportFlags:
    """Boolean/optional flags that steer the export pipeline."""

    save_text: bool
    save_media: bool
    dry_run: bool = False
    verbose: bool = False
    limit: Optional[int] = None


@dataclass
class ExportRange:
    """Inclusive admin-log message-id range (``0`` means open)."""

    min_id: int = 0
    max_id: int = 0


@dataclass
class ExportCounters:
    """Run-time accumulators shared between the main and retry passes."""

    counts: dict[str, int] = field(
        default_factory=lambda: {"messages": 0, "media": 0, "skipped": 0}
    )
    saved_ids: set[int] = field(default_factory=set)
    failed_downloads: list = field(default_factory=list)
    permanent_failures: list = field(default_factory=list)


@dataclass
class DownloadRequest:
    """Single media-download attempt + its retry/permanent sinks."""

    media: Any
    file_path: str
    failed: Optional[list] = None
    permanent: Optional[list] = None
    error_log: Optional[str] = None


@dataclass
class FinalReportContext:
    """Inputs for the end-of-run summary table."""

    counters: ExportCounters
    still_failed: list
    paths: ExportPaths
    elapsed: float
    dry_run: bool
