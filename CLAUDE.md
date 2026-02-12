# CLAUDE.md

CCBot — Telegram bot that bridges Telegram Forum topics to Claude Code sessions via tmux windows. Each topic is bound to one tmux window running one Claude Code instance.

Tech stack: Python 3.12+, python-telegram-bot, libtmux, Pillow, uv.

## Quick Reference

- **README.md** — User-facing docs: setup, configuration, usage, features
- **doc/ARCHITECTURE.md** — Complete technical deep-dive: every module, class, data flow, state file
- **.claude/rules/architecture.md** — System diagram
- **.claude/rules/topic-architecture.md** — Topic→window→session mapping
- **.claude/rules/message-handling.md** — Message queue, merging, rate limiting

## Common Commands

```bash
uv run ruff check src/ tests/         # Lint — MUST pass before committing
uv run ruff format src/ tests/        # Format — auto-fix, then verify with --check
uv run pyright src/ccbot/             # Type check — MUST be 0 errors before committing
uv run pytest                          # Run tests (177 tests)
./scripts/restart.sh                  # Restart the ccbot service after code changes
ccbot hook --install                  # Auto-install Claude Code SessionStart hook
ccbot-sync /path/to/project           # Sync skills from .claude/commands/ to skills.json
```

## Entry Points

| CLI Command | Module | Description |
|---|---|---|
| `ccbot` | `main.py:main()` | Start Telegram bot |
| `ccbot hook` | `hook.py:hook_main()` | SessionStart hook handler |
| `ccbot hook --install` | `hook.py:hook_main()` | Auto-install hook |
| `ccbot-sync [dir]` | `sync_skills.py:main()` | Generate skills.json |

## Core Design Constraints

- **1 Topic = 1 Window = 1 Session** — all internal routing keyed by tmux window ID (`@0`, `@12`), not window name. Window names kept as display names. Same directory can have multiple windows.
- **Topic-only** — no backward-compat for non-topic mode. No `active_sessions`, no `/list`, no General topic routing.
- **No message truncation** at parse layer — splitting only at send layer (`split_message`, 4096 char limit).
- **MarkdownV2 only** — use `safe_reply`/`safe_edit`/`safe_send` helpers (auto fallback to plain text). Internal queue/UI code calls bot API directly with its own fallback.
- **Hook-based session tracking** — `SessionStart` hook writes `session_map.json`; monitor polls it to detect session changes.
- **Message queue per user** — FIFO ordering, message merging (3800 char limit), tool_use/tool_result pairing.
- **Rate limiting** — 1.1s minimum interval between messages per user via `rate_limit_send()`.
- **Callback data < 64 bytes** — use index-based references for long values.

## Module Map

```
src/ccbot/
├── main.py              # CLI entry: dispatch to hook or bot
├── config.py            # Config singleton + NotifyConfig (env + notify.json)
├── bot.py               # All Telegram handlers, skill loading, lifecycle
├── session.py           # State hub: bindings, sessions, history, offsets
├── session_monitor.py   # JSONL poller: byte-offset incremental reads
├── tmux_manager.py      # libtmux wrapper: windows, keys, capture
├── hook.py              # SessionStart hook: writes session_map.json
├── sync_skills.py       # ccbot-sync: .claude/commands/ → skills.json
├── transcript_parser.py # JSONL parser: content types, tool pairing
├── terminal_parser.py   # Pane parser: interactive UI, status line
├── monitor_state.py     # Byte offset persistence
├── utils.py             # ccbot_dir(), atomic_write_json()
├── markdown_v2.py       # MD → MarkdownV2 conversion
├── telegram_sender.py   # Message splitting (4096 limit)
├── screenshot.py        # Terminal text → PNG rendering
└── handlers/
    ├── callback_data.py     # CB_* prefix constants
    ├── message_queue.py     # Per-user FIFO queue + worker
    ├── message_sender.py    # safe_reply/safe_edit/safe_send
    ├── response_builder.py  # Format responses into pages
    ├── directory_browser.py # Dir browser + window picker UIs
    ├── history.py           # Message history pagination
    ├── interactive_ui.py    # AskUserQuestion/ExitPlanMode handler
    ├── status_polling.py    # Background terminal status polling
    ├── resume.py            # /resume session picker
    └── cleanup.py           # Topic state cleanup
```

## Singletons

| Singleton | Module | Description |
|---|---|---|
| `config` | config.py | Application configuration |
| `session_manager` | session.py | State management |
| `tmux_manager` | tmux_manager.py | Tmux operations |

## Key Data Structures

| Class | Module | Purpose |
|---|---|---|
| `NewMessage` | session_monitor.py | Detected message from JSONL polling |
| `WindowState` | session.py | Per-window state (session_id, cwd, name) |
| `ClaudeSession` | session.py | Session metadata (file_path, summary) |
| `TmuxWindow` | tmux_manager.py | Tmux window info (id, name, cwd) |
| `SessionSummary` | handlers/resume.py | Past session for /resume picker |
| `MessageTask` | handlers/message_queue.py | Queued message for delivery |
| `ParsedEntry` | transcript_parser.py | Parsed JSONL content |
| `TrackedSession` | monitor_state.py | Byte offset tracking |

## State Files (~/.ccbot/)

| File | Written By | Purpose |
|---|---|---|
| `state.json` | Bot | Thread bindings, window states, read offsets |
| `session_map.json` | Hook | Window ID → session ID mappings |
| `monitor_state.json` | Monitor | JSONL byte offsets |
| `notify.json` | User/auto | Notification filtering toggles |
| `skills.json` | ccbot-sync | Telegram command → Claude command mappings |

## Code Conventions

- Every `.py` file starts with a module-level docstring: purpose clear within 10 lines, one-sentence summary first line, then core responsibilities and key components.
- Telegram interaction: prefer inline keyboards over reply keyboards; use `edit_message_text` for in-place updates; keep callback data under 64 bytes; use `answer_callback_query` for instant feedback.
- All blocking libtmux calls wrapped in `asyncio.to_thread()`.
- `hook.py` must NOT import `config.py` (hooks run without bot env vars).

## Notification Filtering

Controlled via `~/.ccbot/notify.json`. Each content type individually toggleable. `tool_error` is independent of `tool_result`. Interactive prompts (AskUserQuestion, ExitPlanMode) always pass through regardless of settings.

## Skill Command System

- Hardcoded defaults in `_DEFAULT_SKILL_COMMANDS` (bot.py) cover commands without `.claude/commands/` files (beads, etc.)
- `~/.ccbot/skills.json` generated by `ccbot-sync` from project's `.claude/commands/` frontmatter
- On startup, `_load_skill_commands()` merges defaults + skills.json
- `_SKILL_TRANSLATE` maps Telegram names to Claude commands: `gsd_progress` → `/gsd:progress`
- Translation applied in `forward_command_handler()` before forwarding to tmux
