"""Terminal output parser for Claude Code AskUserQuestion UI.

Detects when terminal shows an AskUserQuestion UI and extracts the content
between the horizontal separator lines for display to the user.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AskQuestionContent:
    """Content extracted from AskUserQuestion UI."""

    content: str  # The full content between separator lines
    supports_esc: bool = True  # AskUserQuestion always supports Esc


def _is_separator_line(line: str) -> bool:
    """Check if a line is a horizontal separator."""
    clean = line.strip()
    if not clean or len(clean) < 10:
        return False
    dash_count = sum(1 for c in clean if c in "─━═")
    return dash_count / len(clean) > 0.8


def extract_ask_question_content(pane_text: str) -> AskQuestionContent | None:
    """Extract content from AskUserQuestion UI.

    The UI format has horizontal separator lines (─) at the top and bottom
    of the interactive area. This function searches from the bottom up to find
    the last two separators and extracts the content between them.

    Returns None if the text doesn't contain a recognizable question UI.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")
    if len(lines) < 5:
        return None

    # Find horizontal separator lines from bottom up
    separator_indices: list[int] = []
    for i in range(len(lines) - 1, -1, -1):
        if _is_separator_line(lines[i]):
            separator_indices.append(i)
            if len(separator_indices) >= 2:
                break

    if len(separator_indices) < 2:
        return None

    # separator_indices[0] is bottom line, separator_indices[1] is top line
    bottom_idx = separator_indices[0]
    top_idx = separator_indices[1]

    if bottom_idx - top_idx < 3:
        return None

    # Extract content between the two separators, filtering out any separator lines
    content_lines: list[str] = []
    for i in range(top_idx + 1, bottom_idx):
        line = lines[i]
        if not _is_separator_line(line):
            content_lines.append(line)

    content = "\n".join(content_lines)

    return AskQuestionContent(
        content=content,
        supports_esc=True,
    )


def is_ask_question_ui(pane_text: str) -> bool:
    """Quick check if terminal shows an AskUserQuestion UI.

    This is a fast heuristic check before doing full parsing.
    Looks for characteristic patterns of the AskUserQuestion UI:
    - Has ❯ selector and numbered options
    - Has question tabs with checkboxes (☐ or ✔)
    """
    if not pane_text:
        return False

    # Look for characteristic patterns
    # 1. Has numbered options with ❯ selector
    has_selector = "❯" in pane_text
    has_numbered = any(f"{i}." in pane_text for i in range(1, 10))

    if has_selector and has_numbered:
        return True

    # 2. Has question tabs with checkboxes and Submit
    has_checkbox = "☐" in pane_text or "✔" in pane_text
    has_submit = "Submit" in pane_text

    if has_checkbox and has_submit:
        return True

    return False
