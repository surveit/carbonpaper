# Claude Code hooks

`scripts/claude_hooks/require_pr_url.py` is a Stop hook. A turn starts at the last human message, task notification, peer message or Stop. If the turn ran `gh pr create|edit|comment|merge|ready|review|close|reopen`, or a writing `gh api .../pulls/...` call, the hook blocks the stop once until the final text holds each changed PR's URL; the next Stop carries `stop_hook_active` and passes. A merge run inside a script is invisible to it.

Not installed. To install, copy it, then merge the JSON into `~/.claude/settings.json`:

```sh
mkdir -p ~/.claude/hooks && cp scripts/claude_hooks/require_pr_url.py ~/.claude/hooks/
```

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "test -f ~/.claude/hooks/require_pr_url.py || exit 0; python3 ~/.claude/hooks/require_pr_url.py", "timeout": 10}]}]}}
```

`uv run python -m scripts.claude_hooks.replay_require_pr_url` replays it at each recorded Stop.
