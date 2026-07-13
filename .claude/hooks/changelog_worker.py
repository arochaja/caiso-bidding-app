#!/usr/bin/env python3
"""
Post-commit changelog worker → posts to Slack #all-caiso-code.

Launched detached by changelog_hook.sh after each commit. It:
  1. posts a plain-language changelog entry for the HEAD commit (files + an
     AI one-line summary of the diff), and
  2. if the commit made a STRUCTURAL change (new file, new pipeline stage, new
     dashboard page, or new top-level function), regenerates and re-posts a
     short refreshed codebase overview from the current structure.

Best-effort and non-blocking: never edits code, swallows its own errors.
"""

import json
import os
import pathlib
import re
import subprocess
import urllib.request


def project_dir() -> pathlib.Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return pathlib.Path(env)
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    ).stdout.strip()
    return pathlib.Path(top)


PROJ = project_dir()
WEBHOOK = PROJ / ".claude" / "slack-allchan-webhook"
LOCK = PROJ / ".claude" / "reviews" / ".changelog.lock"


def post(text: str) -> None:
    if not WEBHOOK.exists():
        return
    url = WEBHOOK.read_text().strip()
    if not url.startswith("https://hooks.slack.com/"):
        return
    body = json.dumps({"text": text.replace("**", "*")}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # best-effort notifier; never disrupt the commit flow


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=PROJ, capture_output=True, text=True).stdout


def claude(prompt: str, timeout: int) -> str:
    # Clean context; CAISO_REVIEW_RUNNING stops the Lever 2 Stop hook recursing.
    env = dict(os.environ, CAISO_REVIEW_RUNNING="1")
    try:
        res = subprocess.run(
            ["claude", "-p", prompt],
            cwd=PROJ,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (res.stdout or "").strip()
    except Exception:
        return ""


OVERVIEW_PROMPT = """\
Write a concise refreshed overview of this codebase for a Slack channel, in
Slack mrkdwn (use *single-asterisk bold*, `code`, and • bullets — NOT markdown
##/** ). 3 short sections, each a few lines: (1) what the app does, (2)
architecture / data flow, (3) code map (key files + what they do). Base it ONLY
on the structure summary below. Be accurate and brief. Output only the message.

Structure summary:
%s
"""


def structure_summary() -> str:
    """Cheap, dependency-free snapshot of the code's shape for regeneration."""
    parts = []
    for name in ("pipeline.py", "dashboard.py", "auth.py"):
        p = PROJ / "app" / name
        if not p.exists():
            continue
        text = p.read_text(errors="ignore")
        lines = text.splitlines()
        defs = [ln.strip() for ln in lines if re.match(r"def \w", ln)]
        stages = [ln for ln in lines if re.search(r"Stage \d", ln)]
        pages = [ln for ln in lines if re.search(r"PAGE \d|PAGE ==", ln)]
        parts.append(
            f"{name} ({len(lines)} lines): "
            f"defs={[d[:40] for d in defs][:12]} "
            + (f"stages={len(stages)} " if stages else "")
            + (f"pages={len(pages)}" if pages else "")
        )
    return "\n".join(parts)


def _run() -> None:
    sha = git("rev-parse", "--short", "HEAD").strip()
    subject = git("log", "-1", "--format=%s").strip()
    author = git("log", "-1", "--format=%an").strip()
    # HEAD~1 may not exist for the very first commit — guard it.
    if not git("rev-parse", "--verify", "HEAD~1").strip():
        return
    namestatus = git("diff", "--name-status", "HEAD~1", "HEAD").strip()
    stat = git("diff", "--stat", "HEAD~1", "HEAD").strip()
    diff = git("diff", "HEAD~1", "HEAD")
    if not namestatus:
        return  # empty/merge commit — nothing to report

    # --- structural-change detection (plain text, no AI) ---
    added = [ln.split("\t")[-1] for ln in namestatus.splitlines() if ln.startswith("A")]
    signals = []
    if added:
        signals.append(f"new file(s): {', '.join(added)}")
    diff_added = [ln for ln in diff.splitlines() if ln.startswith("+")]
    if any(re.match(r"\+def \w", ln) for ln in diff_added):
        signals.append("new top-level function")
    if any(re.search(r'\+.*Stage \d|\+log\("Stage', ln) for ln in diff_added):
        signals.append("new pipeline stage")
    if any(re.search(r"\+# PAGE|\+(if|elif) PAGE ==", ln) for ln in diff_added):
        signals.append("new dashboard page")
    structural = bool(signals)

    # --- 1) changelog entry (AI one-liner) ---
    oneliner = claude(
        "In ONE plain-language sentence (max 22 words) for a non-expert, say what "
        "this commit changes. Output only the sentence, no preamble.\n\n"
        f"Commit: {subject}\n\n{stat}\n\n{diff[:6000]}",
        timeout=120,
    ).split("\n")[0]

    files_line = ", ".join(f"`{ln.split(chr(9))[-1]}`" for ln in namestatus.splitlines()[:8])
    n_files = len(namestatus.splitlines())
    more = f" (+{n_files - 8} more)" if n_files > 8 else ""
    msg = (
        f":memo: *New commit* `{sha}` · {author}\n"
        f"*{subject}*\n"
        + (f"{oneliner}\n" if oneliner else "")
        + (f"_changed:_ {files_line}{more}" if files_line else "")
    )
    post(msg)

    # --- 2) refreshed overview on structural change ---
    if structural:
        post(
            f":arrows_counterclockwise: *Structural change* ({'; '.join(signals)}) — refreshing overview…"
        )
        overview = claude(OVERVIEW_PROMPT % structure_summary(), timeout=180)
        if overview:
            post(overview)


def main() -> None:
    try:
        _run()
    finally:
        try:
            LOCK.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
