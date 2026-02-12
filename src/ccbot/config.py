"""Application configuration — reads env vars and exposes a singleton.

Loads TELEGRAM_BOT_TOKEN, ALLOWED_USERS, tmux/Claude paths, and
monitoring intervals from environment variables (with .env support).
.env loading priority: local .env (cwd) > $CCBOT_DIR/.env (default ~/.ccbot).
The module-level `config` instance is imported by nearly every other module.

Notification preferences are loaded from $CCBOT_DIR/notify.json.
If the file doesn't exist, it is created with defaults (everything on).

Key class: Config (singleton instantiated as `config`).
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .utils import ccbot_dir

logger = logging.getLogger(__name__)

# Default notification settings — all on for backward compatibility.
# Each key maps to a content_type from the transcript parser.
NOTIFY_DEFAULTS: dict[str, bool] = {
    "text": True,  # Claude's text responses (conversation output)
    "thinking": True,  # Internal reasoning / thinking blocks
    "tool_use": True,  # Tool call summaries (e.g. "Read(file.py)")
    "tool_result": True,  # Tool output (e.g. "Read 50 lines")
    "tool_error": True,  # Errors from tool execution
    "local_command": True,  # Slash command results (e.g. /commit)
    "user": True,  # User messages echoed back (👤 prefix)
}


class NotifyConfig:
    """Per-content-type notification toggle loaded from notify.json."""

    def __init__(self, config_dir: Path) -> None:
        self._file = config_dir / "notify.json"
        self._settings: dict[str, bool] = dict(NOTIFY_DEFAULTS)
        self._load()

    def _load(self) -> None:
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for key in NOTIFY_DEFAULTS:
                        if key in data and isinstance(data[key], bool):
                            self._settings[key] = data[key]
                logger.debug("Loaded notify config from %s", self._file)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to read notify.json: %s (using defaults)", e)
        else:
            # Create the file with defaults so the user can edit it
            self._save()
            logger.info("Created default notify.json at %s", self._file)

    def _save(self) -> None:
        try:
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2)
                f.write("\n")
        except OSError as e:
            logger.error("Failed to write notify.json: %s", e)

    def should_notify(self, content_type: str, *, is_error: bool = False) -> bool:
        """Check whether a message with this content_type should be sent.

        Tool errors are controlled by the separate 'tool_error' toggle,
        so an error in a tool_result can still be shown even if
        'tool_result' is off.
        """
        if is_error:
            return self._settings.get("tool_error", True)
        return self._settings.get(content_type, True)

    def summary(self) -> str:
        """One-line summary for logging."""
        on = [k for k, v in self._settings.items() if v]
        off = [k for k, v in self._settings.items() if not v]
        return f"on={on}, off={off}"


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccbot_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir
        # load_dotenv default override=False means first-loaded wins
        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded env from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded env from %s", global_env)

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        try:
            self.allowed_users: set[int] = {
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"ALLOWED_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e

        # Tmux session name and window naming
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "ccbot")
        self.tmux_main_window_name = "__main__"

        # Claude command to run in new windows
        self.claude_command = os.getenv("CLAUDE_COMMAND", "claude")

        # All state files live under config_dir
        self.state_file = self.config_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"

        # Claude Code session monitoring configuration
        self.claude_projects_path = Path.home() / ".claude" / "projects"
        self.monitor_poll_interval = float(os.getenv("MONITOR_POLL_INTERVAL", "2.0"))

        # Display user messages in history and real-time notifications
        # When True, user messages are shown with a 👤 prefix
        self.show_user_messages = True

        # Per-content-type notification filtering
        self.notify = NotifyConfig(self.config_dir)

        logger.debug(
            "Config initialized: dir=%s, token=%s..., allowed_users=%d, "
            "tmux_session=%s, notify=[%s]",
            self.config_dir,
            self.telegram_bot_token[:8],
            len(self.allowed_users),
            self.tmux_session_name,
            self.notify.summary(),
        )

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if a user is in the allowed list."""
        return user_id in self.allowed_users


config = Config()
