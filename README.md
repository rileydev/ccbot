# CCBot

[中文文档](README_CN.md)

Control Claude Code sessions remotely via Telegram — monitor, interact, and manage AI coding sessions running in tmux.

https://github.com/user-attachments/assets/15ffb38e-5eb9-4720-93b9-412e4961dc93

## Why CCBot?

Claude Code runs in your terminal. When you step away from your computer — commuting, on the couch, or just away from your desk — the session keeps working, but you lose visibility and control.

CCBot solves this by letting you **seamlessly continue the same session from Telegram**. The key insight is that it operates on **tmux**, not the Claude Code SDK. Your Claude Code process stays exactly where it is, in a tmux window on your machine. CCBot simply reads its output and sends keystrokes to it. This means:

- **Switch from desktop to phone mid-conversation** — Claude is working on a refactor? Walk away, keep monitoring and responding from Telegram.
- **Switch back to desktop anytime** — Since the tmux session was never interrupted, just `tmux attach` and you're back in the terminal with full scrollback and context.
- **Run multiple sessions in parallel** — Each Telegram topic maps to a separate tmux window, so you can juggle multiple projects from one chat group.

Other Telegram bots for Claude Code typically wrap the Claude Code SDK to create separate API sessions. Those sessions are isolated — you can't resume them in your terminal. CCBot takes a different approach: it's just a thin control layer over tmux, so the terminal remains the source of truth and you never lose the ability to switch back.

## Features

- **Topic-based sessions** — Each Telegram topic maps 1:1 to a tmux window and Claude session
- **Real-time notifications** — Get Telegram messages for assistant responses, thinking content, tool use/result, and local command output
- **Configurable notification filtering** — Per-content-type toggles via `~/.ccbot/notify.json` to control what gets forwarded
- **Interactive UI** — Navigate AskUserQuestion, ExitPlanMode, and Permission Prompts via inline keyboard
- **Send messages** — Forward text to Claude Code via tmux keystrokes
- **Slash command forwarding** — Send any `/command` directly to Claude Code (e.g. `/clear`, `/compact`, `/cost`)
- **Skill command menu** — Register project-specific Claude Code skills as Telegram bot commands with automatic name translation (e.g. `/gsd_progress` -> `/gsd:progress`)
- **Resume sessions** — Browse and resume previous Claude Code conversations via `/resume` with paginated session picker
- **Create new sessions** — Start Claude Code sessions from Telegram via directory browser or window picker
- **Kill sessions** — Close a topic to auto-kill the associated tmux window
- **Message history** — Browse conversation history with pagination (newest first)
- **Terminal screenshots** — Capture pane content as PNG with control key overlay for navigation
- **Bash command capture** — `!command` syntax captures and streams command output
- **Hook-based session tracking** — Auto-associates tmux windows with Claude sessions via `SessionStart` hook
- **Persistent state** — Thread bindings, read offsets, and display names survive restarts
- **Skill sync CLI** — `ccbot-sync` auto-generates Telegram bot commands from a project's `.claude/commands/` directory

## Prerequisites

- **Python** >= 3.12
- **tmux** — must be installed and available in PATH
- **Claude Code** — the CLI tool (`claude`) must be installed

## Installation

### Option 1: Install from GitHub (Recommended)

```bash
# Using uv (recommended)
uv tool install git+https://github.com/six-ddc/ccmux.git

# Or using pipx
pipx install git+https://github.com/six-ddc/ccmux.git
```

### Option 2: Install from source

```bash
git clone https://github.com/six-ddc/ccmux.git
cd ccmux
uv sync
```

Both options install two CLI commands: `ccbot` (the bot) and `ccbot-sync` (skill sync utility).

## Configuration

**1. Create a Telegram bot and enable Threaded Mode:**

1. Chat with [@BotFather](https://t.me/BotFather) to create a new bot and get your bot token
2. Open @BotFather's profile page, tap **Open App** to launch the mini app
3. Select your bot, then go to **Settings** > **Bot Settings**
4. Enable **Threaded Mode**

**2. Configure environment variables:**

Create `~/.ccbot/.env`:

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
```

**Required:**

| Variable             | Description                       |
| -------------------- | --------------------------------- |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather         |
| `ALLOWED_USERS`      | Comma-separated Telegram user IDs |

**Optional:**

| Variable                | Default    | Description                                      |
| ----------------------- | ---------- | ------------------------------------------------ |
| `CCBOT_DIR`             | `~/.ccbot` | Config/state directory (`.env` loaded from here) |
| `TMUX_SESSION_NAME`     | `ccbot`    | Tmux session name                                |
| `CLAUDE_COMMAND`        | `claude`   | Command to run in new windows                    |
| `MONITOR_POLL_INTERVAL` | `2.0`      | Polling interval in seconds                      |

> If running on a VPS where there's no interactive terminal to approve permissions, consider:
>
> ```
> CLAUDE_COMMAND=IS_SANDBOX=1 claude --dangerously-skip-permissions
> ```

## Hook Setup (Recommended)

Auto-install via CLI:

```bash
ccbot hook --install
```

Or manually add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }]
      }
    ]
  }
}
```

This writes window-session mappings to `$CCBOT_DIR/session_map.json` (`~/.ccbot/` by default), so the bot automatically tracks which Claude session is running in each tmux window — even after `/clear` or session restarts.

## Usage

```bash
# If installed via uv tool / pipx
ccbot

# If installed from source
uv run ccbot
```

### Commands

**Bot commands:**

| Command       | Description                                       |
| ------------- | ------------------------------------------------- |
| `/start`      | Show welcome message                              |
| `/history`    | Message history for this topic                    |
| `/resume`     | Browse and resume a previous Claude conversation  |
| `/screenshot` | Capture terminal screenshot with control keys     |
| `/esc`        | Send Escape to interrupt Claude                   |

**Claude Code commands (forwarded via tmux):**

| Command    | Description                  |
| ---------- | ---------------------------- |
| `/clear`   | Clear conversation history   |
| `/compact` | Compact conversation context |
| `/cost`    | Show token/cost usage        |
| `/help`    | Show Claude Code help        |
| `/memory`  | Edit CLAUDE.md               |

**Skill commands (auto-translated):**

Any `/command` not handled by the bot is forwarded to Claude Code as-is. Skill commands that contain characters not valid in Telegram command names (`:`, `-`, `.`) are automatically translated:

| Telegram Command     | Forwarded As        |
| -------------------- | ------------------- |
| `/gsd_progress`      | `/gsd:progress`     |
| `/review_pr`         | `/review-pr`        |
| `/bd_list`           | `/beads:list`       |
| `/speckit_analyze`   | `/speckit.analyze`  |

See [Skill Sync](#skill-sync) for how to populate these from your project.

### Topic Workflow

**1 Topic = 1 Window = 1 Session.** The bot runs in Telegram Forum (topics) mode.

**Creating a new session:**

1. Create a new topic in the Telegram group
2. Send any message in the topic
3. If unbound windows exist, a window picker appears; otherwise a directory browser
4. Select a window or directory — a tmux window is created, `claude` starts, and your pending message is forwarded

**Sending messages:**

Once a topic is bound to a session, just send text in that topic — it gets forwarded to Claude Code via tmux keystrokes.

**Running shell commands:**

Prefix a message with `!` to run it as a bash command. CCBot captures and streams the output back to Telegram in real-time.

**Killing a session:**

Close (or delete) the topic in Telegram. The associated tmux window is automatically killed and the binding is removed.

### Resume Sessions

Use `/resume` in any bound topic to browse previous Claude Code conversations:

```
📋 Recent Sessions

1. fix the login bug  (2h ago, 12 msgs)
2. add dark mode       (1d ago, 45 msgs)
3. refactor auth       (3d ago, 8 msgs)

[◀ Prev]    [1/3]    [Next ▶]
```

Select a session, confirm, and CCBot sends `claude --resume <session_id>` to the tmux window.

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

### Notification Filtering

Control which message types are forwarded to Telegram via `~/.ccbot/notify.json`:

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

| Key             | Content                                | Default |
| --------------- | -------------------------------------- | ------- |
| `text`          | Claude's text responses                | `true`  |
| `thinking`      | Internal reasoning / thinking blocks   | `true`  |
| `tool_use`      | Tool call summaries (e.g. "Read(...)")  | `true`  |
| `tool_result`   | Tool output (e.g. "Read 50 lines")     | `true`  |
| `tool_error`    | Errors from tool execution             | `true`  |
| `local_command` | Slash command results                  | `true`  |
| `user`          | User messages echoed back              | `true`  |

The `tool_error` toggle is independent of `tool_result` — you can suppress tool output but still see errors. Interactive prompts (AskUserQuestion, ExitPlanMode, permissions) always come through regardless of settings. The file is auto-created with all-on defaults on first run.

### Skill Sync

`ccbot-sync` scans a project's `.claude/commands/` directory and generates `~/.ccbot/skills.json` — a mapping of Telegram-safe command names to Claude Code slash commands.

```bash
# Sync commands from a project
ccbot-sync /path/to/project

# Then restart the bot to pick up changes
systemctl --user restart ccbot.service
```

**How it works:**

1. Walks `.claude/commands/` recursively for `.md` files
2. Parses YAML frontmatter for `name` and `description` fields
3. Converts names to Telegram-safe format (`/gsd:progress` -> `gsd_progress`)
4. Writes `~/.ccbot/skills.json`
5. On startup, the bot merges `skills.json` with hardcoded defaults (beads, etc.)

**Example skills.json entry:**

```json
{
  "gsd_progress": {
    "command": "/gsd:progress",
    "description": "Check project progress and route to next action"
  }
}
```

**Integration with rulesync:** If your project uses [rulesync](https://github.com/nicholasgriffintn/rulesync), create a wrapper script that chains both:

```bash
#!/usr/bin/env bash
npx rulesync generate "$@"
ccbot-sync "$(pwd)"
systemctl --user restart ccbot.service
```

## Running Claude Code in tmux

### Option 1: Create via Telegram (Recommended)

1. Create a new topic in the Telegram group
2. Send any message
3. Select the project directory from the browser

### Option 2: Create Manually

```bash
tmux attach -t ccbot
tmux new-window -n myproject -c ~/Code/myproject
# Then start Claude Code in the new window
claude
```

The window must be in the `ccbot` tmux session (configurable via `TMUX_SESSION_NAME`). The hook will automatically register it in `session_map.json` when Claude starts.

## Data Storage

All state files live under `$CCBOT_DIR` (default `~/.ccbot/`):

| File                  | Description                                                              | Written By     |
| --------------------- | ------------------------------------------------------------------------ | -------------- |
| `state.json`          | Thread bindings, window states, display names, per-user read offsets     | Bot            |
| `session_map.json`    | Window ID -> session ID + cwd mappings                                   | Hook           |
| `monitor_state.json`  | Byte offsets per session file (prevents duplicate notifications)         | Monitor        |
| `notify.json`         | Per-content-type notification toggles                                    | User / auto    |
| `skills.json`         | Telegram command -> Claude Code command mappings                          | `ccbot-sync`   |
| `.env`                | Environment variable overrides                                           | User           |

Claude Code session data is read from `~/.claude/projects/` (read-only).

## Running as a systemd Service

```bash
# Create the service file
cat > ~/.config/systemd/user/ccbot.service << 'EOF'
[Unit]
Description=CCBot - Telegram bridge for Claude Code sessions

[Service]
ExecStart=/path/to/ccbot
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now ccbot.service

# View logs
journalctl --user -u ccbot.service -f
```

If using Doppler for secrets:
```ini
ExecStart=/usr/bin/doppler run --project myproject --config dev -- /path/to/ccbot
```

## Architecture

See [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) for a detailed technical deep-dive including:
- System diagram and data flow
- Module inventory with every class, function, and data structure
- State management and persistence
- Message processing pipeline
- Tmux integration details
- Notification filtering internals
- Callback data routing

## Development

```bash
# Clone and install dev dependencies
git clone https://github.com/six-ddc/ccmux.git
cd ccmux
uv sync --dev

# Run quality checks
uv run ruff check src/ tests/         # Lint
uv run ruff format src/ tests/        # Format
uv run pyright src/ccbot/             # Type check
uv run pytest                          # Run tests (177 tests)

# Restart after code changes
./scripts/restart.sh
```

## File Structure

```
src/ccbot/
├── __init__.py              # Package version
├── main.py                  # CLI entry: dispatch to hook or bot
├── hook.py                  # SessionStart hook: writes session_map.json
├── config.py                # Config singleton: env vars + notify.json
├── bot.py                   # Telegram handlers: commands, callbacks, messages
├── session.py               # State hub: bindings, sessions, history, offsets
├── session_monitor.py       # JSONL poller: detect new messages, emit events
├── monitor_state.py         # Byte offset persistence for incremental reads
├── tmux_manager.py          # libtmux wrapper: windows, keys, capture
├── transcript_parser.py     # JSONL parser: content types, tool pairing
├── terminal_parser.py       # Pane parser: interactive UI, status line
├── sync_skills.py           # ccbot-sync CLI: .claude/commands/ -> skills.json
├── markdown_v2.py           # Markdown -> Telegram MarkdownV2 conversion
├── telegram_sender.py       # Message splitting for 4096-char limit
├── screenshot.py            # Terminal text -> PNG with ANSI + font rendering
├── utils.py                 # ccbot_dir(), atomic_write_json(), JSONL helpers
├── fonts/                   # TTF fonts for screenshot rendering
└── handlers/
    ├── callback_data.py     # CB_* prefix constants for inline keyboards
    ├── message_queue.py     # Per-user FIFO queue with merge + rate limiting
    ├── message_sender.py    # safe_reply/safe_edit/safe_send with fallback
    ├── response_builder.py  # Format tool_use, thinking, text into pages
    ├── directory_browser.py # Dir browser + window picker inline keyboards
    ├── history.py           # Message history pagination
    ├── interactive_ui.py    # AskUserQuestion/ExitPlanMode/Permission handler
    ├── status_polling.py    # Background terminal status polling
    ├── resume.py            # /resume session picker with pagination
    └── cleanup.py           # Topic state cleanup on close
```
