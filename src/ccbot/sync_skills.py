"""Generate ~/.ccbot/skills.json from a project's .claude/commands/ directory.

Scans .claude/commands/ for markdown files with YAML frontmatter,
extracts command names and descriptions, and writes a JSON mapping
of Telegram-safe command names to Claude Code slash commands.

The generated skills.json is loaded by ccbot at startup to populate
the Telegram bot menu and translate commands before forwarding.

Usage (as CLI):
    ccbot-sync [project_dir]          # defaults to cwd
    ccbot-sync /data/projects/my-app

Usage (as library):
    from ccbot.sync_skills import scan_commands
    skills = scan_commands(Path("/my/project"))
"""

import json
import re
import sys
from pathlib import Path

from .utils import ccbot_dir

# Telegram command names: lowercase letters, digits, underscores only, 1-32 chars.
_TG_CMD_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Commands handled natively by the bot or CC_COMMANDS — skip these.
_SKIP_NAMES = frozenset({
    "start", "history", "resume", "screenshot", "esc", "kill",
    "clear", "compact", "cost", "help", "memory",
})


def _parse_frontmatter(path: Path) -> dict[str, str]:
    """Extract YAML frontmatter key-value pairs from a markdown file.

    Handles single-line values and multi-line >- continuation blocks.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    if not text.startswith("---"):
        return {}

    end = text.find("---", 3)
    if end < 0:
        return {}

    block = text[3:end].strip()
    result: dict[str, str] = {}
    current_key = ""
    current_val = ""

    for line in block.splitlines():
        m = re.match(r"^(\w+):\s*(>-|.*)", line)
        if m:
            if current_key:
                result[current_key] = current_val.strip()
            current_key = m.group(1)
            val = m.group(2)
            current_val = "" if val == ">-" else val
        elif current_key and line.startswith("  "):
            current_val += " " + line.strip()

    if current_key:
        result[current_key] = current_val.strip()

    return result


def to_telegram_name(cc_command: str) -> str:
    """Convert a Claude Code command name to a Telegram-safe name.

    Examples:
        /gsd:progress     → gsd_progress
        /review-pr        → review_pr
        /speckit.analyze  → speckit_analyze
    """
    name = cc_command.lstrip("/")
    name = name.replace(":", "_").replace("-", "_").replace(".", "_")
    return name.lower()


def scan_commands(project_dir: Path) -> dict[str, dict[str, str]]:
    """Scan .claude/commands/ and return skill mappings.

    Returns:
        {telegram_name: {"command": "/gsd:progress", "description": "..."}, ...}
    """
    commands_dir = project_dir / ".claude" / "commands"
    if not commands_dir.is_dir():
        return {}

    skills: dict[str, dict[str, str]] = {}

    for md_file in sorted(commands_dir.rglob("*.md")):
        if md_file.name.endswith(".bak"):
            continue

        fm = _parse_frontmatter(md_file)

        # Derive the Claude Code command name
        if "name" in fm:
            cc_command = "/" + fm["name"]
        else:
            rel = md_file.relative_to(commands_dir)
            stem = rel.with_suffix("").as_posix()
            cc_command = "/" + stem.replace("/", ":")

        description = fm.get("description", "")
        if len(description) > 200:
            description = description[:197] + "..."

        tg_name = to_telegram_name(cc_command)

        if not _TG_CMD_RE.match(tg_name):
            print(f"  skip: {cc_command} → '{tg_name}' (invalid Telegram name)",
                  file=sys.stderr)
            continue

        if tg_name in _SKIP_NAMES:
            continue

        skills[tg_name] = {
            "command": cc_command,
            "description": description,
        }

    return skills


def main() -> None:
    """CLI entry point for ccbot-sync."""
    project_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()

    if not project_dir.is_dir():
        print(f"Error: {project_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    commands_dir = project_dir / ".claude" / "commands"
    if not commands_dir.is_dir():
        print(f"Error: {commands_dir} not found", file=sys.stderr)
        print("Is this a project with Claude Code commands?", file=sys.stderr)
        sys.exit(1)

    skills = scan_commands(project_dir)

    out_dir = ccbot_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "skills.json"

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(skills, f, indent=2)
        f.write("\n")

    print(f"Wrote {len(skills)} skill commands to {out_file}")

    for tg_name, info in list(skills.items())[:5]:
        print(f"  /{tg_name} → {info['command']}")
    if len(skills) > 5:
        print(f"  ... and {len(skills) - 5} more")


if __name__ == "__main__":
    main()
