# Deploy — one server per tester

carbonpaper deploys as ONE Fly.io app PER TESTER, not one shared server. Each
tester gets their own machine, their own file-backed state, and their own
spend-capped API key — so one tester's runs, uploads, and LLM spend never
touch another's, and revoking a tester is deleting one app.

The pieces:

- `Dockerfile` — python 3.12 slim + the runtime requirements, plus Node LTS
  and the `@anthropic-ai/claude-code` package so the `claude` CLI is on PATH
  (the `llm_transform` backend shells out to it via `claude-agent-sdk` and
  raises without it — see `app/runtime/options.py`). Serves
  `uvicorn app.main:app` on port 8080 with state under `/data` and
  `CARBONPAPER_SEED_DEMO=1`, so a fresh instance boots with the bundled
  example project already imported.
- `fly.toml` — the app template the stamp copies: a single shared-cpu-1x /
  1GB machine, `auto_stop_machines = "stop"` + `auto_start_machines = true`
  with `min_machines_running = 0`, so an idle instance costs nothing and
  wakes on the next request.
- `tools/stamp_tester.sh` — mints one tester's app.

## Stamping a tester

```
tools/stamp_tester.sh <tester-name> <anthropic-api-key>
```

The script refuses to run without both arguments. It registers
`carbonpaper-<tester-name>` from the repo's `fly.toml`
(`fly launch --no-deploy --copy-config --yes`), stores the key as the app's
`ANTHROPIC_API_KEY` secret BEFORE the first deploy (so the first boot already
holds it), deploys, and prints `https://carbonpaper-<tester-name>.fly.dev`.
The key goes only to `fly secrets set`; the script never prints it.

## Per-tester keys and the spend cap

Each tester's key comes from a spend-capped workspace in the Anthropic
Console: create a workspace for the testing program, set its monthly spend
limit there, and mint one API key per tester inside it. The key lives only as
that one app's Fly secret, so a leaked or overspending key is revoked in the
Console and rotated with `fly secrets set` on the one affected app.

## State is ephemeral by default

`/data` (the projects directory, the SQLite KV store, and the parquet frames
beside it) sits on the machine's root filesystem. A redeploy or machine
replacement resets it to a fresh instance — which then re-seeds the demo
project and nothing else. This is deliberate: tester instances are disposable,
and there is no shared database to migrate or back up.

An instance that should keep its state across restarts opts in with a Fly
volume mounted on `/data`; the `[mounts]` block to add and the
`fly volumes create` command are in the comment at the top of `fly.toml`.

## Getting work out: the export endpoint

`GET /project/{project}/export.zip` downloads a project's whole on-disk tree
as one zip — `compiled/`, `schemas/`, `document.md`, `project.json`, the
version history, `runs/` (manifests, `outputs/*.parquet`, artifacts), and
`uploads/`. This is how a tester's work leaves an instance before it is torn
down: download the zip, and the archive carries everything the project
directory held, byte for byte. An unknown project 404s; a file the server
cannot read fails the download rather than silently missing from the archive.
