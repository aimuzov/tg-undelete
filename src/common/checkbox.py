"""Shared `questionary.checkbox` UI for `src.backup` and `src.resender`.

questionary's defaults don't match what we want — bullet glyphs ●/○, a
leading "?" and a "❯" pointer, full-row reverse-video on the focused item,
hard-coded space-padding on the question header, and `class:selected`
overriding `class:highlighted` so a checked-row-under-cursor never reveals
focus.

This module:
  * Patches `questionary.prompts.common` once per process to fix all of the
    above. Patches are idempotent — re-importing the module is safe.
  * Exposes `undelete_checkbox()` — a thin async wrapper that applies the
    patches, builds our shared `Style`, and calls `ask_async()`.

Use it instead of touching `questionary` directly so both CLIs stay in sync.
"""

from __future__ import annotations

from typing import Any


_PATCHED: bool = False


def _apply_patches() -> None:
    """Apply all questionary tweaks. Safe to call multiple times."""
    global _PATCHED
    if _PATCHED:
        return

    import questionary.prompts.common as qcommon

    # 1. Replace ●/○ with [x]/[ ]. The constants live in
    # questionary.constants but are imported by name into common.py at
    # module-load time — patch the consumer copy.
    qcommon.INDICATOR_SELECTED = "[x]"
    qcommon.INDICATOR_UNSELECTED = "[ ]"

    # 2. Strip the hard-coded leading whitespace that the question header
    # gets via `" {message} "`. The token-building callback is a closure
    # inside `checkbox()` we can't reach, so we wrap the layout factory and
    # left-trim the very first non-empty token before render.
    if not getattr(qcommon, "_undelete_question_lstripped", False):
        _orig_create_layout = qcommon.create_inquirer_layout

        def _create_layout_lstripped(ic: Any, get_prompt_tokens: Any, **kwargs: Any) -> Any:
            def stripped_callback() -> list:
                toks = list(get_prompt_tokens())
                out: list = []
                head_done = False
                for tok in toks:
                    if not head_done:
                        klass, text = tok[0], tok[1]
                        stripped = text.lstrip()
                        if not stripped:
                            # All-whitespace token — drop entirely.
                            continue
                        if stripped != text:
                            tok = (klass, stripped)
                        head_done = True
                    out.append(tok)
                return out

            return _orig_create_layout(ic, stripped_callback, **kwargs)

        qcommon.create_inquirer_layout = _create_layout_lstripped
        qcommon._undelete_question_lstripped = True  # type: ignore[attr-defined]

    # 3. Strip the leading pointer-sized indent that questionary unconditionally
    # prepends to every choice; and promote `class:selected`/`class:text` on
    # the cursor's row to `class:highlighted` so focus shows even on checked
    # items (questionary's default render makes `selected` win over `highlighted`).
    if not getattr(qcommon, "_undelete_cursor_promoted", False):
        _orig_get_choice_tokens = qcommon.InquirerControl._get_choice_tokens

        def _strip_leading_indent(row: list) -> list:
            out: list = []
            head_done = False
            for tok in row:
                if not head_done:
                    klass, text = tok[0], tok[1]
                    if klass == "[SetCursorPosition]":
                        # Keep the cursor marker — prompt_toolkit needs it
                        # to position the terminal caret inside the prompt.
                        out.append(tok)
                        continue
                    if text.strip() == "":
                        continue
                    head_done = True
                out.append(tok)
            return out

        def _promote_cursor_row(self: Any) -> list:
            tokens = _orig_get_choice_tokens(self)
            # Split into per-choice rows by trailing "\n". Trailing extras
            # (description / answer hint) end up past `len(filtered_choices)`.
            rows: list[list] = []
            current: list = []
            for tok in tokens:
                current.append(tok)
                if tok[1].endswith("\n"):
                    rows.append(current)
                    current = []
            if current:
                rows.append(current)

            pointed = self.pointed_at
            choice_count = len(self.filtered_choices)
            for i in range(min(choice_count, len(rows))):
                rows[i] = _strip_leading_indent(rows[i])
                if i == pointed:
                    rows[i] = [
                        (
                            "class:highlighted"
                            if t[0] in ("class:selected", "class:text")
                            else t[0],
                            t[1],
                        )
                        for t in rows[i]
                    ]
            return [t for row in rows for t in row]

        qcommon.InquirerControl._get_choice_tokens = _promote_cursor_row
        qcommon._undelete_cursor_promoted = True  # type: ignore[attr-defined]

    _PATCHED = True


def _build_style() -> Any:
    """Shared Style — grey rows, cyan question, orange focus."""
    import questionary

    return questionary.Style(
        [
            ("qmark", "fg:ansibrightcyan bold bg:default noreverse"),
            ("question", "fg:ansibrightcyan bold bg:default noreverse"),
            ("highlighted", "fg:orange bold bg:default noreverse"),
            ("selected", "fg:ansibrightblack bg:default noreverse"),
            ("text", "fg:ansibrightblack bg:default noreverse"),
            ("instruction", "fg:ansibrightblack bg:default noreverse"),
            ("disabled", "fg:ansibrightblack italic bg:default noreverse"),
        ]
    )


async def undelete_checkbox(
    message: str,
    choices: list,
    *,
    instruction: str = "(space toggles, enter confirms)",
) -> Any:
    """Ask a multi-select checkbox question with our consistent UI.

    Returns the list of selected ``Choice.value``s, or ``None`` if the user
    pressed Esc / Ctrl+C. The caller decides what an empty selection means
    and whether to re-prompt.

    Raises ``ImportError`` if questionary isn't installed — the caller is
    expected to fall back to plain text input.
    """
    import questionary

    _apply_patches()

    return await questionary.checkbox(
        message,
        choices=choices,
        instruction=instruction,
        qmark="",
        pointer="",
        style=_build_style(),
    ).ask_async(kbi_msg="")  # silence questionary's own "\nCancelled by user\n" print on Ctrl+C — we log our own [info] message


__all__ = ["undelete_checkbox"]
