# CCMux

Telegram Bot for monitoring and interacting with Claude Code sessions running in tmux.

## Features

- **Monitor Claude Code sessions** — Auto-detects sessions from `~/.claude/projects/` with active tmux windows
- **Real-time notifications** — Get Telegram messages when Claude responds (text and thinking content)
- **Interactive UI** — Navigate AskUserQuestion, ExitPlanMode, and Permission Prompts via inline keyboard
- **Local command output** — See stdout from local commands (e.g. `git status`) in Telegram
- **Send messages** — Forward text to Claude Code via tmux keystrokes
- **Slash command forwarding** — Send any `/command` directly to Claude Code (e.g. `/clear`, `/compact`, `/cost`)
- **Create new sessions** — Start Claude Code sessions from Telegram via directory browser
- **Kill sessions** — Terminate sessions remotely
- **Message history** — Browse conversation history with pagination
- **Unread catch-up** — Switching to a window shows unread messages since last visit
- **Persistent state** — Active window selection and read offsets survive restarts
- **Hook-based session tracking** — Auto-associates tmux windows with Claude sessions via hooks

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Telegram Bot (bot.py)                       │
│  - /list: Browse sessions (inline buttons)                         │
│  - /history: Paginated message history (default: latest page)      │
│  - /screenshot: Capture tmux pane as PNG                           │
│  - /esc: Send Escape to interrupt Claude                           │
│  - Send text → Claude Code via tmux keystrokes                     │
│  - Forward /commands to Claude Code                                │
│  - Create / kill sessions via directory browser                    │
│  - Tool use → tool result: edit message in-place                   │
│  - Interactive UI: AskUserQuestion / ExitPlanMode / Permission     │
│  - Per-user message queue + worker (merge, rate limit)             │
│  - MarkdownV2 output with auto fallback to plain text              │
├──────────────────────┬──────────────────────────────────────────────┤
│  markdown_v2.py      │  telegram_sender.py                         │
│  MD → MarkdownV2     │  split_message (4096 limit)                 │
│  + expandable quotes │  + sync HTTP send (for hooks)               │
├──────────────────────┴──────────────────────────────────────────────┤
│  terminal_parser.py                                                 │
│  - Detect interactive UIs (AskUserQuestion, ExitPlanMode, etc.)    │
│  - Parse status line (spinner + working text)                      │
└──────────┬──────────────────────────────┬───────────────────────────┘
           │                              │
           │ Notify (NewMessage callback) │ Send (tmux keys)
           │                              │
┌──────────┴──────────────┐    ┌──────────┴──────────────────────┐
│  SessionMonitor         │    │  TmuxManager (tmux_manager.py)  │
│  (session_monitor.py)   │    │  - list/find/create/kill windows│
│  - Poll JSONL every 2s  │    │  - send_keys to pane            │
│  - Detect mtime changes │    │  - capture_pane for screenshot  │
│  - Parse new lines      │    └──────────────┬─────────────────┘
│  - Track pending tools  │                   │
│    across poll cycles   │                   │
└──────────┬──────────────┘                   │
           │                                  │
           ▼                                  ▼
┌────────────────────────┐         ┌─────────────────────────┐
│  TranscriptParser      │         │  Tmux Windows           │
│  (transcript_parser.py)│         │  - Claude Code process  │
│  - Parse JSONL entries │         │  - One window per       │
│  - Pair tool_use ↔     │         │    project/session      │
│    tool_result         │         └────────────┬────────────┘
│  - Format expandable   │                      │
│    quotes for thinking │              SessionStart hook
│  - Extract history     │                      │
└────────────────────────┘                      ▼
                                   ┌────────────────────────┐
┌────────────────────────┐         │  Hook (hook.py)        │
│  SessionManager        │◄────────│  - Receive hook stdin  │
│  (session.py)          │  reads  │  - Write session_map   │
│  - Window ↔ Session    │  map    │    .json               │
│    resolution          │         └────────────────────────┘
│  - Active window per   │
│    user                │         ┌────────────────────────┐
│  - Unread tracking     │────────►│  Claude Sessions       │
│  - Message history     │  reads  │  ~/.claude/projects/   │
│    retrieval           │  JSONL  │  - sessions-index      │
└────────────────────────┘         │  - *.jsonl files       │
                                   └────────────────────────┘
┌────────────────────────┐
│  MonitorState          │
│  (monitor_state.py)    │
│  - Track byte offset   │
│  - Prevent duplicates  │
│    after restart       │
└────────────────────────┘

State files (~/.ccmux/):
  state.json         ─ user→window mapping + window states + read offsets
  session_map.json   ─ hook-generated window→session mapping
  monitor_state.json ─ poll progress (byte offset) per JSONL file
```

**Key design decisions:**
- **Window-centric** — All state anchored to tmux window names (e.g. `myproject`), not directories. Same directory can have multiple windows (auto-suffixed: `myproject-2`).
- **Hook-based session tracking** — Claude Code `SessionStart` hook writes `session_map.json`; monitor reads it each poll cycle to auto-detect session changes.
- **Tool use ↔ tool result pairing** — `tool_use_id` tracked across poll cycles; tool result edits the original tool_use Telegram message in-place.
- **MarkdownV2 with fallback** — All messages go through `_safe_reply`/`_safe_edit`/`_safe_send` which convert via `telegramify-markdown` and fall back to plain text on parse failure.
- **No truncation at parse layer** — Full content preserved; splitting at send layer respects Telegram's 4096 char limit with expandable quote atomicity.
- Only sessions registered in `session_map.json` (via hook) are monitored and displayed
- Notifications sent only to users whose active window matches the message's session

## Installation

```bash
cd ccmux
uv sync
```

## Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

**Required:**

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs |

**Optional:**

| Variable | Default | Description |
|---|---|---|
| `TMUX_SESSION_NAME` | `ccmux` | Tmux session name |
| `CLAUDE_COMMAND` | `claude` | Command to run in new windows |
| `MONITOR_POLL_INTERVAL` | `2.0` | Polling interval in seconds |

## Hook Setup (Recommended)

Auto-install via CLI:

```bash
ccmux hook --install
```

Or manually add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "ccmux hook", "timeout": 5 }]
      }
    ]
  }
}
```

This writes window↔session mappings to `~/.ccmux/session_map.json`, so the bot automatically tracks which Claude session is running in each tmux window — even after `/clear` or session restarts.

## Usage

```bash
uv run ccmux
```

### Commands

**Bot commands:**

| Command | Description |
|---|---|
| `/start` | Show session menu |
| `/list` | Browse active sessions (inline buttons) |
| `/history` | Show history for active session |
| `/screenshot` | Capture terminal screenshot |
| `/esc` | Send Escape to interrupt Claude |

**Claude Code commands (forwarded via tmux):**

| Command | Description |
|---|---|
| `/clear` | Clear conversation history |
| `/compact` | Compact conversation context |
| `/cost` | Show token/cost usage |
| `/help` | Show Claude Code help |
| `/memory` | Edit CLAUDE.md |

Any unrecognized `/command` is also forwarded to Claude Code as-is (e.g. `/review`, `/doctor`, `/init`).

### Session List (`/list`)

Sessions are shown as inline buttons. Tap a session to select it as active:

```
📊 3 active sessions:

[✅ [ccmux] Telegram Bot...]
[   [resume] Resume Builder...]
[   [tickflow] Task Management...]
[➕ New Session]
```

After selecting a session, you get detail info and action buttons:

```
📤 Selected: ccmux

📝 Telegram Bot for Claude Code monitoring
💬 42 messages

[📋 History] [🔄 Refresh] [❌ Kill]
```

If there are unread messages since your last visit, they are shown automatically.

### Sending Messages

1. Use `/list` to select a session
2. Send any text — it gets forwarded to Claude Code via tmux keystrokes
3. A typing indicator appears while Claude is working, then the response is sent

### Message History

Navigate with inline buttons:

```
📋 [project-name] Messages (42 total)

───── 14:32 ─────

👤 fix the login bug

───── 14:33 ─────

I'll look into the login bug...

[◀ Older]    [2/9]    [Newer ▶]
```

### Creating New Sessions

1. Tap **➕ New Session** in `/list`
2. Browse and select a directory using the inline directory browser
3. A new tmux window is created and `claude` starts automatically

### Notifications

The monitor polls session JSONL files every 2 seconds and sends notifications for:
- **Assistant responses** — Claude's text replies
- **Thinking content** — Shown as expandable blockquotes
- **Tool use/result** — Summarized with stats (e.g. "Read 42 lines", "Found 5 matches")
- **Local command output** — stdout from commands like `git status`, prefixed with `❯ command_name`

Notifications are only sent to users whose active window matches the session.

## Running Claude Code in tmux

### Option 1: Create via Telegram (Recommended)

1. Run `/list`
2. Tap **➕ New Session**
3. Select the project directory

### Option 2: Create Manually

```bash
tmux attach -t ccmux
tmux new-window -n myproject -c ~/Code/myproject
# Then start Claude Code in the new window
claude
```

The window must be in the `ccmux` tmux session (configurable via `TMUX_SESSION_NAME`). The hook will automatically register it in `session_map.json` when Claude starts.

## Data Storage

| Path | Description |
|---|---|
| `~/.ccmux/state.json` | Active window selections, window↔session states, and per-user read offsets |
| `~/.ccmux/session_map.json` | Hook-generated `{tmux_session:window_name: {session_id, cwd}}` mappings |
| `~/.ccmux/monitor_state.json` | Monitor byte offsets per session (prevents duplicate notifications) |
| `~/.claude/projects/` | Claude Code session data (read-only) |

## File Structure

```
src/ccmux/
├── __init__.py          # Package entry point
├── main.py              # CLI dispatcher (hook subcommand + bot bootstrap)
├── hook.py              # Hook subcommand for session tracking (+ --install)
├── config.py            # Configuration from environment variables
├── bot.py               # Telegram bot handlers, message queue, inline UI
├── session.py           # Session management, state persistence, message history
├── session_monitor.py   # JSONL file monitoring (polling + change detection)
├── monitor_state.py     # Monitor state persistence (byte offsets)
├── transcript_parser.py # Claude Code JSONL transcript parsing
├── terminal_parser.py   # Terminal pane parsing (interactive UI + status line)
├── markdown_v2.py       # Markdown → Telegram MarkdownV2 conversion
├── telegram_sender.py   # Message splitting + synchronous HTTP send
├── screenshot.py        # Terminal text → PNG image with ANSI color support
├── utils.py             # Shared utilities (atomic JSON writes, JSONL helpers)
├── tmux_manager.py      # Tmux window management (list, create, send keys, kill)
└── fonts/               # Bundled fonts for screenshot rendering
```
