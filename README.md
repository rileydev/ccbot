# CCMux

Telegram Bot for monitoring and interacting with Claude Code sessions running in tmux.

## Features

- **Monitor Claude Code sessions** — Auto-detects sessions from `~/.claude/projects/` with active tmux windows
- **Real-time notifications** — Get Telegram messages when Claude responds (text and thinking content)
- **Local command output** — See stdout from local commands (e.g. `git status`) in Telegram
- **Send messages** — Forward text to Claude Code via tmux keystrokes
- **Slash command forwarding** — Send any `/command` directly to Claude Code (e.g. `/clear`, `/compact`, `/cost`)
- **Create new sessions** — Start Claude Code sessions from Telegram via directory browser
- **Kill sessions** — Terminate sessions remotely
- **Message history** — Browse conversation history with pagination
- **Persistent state** — Active window selection survives restarts
- **Hook-based session tracking** — Auto-associates tmux windows with Claude sessions via hooks

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Telegram Bot (bot.py)                       │
│  - /list: Browse sessions (inline buttons)                         │
│  - /history: Paginated message history (default: latest page)      │
│  - /screenshot: Capture tmux pane as PNG                           │
│  - Send text → Claude Code via tmux keystrokes                     │
│  - Forward /commands to Claude Code                                │
│  - Create / kill sessions via directory browser                    │
│  - Tool use → tool result: edit message in-place                   │
│  - MarkdownV2 output with auto fallback to plain text              │
├──────────────────────┬──────────────────────────────────────────────┤
│  markdown_v2.py      │  telegram_sender.py                         │
│  MD → MarkdownV2     │  split_message (4096 limit)                 │
│  + expandable quotes │  + inline keyboard pagination               │
└──────────┬───────────┴──────────────────┬───────────────────────────┘
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
│  TranscriptParser      │         │  Tmux Windows (cc:*)    │
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
│  - Message history     │────────►│  Claude Sessions       │
│    retrieval           │  reads  │  ~/.claude/projects/   │
└────────────────────────┘  JSONL  │  - sessions-index      │
                                   │  - *.jsonl files       │
┌────────────────────────┐         └────────────────────────┘
│  MonitorState          │
│  (monitor_state.py)    │
│  - Track file mtime    │
│  - Track line count    │
│  - Prevent duplicates  │
│    after restart       │
└────────────────────────┘

State files (~/.ccmux/):
  state.json         ─ user→window mapping + window states
  session_map.json   ─ hook-generated window→session mapping
  monitor_state.json ─ poll progress per JSONL file
```

**Key design decisions:**
- **Window-centric** — All state anchored to tmux window names (`cc:project`), not directories. Same directory can have multiple windows.
- **Hook-based session tracking** — Claude Code `SessionStart` hook writes `session_map.json`; monitor reads it each poll cycle to auto-detect session changes.
- **Tool use ↔ tool result pairing** — `tool_use_id` tracked across poll cycles; tool result edits the original tool_use Telegram message in-place.
- **MarkdownV2 with fallback** — All messages go through `_safe_reply`/`_safe_edit`/`_safe_send` which convert via `telegramify-markdown` and fall back to plain text on parse failure.
- **No truncation at parse layer** — Full content preserved; splitting at send layer respects Telegram's 4096 char limit with expandable quote atomicity.
- Only sessions with matching `cc:` tmux windows are displayed (enables bidirectional communication)
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
| `CLAUDE_COMMAND` | `claude --dangerously-skip-permissions` | Command to run in new windows |
| `MONITOR_POLL_INTERVAL` | `2.0` | Polling interval in seconds |
| `MONITOR_STABLE_WAIT` | `2.0` | File stability wait time in seconds |

## Hook Setup (Recommended)

To enable automatic session tracking when Claude Code starts or ends a session, add the following to `~/.claude/settings.json`:

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

This writes window↔session mappings to `~/.ccmux/session_map.json`, so the bot automatically tracks which Claude session is running in each tmux window — even after `/new` or session restarts.

## Usage

```bash
uv run ccmux
```

### Commands

**Bot commands:**

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/list` | Browse active sessions (inline buttons) |
| `/history` | Show history for active session |
| `/screenshot` | Capture terminal screenshot |

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

### Sending Messages

1. Use `/list` to select a session
2. Send any text — it gets forwarded to Claude Code via tmux keystrokes
3. The bot creates a ⏳ placeholder, then sends Claude's response when ready

### Message History

Navigate with inline buttons:

```
📋 [project-name] Messages (6-10 of 42)

👤 fix the login bug

🤖 I'll look into the login bug...

👤 also check the session timeout

🤖 Found the issue...

[◀ Older]    [2/9]    [Newer ▶]
```

### Creating New Sessions

1. Tap **➕ New Session** in `/list`
2. Browse and select a directory using the inline directory browser
3. A new tmux window is created and `claude` starts automatically

### Notifications

The monitor polls session JSONL files every 2 seconds and sends notifications for:
- **Assistant responses** — Claude's text replies
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
tmux new-window -n cc:myproject
cd ~/Code/myproject
claude
```

Window names must start with the prefix `cc:` to be recognized.

## Data Storage

| Path | Description |
|---|---|
| `~/.ccmux/state.json` | Active window selections and window states (`{user_id: window_name}`, `{window_name: {session_id, last_msg_id, pending_text}}`) |
| `~/.ccmux/session_map.json` | Hook-generated window↔session mappings |
| `~/.ccmux/monitor_state.json` | Monitor state (prevents duplicate notifications) |
| `~/.claude/projects/` | Claude Code session data (read-only) |

## File Structure

```
src/ccmux/
├── main.py              # Entry point (subcommand dispatch + bot start)
├── hook.py              # Hook subcommand for session tracking
├── config.py            # Configuration from environment variables
├── bot.py               # Telegram bot handlers and inline UI
├── session.py           # Session management + message history
├── session_monitor.py   # JSONL file monitoring (polling + change detection)
├── monitor_state.py     # Monitor state persistence
├── transcript_parser.py # Claude Code JSONL transcript parsing
├── markdown_v2.py       # Markdown → Telegram MarkdownV2 conversion
├── utils.py             # Shared utilities (atomic JSON writes, JSONL helpers)
├── telegram_sender.py   # Message splitting and sending utilities
├── screenshot.py        # Terminal text → PNG image for /screenshot
└── tmux_manager.py      # Tmux window management (list, create, send keys, kill)
```
