# Self-hosting — the file endpoint and the deploy

Running it for yourself is [getting-started.md](getting-started.md), which also says
what `~/.carbonpaper/` holds and which environment variables repoint it. This covers
what only a server serving other people needs.

## Getting a data file in

A run reads its inputs off the server's disk by absolute path. A browser hands over
bytes and never a path, so the run form's Browse… posts the file to the server, which
stores it in a directory named by the record it writes for it, and hands the path back.
That endpoint is plain multipart and takes any caller — an agent that can run `curl`
needs no browser:

```
curl -F file=@2026-lobbying.csv http://localhost:8765/project/<project>/files
{"ok":true,"file_id":"c41d…","filename":"2026-lobbying.csv","bytes":9470974,
 "path":"~/.carbonpaper/files/c41d…/2026-lobbying.csv"}
```

`file_id` is what names this file to a later run — `run_workflow`'s `files`, and the
run form's picker. The same bytes sent twice are two records over two copies: each send
keeps its own account of where the file came from and when, which one shared copy could
not. One store serves the workspace, beside the document store and the frames, and a
record says which project claims each file. One
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
