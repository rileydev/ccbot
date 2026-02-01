# CLAUDE.md

## Development Principles

### No Message Truncation

Historical messages (tool_use summaries, tool_result text, user/assistant messages) are always kept in full — no character-level truncation at the parsing layer. Long text is handled exclusively at the send layer: `split_message` paginates by Telegram's 4096-character limit, with inline keyboard navigation.

### History Pagination Shows Latest First

`/history` defaults to the last page (newest messages). Users browse older content via the "◀ Older" button.

### Follow Telegram Bot Best Practices

Interaction design follows Telegram Bot platform best practices: prefer inline keyboards over reply keyboards; use `edit_message_text` for in-place updates instead of sending new messages; keep callback data compact (64-byte limit); use `answer_callback_query` for instant feedback.

### File Header Docstring Convention

Every Python source file must start with a module-level docstring (`"""..."""`) describing its core purpose. Requirements:

- **Purpose clear within 10 lines**: An AI or developer reading only the first 10 lines can determine the file's role, responsibilities, and key classes/functions.
- **Structure**: First line is a one-sentence summary; subsequent lines describe core responsibilities, key components (class/function names), and relationships with other modules.
- **Keep updated**: When a file undergoes major changes (adding/removing core features, changing module responsibilities, renaming key classes/functions), update the header docstring. Minor bug fixes or internal refactors do not require updates.

### Code Quality Checks

After every code change, run `pyright src/ccmux/` to check for type errors. Ensure 0 errors before committing.

### Unified MarkdownV2 Formatting

All messages sent to Telegram use `parse_mode="MarkdownV2"`. The `telegramify-markdown` library converts standard Markdown to Telegram MarkdownV2 format. All send/edit message calls must go through `_safe_reply`/`_safe_edit`/`_safe_send` helper functions, which handle MarkdownV2 conversion automatically and fall back to plain text on parse failure. Never call `reply_text`/`edit_message_text`/`send_message` directly.

### Window as the Core Unit

All logic (session listing, message sending, history viewing, notifications) operates on tmux windows as the core unit, not project directories (cwd). Window names default to the directory name (e.g., `project`). The same directory can have multiple windows (auto-suffixed, e.g., `project-2`), each independently associated with its own Claude session.

### User, Window, and Session Relationships

Three core entities and their mappings:

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│   User ID   │ ───▶ │ Window Name │ ───▶ │ Session ID  │
│  (Telegram) │      │   (tmux)    │      │  (Claude)   │
└─────────────┘      └─────────────┘      └─────────────┘
     active_sessions      session_map.json
     (memory + state.json)  (written by hook)
```

**Mapping 1: User → Window (active window)**

```python
# session.py: SessionManager
active_sessions: dict[int, str]  # user_id → window_name
```

- Storage: memory + `~/.ccmux/state.json`
- Written when: user selects a session via `/list` or creates a new one
- Property: **one user has exactly one active window** (guaranteed by dict key uniqueness)
- Purpose: route user messages to the correct tmux window

**Mapping 2: Window → Session (window-session binding)**

```python
# session_map.json (key format: "tmux_session:window_name")
{
  "ccmux:project": {"session_id": "uuid-xxx", "cwd": "/path/to/project"},
  "ccmux:project-2": {"session_id": "uuid-yyy", "cwd": "/path/to/project"}
}
```

- Storage: `~/.ccmux/session_map.json`
- Written when: Claude Code's `SessionStart` hook fires
- Property: one window maps to one session; session_id changes after `/clear`
- Purpose: SessionMonitor uses this mapping to decide which sessions to watch

**Outbound message flow**

```
User sends "hello"
    │
    ▼
active_sessions[user_id] → "project"  (get active window)
    │
    ▼
send_to_window("project", "hello")    (send to tmux)
```

**Inbound message flow**

```
SessionMonitor reads new message (session_id = "uuid-xxx")
    │
    ▼
Iterate active_sessions, find user whose window maps to this session
    │
    ▼
session_map["ccmux:project"].session_id == "uuid-xxx" ?
    │
    ▼
If user's active_window is "project", deliver message to user
Otherwise discard (user has switched to another window)
```

**Unread catch-up**: The system maintains per-user `user_window_offsets` (independent from SessionMonitor's offset), recording each user's last-read position per window. Messages produced while the user is on another window are not pushed in real time, but when the user switches back via `/list`, the unread range is automatically detected and displayed.

### Telegram Flood Control Protection

The bot implements send rate limiting to avoid triggering Telegram's flood control:
- Minimum 1.1-second interval between messages per user
- Status polling interval is 1 second (send layer has rate limiting protection)
- All `send_message` calls go through `_rate_limit_send()` which checks and waits

### Message Queue Architecture

The bot uses per-user message queues + worker pattern for all send tasks, ensuring:
- Messages are sent in receive order (FIFO)
- Status messages always follow content messages
- Multi-user concurrent processing without interference

**Message merging**: The worker automatically merges consecutive mergeable content messages on dequeue, reducing API calls:
- Content messages for the same window can be merged (including text, thinking)
- tool_use breaks the merge chain and is sent separately (message ID recorded for later editing)
- tool_result breaks the merge chain and is edited into the tool_use message (preventing order confusion)
- Merging stops when combined length exceeds 3800 characters (to avoid pagination)

### Status Message Handling

Status messages (Claude status line) use special handling to optimize user experience:

**Conversion**: The status message is edited into the first content message, reducing message count:
- When a status message exists, the first content message updates it via edit
- Subsequent content messages are sent as new messages

**Polling**: A background task polls terminal status for all active windows at 1-second intervals. Send-layer rate limiting ensures flood control is not triggered.

### Session Lifecycle Management

Session monitor tracks window → session_id mappings via `session_map.json` (written by hook):

**Startup cleanup**: On bot startup, all tracked sessions not present in session_map are cleaned up, preventing monitoring of closed sessions.

**Runtime change detection**: Each polling cycle checks for session_map changes:
- Window's session_id changed (e.g., after `/clear`) → clean up old session
- Window deleted → clean up corresponding session

### Performance Optimizations

**mtime cache**: The monitoring loop maintains an in-memory file mtime cache, skipping reads for unchanged files.

**Byte offset incremental reads**: Each tracked session records `last_byte_offset`, reading only new content. File truncation (offset > file_size) is detected and offset is auto-reset.

**Status deduplication**: The worker compares `last_text` when processing status updates; identical content skips the edit, reducing API calls.

### Service Restart

To restart the ccmux service after code changes, run `./scripts/restart.sh`. The script detects whether a running `uv run ccmux` process exists in the `__main__` window of tmux session `ccmux`, sends Ctrl-C to stop it, restarts, and outputs startup logs for confirmation.

### Hook Configuration

Auto-install: `ccmux hook --install`

Or manually configure in `~/.claude/settings.json`:

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
