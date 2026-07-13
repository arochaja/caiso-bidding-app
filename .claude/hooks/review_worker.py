#!/usr/bin/env python3
"""
Background worker for the Lever 2 "clean-context AI review" Stop hook.

Not meant to be run directly — the dispatcher (review_hook.sh) launches this
detached so the Claude Code turn returns instantly. This process then:

  1. builds a unified diff of working-tree changes to tracked .py files,
  2. asks a FRESH `claude` (headless, clean context) to review just that diff,
  3. writes the full findings to .claude/reviews/latest.md, and
  4. if there are findings, posts a short summary to Slack.

Advisory only: it never edits your code and never blocks anything.
"""

import json
import os
import pathlib
import subprocess
import sys
import urllib.request
from datetime import datetime


def project_dir() -> pathlib.Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return pathlib.Path(env)
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return pathlib.Path(top)


PROJ = project_dir()
REVIEW_DIR = PROJ / ".claude" / "reviews"
LOCK = REVIEW_DIR / ".lock"
OUT = REVIEW_DIR / "latest.md"
WEBHOOK_FILE = PROJ / ".claude" / "slack-webhook"


def cleanup() -> None:
    try:
        LOCK.unlink()
    except FileNotFoundError:
        pass


def post_slack(text: str) -> None:
    """Post plain text to the incoming webhook, if one is configured."""
    if not WEBHOOK_FILE.exists():
        return
    url = WEBHOOK_FILE.read_text().strip()
    if not url.startswith("https://hooks.slack.com/"):
        return
    # Slack mrkdwn uses *single* asterisks for bold; our review uses **double**.
    body = json.dumps({"text": text.replace("**", "*")}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # surface, don't crash the hook
        (REVIEW_DIR / "slack-error.log").write_text(f"{datetime.now()}: {e}\n")


REVIEW_PROMPT = """\
You are a meticulous senior code reviewer. Review ONLY the unified diff below —
working-tree changes to a Python data/analytics app (Streamlit + DuckDB).

Focus on things that require judgment: real correctness bugs, wrong assumptions,
unsafe SQL or input handling, silent data-loss, and off-by-one / edge-case errors.
Do NOT report style or formatting — a linter/formatter already handles that.

Respond in GitHub-flavored markdown, in EXACTLY this shape:

- FIRST line, nothing before it:
  `SUMMARY: N findings (H high, M medium, L low)`  — or  `SUMMARY: no findings`
- Then one bullet per finding:
  `- **[high|medium|low]** <file>:<line> — <one-line description of the problem>`
- Then an optional `### Detail` section with a short paragraph per finding.

Be concise and concrete. If nothing is genuinely wrong, output ONLY the SUMMARY
line. Here is the diff:

```diff
%s
```
"""


def main() -> int:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    try:
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--", "*.py"],
            cwd=PROJ,
            capture_output=True,
            text=True,
        ).stdout
        if not diff.strip():
            return 0  # nothing to review

        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=PROJ,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Clean-context review: a fresh headless claude that only sees the diff.
        # CAISO_REVIEW_RUNNING guards against the child's own Stop hook recursing.
        env = dict(os.environ, CAISO_REVIEW_RUNNING="1")
        res = subprocess.run(
            ["claude", "-p", REVIEW_PROMPT % diff],
            cwd=PROJ,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        review = (res.stdout or "").strip()
        if not review:
            review = "SUMMARY: review error\n\n" + (res.stderr or "no output")[:2000]

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        summary = review.splitlines()[0].replace("SUMMARY:", "").strip()
        OUT.write_text(
            f"# Code review — {stamp}\n"
            f"_branch `{branch}` · working-tree .py changes vs HEAD_\n\n"
            f"{review}\n"
        )

        # Only ping Slack when there's actually something to look at.
        has_findings = not review.lower().startswith("summary: no findings")
        if has_findings:
            # Keep Slack short: header + summary + the bullet lines only.
            bullets = "\n".join(ln for ln in review.splitlines() if ln.lstrip().startswith("- **["))
            post_slack(
                f":mag: *Code review* · `{branch}`\n"
                f"{summary}\n{bullets}\n"
                f"_full report: `.claude/reviews/latest.md`_"
            )
        return 0
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
