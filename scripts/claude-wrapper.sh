#!/usr/bin/env bash
# Claude Code wrapper with SIGHUP reload support.
#
# Runs Claude Code in a loop. On normal exit, the wrapper exits too.
# On SIGHUP (exit code 129), it restarts Claude with --resume so the
# conversation continues with freshly loaded MCP servers, hooks, and settings.
#
# Usage:
#   Set CLAUDE_COMMAND to this script in ~/.ccbot/.env:
#     CLAUDE_COMMAND=/path/to/claude-wrapper.sh
#
#   Or with --dangerously-skip-permissions for unattended VPS usage:
#     CLAUDE_COMMAND="IS_SANDBOX=1 /path/to/claude-wrapper.sh"

set -euo pipefail

CLAUDE_BIN="${CLAUDE_BIN:-claude}"
CLAUDE_EXTRA_ARGS="${CLAUDE_EXTRA_ARGS:---dangerously-skip-permissions}"

session_id=""

while true; do
    # Build command
    args=($CLAUDE_EXTRA_ARGS)
    if [[ -n "$session_id" ]]; then
        args+=(--resume "$session_id")
    fi

    # Run Claude Code, capturing the session ID from its output
    # Claude prints the session ID on startup; we also try to detect it
    # from the JSONL session files after exit
    set +e
    "$CLAUDE_BIN" "${args[@]}"
    exit_code=$?
    set -e

    if [[ $exit_code -eq 129 ]]; then
        # SIGHUP received — reload requested
        # Try to find the most recent session ID from Claude's projects dir
        latest_session=$(find ~/.claude/projects/ -name "*.jsonl" -newer /tmp/.claude-wrapper-start 2>/dev/null \
            | sort -t/ -k1 | tail -1 | xargs -r basename 2>/dev/null | sed 's/\.jsonl$//')

        if [[ -n "$latest_session" ]]; then
            session_id="$latest_session"
            echo "[claude-wrapper] Reloading with session: $session_id"
        else
            echo "[claude-wrapper] Reloading (no session ID found, starting fresh)"
            session_id=""
        fi

        # Touch marker for next cycle
        touch /tmp/.claude-wrapper-start
        continue
    fi

    # Normal exit — stop the loop
    echo "[claude-wrapper] Claude exited with code $exit_code"
    exit $exit_code
done
