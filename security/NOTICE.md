# Attribution

These opengrep rules are vendored verbatim from https://github.com/AikidoSec/opengrep-rules
(MIT License, © Aikido Security), developed by Aikido's internal security research team.
Upstream keeps the canonical copies; re-sync from there when it changes.

- `github_workflow_prompt_injection.yaml` — user input concatenated into an AI prompt inside a
  GitHub workflow, or Claude Code Actions `claude_args` with `--allowedTools` while
  `allowed_non_write_users: "*"` (untrusted users can steer the agent).
- `npm_staged_publishing_missing.yaml` — `npm publish` without staged publishing.
