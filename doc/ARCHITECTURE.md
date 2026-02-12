# CCBot Architecture Guide

This document provides a detailed technical reference for every module, data structure, and data flow in CCBot. It is written for AI agents and developers who need to navigate, understand, and modify the codebase.

## Table of Contents

- [System Overview](#system-overview)
- [Entry Points](#entry-points)
- [Startup & Shutdown Sequence](#startup--shutdown-sequence)
- [Core Modules](#core-modules)
  - [config.py — Configuration](#configpy--configuration)
  - [bot.py — Telegram Bot Handlers](#botpy--telegram-bot-handlers)
  - [session.py — State Management](#sessionpy--state-management)
  - [session_monitor.py — JSONL Polling](#session_monitorpy--jsonl-polling)
  - [tmux_manager.py — Tmux Integration](#tmux_managerpy--tmux-integration)
  - [hook.py — SessionStart Hook](#hookpy--sessionstart-hook)
  - [sync_skills.py — Skill Sync CLI](#sync_skillspy--skill-sync-cli)
  - [transcript_parser.py — JSONL Parsing](#transcript_parserpy--jsonl-parsing)
  - [terminal_parser.py — Pane Parsing](#terminal_parserpy--pane-parsing)
  - [monitor_state.py — Byte Offset Persistence](#monitor_statepy--byte-offset-persistence)
  - [utils.py — Shared Utilities](#utilspy--shared-utilities)
  - [markdown_v2.py — Markdown Conversion](#markdown_v2py--markdown-conversion)
  - [telegram_sender.py — Message Splitting](#telegram_senderpy--message-splitting)
  - [screenshot.py — Terminal Screenshots](#screenshotpy--terminal-screenshots)
- [Handler Modules](#handler-modules)
  - [callback_data.py — Callback Constants](#callback_datapy--callback-constants)
  - [message_queue.py — Per-User Message Queue](#message_queuepy--per-user-message-queue)
  - [message_sender.py — Safe Send Helpers](#message_senderpy--safe-send-helpers)
  - [response_builder.py — Response Formatting](#response_builderpy--response-formatting)
  - [directory_browser.py — Directory & Window Picker](#directory_browserpy--directory--window-picker)
  - [history.py — Message History Pagination](#historypy--message-history-pagination)
  - [interactive_ui.py — Interactive UI Handling](#interactive_uipy--interactive-ui-handling)
  - [status_polling.py — Status Line Polling](#status_pollingpy--status-line-polling)
  - [resume.py — Session Resume Picker](#resumepy--session-resume-picker)
  - [cleanup.py — Topic Cleanup](#cleanuppy--topic-cleanup)
- [Data Structures](#data-structures)
- [State Files](#state-files)
- [Data Flow Diagrams](#data-flow-diagrams)
- [Callback Routing Map](#callback-routing-map)
- [Configuration Reference](#configuration-reference)
- [Design Constraints](#design-constraints)

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Telegram Bot (bot.py)                           │
│  Commands: /start, /history, /resume, /screenshot, /esc                │
│  Callbacks: directory browser, window picker, history, interactive UI  │
│  Text: forward to Claude via tmux | Skill translation for /commands   │
│  Per-user message queue + worker (merge, rate limit, FIFO)            │
├─────────────────────────────┬───────────────────────────────────────────┤
│  markdown_v2.py             │  telegram_sender.py                       │
│  MD → MarkdownV2            │  split_message (4096 limit)               │
├─────────────────────────────┴───────────────────────────────────────────┤
│  terminal_parser.py                                                     │
│  Detect interactive UIs + parse status line from pane capture           │
└──────────┬──────────────────────────────────────────────────────────────┘
           │ Notify (NewMessage)            │ Send (tmux keys)
┌──────────┴──────────────┐    ┌────────────┴─────────────────────────────┐
│  SessionMonitor          │    │  TmuxManager (tmux_manager.py)           │
│  (session_monitor.py)    │    │  list/find/create/kill windows           │
│  Poll JSONL every 2s     │    │  send_keys, capture_pane                 │
│  Byte-offset incremental │    └────────────┬────────────────────────────┘
│  mtime cache             │                 │
└──────────┬───────────────┘                 ▼
           │                      ┌──────────────────────────────┐
           ▼                      │  Tmux Windows (claude procs) │
┌──────────────────────┐         │  SessionStart hook → hook.py │
│  TranscriptParser    │         │  Writes session_map.json     │
│  (transcript_parser) │         └──────────────────────────────┘
│  Parse JSONL content │
│  Pair tool_use ↔     │         ┌──────────────────────────────┐
│    tool_result       │         │  SessionManager (session.py) │
└──────────────────────┘         │  Window↔Session resolution   │
                                 │  Thread bindings + offsets   │
┌──────────────────────┐         │  Reads session_map.json      │
│  MonitorState         │         │  Reads JSONL for history     │
│  (monitor_state.py)   │         └──────────────────────────────┘
│  Track byte offsets   │
│  Prevent duplicates   │         ┌──────────────────────────────┐
└──────────────────────┘         │  Config (config.py)          │
                                 │  Env vars + notify.json      │
                                 │  Singleton: config            │
                                 └──────────────────────────────┘
```

---

## Entry Points

| CLI Command | Entry Point | Description |
|---|---|---|
| `ccbot` | `ccbot.main:main()` | Start the Telegram bot polling loop |
| `ccbot hook` | `ccbot.hook:hook_main()` | Handle SessionStart hook from Claude Code |
| `ccbot hook --install` | `ccbot.hook:hook_main()` | Auto-install hook to `~/.claude/settings.json` |
| `ccbot-sync [dir]` | `ccbot.sync_skills:main()` | Generate `skills.json` from `.claude/commands/` |

Defined in `pyproject.toml` under `[project.scripts]`.

---

## Startup & Shutdown Sequence

### Startup

1. `main()` checks for `"hook"` argument — delegates to `hook_main()` if present
2. Loads `config` singleton (reads env + `.env` files, creates `NotifyConfig`)
3. Creates/ensures tmux session via `tmux_manager.get_or_create_session()`
4. Calls `create_bot()`:
   - Builds `Application` with token from config
   - Registers handlers in order: `/start`, `/history`, `/resume`, `/screenshot`, `/esc`, `CallbackQueryHandler`, topic closed, `/command` forwarding, text, unsupported content
   - Sets `post_init` and `post_shutdown` callbacks
5. `application.run_polling()` starts
6. `post_init()` runs:
   - Deletes old bot commands, registers new ones (bot + CC + skill commands)
   - Calls `session_manager.resolve_stale_ids()` to re-map any stale window IDs
   - Creates and starts `SessionMonitor` with `handle_new_message` callback
   - Creates status polling background task

### Shutdown

1. `post_shutdown()` runs:
   - Cancels status polling task
   - Calls `shutdown_workers()` to drain all per-user message queues
   - Stops `SessionMonitor`

---

## Core Modules

### config.py — Configuration

**Purpose**: Load environment variables and notification preferences into a singleton.

**Singleton**: `config = Config()` (module-level, imported everywhere)

**Classes**:

```python
class NotifyConfig:
    """Per-content-type notification toggle from ~/.ccbot/notify.json."""
    _file: Path                          # Path to notify.json
    _settings: dict[str, bool]           # Content type -> enabled

    def should_notify(content_type: str, *, is_error: bool = False) -> bool
    def summary() -> str                 # "on=[...], off=[...]"

class Config:
    config_dir: Path                     # ~/.ccbot or $CCBOT_DIR
    telegram_bot_token: str              # Required
    allowed_users: set[int]              # Required, comma-separated IDs
    tmux_session_name: str               # Default: "ccbot"
    tmux_main_window_name: str           # Always "__main__"
    claude_command: str                   # Default: "claude"
    state_file: Path                     # config_dir / "state.json"
    session_map_file: Path               # config_dir / "session_map.json"
    monitor_state_file: Path             # config_dir / "monitor_state.json"
    claude_projects_path: Path           # ~/.claude/projects
    monitor_poll_interval: float         # Default: 2.0
    show_user_messages: bool             # Always True
    notify: NotifyConfig                 # Notification config

    def is_user_allowed(user_id: int) -> bool
```

**Notify defaults** (`NOTIFY_DEFAULTS`): All content types default to `True`. The JSON file is auto-created on first run.

**`.env` loading priority**: local `.env` (cwd) > `$CCBOT_DIR/.env`. First loaded wins (python-dotenv `override=False`).

---

### bot.py — Telegram Bot Handlers

**Purpose**: The main UI layer — registers all Telegram handlers, routes messages, manages bot lifecycle.

**Key data** (module-level):

```python
CC_COMMANDS: dict[str, str]                    # Built-in Claude Code commands
_DEFAULT_SKILL_COMMANDS: dict[str, tuple]      # Hardcoded skill defaults
SKILL_COMMANDS: dict[str, tuple[str, str]]     # Merged: defaults + skills.json
_SKILL_TRANSLATE: dict[str, str]               # tg_name -> /cc:command
_KEYS_SEND_MAP: dict[str, tuple]              # Screenshot keyboard key mappings
session_monitor: SessionMonitor | None         # Global monitor instance
_status_poll_task: asyncio.Task | None         # Background polling task
_bash_capture_tasks: dict[tuple, asyncio.Task] # Active ! command captures
```

**Skill loading** (`_load_skill_commands()`):
1. Starts with `_DEFAULT_SKILL_COMMANDS` (hardcoded GSD, beads, workflow commands)
2. If `~/.ccbot/skills.json` exists, merges entries on top (override + extend)
3. Returns merged dict; builds `_SKILL_TRANSLATE` reverse lookup

**Command translation** (in `forward_command_handler`):
```
User types: /gsd_progress some args
Split:      cmd_name="gsd_progress", cmd_args="some args"
Translate:  _SKILL_TRANSLATE["gsd_progress"] = "/gsd:progress"
Forward:    "/gsd:progress some args" → tmux
```

**Handler registration order** (in `create_bot()`):
1. `CommandHandler("start", start_command)`
2. `CommandHandler("history", history_command)`
3. `CommandHandler("resume", resume_command)`
4. `CommandHandler("screenshot", screenshot_command)`
5. `CommandHandler("esc", esc_command)`
6. `CallbackQueryHandler(callback_handler)` — catches ALL callback queries
7. `MessageHandler(FORUM_TOPIC_CLOSED, topic_closed_handler)`
8. `MessageHandler(COMMAND, forward_command_handler)` — catch-all for `/commands`
9. `MessageHandler(TEXT & ~COMMAND, text_handler)` — regular text
10. `MessageHandler(~COMMAND & ~TEXT & ~StatusUpdate, unsupported_content_handler)`

**Notification filter** (in `handle_new_message()`):
- Interactive tools (AskUserQuestion, ExitPlanMode) always pass through
- Other messages checked against `config.notify.should_notify(content_type, is_error=...)`
- Errors in tool_result checked via substring match: `"Error:"` or `"⏹ Interrupted"`

---

### session.py — State Management

**Purpose**: The core state hub managing all mappings between topics, windows, and sessions.

**Singleton**: `session_manager = SessionManager()` (module-level)

**Key mappings**:

```
Topic (thread_id) ──thread_bindings──> Window ID (@0) ──session_map──> Session ID (uuid)
```

**Fields**:

```python
@dataclass
class SessionManager:
    window_states: dict[str, WindowState]              # wid -> state
    user_window_offsets: dict[int, dict[str, int]]     # uid -> {wid -> byte_offset}
    thread_bindings: dict[int, dict[int, str]]         # uid -> {tid -> wid}
    group_chat_ids: dict[str, int]                     # "uid:tid" -> chat_id
    window_display_names: dict[str, str]               # wid -> display name
    _window_to_thread: dict[tuple[int, str], int]      # (uid, wid) -> tid (reverse index)
```

**State persistence**: All fields (except `_window_to_thread`) saved to `state.json` via `atomic_write_json` on every change.

**Session resolution**: `resolve_session_for_window(wid)` reads `session_map.json` to find the session ID, then searches `~/.claude/projects/` for the corresponding JSONL file.

**Message history**: `get_recent_messages(wid, start_byte, end_byte)` reads the JSONL file from the specified byte range and parses via `TranscriptParser.parse_entries()`.

**Startup re-resolution**: `resolve_stale_ids()` handles tmux server restarts where window IDs change. It matches persisted display names against live tmux windows to re-map all state references.

---

### session_monitor.py — JSONL Polling

**Purpose**: Watch Claude Code JSONL session files for new content and emit `NewMessage` events.

**Key class**:

```python
@dataclass
class NewMessage:
    session_id: str
    text: str
    is_complete: bool              # True when stop_reason is set
    content_type: str = "text"     # text|thinking|tool_use|tool_result|tool_error|local_command|user
    tool_use_id: str | None = None
    role: str = "assistant"
    tool_name: str | None = None   # Tool name for tool_use messages
```

**Polling loop** (runs every `monitor_poll_interval` seconds):
1. Load `session_map.json` — check for new/changed/removed windows
2. For each tracked session:
   a. Check file mtime (skip if unchanged)
   b. Read new bytes from last offset
   c. Parse JSONL lines via `TranscriptParser`
   d. Emit `NewMessage` for each complete entry
3. Persist byte offsets via `MonitorState`

**Optimizations**:
- **mtime cache**: Avoids re-reading unchanged files
- **Byte offsets**: Only reads new content since last poll
- **File truncation detection**: If offset > file size, resets to 0 (handles `/clear`)

---

### tmux_manager.py — Tmux Integration

**Purpose**: Async wrapper around libtmux for all tmux operations.

**Singleton**: `tmux_manager = TmuxManager()`

**Key class**:

```python
@dataclass
class TmuxWindow:
    window_id: str              # "@0", "@12" — unique within tmux server
    window_name: str            # Display name
    cwd: str                    # Current working directory
    pane_current_command: str   # Running process name
```

**Operations** (all async via `asyncio.to_thread`):

| Method | Description |
|---|---|
| `get_or_create_session()` | Ensure the ccbot tmux session exists |
| `list_windows()` | List all windows (excluding `__main__`) |
| `find_window_by_id(id)` | Find window by tmux ID (`@N`) |
| `find_window_by_name(name)` | Find window by display name |
| `capture_pane(wid, with_ansi)` | Capture visible pane content |
| `send_keys(wid, keys, enter, literal)` | Send keystrokes to window |
| `create_window(cwd, name)` | Create window, start claude, return (ok, msg, name, id) |
| `kill_window(wid)` | Kill a tmux window |

**Window creation flow**:
1. Check for name conflicts, auto-deduplicate (append `-2`, `-3`, etc.)
2. Create window in tmux session at specified directory
3. Send configured `claude_command` to start Claude Code
4. Return success status, message, window name, window ID

---

### hook.py — SessionStart Hook

**Purpose**: Called by Claude Code's SessionStart hook to maintain window-session mappings.

**Important**: This module does NOT import `config.py` (which requires `TELEGRAM_BOT_TOKEN`), since hooks run in tmux panes where bot env vars are not set. It imports `utils.py` only.

**Flow**:
1. Claude Code starts in a tmux window
2. SessionStart hook fires, pipes JSON to stdin
3. `ccbot hook` reads stdin, extracts `session_id` and `cwd`
4. Determines tmux window ID from `$TMUX_PANE` env var
5. Writes mapping to `session_map.json`:
   ```json
   {"ccbot:@5": {"session_id": "uuid", "cwd": "/path", "window_name": "name"}}
   ```

**Auto-install** (`ccbot hook --install`):
- Reads `~/.claude/settings.json`
- Adds SessionStart hook entry if not present
- Preserves existing hooks

---

### sync_skills.py — Skill Sync CLI

**Purpose**: Generate `~/.ccbot/skills.json` from a project's `.claude/commands/` directory.

**Entry point**: `ccbot-sync [project_dir]`

**Flow**:
1. Walk `.claude/commands/` recursively for `.md` files
2. Parse YAML frontmatter for `name` and `description` fields
3. If `name` present, use it; otherwise derive from file path (`gsd/progress.md` -> `gsd:progress`)
4. Convert to Telegram-safe name: replace `:`, `-`, `.` with `_`, lowercase
5. Skip native bot commands (start, history, esc, etc.)
6. Validate against Telegram pattern: `[a-z][a-z0-9_]{0,31}`
7. Write JSON to `~/.ccbot/skills.json`

**Key functions**:

```python
def scan_commands(project_dir: Path) -> dict[str, dict[str, str]]
def to_telegram_name(cc_command: str) -> str
def _parse_frontmatter(path: Path) -> dict[str, str]
```

---

### transcript_parser.py — JSONL Parsing

**Purpose**: Parse Claude Code session JSONL files into structured messages.

**Content types produced**:
- `text` — Claude's text responses
- `thinking` — Internal reasoning blocks (shown as expandable quotes)
- `tool_use` — Tool call summaries (e.g., "**Read**(file.py)")
- `tool_result` — Tool output (e.g., "Read 50 lines from file.py")
- `local_command` — Slash command results
- `user` — User messages (with `👤` prefix)

**Tool pairing**: `tool_use` blocks are matched with their `tool_result` blocks via `tool_use_id`. This enables the bot to edit the tool_use message in-place when the result arrives, showing the output inline.

**Key classes**:

```python
@dataclass
class ParsedEntry:
    role: str                    # "user" or "assistant"
    text: str                    # Formatted message text
    content_type: str            # text|thinking|tool_use|tool_result|local_command|user
    timestamp: str               # Human-readable time
    tool_use_id: str | None      # For tool_use/tool_result pairing
    tool_name: str | None        # Tool name for tool_use

class TranscriptParser:
    @staticmethod
    def parse_entries(entries) -> tuple[list[ParsedEntry], dict[str, PendingToolInfo]]
```

---

### terminal_parser.py — Pane Parsing

**Purpose**: Parse captured tmux pane text to detect Claude Code UI elements.

**Interactive UI patterns** detected:
- **AskUserQuestion** — Multi-choice prompts
- **ExitPlanMode** — Plan approval dialog
- **PermissionPrompt** — Tool permission requests
- **RestoreCheckpoint** — Checkpoint restoration
- **Settings** — Settings menu

Each pattern has a `top` regex (top delimiter), `bottom` regex (bottom delimiter), and `min_gap` (minimum lines between).

**Key functions**:

```python
def is_interactive_ui(text: str) -> bool
def extract_interactive_content(text: str) -> InteractiveUIContent | None
def parse_status_line(text: str) -> str | None
def strip_pane_chrome(text: str) -> str
def extract_bash_output(pane_text: str, command: str) -> str | None
```

---

### monitor_state.py — Byte Offset Persistence

**Purpose**: Track how far into each JSONL file the monitor has read.

```python
@dataclass
class TrackedSession:
    session_id: str
    file_path: str
    last_byte_offset: int

class MonitorState:
    def load() -> None
    def save() -> None
    def get_session(session_id) -> TrackedSession | None
    def update_session(tracked: TrackedSession) -> None
    def remove_session(session_id) -> None
    def save_if_dirty() -> None
```

Persisted to `~/.ccbot/monitor_state.json`.

---

### utils.py — Shared Utilities

```python
def ccbot_dir() -> Path                              # $CCBOT_DIR or ~/.ccbot
def atomic_write_json(path, data, indent=2) -> None  # temp + rename pattern
def read_cwd_from_jsonl(file_path) -> str             # Extract cwd from first JSONL entry
```

---

### markdown_v2.py — Markdown Conversion

Converts markdown text to Telegram's MarkdownV2 format using `telegramify-markdown`. Handles blockquotes, code blocks, and expandable quotes for thinking content.

---

### telegram_sender.py — Message Splitting

Splits messages exceeding Telegram's 4096-character limit while preserving markdown structure.

```python
def split_message(text: str, max_length: int = 4096) -> list[str]
```

---

### screenshot.py — Terminal Screenshots

Renders terminal text (with ANSI color codes) to PNG using Pillow and bundled TTF fonts.

```python
async def text_to_image(text: str, *, with_ansi: bool = False) -> bytes
```

Fonts in `src/ccbot/fonts/`: JetBrains Mono (primary), Noto Sans Mono CJK (CJK), Symbola (emoji/symbols).

---

## Handler Modules

All in `src/ccbot/handlers/`.

### callback_data.py — Callback Constants

All callback data prefixes used for routing in `callback_handler()`:

| Constant | Prefix | Source |
|---|---|---|
| `CB_HISTORY_PREV` | `hp:` | History pagination (older) |
| `CB_HISTORY_NEXT` | `hn:` | History pagination (newer) |
| `CB_DIR_SELECT` | `db:sel:` | Directory browser: select subdir |
| `CB_DIR_UP` | `db:up` | Directory browser: go to parent |
| `CB_DIR_CONFIRM` | `db:confirm` | Directory browser: confirm selection |
| `CB_DIR_CANCEL` | `db:cancel` | Directory browser: cancel |
| `CB_DIR_PAGE` | `db:page:` | Directory browser: paginate |
| `CB_WIN_BIND` | `wb:sel:` | Window picker: bind existing window |
| `CB_WIN_NEW` | `wb:new` | Window picker: create new session |
| `CB_WIN_CANCEL` | `wb:cancel` | Window picker: cancel |
| `CB_SCREENSHOT_REFRESH` | `ss:ref:` | Screenshot: refresh capture |
| `CB_ASK_UP/DOWN/LEFT/RIGHT` | `aq:up:` etc. | Interactive UI: arrow keys |
| `CB_ASK_ESC/ENTER/SPACE/TAB` | `aq:esc:` etc. | Interactive UI: special keys |
| `CB_ASK_REFRESH` | `aq:ref:` | Interactive UI: refresh display |
| `CB_KEYS_PREFIX` | `kb:` | Screenshot keyboard: control keys |
| `CB_RESUME_SELECT` | `rs:sel:` | Resume picker: select session |
| `CB_RESUME_PAGE` | `rs:pg:` | Resume picker: paginate |
| `CB_RESUME_CONFIRM` | `rs:ok:` | Resume picker: confirm selection |
| `CB_RESUME_CANCEL` | `rs:cancel` | Resume picker: cancel |

**Constraint**: Telegram callback data is limited to 64 bytes. Index-based references are used for long values (directory names, session IDs).

---

### message_queue.py — Per-User Message Queue

**Purpose**: FIFO message queue per user with a dedicated async worker.

**Message types**: `content`, `status_update`, `status_clear`

**Merging logic**: The worker dequeues tasks and attempts to merge consecutive content messages for the same window (up to 3800 chars). `tool_use` and `tool_result` break the merge chain because:
- `tool_use` needs its own message (to get a `message_id` for later editing)
- `tool_result` edits the corresponding `tool_use` message in-place

**Rate limiting**: Enforced via `rate_limit_send()` — minimum 1.1s between sends per user.

**Status message handling**: Status updates edit into the first content message to reduce message count. Deduplication skips edits when text hasn't changed.

---

### message_sender.py — Safe Send Helpers

Provides MarkdownV2-first sending with automatic plain-text fallback:

```python
async def safe_reply(message, text, **kwargs) -> Message
async def safe_edit(target, text, **kwargs) -> None
async def safe_send(bot, chat_id, text, **kwargs) -> Message | None
async def rate_limit_send_message(bot, chat_id, text, **kwargs) -> Message | None
```

All functions attempt MarkdownV2 first via `convert_markdown()`, then fall back to plain text if parsing fails.

---

### response_builder.py — Response Formatting

Builds paginated response messages from Claude Code output:

```python
def build_response_parts(text, is_complete, content_type="text", role="assistant") -> list[str]
```

- `thinking` content truncated to ~500 chars with expandable quote wrapper
- `tool_use` content shown as bold tool name
- Messages split if exceeding 4096 chars

---

### directory_browser.py — Directory & Window Picker

Two inline keyboard UIs:

**Window picker** — shown when unbound windows exist:
```
📋 Select a window to bind:
[window-1: /path/to/project]
[window-2: /other/project]
[➕ New Session]
[✖ Cancel]
```

**Directory browser** — for creating new sessions:
```
📁 /data/projects
[my-app]
[other-project]
[⬆ Parent]  [◀ Prev]  [Next ▶]
[✅ Select This Directory]
[✖ Cancel]
```

**State keys** stored in `user_data`:
- `STATE_KEY` — `"browsing_directory"` or `"selecting_window"`
- `BROWSE_PATH_KEY`, `BROWSE_PAGE_KEY`, `BROWSE_DIRS_KEY` — browser state
- `UNBOUND_WINDOWS_KEY` — cached window IDs for picker

---

### history.py — Message History Pagination

```python
async def send_history(target, window_id, offset=-1, edit=False, *,
                       start_byte=0, end_byte=0, user_id=None,
                       bot=None, message_thread_id=None)
```

- 10 messages per page
- Newest messages shown first (reversed chronological)
- Inline keyboard for pagination: `[◀ Older] [page/total] [Newer ▶]`
- Supports both initial send and in-place edit for pagination

---

### interactive_ui.py — Interactive UI Handling

Handles Claude Code's terminal-based interactive prompts by capturing the pane, detecting the UI pattern, and rendering it as a Telegram inline keyboard:

```python
INTERACTIVE_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})

async def handle_interactive_ui(bot, user_id, window_id, thread_id) -> bool
```

**Flow**:
1. Capture tmux pane content
2. Detect interactive UI via `terminal_parser.extract_interactive_content()`
3. Parse options from the UI text
4. Build inline keyboard with arrow keys, Enter, Escape, Space, Tab, Refresh
5. Send (or edit existing) message with keyboard

**Tracked per user+thread**: `_interactive_windows`, `_interactive_msg_ids`

---

### status_polling.py — Status Line Polling

Background async task that polls terminal status lines for all active sessions:

```python
async def status_poll_loop(bot: Bot) -> None
```

- Polls every 1 second
- For each bound window, captures pane and extracts status line via `parse_status_line()`
- Enqueues status updates to the per-user message queue
- Skips windows in interactive mode (to avoid status messages during prompts)

---

### resume.py — Session Resume Picker

Reads `~/.claude/history.jsonl` to discover past sessions for a project.

```python
@dataclass
class SessionSummary:
    session_id: str
    title: str           # First user message (truncated to 50 chars)
    last_active: float   # Unix timestamp (ms)
    message_count: int   # Number of user inputs
    project: str         # Project path
```

**Flow**:
1. `/resume` command triggers `resume_command()`
2. `scan_sessions()` reads history.jsonl, groups by sessionId, filters by project
3. `build_resume_keyboard()` creates paginated inline keyboard (6 per page)
4. User selects a session → confirmation step
5. On confirm: sends Escape + Escape + `/exit` + waits + `claude --resume <sid>` to tmux

---

### cleanup.py — Topic Cleanup

```python
async def clear_topic_state(user_id, thread_id, bot, user_data) -> None
```

Called when a topic is closed. Clears:
- Interactive mode state
- Interactive message IDs
- Status message info
- Message queue state
- Browse/picker state

---

## Data Structures

### Core Data Classes

| Class | Module | Fields |
|---|---|---|
| `WindowState` | session.py | `session_id`, `cwd`, `window_name` |
| `ClaudeSession` | session.py | `session_id`, `summary`, `message_count`, `file_path` |
| `UnreadInfo` | session.py | `has_unread`, `start_offset`, `end_offset` |
| `NewMessage` | session_monitor.py | `session_id`, `text`, `is_complete`, `content_type`, `tool_use_id`, `role`, `tool_name` |
| `SessionInfo` | session_monitor.py | `session_id`, `file_path` |
| `TmuxWindow` | tmux_manager.py | `window_id`, `window_name`, `cwd`, `pane_current_command` |
| `SessionSummary` | handlers/resume.py | `session_id`, `title`, `last_active`, `message_count`, `project` |
| `TrackedSession` | monitor_state.py | `session_id`, `file_path`, `last_byte_offset` |
| `MessageTask` | handlers/message_queue.py | `task_type`, `text`, `window_id`, `parts`, `tool_use_id`, `content_type`, `thread_id` |
| `ParsedEntry` | transcript_parser.py | `role`, `text`, `content_type`, `timestamp`, `tool_use_id`, `tool_name` |
| `InteractiveUIContent` | terminal_parser.py | `content`, `name` |

---

## State Files

All under `$CCBOT_DIR` (default `~/.ccbot/`):

### state.json

```json
{
  "window_states": {
    "@5": {"session_id": "uuid", "cwd": "/path", "window_name": "name"}
  },
  "user_window_offsets": {
    "123456789": {"@5": 48230}
  },
  "thread_bindings": {
    "123456789": {"42": "@5", "43": "@7"}
  },
  "group_chat_ids": {
    "123456789:42": -1001234567890
  },
  "window_display_names": {
    "@5": "my-project",
    "@7": "other-project"
  }
}
```

### session_map.json

Written by the SessionStart hook:

```json
{
  "ccbot:@5": {
    "session_id": "uuid-xxx",
    "cwd": "/data/projects/my-app",
    "window_name": "my-app"
  }
}
```

### monitor_state.json

```json
{
  "sessions": {
    "uuid-xxx": {
      "session_id": "uuid-xxx",
      "file_path": "/home/user/.claude/projects/.../uuid-xxx.jsonl",
      "last_byte_offset": 152847
    }
  }
}
```

### notify.json

```json
{
  "text": true,
  "thinking": false,
  "tool_use": false,
  "tool_result": false,
  "tool_error": true,
  "local_command": false,
  "user": false
}
```

### skills.json

Generated by `ccbot-sync`:

```json
{
  "gsd_progress": {
    "command": "/gsd:progress",
    "description": "Check project progress"
  },
  "review_pr": {
    "command": "/review-pr",
    "description": "Review a pull request"
  }
}
```

---

## Data Flow Diagrams

### Outbound: User -> Claude

```
User sends "hello" in topic (thread_id=42)
  │
  ▼
text_handler()
  │ thread_bindings[user_id][42] = "@5"
  ▼
session_manager.send_to_window("@5", "hello")
  │
  ▼
tmux_manager.send_keys("@5", "hello\n")
  │
  ▼
Claude Code receives input in tmux pane
```

### Inbound: Claude -> User

```
Claude writes response to JSONL file
  │
  ▼
SessionMonitor detects new bytes (mtime + offset)
  │ Parses via TranscriptParser
  ▼
NewMessage(session_id="uuid", text="...", content_type="text")
  │
  ▼
handle_new_message()
  │ Check notify filter → config.notify.should_notify("text")
  │ Find users: session_manager.find_users_for_session("uuid")
  │   → matches (user_id=123, wid="@5", thread_id=42)
  ▼
enqueue_content_message(bot, user_id=123, window_id="@5", ...)
  │
  ▼
_message_queue_worker() dequeues task
  │ Attempts merge with next tasks
  │ Rate-limits via rate_limit_send()
  ▼
safe_send(bot, chat_id, text, message_thread_id=42)
  │ Convert markdown → MarkdownV2
  │ Fallback to plain text on failure
  ▼
Telegram delivers message to user in topic 42
```

### Skill Command Translation

```
User types: /gsd_progress --verbose
  │
  ▼
forward_command_handler()
  │ Split: cmd_name="gsd_progress", cmd_args="--verbose"
  │ Lookup: _SKILL_TRANSLATE["gsd_progress"] = "/gsd:progress"
  │ Reconstruct: cc_slash = "/gsd:progress --verbose"
  ▼
session_manager.send_to_window(wid, "/gsd:progress --verbose")
```

### Session Creation (New Topic)

```
User sends message in unbound topic
  │
  ▼
text_handler() → wid is None (no binding)
  │
  ├── Unbound windows exist?
  │   YES → build_window_picker() → show inline keyboard
  │   User selects window → bind_thread(uid, tid, wid)
  │
  │   NO → build_directory_browser() → show dir picker
  │         User navigates and confirms → tmux_manager.create_window(path)
  │         → Wait for session_map entry via hook
  │         → bind_thread(uid, tid, new_wid)
  │         → Forward pending text to Claude
  ▼
Topic is now bound — future messages forwarded to Claude
```

---

## Callback Routing Map

All callbacks flow through `callback_handler()` in `bot.py`. Routing is prefix-based:

```python
if data.startswith(CB_HISTORY_PREV) or data.startswith(CB_HISTORY_NEXT):
    # History pagination
elif data.startswith(CB_DIR_SELECT):     # Directory browser: navigate
elif data == CB_DIR_UP:                  # Directory browser: parent
elif data.startswith(CB_DIR_PAGE):       # Directory browser: paginate
elif data == CB_DIR_CONFIRM:             # Directory browser: create window
elif data == CB_DIR_CANCEL:              # Directory browser: cancel
elif data.startswith(CB_WIN_BIND):       # Window picker: bind existing
elif data == CB_WIN_NEW:                 # Window picker: → directory browser
elif data == CB_WIN_CANCEL:              # Window picker: cancel
elif data.startswith(CB_SCREENSHOT_REFRESH):  # Screenshot: refresh
elif data == "noop":                     # No-op (spacer buttons)
elif data.startswith(CB_RESUME_*):       # Resume picker: all actions
elif data.startswith(CB_ASK_*):          # Interactive UI: all keys
elif data.startswith(CB_KEYS_PREFIX):    # Screenshot keyboard: control keys
```

---

## Configuration Reference

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram Bot API token |
| `ALLOWED_USERS` | Yes | — | Comma-separated Telegram user IDs |
| `CCBOT_DIR` | No | `~/.ccbot` | Config and state directory |
| `TMUX_SESSION_NAME` | No | `ccbot` | Name of the tmux session |
| `CLAUDE_COMMAND` | No | `claude` | Command to run in new tmux windows |
| `MONITOR_POLL_INTERVAL` | No | `2.0` | Seconds between JSONL polls |

### Config Files

| File | Auto-created | Description |
|---|---|---|
| `~/.ccbot/.env` | No | Environment variable overrides |
| `~/.ccbot/notify.json` | Yes (all on) | Per-content-type notification toggles |
| `~/.ccbot/skills.json` | No (`ccbot-sync`) | Telegram command -> Claude command mappings |
| `~/.ccbot/state.json` | Yes | Thread bindings, window states, read offsets |
| `~/.ccbot/session_map.json` | Yes (by hook) | Window -> session mappings |
| `~/.ccbot/monitor_state.json` | Yes | JSONL byte offsets |

---

## Design Constraints

1. **1 Topic = 1 Window = 1 Session** — all routing keyed by tmux window ID (`@N`), not window name
2. **Topic-only** — no backward-compat for non-topic mode
3. **No message truncation at parse layer** — splitting only at send layer (4096 char limit)
4. **MarkdownV2 only** — auto fallback to plain text via `safe_*` helpers
5. **Hook-based session tracking** — `SessionStart` hook writes `session_map.json`; bot polls it
6. **Per-user FIFO queue** — message ordering guaranteed, merge up to 3800 chars
7. **Rate limiting** — 1.1s minimum between messages per user
8. **Window ID keyed** — `@N` format, guaranteed unique within tmux server lifetime
9. **Callback data < 64 bytes** — use index-based references for long values
10. **Module docstrings mandatory** — purpose clear within first 10 lines
11. **Max file size** — aim for under 500 lines per module
12. **Interactive prompts bypass filters** — AskUserQuestion/ExitPlanMode always forwarded
