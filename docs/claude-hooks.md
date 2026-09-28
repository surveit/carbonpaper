# Claude Code hooks

`scripts/claude_hooks/require_pr_url.py` is a Stop hook. When a turn ran a `gh pr create|edit|comment|merge|ready|review|close|reopen`, or a writing `gh api .../pulls/...` call, it blocks the stop until the final text holds each changed PR's `https://github.com/<owner>/<repo>/pull/<n>` URL. It stays silent when `stop_hook_active` is set or its input will not parse.

It is not installed. To install, merge into `~/.claude/settings.json`:

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 /Users/shuhan/carbonpaper/scripts/claude_hooks/require_pr_url.py", "timeout": 10}]}]}}
```
