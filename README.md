# Carbon Paper — reviewable AI workflows

Run data/OSINT pipelines as **workflows of typed, schema-validated stages** with
human-review gates and fully persisted runs — testable and reviewable, not a black box.

```
./start
```

One command from a fresh clone to `http://127.0.0.1:8765`, on a machine that
brings only `git`, `curl` and `bash`: `./start` installs uv if it is missing,
builds `.venv` from `uv.lock`, migrates the store, and serves.
[docs/getting-started.md](docs/getting-started.md) covers signing in so the
`llm_transform` stages run, and the gates to run before you push.

- What & why: [docs/overview.md](docs/overview.md)
- Code map: [docs/architecture.md](docs/architecture.md)
- Contributor guide / conventions: [AGENTS.md](AGENTS.md)

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock` — there is
no requirements.txt. `uv sync` builds `.venv` from the lock; `--frozen` makes a
lock that has drifted from `pyproject.toml` an error instead of a re-resolve.
Underneath `./start`, and for a workflow run with no UI at all:

```
uv sync --frozen
uv run python -m uvicorn app.main:app --port 8765   # web UI
uv run python -m app.cli <project>                  # run a project's workflow from the CLI
```

Local state lives in `~/.carbonpaper/` — `app.db` (the document store), `frames/`
and `examples/` (the project working copies) — so every checkout and worktree
reads and writes the one store. `CARBON_PAPER_DB_PATH` and
`CARBON_PAPER_PROJECTS_DIR` repoint it, which is how the deploy below pins `/data`.

## Getting a data file in

A run reads its inputs off the server's disk by absolute path. A browser hands over
bytes and never a path, so the run form's Browse… posts the file to the server, which
stores it under the hash of its own contents and hands the path back. That endpoint is
plain multipart and takes any caller — an agent that can run `curl` needs no browser:

```
curl -F file=@2026-lobbying.csv http://localhost:8765/project/<project>/files
{"ok":true,"sha256":"a3f9…","filename":"2026-lobbying.csv","bytes":9470974,
 "path":"~/.carbonpaper/files/a3f9…/2026-lobbying.csv"}
```

The same bytes sent twice are one copy — one store serves the workspace, beside the
document store and the frames, and a record says which project claims each file. One
file may be up to 512MB and the store 4GB in total; `CARBON_PAPER_MAX_UPLOAD_BYTES` and
`CARBON_PAPER_FILES_QUOTA_BYTES` raise those on a bigger machine, and
`CARBON_PAPER_FILES_ROOT` repoints the store. The per-file ceiling is what a run can
load into memory, not what the disk holds — `input_data` hands a csv/json/xlsx source
to pandas whole.

Nothing authenticates this endpoint, so a hosted instance is one tester's instance.

## Deploying to Fly.io

`Dockerfile` + `fly.toml` describe a single machine with one volume mounted at
`/data`. The Fly GitHub integration builds the Dockerfile on push, so the repo
carries no deploy workflow and no token.

State lives on the volume: `CARBON_PAPER_DB_PATH=/data/app.db` (the document
store) and `CARBON_PAPER_PROJECTS_DIR=/data/projects`. The frame store follows the
database path's own directory, so `CARBON_PAPER_FRAMES_ROOT` stays unset.
`CLAUDE_CONFIG_DIR=/data/claude` puts a third store there: the Claude Code CLI
writes each chat's transcript under its config dir, and a chat resumes by an id
the document store holds, so leaving the transcripts in the image ended every
deploy with resume tokens naming sessions the CLI had thrown away.
`docker-entrypoint.sh` creates the directories, runs `alembic upgrade head`, then
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
