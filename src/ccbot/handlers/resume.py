"""Resume session picker — browse and resume previous Claude Code conversations.

Reads ~/.claude/history.jsonl to discover past sessions for the current
project, presents a paginated inline keyboard, and resumes the selected
session by sending Ctrl+C (to exit the current session) followed by
``claude --resume <session_id>`` in the tmux pane.

Key functions:
  - scan_sessions: Read history.jsonl and group by session
  - build_resume_keyboard: Paginated inline keyboard of sessions
  - resume_command: /resume command handler
  - handle_resume_callback: Callback handler for session selection/pagination
"""

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..config import config
from ..handlers.message_sender import safe_edit, safe_reply
from ..session import session_manager
from ..tmux_manager import tmux_manager

logger = logging.getLogger(__name__)

# Callback data prefixes
CB_RESUME_SELECT = "rs:sel:"  # rs:sel:<index>
CB_RESUME_PAGE = "rs:pg:"  # rs:pg:<page>
CB_RESUME_CANCEL = "rs:cancel"
CB_RESUME_CONFIRM = "rs:ok:"  # rs:ok:<index>

# User data keys for caching session list
RESUME_SESSIONS_KEY = "_resume_sessions"  # list of SessionSummary dicts
RESUME_PAGE_KEY = "_resume_page"
RESUME_WINDOW_KEY = "_resume_window_id"

SESSIONS_PER_PAGE = 6


@dataclass
class SessionSummary:
    """Summary of a past Claude Code session."""

    session_id: str
    title: str  # First user message (truncated)
    last_active: float  # Unix timestamp in seconds
    message_count: int  # Number of user inputs
    project: str  # Project path

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "last_active": self.last_active,
            "message_count": self.message_count,
            "project": self.project,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionSummary":
        return cls(
            session_id=d["session_id"],
            title=d["title"],
            last_active=d["last_active"],
            message_count=d["message_count"],
            project=d["project"],
        )


def _relative_time(ts: float) -> str:
    """Format a timestamp as a human-readable relative time."""
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        mins = int(delta / 60)
        return f"{mins}m ago"
    if delta < 86400:
        hours = int(delta / 3600)
        return f"{hours}h ago"
    days = int(delta / 86400)
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days}d ago"
    return f"{days // 30}mo ago"


def scan_sessions(project_path: str, limit: int = 30) -> list[SessionSummary]:
    """Scan ~/.claude/history.jsonl for past sessions matching a project.

    Args:
        project_path: Absolute path to the project directory
        limit: Maximum number of sessions to return

    Returns:
        List of SessionSummary sorted by last_active descending
    """
    history_file = Path.home() / ".claude" / "history.jsonl"
    if not history_file.exists():
        logger.warning("history.jsonl not found at %s", history_file)
        return []

    # Group entries by sessionId
    sessions: dict[str, list[dict]] = defaultdict(list)

    try:
        with open(history_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Filter by project
                if entry.get("project") != project_path:
                    continue

                sid = entry.get("sessionId", "")
                if sid:
                    sessions[sid].append(entry)
    except OSError as e:
        logger.error("Failed to read history.jsonl: %s", e)
        return []

    # Build summaries
    summaries = []
    for sid, entries in sessions.items():
        # Find the first non-command display text as title
        title = ""
        for e in entries:
            display = e.get("display", "").strip()
            if display and not display.startswith("/"):
                title = display
                break
        if not title:
            # Fall back to first display text even if it's a command
            title = entries[0].get("display", "Untitled").strip()

        # Truncate title
        if len(title) > 60:
            title = title[:57] + "..."

        # Timestamps are in milliseconds
        last_active = max(e.get("timestamp", 0) for e in entries) / 1000.0
        message_count = len(entries)

        summaries.append(
            SessionSummary(
                session_id=sid,
                title=title,
                last_active=last_active,
                message_count=message_count,
                project=project_path,
            )
        )

    # Sort by most recent first
    summaries.sort(key=lambda s: s.last_active, reverse=True)
    return summaries[:limit]


def build_resume_keyboard(
    sessions: list[SessionSummary], page: int = 0
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a paginated inline keyboard for session selection.

    Args:
        sessions: List of session summaries
        page: Current page (0-indexed)

    Returns:
        Tuple of (message text, keyboard markup)
    """
    if not sessions:
        text = "No previous sessions found for this project."
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Cancel", callback_data=CB_RESUME_CANCEL)]]
        )
        return text, keyboard

    total_pages = (len(sessions) + SESSIONS_PER_PAGE - 1) // SESSIONS_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    start = page * SESSIONS_PER_PAGE
    end = min(start + SESSIONS_PER_PAGE, len(sessions))
    page_sessions = sessions[start:end]

    text = f"**Resume Session** (Page {page + 1}/{total_pages})\n\n"
    rows: list[list[InlineKeyboardButton]] = []

    for i, s in enumerate(page_sessions):
        idx = start + i
        rel = _relative_time(s.last_active)
        emoji = "\U0001f4ac"  # speech bubble
        label = f"{emoji} {s.title[:40]}"
        text += f"**{idx + 1}.** {s.title}\n    {rel} \u2022 {s.message_count} msgs\n\n"
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"{CB_RESUME_SELECT}{idx}"[:64])]
        )

    # Pagination row
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                "\u25c0 Older", callback_data=f"{CB_RESUME_PAGE}{page - 1}"
            )
        )
    nav_row.append(
        InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
    )
    if page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                "Newer \u25b6", callback_data=f"{CB_RESUME_PAGE}{page + 1}"
            )
        )
    rows.append(nav_row)

    # Cancel row
    rows.append(
        [InlineKeyboardButton("\u274c Cancel", callback_data=CB_RESUME_CANCEL)]
    )

    keyboard = InlineKeyboardMarkup(rows)
    return text, keyboard


def build_confirm_keyboard(session: SessionSummary, idx: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build a confirmation prompt for resuming a specific session.

    Args:
        session: The selected session
        idx: Index in the session list

    Returns:
        Tuple of (message text, keyboard markup)
    """
    rel = _relative_time(session.last_active)
    text = (
        f"**Resume this session?**\n\n"
        f"\U0001f4ac **{session.title}**\n"
        f"\u23f0 {rel} \u2022 {session.message_count} msgs\n\n"
        f"\u26a0\ufe0f This will exit the current Claude session and resume the selected one."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\u2705 Resume", callback_data=f"{CB_RESUME_CONFIRM}{idx}"[:64]
                ),
                InlineKeyboardButton(
                    "\u25c0 Back", callback_data=f"{CB_RESUME_PAGE}0"
                ),
            ],
            [InlineKeyboardButton("\u274c Cancel", callback_data=CB_RESUME_CANCEL)],
        ]
    )
    return text, keyboard


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /resume command — show session picker."""
    user = update.effective_user
    if not user or not update.message:
        return

    thread_id = None
    msg = update.message
    tid = getattr(msg, "message_thread_id", None)
    if tid is not None and tid != 1:
        thread_id = tid

    if thread_id is None:
        await safe_reply(msg, "Use /resume inside a topic thread.")
        return

    # Get the window bound to this thread
    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(msg, "No session bound to this topic. Send a message first.")
        return

    # Get the cwd for this window
    ws = session_manager.get_window_state(wid)
    if not ws or not ws.cwd:
        await safe_reply(msg, "Could not determine project path for this session.")
        return

    project_path = ws.cwd

    # Scan sessions
    sessions = scan_sessions(project_path)

    # Cache in user_data
    if context.user_data is not None:
        context.user_data[RESUME_SESSIONS_KEY] = [s.to_dict() for s in sessions]
        context.user_data[RESUME_PAGE_KEY] = 0
        context.user_data[RESUME_WINDOW_KEY] = wid

    text, keyboard = build_resume_keyboard(sessions, page=0)
    await safe_reply(msg, text, reply_markup=keyboard)


async def handle_resume_callback(
    data: str,
    query: CallbackQuery,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    thread_id: int | None,
) -> None:
    """Handle resume-related callback queries.

    Args:
        data: The callback data string
        query: The Telegram callback query
        user_id: The user's Telegram ID
        context: Bot context with user_data
        thread_id: The thread ID from the update
    """
    # Load cached sessions
    raw_sessions = context.user_data.get(RESUME_SESSIONS_KEY, []) if context.user_data else []
    sessions = [SessionSummary.from_dict(d) for d in raw_sessions]
    cached_wid = context.user_data.get(RESUME_WINDOW_KEY) if context.user_data else None

    if data == CB_RESUME_CANCEL:
        if context.user_data is not None:
            context.user_data.pop(RESUME_SESSIONS_KEY, None)
            context.user_data.pop(RESUME_PAGE_KEY, None)
            context.user_data.pop(RESUME_WINDOW_KEY, None)
        await safe_edit(query, "Resume cancelled.")
        await query.answer("Cancelled")
        return

    if data.startswith(CB_RESUME_PAGE):
        try:
            page = int(data[len(CB_RESUME_PAGE):])
        except ValueError:
            await query.answer("Invalid page")
            return

        if context.user_data is not None:
            context.user_data[RESUME_PAGE_KEY] = page

        text, keyboard = build_resume_keyboard(sessions, page=page)
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer()
        return

    if data.startswith(CB_RESUME_SELECT):
        try:
            idx = int(data[len(CB_RESUME_SELECT):])
        except ValueError:
            await query.answer("Invalid selection")
            return

        if idx < 0 or idx >= len(sessions):
            await query.answer("Session no longer available", show_alert=True)
            return

        session = sessions[idx]
        text, keyboard = build_confirm_keyboard(session, idx)
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer()
        return

    if data.startswith(CB_RESUME_CONFIRM):
        try:
            idx = int(data[len(CB_RESUME_CONFIRM):])
        except ValueError:
            await query.answer("Invalid selection")
            return

        if idx < 0 or idx >= len(sessions):
            await query.answer("Session no longer available", show_alert=True)
            return

        session = sessions[idx]
        wid = cached_wid

        if not wid:
            await query.answer("No window bound", show_alert=True)
            return

        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            await query.answer("Window no longer exists", show_alert=True)
            return

        # Clean up user_data
        if context.user_data is not None:
            context.user_data.pop(RESUME_SESSIONS_KEY, None)
            context.user_data.pop(RESUME_PAGE_KEY, None)
            context.user_data.pop(RESUME_WINDOW_KEY, None)

        await safe_edit(
            query,
            f"\u23f3 Resuming: **{session.title}**\n\nExiting current session...",
        )

        # Exit the current Claude Code session: send Escape then /exit
        await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)
        await _async_sleep(0.5)
        await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)
        await _async_sleep(0.5)
        # Type /exit to cleanly exit Claude Code
        await tmux_manager.send_keys(w.window_id, "/exit")
        await _async_sleep(2.0)

        # Now launch claude --resume with the selected session ID
        resume_cmd = f"{config.claude_command} --resume {session.session_id}"
        await tmux_manager.send_keys(w.window_id, resume_cmd)

        logger.info(
            "Resumed session %s in window %s (title: %s)",
            session.session_id,
            wid,
            session.title,
        )

        # Wait for the hook to register the new session
        await session_manager.wait_for_session_map_entry(wid, timeout=10.0)

        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        await safe_edit(
            query,
            f"\u2705 Resumed: **{session.title}**\n\n"
            f"Session `{session.session_id[:8]}...` is now active.",
        )
        await query.answer("Resumed")
        return


async def _async_sleep(seconds: float) -> None:
    """Async sleep helper."""
    import asyncio

    await asyncio.sleep(seconds)
