---
description: Narrate the current changes back in plain language (with an optional fairy tale) to sanity-check them
argument-hint: "[PR number | git ref]  — optional; defaults to this branch vs main"
allowed-tools: Bash(git diff:*), Bash(git log:*), Bash(git status:*), Bash(git branch:*), Bash(gh pr diff:*), Bash(gh pr view:*)
---

You are helping the reader sanity-check a set of code changes by **explaining them
back** — the "explain the PR" trick. A short, faithful retelling forces the changes
into concrete terms, so anything wrong or unintended jumps out at a glance.

## 1. Gather the diff to explain (read-only)

Decide what to explain from the argument `$ARGUMENTS`:

- **A PR number** (e.g. `3`): run `gh pr diff $ARGUMENTS`.
- **A git ref / branch name**: run `git diff <ref>...HEAD`.
- **Empty**: explain everything this branch changes versus the default branch —
  run `git diff main...HEAD` (committed) **and** `git diff` (uncommitted working
  tree). If the repo's default branch isn't `main`, use the correct one
  (`git branch -r` / `git remote show origin` to check).

Only read. Never edit, stage, commit, or push anything here.

## 2. Explain it back — two parts

**Part A — The fairy tale (short & fun).** Retell what these changes *do* as a brief
whimsical story: the files are characters, the changes are the plot. 4–8 sentences.
The whimsy isn't the point — a faithful story forces the changes into plain terms the
reader can check. If the story has to narrate something that sounds pointless, wrong,
or contradictory, **that's a signal** — surface it.

**Part B — Plain-language changelog.** Then a grounded, skimmable list: group by file,
one line per meaningful change — *what* changed and *why it appears to be there*,
inferred from the diff. Ignore pure formatting/whitespace noise (say "reformatting"
once and move on).

**🚩 Worth a second look.** If anything looks unintended, risky, inconsistent, or you
can't explain it from the diff alone, list it here with the `file:line`. If nothing,
omit this section.

Be concise and strictly faithful — never invent changes that aren't in the diff. If
there are no changes to explain, just say so.
