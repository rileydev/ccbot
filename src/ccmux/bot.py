"""Telegram bot handlers for Claude Code session monitoring."""

import asyncio
import io
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import config
from .markdown_v2 import convert_markdown
from .screenshot import text_to_image
from .session import session_manager
from .session_monitor import NewMessage, SessionMonitor
from .telegram_sender import split_message
from .tmux_manager import tmux_manager

logger = logging.getLogger(__name__)

# Session monitor instance
session_monitor: SessionMonitor | None = None

# Status polling task
_status_poll_task: asyncio.Task | None = None
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Rate limiting: last send time per user to avoid Telegram flood control
_last_send_time: dict[int, float] = {}
MESSAGE_SEND_INTERVAL = 1.1  # seconds between messages to same user

# Queue overflow protection
MAX_QUEUE_SIZE = 5  # Drop middle messages if queue exceeds this
QUEUE_CHECK_ENABLED = True  # Feature flag

# Map (tool_use_id, user_id) -> telegram message_id for editing tool_use messages with results
_tool_msg_ids: dict[tuple[str, int], int] = {}

# Status message tracking: user_id -> (message_id, window_name, last_text)
# Note: last_text may be missing in old entries during rolling update
_status_msg_info: dict[int, tuple[int, str] | tuple[int, str, str]] = {}

# Claude Code spinner characters that indicate status line
STATUS_SPINNERS = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])


async def _rate_limit_send(user_id: int) -> None:
    """Wait if necessary to avoid Telegram flood control (max 1 msg/sec per user)."""
    now = time.time()
    if user_id in _last_send_time:
        elapsed = now - _last_send_time[user_id]
        if elapsed < MESSAGE_SEND_INTERVAL:
            wait_time = MESSAGE_SEND_INTERVAL - elapsed
            logger.debug(f"Rate limiting: waiting {wait_time:.2f}s for user {user_id}")
            await asyncio.sleep(wait_time)
    _last_send_time[user_id] = time.time()


# --- Message queue management ---


@dataclass
class MessageTask:
    """Message task for queue processing."""

    task_type: Literal["content", "status_update", "status_clear"]
    text: str | None = None
    window_name: str | None = None
    # content type fields
    parts: list[str] = field(default_factory=list)
    tool_use_id: str | None = None
    content_type: str = "text"
    is_complete: bool = True


# Per-user message queues and worker tasks
_message_queues: dict[int, asyncio.Queue[MessageTask]] = {}
_queue_workers: dict[int, asyncio.Task[None]] = {}
_queue_locks: dict[int, asyncio.Lock] = {}  # Protect drain/refill operations


def _get_or_create_queue(bot: Bot, user_id: int) -> asyncio.Queue[MessageTask]:
    """Get or create message queue and worker for a user."""
    if user_id not in _message_queues:
        _message_queues[user_id] = asyncio.Queue()
        _queue_locks[user_id] = asyncio.Lock()
        # Start worker task for this user
        _queue_workers[user_id] = asyncio.create_task(
            _message_queue_worker(bot, user_id)
        )
    return _message_queues[user_id]


def _inspect_queue(queue: asyncio.Queue[MessageTask]) -> list[MessageTask]:
    """Non-destructively inspect all items in queue.

    Drains the queue and returns all items. Caller must refill.
    """
    items: list[MessageTask] = []
    while not queue.empty():
        try:
            item = queue.get_nowait()
            items.append(item)
        except asyncio.QueueEmpty:
            break
    return items


def _compact_queue(
    items: list[MessageTask],
    max_size: int,
) -> tuple[list[MessageTask], int]:
    """Compact queue by removing middle messages.

    Strategy:
    1. Keep first content message (context)
    2. Keep last N messages (recency)
    3. Drop middle content messages
    4. Deduplicate status (keep only latest per window)

    Returns: (compacted_items, num_dropped)
    """
    if len(items) <= max_size:
        return items, 0

    # Separate by type
    content_msgs: list[tuple[int, MessageTask]] = []
    status_msgs: dict[str, tuple[int, MessageTask]] = {}  # window -> (idx, task)

    for i, task in enumerate(items):
        if task.task_type == "content":
            content_msgs.append((i, task))
        elif task.task_type == "status_update":
            # Keep only latest status per window
            wname = task.window_name or ""
            status_msgs[wname] = (i, task)

    # Build compacted list
    kept: list[tuple[int, MessageTask]] = []

    # Always keep first content (context)
    if content_msgs:
        kept.append(content_msgs[0])
        remaining_content = content_msgs[1:]
    else:
        remaining_content = []

    # Keep last N items from remaining
    remaining_items = remaining_content + list(status_msgs.values())
    remaining_items.sort(key=lambda x: x[0])

    slots_available = max_size - len(kept)
    if slots_available > 0:
        kept.extend(remaining_items[-slots_available:])

    # Sort by original index
    kept.sort(key=lambda x: x[0])

    compacted = [task for _, task in kept]
    num_dropped = len(items) - len(compacted)

    return compacted, num_dropped


async def _check_and_compact_queue(
    bot: Bot,
    user_id: int,
    queue: asyncio.Queue[MessageTask],
) -> None:
    """Check queue size and compact if necessary.

    Called before enqueueing. If queue > MAX_QUEUE_SIZE:
    1. Acquire lock (prevent worker from getting while we drain)
    2. Drain queue
    3. Compact (keep first + last N)
    4. Enqueue warning message at front
    5. Refill queue
    6. Release lock
    """
    if not QUEUE_CHECK_ENABLED:
        return

    current_size = queue.qsize()
    if current_size <= MAX_QUEUE_SIZE:
        return

    logger.warning(
        f"Queue overflow for user {user_id}: "
        f"{current_size} messages (max {MAX_QUEUE_SIZE})"
    )

    # Acquire lock to prevent worker interference during drain/refill
    lock = _queue_locks.get(user_id)
    if not lock:
        logger.error(f"No lock found for user {user_id}, skipping compact")
        return

    async with lock:
        # Drain and compact
        items = _inspect_queue(queue)
        compacted, num_dropped = _compact_queue(items, MAX_QUEUE_SIZE)

        # Refill queue with warning first, then compacted items
        if num_dropped > 0:
            warning_text = (
                f"⚠️ 消息量过多，已丢弃 {num_dropped} 条中间消息\n\n"
                f"（保留了最新的 {len(compacted)} 条消息）"
            )
            warning_task = MessageTask(
                task_type="content",
                text=warning_text,
                window_name=None,
                parts=[convert_markdown(warning_text)],
                is_complete=True,
            )
            # Insert warning at front
            queue.put_nowait(warning_task)

        # Refill compacted items
        for item in compacted:
            queue.put_nowait(item)


async def _message_queue_worker(bot: Bot, user_id: int) -> None:
    """Process message tasks for a user sequentially."""
    queue = _message_queues[user_id]
    logger.info(f"Message queue worker started for user {user_id}")

    while True:
        try:
            task = await queue.get()
            try:
                if task.task_type == "content":
                    await _process_content_task(bot, user_id, task)
                elif task.task_type == "status_update":
                    await _process_status_update_task(bot, user_id, task)
                elif task.task_type == "status_clear":
                    await _do_clear_status_message(bot, user_id)
            except Exception as e:
                logger.error(f"Error processing message task for user {user_id}: {e}")
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            logger.info(f"Message queue worker cancelled for user {user_id}")
            break
        except Exception as e:
            logger.error(f"Unexpected error in queue worker for user {user_id}: {e}")


async def _process_content_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Process a content message task."""
    wname = task.window_name or ""

    # 1. Handle tool_result editing (lookup happens here to ensure sequential order)
    if task.content_type == "tool_result" and task.tool_use_id and task.text:
        _tkey = (task.tool_use_id, user_id)
        edit_msg_id = _tool_msg_ids.pop(_tkey, None)
        if edit_msg_id is not None:
            # Clear status message first (tool_result edits a different message)
            # Don't remove keyboard - _check_and_send_status will send new status with keyboard
            await _do_clear_status_message(bot, user_id)
            text_md = convert_markdown(task.text)
            try:
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=edit_msg_id,
                    text=text_md,
                    parse_mode="MarkdownV2",
                )
                # After content, check and send status
                await _check_and_send_status(bot, user_id, wname)
                return
            except Exception:
                try:
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=edit_msg_id,
                        text=task.text,
                    )
                    await _check_and_send_status(bot, user_id, wname)
                    return
                except Exception:
                    logger.debug(f"Failed to edit tool msg {edit_msg_id}, sending new")
                    # Fall through to send as new message

    # 2. Send content messages, converting status message to first content part
    first_part = True
    last_msg_id: int | None = None
    for part in task.parts:
        sent = None

        # For first part, try to convert status message to content (edit instead of delete)
        if first_part:
            first_part = False
            converted_msg_id = await _convert_status_to_content(bot, user_id, wname, part)
            if converted_msg_id is not None:
                # Status message was edited to show content
                last_msg_id = converted_msg_id
                continue

        await _rate_limit_send(user_id)
        try:
            sent = await bot.send_message(
                chat_id=user_id, text=part, parse_mode="MarkdownV2"
            )
        except Exception:
            try:
                sent = await bot.send_message(chat_id=user_id, text=part)
            except Exception as e:
                logger.error(f"Failed to send message to {user_id}: {e}")

        if sent:
            last_msg_id = sent.message_id

    # Record tool_use message ID for later editing (use last message sent)
    if last_msg_id and task.tool_use_id and task.content_type == "tool_use":
        _tool_msg_ids[(task.tool_use_id, user_id)] = last_msg_id

    # 3. After content, check and send status
    await _check_and_send_status(bot, user_id, wname)


async def _convert_status_to_content(
    bot: Bot, user_id: int, window_name: str, content_text: str
) -> int | None:
    """Convert status message to content message by editing it.

    Returns the message_id if converted successfully, None otherwise.
    """
    info = _status_msg_info.pop(user_id, None)
    if not info:
        return None

    # Handle both old (2-tuple) and new (3-tuple) format
    msg_id = info[0]
    stored_wname = info[1]
    if stored_wname != window_name:
        # Different window, just delete the old status
        try:
            await bot.delete_message(chat_id=user_id, message_id=msg_id)
        except Exception:
            pass
        return None

    # Edit status message to show content
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=msg_id,
            text=content_text,
            parse_mode="MarkdownV2",
        )
        return msg_id
    except Exception:
        try:
            # Fallback to plain text
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=msg_id,
                text=content_text,
            )
            return msg_id
        except Exception as e:
            logger.debug(f"Failed to convert status to content: {e}")
            # Message might be deleted or too old, caller will send new message
            return None


async def _process_status_update_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Process a status update task."""
    wname = task.window_name or ""
    status_text = task.text or ""

    if not status_text:
        # No status text means clear status
        await _do_clear_status_message(bot, user_id)
        return

    # Send typing indicator if Claude is interruptible (working)
    if "esc to interrupt" in status_text.lower():
        try:
            await bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
        except Exception:
            pass

    current_info = _status_msg_info.get(user_id)

    if current_info:
        # Handle both old (2-tuple) and new (3-tuple) format for compatibility
        if len(current_info) == 2:
            msg_id, stored_wname = current_info
            last_text = ""
        else:
            msg_id, stored_wname, last_text = current_info

        if stored_wname != wname:
            # Window changed - delete old and send new
            await _do_clear_status_message(bot, user_id)
            await _do_send_status_message(bot, user_id, wname, status_text)
        elif status_text == last_text:
            # Same content, skip edit
            pass
        else:
            # Same window, text changed - edit in place
            try:
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=msg_id,
                    text=convert_markdown(status_text),
                    parse_mode="MarkdownV2",
                )
                _status_msg_info[user_id] = (msg_id, wname, status_text)
            except Exception:
                try:
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=msg_id,
                        text=status_text,
                    )
                    _status_msg_info[user_id] = (msg_id, wname, status_text)
                except Exception as e:
                    logger.debug(f"Failed to edit status message: {e}")
                    _status_msg_info.pop(user_id, None)
                    await _do_send_status_message(bot, user_id, wname, status_text)
    else:
        # No existing status message, send new
        await _do_send_status_message(bot, user_id, wname, status_text)


async def _do_send_status_message(
    bot: Bot, user_id: int, window_name: str, text: str
) -> None:
    """Send a new status message and track it (internal, called from worker)."""
    await _rate_limit_send(user_id)
    try:
        sent = await bot.send_message(
            chat_id=user_id,
            text=convert_markdown(text),
            parse_mode="MarkdownV2",
        )
        _status_msg_info[user_id] = (sent.message_id, window_name, text)
    except Exception:
        try:
            sent = await bot.send_message(chat_id=user_id, text=text)
            _status_msg_info[user_id] = (sent.message_id, window_name, text)
        except Exception as e:
            logger.error(f"Failed to send status message to {user_id}: {e}")


async def _do_clear_status_message(bot: Bot, user_id: int) -> None:
    """Delete the status message for a user (internal, called from worker)."""
    info = _status_msg_info.pop(user_id, None)
    if info:
        msg_id = info[0]
        try:
            await bot.delete_message(chat_id=user_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Failed to delete status message {msg_id}: {e}")


async def _check_and_send_status(bot: Bot, user_id: int, window_name: str) -> None:
    """Check terminal for status line and send status message if present."""
    w = await tmux_manager.find_window_by_name(window_name)
    if not w:
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        return

    status_line = _parse_status_line(pane_text)
    if status_line:
        await _do_send_status_message(bot, user_id, window_name, status_line)

# Callback data prefixes
CB_HISTORY_PREV = "hp:"  # history page older
CB_HISTORY_NEXT = "hn:"  # history page newer

# Directory browser callback prefixes
CB_DIR_SELECT = "db:sel:"
CB_DIR_UP = "db:up"
CB_DIR_CONFIRM = "db:confirm"
CB_DIR_CANCEL = "db:cancel"
CB_DIR_PAGE = "db:page:"

# Session action callback prefixes
CB_SESSION_HISTORY = "sa:hist:"
CB_SESSION_REFRESH = "sa:ref:"
CB_SESSION_KILL = "sa:kill:"

# Screenshot callback prefix
CB_SCREENSHOT_REFRESH = "ss:ref:"

# Bot's own commands — handled locally, NOT forwarded to Claude Code
BOT_COMMANDS = {"start", "list", "history", "screenshot", "esc"}

# Claude Code commands shown in bot menu (forwarded via tmux)
CC_COMMANDS: dict[str, str] = {
    "clear": "↗ Clear conversation history",
    "compact": "↗ Compact conversation context",
    "cost": "↗ Show token/cost usage",
    "help": "↗ Show Claude Code help",
    "memory": "↗ Edit CLAUDE.md",
}

# List inline callback prefixes
CB_LIST_SELECT = "ls:sel:"
CB_LIST_NEW = "ls:new"

# Directories per page in directory browser
DIRS_PER_PAGE = 6


# User state keys
STATE_KEY = "state"
STATE_BROWSING_DIRECTORY = "browsing_directory"
BROWSE_PATH_KEY = "browse_path"
BROWSE_PAGE_KEY = "browse_page"
BROWSE_DIRS_KEY = "browse_dirs"  # Cache of subdirs for current path


def is_user_allowed(user_id: int | None) -> bool:
    return user_id is not None and config.is_user_allowed(user_id)


def _clear_browse_state(user_data: dict | None) -> None:
    """Clear directory browsing state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(BROWSE_PATH_KEY, None)
        user_data.pop(BROWSE_PAGE_KEY, None)
        user_data.pop(BROWSE_DIRS_KEY, None)


async def _build_session_detail(
    window_name: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build session detail text and action buttons for a window."""
    session = await session_manager.resolve_session_for_window(window_name)
    if session:
        detail_text = (
            f"📤 *Selected: {window_name}*\n\n"
            f"📝 {session.summary}\n"
            f"💬 {session.message_count} messages\n\n"
            f"Send text to forward to Claude."
        )
    else:
        detail_text = f"📤 *Selected: {window_name}*\n\nSend text to forward to Claude."
    # Encode callback data with byte-safe truncation
    action_buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 History", callback_data=f"{CB_SESSION_HISTORY}{window_name}"[:64]),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"{CB_SESSION_REFRESH}{window_name}"[:64]),
        InlineKeyboardButton("❌ Kill", callback_data=f"{CB_SESSION_KILL}{window_name}"[:64]),
    ]])
    return detail_text, action_buttons


async def _safe_reply(message, text: str, **kwargs):  # type: ignore[no-untyped-def]
    """Reply with MarkdownV2, falling back to plain text on failure."""
    try:
        return await message.reply_text(
            convert_markdown(text), parse_mode="MarkdownV2", **kwargs,
        )
    except Exception:
        return await message.reply_text(text, **kwargs)


async def _safe_edit(target, text: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
    """Edit message with MarkdownV2, falling back to plain text on failure."""
    try:
        await target.edit_message_text(
            convert_markdown(text), parse_mode="MarkdownV2", **kwargs,
        )
    except Exception:
        try:
            await target.edit_message_text(text, **kwargs)
        except Exception as e:
            logger.error("Failed to edit message: %s", e)


async def _safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    """Send message with MarkdownV2, falling back to plain text on failure."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=convert_markdown(text),
            parse_mode="MarkdownV2",
            **kwargs,
        )
    except Exception:
        try:
            await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")


# --- Message history ---

def _build_history_keyboard(
    window_name: str,
    page_index: int,
    total_pages: int,
    start_byte: int = 0,
    end_byte: int = 0,
) -> InlineKeyboardMarkup | None:
    """Build inline keyboard for history pagination.

    Callback format: hp:<page>:<window>:<start>:<end> or hn:<page>:<window>:<start>:<end>
    When start=0 and end=0, it means full history (no byte range filter).
    """
    if total_pages <= 1:
        return None

    buttons = []
    if page_index > 0:
        cb_data = f"{CB_HISTORY_PREV}{page_index - 1}:{window_name}:{start_byte}:{end_byte}"
        buttons.append(InlineKeyboardButton(
            "◀ Older",
            callback_data=cb_data[:64],
        ))

    buttons.append(InlineKeyboardButton(f"{page_index + 1}/{total_pages}", callback_data="noop"))

    if page_index < total_pages - 1:
        cb_data = f"{CB_HISTORY_NEXT}{page_index + 1}:{window_name}:{start_byte}:{end_byte}"
        buttons.append(InlineKeyboardButton(
            "Newer ▶",
            callback_data=cb_data[:64],
        ))

    return InlineKeyboardMarkup([buttons])


async def send_history(
    target,
    window_name: str,
    offset: int = -1,
    edit: bool = False,
    *,
    start_byte: int = 0,
    end_byte: int = 0,
    user_id: int | None = None,
    bot: Bot | None = None,
) -> None:
    """Send or edit message history for a window's session.

    Args:
        target: Message object (for reply) or CallbackQuery (for edit).
        window_name: Tmux window name (resolved to session via sent messages).
        offset: Page index (0-based). -1 means last page (for full history)
                or first page (for unread range).
        edit: If True, edit existing message instead of sending new one.
        start_byte: Start byte offset (0 = from beginning).
        end_byte: End byte offset (0 = to end of file).
        user_id: User ID for updating read offset (required for unread mode).
        bot: Bot instance for direct send mode (when edit=False and bot is provided).
    """
    # Determine if this is unread mode (specific byte range)
    is_unread = start_byte > 0 or end_byte > 0

    messages, total = await session_manager.get_recent_messages(
        window_name,
        count=0,
        start_byte=start_byte,
        end_byte=end_byte if end_byte > 0 else None,
    )

    if total == 0:
        if is_unread:
            text = f"📬 [{window_name}] No unread messages."
        else:
            text = f"📋 [{window_name}] No messages yet."
        keyboard = None
    else:
        from .transcript_parser import TranscriptParser
        _start = TranscriptParser.EXPANDABLE_QUOTE_START
        _end = TranscriptParser.EXPANDABLE_QUOTE_END

        # Filter messages based on config
        if config.show_user_messages:
            # Keep both user and assistant messages
            pass
        else:
            # Filter to assistant messages only
            messages = [m for m in messages if m["role"] == "assistant"]
        total = len(messages)
        if total == 0:
            if is_unread:
                text = f"📬 [{window_name}] No unread messages."
            else:
                text = f"📋 [{window_name}] No messages yet."
            keyboard = None
            if edit:
                await _safe_edit(target, text, reply_markup=keyboard)
            elif bot is not None and user_id is not None:
                await _safe_send(bot, user_id, text, reply_markup=keyboard)
            else:
                await _safe_reply(target, text, reply_markup=keyboard)
            # Update offset even if no assistant messages
            if user_id is not None and end_byte > 0:
                session_manager.update_user_window_offset(user_id, window_name, end_byte)
            return

        if is_unread:
            header = f"📬 [{window_name}] {total} unread messages"
        else:
            header = f"📋 [{window_name}] Messages ({total} total)"

        lines = [header]
        for msg in messages:
            # Format timestamp as HH:MM
            ts = msg.get("timestamp")
            if ts:
                try:
                    # ISO format: 2024-01-15T14:32:00.000Z
                    time_part = ts.split("T")[1] if "T" in ts else ts
                    hh_mm = time_part[:5]  # "14:32"
                except (IndexError, TypeError):
                    hh_mm = ""
            else:
                hh_mm = ""

            # Add separator with time
            if hh_mm:
                lines.append(f"───── {hh_mm} ─────")
            else:
                lines.append("─────────────")

            # Format message content
            msg_text = msg["text"]
            content_type = msg.get("content_type", "text")
            msg_role = msg.get("role", "assistant")

            # Strip expandable quote sentinels for history view
            msg_text = msg_text.replace(_start, "").replace(_end, "")

            # Add prefix based on role/type
            if msg_role == "user":
                # User message with emoji prefix (no newline)
                lines.append(f"👤 {msg_text}")
            elif content_type == "thinking":
                # Thinking prefix to match real-time format
                lines.append(f"∴ Thinking…\n{msg_text}")
            else:
                lines.append(msg_text)
        full_text = "\n\n".join(lines)
        pages = split_message(full_text, max_length=4096)

        # Default to last page (newest messages) for both history and unread
        if offset < 0:
            offset = len(pages) - 1
        page_index = max(0, min(offset, len(pages) - 1))
        text = pages[page_index]
        keyboard = _build_history_keyboard(
            window_name, page_index, len(pages), start_byte, end_byte
        )

    if edit:
        await _safe_edit(target, text, reply_markup=keyboard)
    elif bot is not None and user_id is not None:
        # Direct send mode (for unread catch-up after window switch)
        await _safe_send(bot, user_id, text, reply_markup=keyboard)
    else:
        await _safe_reply(target, text, reply_markup=keyboard)

    # Update user's read offset after viewing unread
    if is_unread and user_id is not None and end_byte > 0:
        session_manager.update_user_window_offset(user_id, window_name, end_byte)


# --- Directory browser ---

def build_directory_browser(current_path: str, page: int = 0) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build directory browser UI.

    Returns: (text, keyboard, subdirs) where subdirs is the full list for caching.
    """
    path = Path(current_path).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        path = Path.cwd()

    try:
        subdirs = sorted([
            d.name for d in path.iterdir()
            if d.is_dir() and not d.name.startswith('.')
        ])
    except (PermissionError, OSError):
        subdirs = []

    total_pages = max(1, (len(subdirs) + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DIRS_PER_PAGE
    page_dirs = subdirs[start:start + DIRS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(page_dirs), 2):
        row = []
        for j, name in enumerate(page_dirs[i:i+2]):
            display = name[:12] + "…" if len(name) > 13 else name
            # Use global index (start + i + j) to avoid long dir names in callback_data
            idx = start + i + j
            row.append(InlineKeyboardButton(f"📁 {display}", callback_data=f"{CB_DIR_SELECT}{idx}"))
        buttons.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"{CB_DIR_PAGE}{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"{CB_DIR_PAGE}{page+1}"))
        buttons.append(nav)

    action_row: list[InlineKeyboardButton] = []
    # Allow going up unless at filesystem root
    if path != path.parent:
        action_row.append(InlineKeyboardButton("..", callback_data=CB_DIR_UP))
    action_row.append(InlineKeyboardButton("Select", callback_data=CB_DIR_CONFIRM))
    action_row.append(InlineKeyboardButton("Cancel", callback_data=CB_DIR_CANCEL))
    buttons.append(action_row)

    display_path = str(path).replace(str(Path.home()), "~")
    if not subdirs:
        text = f"*Select Working Directory*\n\nCurrent: `{display_path}`\n\n_(No subdirectories)_"
    else:
        text = f"*Select Working Directory*\n\nCurrent: `{display_path}`\n\nTap a folder to enter, or select current directory"

    return text, InlineKeyboardMarkup(buttons), subdirs


# --- Command / message handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await _safe_reply(update.message, "You are not authorized to use this bot.")
        return

    _clear_browse_state(context.user_data)

    if update.message:
        # Remove any existing reply keyboard
        await _safe_reply(
            update.message,
            "🤖 *Claude Code Monitor*\n\n"
            "Use /list to see sessions.\n"
            "Send text to forward to the active session.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await _safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.text:
        return

    text = update.message.text

    # Ignore text in directory browsing mode
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_BROWSING_DIRECTORY:
        await _safe_reply(
            update.message,
            "Please use the directory browser above, or tap Cancel.",
        )
        return

    # Forward text to active window
    active_wname = session_manager.get_active_window_name(user.id)
    if active_wname:
        w = await tmux_manager.find_window_by_name(active_wname)
        if not w:
            await _safe_reply(
                update.message,
                f"❌ Window '{active_wname}' no longer exists.\n"
                "Select a different session or create a new one.",
            )
            return

        # Show typing indicator while waiting for Claude's response
        await update.message.chat.send_action(ChatAction.TYPING)

        # Clear status message tracking so next status update sends a new message
        # (otherwise it would edit the old status message above user's message)
        _status_msg_info.pop(user.id, None)

        success, message = await session_manager.send_to_active_session(user.id, text)
        if not success:
            await _safe_reply(update.message, f"❌ {message}")
        return

    await _safe_reply(
        update.message,
        "❌ No active session selected.\n"
        "Use /list to select a session or create a new one.",
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        await query.answer("Not authorized")
        return

    data = query.data

    # History: older/newer pagination
    # Format: hp:<page>:<window>:<start>:<end> or hn:<page>:<window>:<start>:<end>
    if data.startswith(CB_HISTORY_PREV) or data.startswith(CB_HISTORY_NEXT):
        prefix_len = len(CB_HISTORY_PREV)  # same length for both
        rest = data[prefix_len:]
        try:
            parts = rest.split(":")
            if len(parts) < 4:
                # Old format without byte range: page:window
                offset_str, window_name = rest.split(":", 1)
                start_byte, end_byte = 0, 0
            else:
                # New format: page:window:start:end (window may contain colons)
                offset_str = parts[0]
                start_byte = int(parts[-2])
                end_byte = int(parts[-1])
                window_name = ":".join(parts[1:-2])
            offset = int(offset_str)
        except (ValueError, IndexError):
            await query.answer("Invalid data")
            return

        w = await tmux_manager.find_window_by_name(window_name)
        if w:
            await send_history(
                query,
                window_name,
                offset=offset,
                edit=True,
                start_byte=start_byte,
                end_byte=end_byte,
                # Don't pass user_id for pagination - offset update only on initial view
                # This prevents offset from going backwards if new messages arrive while paging
            )
        else:
            await _safe_edit(query, "Window no longer exists.")
        await query.answer("Page updated")

    # Directory browser handlers
    elif data.startswith(CB_DIR_SELECT):
        # callback_data contains index, not dir name (to avoid 64-byte limit)
        try:
            idx = int(data[len(CB_DIR_SELECT):])
        except ValueError:
            await query.answer("Invalid data")
            return

        # Look up dir name from cached subdirs
        cached_dirs: list[str] = context.user_data.get(BROWSE_DIRS_KEY, []) if context.user_data else []
        if idx < 0 or idx >= len(cached_dirs):
            await query.answer("Directory list changed, please refresh", show_alert=True)
            return
        subdir_name = cached_dirs[idx]

        default_path = str(Path.cwd())
        current_path = context.user_data.get(BROWSE_PATH_KEY, default_path) if context.user_data else default_path
        new_path = (Path(current_path) / subdir_name).resolve()

        if not new_path.exists() or not new_path.is_dir():
            await query.answer("Directory not found", show_alert=True)
            return

        new_path_str = str(new_path)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(new_path_str)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await _safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_UP:
        default_path = str(Path.cwd())
        current_path = context.user_data.get(BROWSE_PATH_KEY, default_path) if context.user_data else default_path
        current = Path(current_path).resolve()
        parent = current.parent
        # No restriction - allow navigating anywhere

        parent_path = str(parent)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(parent_path)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await _safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data.startswith(CB_DIR_PAGE):
        try:
            pg = int(data[len(CB_DIR_PAGE):])
        except ValueError:
            await query.answer("Invalid data")
            return
        default_path = str(Path.cwd())
        current_path = context.user_data.get(BROWSE_PATH_KEY, default_path) if context.user_data else default_path
        if context.user_data is not None:
            context.user_data[BROWSE_PAGE_KEY] = pg

        msg_text, keyboard, subdirs = build_directory_browser(current_path, pg)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await _safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_CONFIRM:
        default_path = str(Path.cwd())
        selected_path = context.user_data.get(BROWSE_PATH_KEY, default_path) if context.user_data else default_path

        _clear_browse_state(context.user_data)

        success, message, created_wname = await tmux_manager.create_window(selected_path)
        if success:
            session_manager.set_active_window(user.id, created_wname)

            # Update the directory browser message to show refreshed session list
            active_items = await session_manager.list_active_sessions()
            list_text = f"📊 {len(active_items)} active sessions:"
            keyboard = await _build_list_keyboard(user.id)
            await _safe_edit(query, list_text, reply_markup=keyboard)

            # Send creation success as a new message
            await _safe_send(
                context.bot, user.id,
                f"✅ {message}\n\n_You can now send messages directly to this window._",
            )
        else:
            await _safe_edit(query, f"❌ {message}")
        await query.answer("Created" if success else "Failed")

    elif data == CB_DIR_CANCEL:
        _clear_browse_state(context.user_data)
        await _safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Session action: History
    elif data.startswith(CB_SESSION_HISTORY):
        window_name = data[len(CB_SESSION_HISTORY):]
        w = await tmux_manager.find_window_by_name(window_name)
        if w:
            await send_history(query.message, window_name)
        else:
            await _safe_edit(query, "Window no longer exists.")
        await query.answer("Loading history")

    # Session action: Refresh
    elif data.startswith(CB_SESSION_REFRESH):
        window_name = data[len(CB_SESSION_REFRESH):]
        detail_text, action_buttons = await _build_session_detail(window_name)
        await _safe_edit(query, detail_text, reply_markup=action_buttons)
        await query.answer("Refreshed")

    # Session action: Kill
    elif data.startswith(CB_SESSION_KILL):
        window_name = data[len(CB_SESSION_KILL):]
        w = await tmux_manager.find_window_by_name(window_name)
        if w:
            await tmux_manager.kill_window(w.window_id)
            # Clear active session if it was this one
            if user:
                active_wname = session_manager.get_active_window_name(user.id)
                if active_wname == window_name:
                    session_manager.set_active_window(user.id, "")
            await _safe_edit(query, "🗑 Session killed.")
        else:
            await _safe_edit(query, "Window already gone.")
        await query.answer("Killed")

    # Screenshot: Refresh
    elif data.startswith(CB_SCREENSHOT_REFRESH):
        window_name = data[len(CB_SCREENSHOT_REFRESH):]
        w = await tmux_manager.find_window_by_name(window_name)
        if not w:
            await query.answer("Window no longer exists", show_alert=True)
            return

        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if not text:
            await query.answer("Failed to capture pane", show_alert=True)
            return

        png_bytes = await text_to_image(text, with_ansi=True)
        refresh_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data=f"{CB_SCREENSHOT_REFRESH}{window_name}"[:64]),
        ]])
        try:
            await query.edit_message_media(
                media=InputMediaDocument(media=io.BytesIO(png_bytes), filename="screenshot.png"),
                reply_markup=refresh_keyboard,
            )
            await query.answer("Refreshed")
        except Exception as e:
            logger.error(f"Failed to refresh screenshot: {e}")
            await query.answer("Failed to refresh", show_alert=True)

    # List: select session
    elif data.startswith(CB_LIST_SELECT):
        wname = data[len(CB_LIST_SELECT):]
        w = await tmux_manager.find_window_by_name(wname) if wname else None
        if w:
            # Step 1: Clear active window to prevent message interleaving
            # During unread catch-up, we don't want new messages from either
            # old or new window to be sent (they would interleave with unread)
            session_manager.clear_active_session(user.id)

            # Step 2: Send UI feedback
            # Re-render list with checkmark on new window
            active_items = await session_manager.list_active_sessions()
            text = f"📊 {len(active_items)} active sessions:"
            keyboard = await _build_list_keyboard(user.id, pending_selection=w.window_name)
            await _safe_edit(query, text, reply_markup=keyboard)

            # Send session detail message
            detail_text, action_buttons = await _build_session_detail(w.window_name)
            await _safe_send(
                context.bot, user.id, detail_text,
                reply_markup=action_buttons,
            )

            # Step 3: Send unread catch-up (if any)
            unread_info = await session_manager.get_unread_info(user.id, w.window_name)
            if unread_info:
                if unread_info.has_unread:
                    # User has unread messages, send catch-up via send_history
                    await send_history(
                        None,  # target not used in direct send mode
                        w.window_name,
                        start_byte=unread_info.start_offset,
                        end_byte=unread_info.end_offset,
                        user_id=user.id,
                        bot=context.bot,
                    )
                else:
                    # First time or no unread - initialize offset to current file size
                    session_manager.update_user_window_offset(
                        user.id, w.window_name, unread_info.end_offset
                    )

            # Step 4: Now set active window (enables new message delivery)
            session_manager.set_active_window(user.id, w.window_name)

            await query.answer(f"Active: {w.window_name}")
        else:
            await query.answer("Window no longer exists", show_alert=True)

    # List: new session
    elif data == CB_LIST_NEW:
        # Start from current active window's cwd, fallback to browse_root_dir
        start_path = str(Path.cwd())
        active_wname = session_manager.get_active_window_name(user.id)
        if active_wname:
            w = await tmux_manager.find_window_by_name(active_wname)
            if w and w.cwd:
                start_path = w.cwd

        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await _safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == "noop":
        await query.answer()


# --- Status line polling ---


def _parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code status line from terminal output.

    Returns the status text if found, None otherwise.
    Status lines start with spinner characters: · ✻ ✽ ✶ ✳ ✢
    The spinner character is trimmed from the returned text.
    """
    if not pane_text:
        return None

    # Search from bottom up - status line can be anywhere in last ~15 lines
    # (there may be separator lines, prompts, etc. below it)
    lines = pane_text.strip().split("\n")
    for line in reversed(lines[-15:]):
        line = line.strip()
        if not line:
            continue
        # Check if line starts with a spinner character
        first_char = line[0] if line else ""
        if first_char in STATUS_SPINNERS:
            # Remove the spinner character and return the rest
            return line[1:].strip()
    return None


async def _enqueue_status_update(bot: Bot, user_id: int, window_name: str, status_text: str | None) -> None:
    """Enqueue status update, removing old status for same window.

    Status messages are ephemeral - only the latest matters.
    Remove all pending status_update tasks for the same window.

    Optimized: drain/refill only once, combining deduplication and compaction.
    """
    queue = _get_or_create_queue(bot, user_id)
    lock = _queue_locks.get(user_id)
    if not lock:
        logger.error(f"No lock found for user {user_id}, skipping enqueue")
        return

    async with lock:
        # Drain queue once
        items = _inspect_queue(queue)

        # 1. Remove old status messages for the same window (deduplication)
        filtered_items: list[MessageTask] = []
        removed_count = 0

        for task in items:
            # Remove old status_update for SAME window
            if task.task_type == "status_update" and task.window_name == window_name:
                removed_count += 1
                continue
            filtered_items.append(task)

        if removed_count > 0:
            logger.debug(f"Deduped {removed_count} stale status for user {user_id}")

        # 2. Check if compaction is needed after deduplication
        if len(filtered_items) > MAX_QUEUE_SIZE and QUEUE_CHECK_ENABLED:
            logger.warning(
                f"Queue overflow for user {user_id}: "
                f"{len(filtered_items)} messages after dedup (max {MAX_QUEUE_SIZE})"
            )
            compacted, num_dropped = _compact_queue(filtered_items, MAX_QUEUE_SIZE)

            if num_dropped > 0:
                # Insert warning message
                warning_text = (
                    f"⚠️ 消息量过多，已丢弃 {num_dropped} 条中间消息\n\n"
                    f"（保留了最新的 {len(compacted)} 条消息）"
                )
                warning_task = MessageTask(
                    task_type="content",
                    text=warning_text,
                    window_name=None,
                    parts=[convert_markdown(warning_text)],
                    is_complete=True,
                )
                queue.put_nowait(warning_task)

            filtered_items = compacted

        # 3. Refill queue with filtered/compacted items
        for item in filtered_items:
            queue.put_nowait(item)

        # 4. Enqueue new status message
        if status_text:
            task = MessageTask(
                task_type="status_update",
                text=status_text,
                window_name=window_name,
            )
        else:
            task = MessageTask(task_type="status_clear")

        queue.put_nowait(task)


async def _update_status_message(bot: Bot, user_id: int, window_name: str) -> None:
    """Poll terminal and enqueue status update for user's active window."""
    w = await tmux_manager.find_window_by_name(window_name)
    if not w:
        # Window gone, enqueue clear
        await _enqueue_status_update(bot, user_id, window_name, None)
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        # Transient capture failure - keep existing status message
        return

    status_line = _parse_status_line(pane_text)

    if status_line:
        await _enqueue_status_update(bot, user_id, window_name, status_line)
    # If no status line, keep existing status message (don't clear on transient state)


async def _enqueue_content_message(
    bot: Bot,
    user_id: int,
    window_name: str,
    parts: list[str],
    tool_use_id: str | None = None,
    content_type: str = "text",
    is_complete: bool = True,
    text: str | None = None,
) -> None:
    """Enqueue a content message task."""
    queue = _get_or_create_queue(bot, user_id)

    # Check queue size and compact if needed (async call with lock)
    await _check_and_compact_queue(bot, user_id, queue)

    task = MessageTask(
        task_type="content",
        text=text,
        window_name=window_name,
        parts=parts,
        tool_use_id=tool_use_id,
        content_type=content_type,
        is_complete=is_complete,
    )
    queue.put_nowait(task)


# --- Streaming response / notifications ---


def _build_response_parts(
    window_name: str, text: str, is_complete: bool,
    content_type: str = "text",
    role: str = "assistant",
) -> list[str]:
    """Build paginated response messages for Telegram.

    Returns a list of message strings, each within Telegram's 4096 char limit.
    Multi-part messages get a [1/N] suffix.
    """
    text = text.strip()

    # User messages: add emoji prefix (no newline)
    if role == "user":
        prefix = "👤 "
        separator = ""
        # User messages are typically short, no special processing needed
        if len(text) > 3000:
            text = text[:3000] + "…"
        return [convert_markdown(f"{prefix}{text}")]

    # Truncate thinking content to keep it compact
    if content_type == "thinking" and is_complete:
        from .transcript_parser import TranscriptParser
        start_tag = TranscriptParser.EXPANDABLE_QUOTE_START
        end_tag = TranscriptParser.EXPANDABLE_QUOTE_END
        max_thinking = 500
        if start_tag in text and end_tag in text:
            inner = text[text.index(start_tag) + len(start_tag):text.index(end_tag)]
            if len(inner) > max_thinking:
                inner = inner[:max_thinking] + "\n\n… (thinking truncated)"
            text = start_tag + inner + end_tag
        elif len(text) > max_thinking:
            text = text[:max_thinking] + "\n\n… (thinking truncated)"

    # Format based on content type
    if content_type == "thinking":
        # Thinking: prefix with "∴ Thinking…" and single newline
        prefix = "∴ Thinking…"
        separator = "\n"
    else:
        # Plain text: no prefix
        prefix = ""
        separator = ""

    # If text contains expandable quote sentinels, don't split —
    # the quote must stay atomic. Truncation is handled by
    # _render_expandable_quote in markdown_v2.py.
    from .transcript_parser import TranscriptParser
    if TranscriptParser.EXPANDABLE_QUOTE_START in text:
        if prefix:
            return [convert_markdown(f"{prefix}{separator}{text}")]
        else:
            return [convert_markdown(text)]

    # Split markdown first, then convert each chunk to HTML.
    # Use conservative max to leave room for HTML tags added by conversion.
    max_text = 3000 - len(prefix) - len(separator)

    text_chunks = split_message(text, max_length=max_text)
    total = len(text_chunks)

    if total == 1:
        if prefix:
            return [convert_markdown(f"{prefix}{separator}{text_chunks[0]}")]
        else:
            return [convert_markdown(text_chunks[0])]

    parts = []
    for i, chunk in enumerate(text_chunks, 1):
        if prefix:
            parts.append(convert_markdown(f"{prefix}{separator}{chunk}\n\n[{i}/{total}]"))
        else:
            parts.append(convert_markdown(f"{chunk}\n\n[{i}/{total}]"))
    return parts


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    """
    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    # Find users whose active window matches this session
    active_users: list[tuple[int, str]] = []  # (user_id, window_name)
    for uid, wname in session_manager.active_sessions.items():
        resolved = await session_manager.resolve_session_for_window(wname)
        if resolved and resolved.session_id == msg.session_id:
            active_users.append((uid, wname))

    if not active_users:
        logger.info(
            f"No active users for session {msg.session_id}. "
            f"Active sessions: {dict(session_manager.active_sessions)}"
        )
        # Log what each active user resolves to, for debugging
        for uid, wname in session_manager.active_sessions.items():
            resolved = await session_manager.resolve_session_for_window(wname)
            resolved_id = resolved.session_id if resolved else None
            logger.info(
                f"  user={uid}, window={wname} -> resolved_session={resolved_id}"
            )
        return

    for user_id, wname in active_users:
        parts = _build_response_parts(
            wname, msg.text, msg.is_complete, msg.content_type, msg.role,
        )

        if msg.is_complete:
            # Enqueue content message task
            # Note: tool_result editing is handled inside _process_content_task
            # to ensure sequential processing with tool_use message sending
            await _enqueue_content_message(
                bot=bot,
                user_id=user_id,
                window_name=wname,
                parts=parts,
                tool_use_id=msg.tool_use_id,
                content_type=msg.content_type,
                is_complete=True,
                text=msg.text,
            )

            # Update user's read offset to current file position
            # This marks these messages as "read" for this user
            session = await session_manager.resolve_session_for_window(wname)
            if session and session.file_path:
                try:
                    file_size = Path(session.file_path).stat().st_size
                    session_manager.update_user_window_offset(user_id, wname, file_size)
                except OSError:
                    pass


# --- App lifecycle ---


async def _status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all active users."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    while True:
        try:
            # Get all users with active sessions
            for user_id, wname in list(session_manager.active_sessions.items()):
                try:
                    await _update_status_message(bot, user_id, wname)
                except Exception as e:
                    logger.debug(f"Status update error for user {user_id}: {e}")
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)


async def post_init(application: Application) -> None:
    global session_monitor, _status_poll_task

    await application.bot.delete_my_commands()

    bot_commands = [
        BotCommand("start", "Show session menu"),
        BotCommand("list", "List all sessions"),
        BotCommand("history", "Message history for active session"),
        BotCommand("screenshot", "Capture terminal screenshot"),
        BotCommand("esc", "Send Escape to interrupt Claude"),
    ]
    # Add Claude Code slash commands
    for cmd_name, desc in CC_COMMANDS.items():
        bot_commands.append(BotCommand(cmd_name, desc))

    await application.bot.set_my_commands(bot_commands)

    monitor = SessionMonitor()

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, application.bot)

    monitor.set_message_callback(message_callback)
    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")

    # Start status polling task
    _status_poll_task = asyncio.create_task(_status_poll_loop(application.bot))
    logger.info("Status polling task started")


async def post_shutdown(application: Application) -> None:
    global session_monitor, _status_poll_task

    # Stop status polling
    if _status_poll_task:
        _status_poll_task.cancel()
        try:
            await _status_poll_task
        except asyncio.CancelledError:
            pass
        _status_poll_task = None
        logger.info("Status polling stopped")

    # Stop all queue workers
    for user_id, worker in list(_queue_workers.items()):
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    _queue_workers.clear()
    _message_queues.clear()
    _queue_locks.clear()
    logger.info("Message queue workers stopped")

    if session_monitor:
        session_monitor.stop()
        logger.info("Session monitor stopped")


async def forward_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward any non-bot command as a slash command to the active Claude Code session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    cmd_text = update.message.text or ""
    # The full text is already a slash command like "/clear" or "/compact foo"
    cc_slash = cmd_text.split("@")[0]  # strip bot mention

    active_wname = session_manager.get_active_window_name(user.id)
    if not active_wname:
        await _safe_reply(update.message, "❌ No active session. Select a session first.")
        return

    w = await tmux_manager.find_window_by_name(active_wname)
    if not w:
        await _safe_reply(update.message, f"❌ Window '{active_wname}' no longer exists.")
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    success, message = await session_manager.send_to_active_session(user.id, cc_slash)
    if success:
        await _safe_reply(update.message, f"⚡ [{active_wname}] Sent: {cc_slash}")
        # If /clear command was sent, clear the session association
        # so we can detect the new session after first message
        if cc_slash.strip().lower() == "/clear":
            session_manager.clear_window_session(active_wname)
    else:
        await _safe_reply(update.message, f"❌ {message}")


async def _build_list_keyboard(
    user_id: int,
    pending_selection: str | None = None,
) -> InlineKeyboardMarkup:
    """Build inline keyboard with session buttons for /list.

    Args:
        user_id: User ID to check active window for.
        pending_selection: Override active window name for display (used during
            window switch to show checkmark before active_sessions is updated).
    """
    active_items = await session_manager.list_active_sessions()
    active_wname = pending_selection or session_manager.get_active_window_name(user_id)

    buttons: list[list[InlineKeyboardButton]] = []
    for w, session in active_items:
        is_active = active_wname == w.window_name
        check = "✅ " if is_active else ""
        summary = session.short_summary if session else "New session"
        label = f"{check}[{w.window_name}] {summary}"
        if len(label) > 40:
            label = label[:37] + "..."
        buttons.append([InlineKeyboardButton(label, callback_data=f"{CB_LIST_SELECT}{w.window_name}"[:64])])

    buttons.append([InlineKeyboardButton("➕ New Session", callback_data=CB_LIST_NEW)])
    return InlineKeyboardMarkup(buttons)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all active sessions as inline buttons."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    active_items = await session_manager.list_active_sessions()
    text = f"📊 {len(active_items)} active sessions:" if active_items else "No active sessions."
    keyboard = await _build_list_keyboard(user.id)

    await _safe_reply(update.message, text, reply_markup=keyboard)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the active session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    active_wname = session_manager.get_active_window_name(user.id)
    if not active_wname:
        await _safe_reply(update.message, "❌ No active session. Select one first.")
        return

    await send_history(update.message, active_wname)


async def screenshot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture the current tmux pane and send it as an image."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    active_wname = session_manager.get_active_window_name(user.id)
    if not active_wname:
        await _safe_reply(update.message, "❌ No active session. Select one first.")
        return

    w = await tmux_manager.find_window_by_name(active_wname)
    if not w:
        await _safe_reply(update.message, f"❌ Window '{active_wname}' no longer exists.")
        return

    text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
    if not text:
        await _safe_reply(update.message, "❌ Failed to capture pane content.")
        return

    png_bytes = await text_to_image(text, with_ansi=True)
    refresh_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data=f"{CB_SCREENSHOT_REFRESH}{active_wname}"[:64]),
    ]])
    await update.message.reply_document(
        document=io.BytesIO(png_bytes),
        filename="screenshot.png",
        reply_markup=refresh_keyboard,
    )


async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Escape key to interrupt Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    active_wname = session_manager.get_active_window_name(user.id)
    if not active_wname:
        await _safe_reply(update.message, "❌ No active session. Select one first.")
        return

    w = await tmux_manager.find_window_by_name(active_wname)
    if not w:
        await _safe_reply(update.message, f"❌ Window '{active_wname}' no longer exists.")
        return

    # Send Escape control character (no enter)
    await tmux_manager.send_keys(w.window_id, "\x1b", enter=False)
    await _safe_reply(update.message, "⎋ Sent Escape")



def create_bot() -> Application:
    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("esc", esc_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    # Forward any other /command to Claude Code
    application.add_handler(MessageHandler(filters.COMMAND, forward_command_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    return application
