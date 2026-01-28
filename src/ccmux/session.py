"""Claude Code session management.

Manages active sessions and provides access to session information.

State is anchored to tmux window names (stable), not project paths (cwd, volatile).
Each window stores:
  - session_id: The associated Claude session ID (persisted)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiofiles

from .config import config
from .tmux_manager import TmuxWindow, tmux_manager
from .transcript_parser import TranscriptParser
from .utils import atomic_write_json, read_cwd_from_jsonl

logger = logging.getLogger(__name__)


@dataclass
class WindowState:
    """Persistent state for a tmux window.

    Attributes:
        session_id: Associated Claude session ID (empty if not yet detected)
    """

    session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowState":
        return cls(
            session_id=data.get("session_id", ""),
        )


@dataclass
class ClaudeSession:
    """Information about a Claude Code session."""

    session_id: str
    summary: str
    project_path: str
    first_prompt: str
    message_count: int
    modified: str
    file_path: str

    @property
    def short_summary(self) -> str:
        if len(self.summary) > 30:
            return self.summary[:27] + "..."
        return self.summary

    @property
    def project_name(self) -> str:
        return Path(self.project_path).name



async def _read_user_messages_from_jsonl(file_path: str | Path) -> list[str]:
    """Read all user message texts from a JSONL file, ordered chronologically."""
    messages: list[str] = []
    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            async for line in f:
                data = TranscriptParser.parse_line(line)
                if not data:
                    continue
                if TranscriptParser.is_user_message(data):
                    parsed = TranscriptParser.parse_message(data)
                    if parsed and parsed.text.strip():
                        messages.append(parsed.text.strip())
    except OSError as e:
        logger.debug(f"Error reading {file_path}: {e}")
    return messages


async def _read_summary_from_jsonl(file_path: str | Path) -> str:
    """Read the latest summary entry from a JSONL file."""
    summary = ""
    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            async for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("type") == "summary":
                        s = data.get("summary", "")
                        if s:
                            summary = s
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return summary


def _normalize_path(path: str) -> str:
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError):
        return path


@dataclass
class SessionManager:
    """Manages active sessions for Claude Code.

    active_sessions: user_id -> tmux window_name
    window_states: window_name -> WindowState (session_id)
    """

    active_sessions: dict[int, str] = field(default_factory=dict)
    window_states: dict[str, WindowState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._load_state()

    def _save_state(self) -> None:
        state = {
            "active_sessions": {
                str(k): v for k, v in self.active_sessions.items()
            },
            "window_states": {
                k: v.to_dict() for k, v in self.window_states.items()
            },
        }
        atomic_write_json(config.state_file, state)

    def _load_state(self) -> None:
        """Load state synchronously during initialization."""
        if config.state_file.exists():
            try:
                state = json.loads(config.state_file.read_text())
                self.active_sessions = {
                    int(k): v
                    for k, v in state.get("active_sessions", {}).items()
                }
                self.window_states = {
                    k: WindowState.from_dict(v)
                    for k, v in state.get("window_states", {}).items()
                }
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Failed to load state: {e}")
                self.active_sessions = {}
                self.window_states = {}

    async def load_session_map(self) -> None:
        """Read session_map.json and update window_states with new session associations."""
        if not config.session_map_file.exists():
            return
        try:
            async with aiofiles.open(config.session_map_file, "r") as f:
                content = await f.read()
            session_map = json.loads(content)
        except (json.JSONDecodeError, OSError):
            return

        changed = False
        for window_name, info in session_map.items():
            new_sid = info.get("session_id", "")
            if not new_sid:
                continue
            state = self.get_window_state(window_name)
            if state.session_id != new_sid:
                logger.info(
                    f"Session map: window {window_name} changed "
                    f"{state.session_id} -> {new_sid}"
                )
                state.session_id = new_sid
                changed = True

        if changed:
            self._save_state()

    # --- Window state management ---

    def get_window_state(self, window_name: str) -> WindowState:
        """Get or create window state."""
        if window_name not in self.window_states:
            self.window_states[window_name] = WindowState()
        return self.window_states[window_name]

    def set_window_session(self, window_name: str, session_id: str) -> None:
        """Set the session ID for a window."""
        state = self.get_window_state(window_name)
        state.session_id = session_id
        self._save_state()
        logger.info(f"Set window {window_name} -> session {session_id}")

    def clear_window_session(self, window_name: str) -> None:
        """Clear session association for a window (e.g., after /clear command)."""
        state = self.get_window_state(window_name)
        state.session_id = ""
        self._save_state()
        logger.info(f"Cleared session for window {window_name}")

    async def _get_session_by_id(self, session_id: str) -> ClaudeSession | None:
        """Get a ClaudeSession by its ID."""
        all_sessions = await self.list_all_sessions()
        for session in all_sessions:
            if session.session_id == session_id:
                return session
        return None

    # --- Session index scanning ---

    async def list_all_sessions(self) -> list[ClaudeSession]:
        """List all Claude Code sessions sorted by modification time (newest first)."""
        sessions: list[ClaudeSession] = []

        if not config.claude_projects_path.exists():
            return sessions

        for project_dir in config.claude_projects_path.iterdir():
            if not project_dir.is_dir():
                continue

            index_file = project_dir / "sessions-index.json"
            if not index_file.exists():
                continue

            try:
                async with aiofiles.open(index_file, "r") as f:
                    content = await f.read()
                index_data = json.loads(content)
                for entry in index_data.get("entries", []):
                    full_path = entry.get("fullPath", "")
                    jsonl_summary = await _read_summary_from_jsonl(full_path) if full_path else ""
                    if not jsonl_summary and full_path:
                        user_msgs = await _read_user_messages_from_jsonl(full_path)
                        jsonl_summary = user_msgs[-1][:50] if user_msgs else ""
                    summary = jsonl_summary or entry.get("summary", "Untitled")
                    session = ClaudeSession(
                        session_id=entry.get("sessionId", ""),
                        summary=summary,
                        project_path=entry.get("projectPath", ""),
                        first_prompt=entry.get("firstPrompt", ""),
                        message_count=entry.get("messageCount", 0),
                        modified=entry.get("modified", ""),
                        file_path=entry.get("fullPath", ""),
                    )
                    if session.session_id:
                        sessions.append(session)
            except (json.JSONDecodeError, OSError) as e:
                logger.debug(f"Error reading index {index_file}: {e}")

        # Also pick up JSONL files not yet in any index
        for project_dir in config.claude_projects_path.iterdir():
            if not project_dir.is_dir():
                continue
            indexed_ids = {s.session_id for s in sessions}
            index_file = project_dir / "sessions-index.json"
            original_path = ""
            if index_file.exists():
                try:
                    async with aiofiles.open(index_file, "r") as f:
                        content = await f.read()
                    original_path = json.loads(content).get("originalPath", "")
                except (json.JSONDecodeError, OSError):
                    pass

            try:
                for jsonl_file in project_dir.glob("*.jsonl"):
                    sid = jsonl_file.stem
                    if sid in indexed_ids:
                        continue
                    project_path = original_path
                    if not project_path:
                        project_path = await asyncio.to_thread(read_cwd_from_jsonl, jsonl_file)
                    if not project_path:
                        dir_name = project_dir.name
                        if dir_name.startswith("-"):
                            project_path = dir_name.replace("-", "/")
                    user_messages = await _read_user_messages_from_jsonl(jsonl_file)
                    first_prompt = user_messages[0] if user_messages else ""
                    last_prompt = user_messages[-1] if user_messages else ""
                    jsonl_summary_unindexed = await _read_summary_from_jsonl(jsonl_file)
                    summary = (
                        jsonl_summary_unindexed
                        or last_prompt[:50]
                        or "(new session)"
                    )
                    sessions.append(ClaudeSession(
                        session_id=sid,
                        summary=summary,
                        project_path=project_path,
                        first_prompt=first_prompt,
                        message_count=len(user_messages),
                        modified="",
                        file_path=str(jsonl_file),
                    ))
            except OSError:
                pass

        sessions.sort(key=lambda s: s.modified, reverse=True)
        return sessions

    async def list_active_sessions(self) -> list[tuple[TmuxWindow, ClaudeSession | None]]:
        """List active tmux windows paired with their resolved sessions.

        Returns a list of (TmuxWindow, ClaudeSession | None) for each ccmux window.
        Multiple windows for the same directory are all included.
        """
        windows = await tmux_manager.list_windows()
        result: list[tuple[TmuxWindow, ClaudeSession | None]] = []
        for w in windows:
            session = await self.resolve_session_for_window(w.window_name)
            result.append((w, session))
        return result

    # --- Window → Session resolution ---

    async def resolve_session_for_window(self, window_name: str) -> ClaudeSession | None:
        """Resolve a tmux window to the best matching Claude session.

        Steps:
        1. Check if we have a persisted session_id for this window
        2. If yes, return that session (if it still exists)
        3. Fallback: find by cwd match
        """
        state = self.get_window_state(window_name)

        # If we have a persisted session_id, use it
        if state.session_id:
            session = await self._get_session_by_id(state.session_id)
            if session:
                return session
            # Session no longer exists, clear it
            logger.warning(f"Session {state.session_id} no longer exists for window {window_name}")
            state.session_id = ""
            self._save_state()

        # Fallback: find by cwd match
        window = await tmux_manager.find_window_by_name(window_name)
        if not window:
            return None

        cwd = _normalize_path(window.cwd)

        # Find all sessions for this cwd
        all_sessions = await self.list_all_sessions()
        candidates = [
            s for s in all_sessions
            if _normalize_path(s.project_path) == cwd and s.file_path
        ]

        if not candidates:
            return None

        # Return the most recent one
        return candidates[0]

    # --- Active session (by window_name) ---

    def set_active_window(self, user_id: int, window_name: str) -> None:
        logger.info(f"set_active_window: user_id={user_id}, window_name={window_name}")
        self.active_sessions[user_id] = window_name
        self._save_state()

    def get_active_window_name(self, user_id: int) -> str | None:
        return self.active_sessions.get(user_id)

    async def get_active_window(self, user_id: int) -> TmuxWindow | None:
        name = self.get_active_window_name(user_id)
        if not name:
            return None
        return await tmux_manager.find_window_by_name(name)

    async def get_active_cwd(self, user_id: int) -> str | None:
        window = await self.get_active_window(user_id)
        if window:
            return _normalize_path(window.cwd)
        return None

    def clear_active_session(self, user_id: int) -> None:
        if user_id in self.active_sessions:
            del self.active_sessions[user_id]
            self._save_state()

    # --- Tmux helpers ---

    async def send_to_window(self, window_name: str, text: str) -> tuple[bool, str]:
        """Send text to a tmux window by name and record for matching."""
        window = await tmux_manager.find_window_by_name(window_name)
        if not window:
            return False, "Window not found (may have been closed)"
        success = await tmux_manager.send_keys(window.window_id, text)
        if success:
            return True, f"Sent to {window_name}"
        return False, "Failed to send keys"

    async def send_to_active_session(self, user_id: int, text: str) -> tuple[bool, str]:
        name = self.get_active_window_name(user_id)
        if not name:
            return False, "No active session selected"
        return await self.send_to_window(name, text)

    # --- Message history ---

    async def get_recent_messages(
        self, window_name: str, count: int = 5, offset: int = 0
    ) -> tuple[list[dict], int]:
        """Get recent user/assistant messages for a window's session.

        Resolves window → session, then reads the JSONL.
        Returns (messages, total_count).
        """
        session = await self.resolve_session_for_window(window_name)
        if not session or not session.file_path:
            return [], 0

        file_path = Path(session.file_path)
        if not file_path.exists():
            return [], 0

        # Read all JSONL entries
        entries: list[dict] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for line in f:
                    data = TranscriptParser.parse_line(line)
                    if data:
                        entries.append(data)
        except OSError as e:
            logger.error(f"Error reading session file {file_path}: {e}")
            return [], 0

        parsed_entries, _ = TranscriptParser.parse_entries(entries)
        all_messages = [{"role": e.role, "text": e.text, "content_type": e.content_type} for e in parsed_entries]

        total = len(all_messages)
        if total == 0:
            return [], 0

        if count == 0:
            return all_messages, total

        end_idx = total - offset
        start_idx = max(0, end_idx - count)
        if end_idx <= 0:
            return [], total

        return all_messages[start_idx:end_idx], total


session_manager = SessionManager()
