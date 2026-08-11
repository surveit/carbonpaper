# Carbon Paper — reviewable AI workflows

Run data/OSINT pipelines as **workflows of typed, schema-validated stages** with
human-review gates and fully persisted runs — testable and reviewable, not a black box.

- What & why: [docs/overview.md](docs/overview.md)
- Code map: [docs/architecture.md](docs/architecture.md)
- Contributor guide / conventions: [AGENTS.md](AGENTS.md)

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock` — there is
no requirements.txt. `uv sync` builds `.venv` from the lock; `--frozen` makes a
lock that has drifted from `pyproject.toml` an error instead of a re-resolve.

```
uv sync --frozen
uv run python -m uvicorn app.main:app --port 8765   # web UI
uv run python -m app.cli <project>                  # run a project's workflow from the CLI
```

## Deploying to Fly.io

`Dockerfile` + `fly.toml` describe a single machine with one volume mounted at
`/data`. The Fly GitHub integration builds the Dockerfile on push, so the repo
carries no deploy workflow and no token.

State lives on the volume: `CARBON_PAPER_DB_PATH=/data/app.db` (the document
store) and `CARBON_PAPER_PROJECTS_DIR=/data/projects`. The frame store follows the
database path's own directory, so `CARBON_PAPER_FRAMES_ROOT` stays unset.
`docker-entrypoint.sh` creates both directories, runs `alembic upgrade head`, then
execs uvicorn on port 8080 — the migration is in the entrypoint rather than a
`release_command` because a release machine has no volume attached.

One-time setup for a new app:

```
fly volumes create carbonpaper_data --size 10 --region iad
fly secrets set ANTHROPIC_API_KEY=...
```

Credentials are a Fly secret, never a repo value and never `[env]` in `fly.toml`.
No app module reads them: the Claude Code CLI that `claude-agent-sdk` spawns
inherits the server's environment and authenticates from it, which is the one
seam either credential goes through. A subscription-authenticated deploy sets
`CLAUDE_CODE_OAUTH_TOKEN` instead and must NOT also set `ANTHROPIC_API_KEY` —
the API key outranks the OAuth token in the CLI's auth precedence, so setting
both silently bills the metered API (see `.github/workflows/live-llm-smoke.yml`,
which runs on the OAuth token for that reason). Either way it is one
`fly secrets set`, not a rebuild.

Without a credential the server still boots and serves; `llm_transform` stages
are what fail.
