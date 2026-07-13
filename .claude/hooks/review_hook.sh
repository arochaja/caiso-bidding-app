#!/usr/bin/env bash
# Stop-hook dispatcher for the Lever 2 clean-context AI review.
# Runs on EVERY turn end, so it must be fast: it does cheap guards and then
# launches review_worker.py detached, returning control to the CLI instantly.
set -euo pipefail

# 1) Recursion guard. The worker launches `claude`, which fires its own Stop
#    hook; that nested run inherits this env var and bails out here.
[ -n "${CAISO_REVIEW_RUNNING:-}" ] && exit 0

# 1b) Never run inside CI. The Claude GitHub Action loads this repo's
#     .claude/settings.json (its settingSources include "project"), so this
#     Stop hook would otherwise fire there and try to launch the local worker
#     via app/.venv, which doesn't exist on a CI runner. This is a local tool.
if [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ]; then
    exit 0
fi

# 2) Locate the repo.
PROJ="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
[ -z "$PROJ" ] && exit 0
cd "$PROJ" || exit 0

# 3) Only review when tracked Python files actually differ from the last commit.
git diff --quiet HEAD -- '*.py' 2>/dev/null && exit 0

mkdir -p "$PROJ/.claude/reviews"

# 4) Skip if this exact .py diff was already reviewed. Without this the hook
#    would re-review an unchanged working tree (e.g. long-lived WIP) every turn.
HASH="$(git diff HEAD -- '*.py' | shasum | awk '{print $1}')"
HASHFILE="$PROJ/.claude/reviews/.last-hash"
[ -f "$HASHFILE" ] && [ "$(cat "$HASHFILE")" = "$HASH" ] && exit 0

# 5) One review at a time — skip if the previous one is still running.
LOCK="$PROJ/.claude/reviews/.lock"
[ -f "$LOCK" ] && exit 0
touch "$LOCK"
echo "$HASH" > "$HASHFILE"

# 6) Fire the worker in the background and return immediately.
CAISO_REVIEW_RUNNING=1 nohup "$PROJ/app/.venv/bin/python" \
    "$PROJ/.claude/hooks/review_worker.py" >/dev/null 2>&1 &

exit 0
