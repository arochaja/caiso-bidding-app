#!/usr/bin/env bash
# post-commit dispatcher → backgrounds changelog_worker.py so the commit returns
# instantly. The worker posts a changelog entry (+ refreshed overview on a
# structural change) to Slack #all-caiso-code.
set -euo pipefail

# Recursion guard: if a nested `claude -p` (which sets this) ever commits, bail.
[ -n "${CAISO_REVIEW_RUNNING:-}" ] && exit 0

# Never run inside CI.
if [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ]; then
    exit 0
fi

PROJ="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
[ -z "$PROJ" ] && exit 0
cd "$PROJ" || exit 0

# No webhook configured → nothing to do.
[ -f "$PROJ/.claude/slack-allchan-webhook" ] || exit 0

# Single-flight lock (worker clears it in a finally).
mkdir -p "$PROJ/.claude/reviews"
LOCK="$PROJ/.claude/reviews/.changelog.lock"
[ -f "$LOCK" ] && exit 0
touch "$LOCK"

# Fire the worker detached; return to the shell immediately.
CAISO_REVIEW_RUNNING=1 nohup "$PROJ/app/.venv/bin/python" \
    "$PROJ/.claude/hooks/changelog_worker.py" >/dev/null 2>&1 &

exit 0
