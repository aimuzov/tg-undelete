"""Factory for a `rich.progress.Progress` tuned for our lot loops."""

from __future__ import annotations

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from .console import STDOUT_CONSOLE


def make_lot_progress(*, transient: bool = False) -> Progress:
    """Builds the Rich ``Progress`` shared by backup and resender loops.

    Combines a spinner, bar, ``M/N`` counter and elapsed/remaining timers on
    the global ``STDOUT_CONSOLE``. Pass ``transient=True`` to have the bar
    erase itself once the task completes.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("·"),
        TimeElapsedColumn(),
        TextColumn("/"),
        TimeRemainingColumn(),
        console=STDOUT_CONSOLE,
        transient=transient,
    )
