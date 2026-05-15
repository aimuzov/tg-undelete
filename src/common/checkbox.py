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

import questionary
import questionary.prompts.common as qcommon


# Attribute names live in variables so pylint W0212 (protected-access) does
# not statically resolve them — these are legitimate monkey-patches.
_CHOICE_TOKENS_ATTR = "_get_choice_tokens"
_MARKER_QUESTION = "_undelete_question_lstripped"
_MARKER_CURSOR = "_undelete_cursor_promoted"
_MARKER_PATCHED = "_undelete_patched"


def _patch_indicators() -> None:
    """Replace bullet glyphs with ASCII checkbox tokens."""
    # The constants live in questionary.constants but are imported by name
    # into common.py at module-load time — patch the consumer copy.
    qcommon.INDICATOR_SELECTED = "[x]"
    qcommon.INDICATOR_UNSELECTED = "[ ]"


def _patch_question_header_lstrip() -> None:
    """Strip the hard-coded leading whitespace from the question header.

    The token-building callback is a closure inside ``checkbox()`` we can't
    reach, so we wrap the layout factory and left-trim the first non-empty
    token before render.
    """
    if getattr(qcommon, _MARKER_QUESTION, False):
        return
    orig_create_layout = qcommon.create_inquirer_layout

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
                        continue
                    if stripped != text:
                        tok = (klass, stripped)
                    head_done = True
                out.append(tok)
            return out

        return orig_create_layout(ic, stripped_callback, **kwargs)

    qcommon.create_inquirer_layout = _create_layout_lstripped
    setattr(qcommon, _MARKER_QUESTION, True)


def _strip_leading_indent(row: list) -> list:
    """Drop the pointer-sized indent prepended to each choice row."""
    out: list = []
    head_done = False
    for tok in row:
        if not head_done:
            klass, text = tok[0], tok[1]
            if klass == "[SetCursorPosition]":
                # Keep the cursor marker — prompt_toolkit needs it to
                # position the terminal caret inside the prompt.
                out.append(tok)
                continue
            if text.strip() == "":
                continue
            head_done = True
        out.append(tok)
    return out


def _patch_cursor_promotion() -> None:
    """Make the cursor row win style precedence over checked rows.

    Strips the leading pointer-sized indent that questionary unconditionally
    prepends to every choice, and promotes ``class:selected`` / ``class:text``
    on the cursor row to ``class:highlighted`` so focus shows even on
    checked items.
    """
    if getattr(qcommon, _MARKER_CURSOR, False):
        return
    control_cls = qcommon.InquirerControl
    orig_get_choice_tokens = getattr(control_cls, _CHOICE_TOKENS_ATTR)

    def _promote_cursor_row(self: Any) -> list:
        tokens = orig_get_choice_tokens(self)
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

    setattr(control_cls, _CHOICE_TOKENS_ATTR, _promote_cursor_row)
    setattr(qcommon, _MARKER_CURSOR, True)


def _apply_patches() -> None:
    """Apply all questionary tweaks. Safe to call multiple times."""
    if getattr(qcommon, _MARKER_PATCHED, False):
        return
    _patch_indicators()
    _patch_question_header_lstrip()
    _patch_cursor_promotion()
    setattr(qcommon, _MARKER_PATCHED, True)


def _build_style() -> Any:
    """Shared Style — grey rows, cyan question, orange focus."""
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
    """
    _apply_patches()

    # `kbi_msg=""` silences questionary's own "\nCancelled by user\n" print
    # on Ctrl+C — we log our own [info] message instead.
    return await questionary.checkbox(
        message,
        choices=choices,
        instruction=instruction,
        qmark="",
        pointer="",
        style=_build_style(),
    ).ask_async(kbi_msg="")


__all__ = ["undelete_checkbox"]
