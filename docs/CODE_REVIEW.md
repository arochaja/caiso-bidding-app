# Automated code-review guardrails

This repo layers five automated guardrails so review scales with AI-generated
code. Each catches a different class of problem at a different moment.

| # | Lever | Runs on | Catches |
|---|-------|---------|---------|
| 1 | Pre-commit hooks | every `git commit` | style, unused imports, SQL-injection patterns (ruff) |
| 2 | Local AI review | end of a Claude Code turn | logic bugs, bad assumptions (clean-context review → Slack `#code-reviews`) |
| 3 | CI PR review | pull request | correctness/security review on the diff (inline + summary) |
| 4 | `/explain-pr` | on demand | comprehension — narrates a diff back in plain language |
| 5 | Review the context | after each CI review | the agent's full reasoning, kept as an artifact + linked in Slack `#all-caiso-code` |

## Setup from a fresh clone

```bash
app/.venv/bin/pip install -r requirements-dev.txt
pre-commit install                       # Lever 1 (pre-commit)
pre-commit install --hook-type post-commit   # continuous changelog → Slack
```

Local Slack posts (Levers 2 & the changelog) read gitignored webhook files
(`.claude/slack-webhook`, `.claude/slack-allchan-webhook`). CI posts use the
`SLACK_ALLCHAN_WEBHOOK` and `CLAUDE_CODE_OAUTH_TOKEN` repo secrets.

## Notes

- Style is owned by ruff (Lever 1); the AI levers deliberately skip it.
- The CI token (`CLAUDE_CODE_OAUTH_TOKEN`) must stay valid — refresh with
  `claude setup-token` in a real terminal, then `gh secret set`.
