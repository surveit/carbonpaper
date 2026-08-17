# Carbon Paper — reviewable AI workflows

Some questions you only ask once. How much did firms spend lobbying about AI last
year. Which of these 400 contracts name the same shell company. What changed
between two editions of a public register.

You are not building a product. You want the answer, and you need it to hold up.

Both usual routes fail that. Hand the files to a chat model and a number comes back
in minutes, but you cannot publish it: the model made judgement calls you never saw,
over rows you never looked at. Write the analysis yourself and every step is
checkable, but you have spent two days engineering something you will run twice.

Carbon Paper is the third route. You write down how the investigation works, in
prose. That becomes a **workflow**: a chain of small named steps running against your
original files. Most steps are ordinary deterministic code. A step that genuinely
needs judgement calls a model, and it is the only one that does.

Every step is then open to a reader who did not write it:

- it states in plain English what it does, and shows what it changed in your rows
- a step needing judgement can halt the run and wait for a person to decide
- every published figure traces back to the source rows it was computed from
- the whole run exports as a folder that opens in a browser with no app and no network

The walkthrough at **<https://carbonpaper.fly.dev/intro>** follows one question through
this end to end — 45,061 lobbying filings, a chat model's $499m against the workflow's
$42.0m, and what the difference was made of. It is served from `intro/` in this repo,
so a local server has it at `/intro` too.

The goal is not perfect code. It is an answer you can stand behind, a record that
proves it, and a workflow you can run again when next quarter's file lands.

Try it at **[carbonpaper.fly.dev](https://carbonpaper.fly.dev)** — a public instance
with no login, so treat anything you put there as public.

## Getting started

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock` — there is no
requirements.txt. `uv sync` builds `.venv` from the lock; `--frozen` makes a lock that
has drifted from `pyproject.toml` an error instead of a re-resolve.

```
uv sync --frozen
uv run python -m uvicorn app.main:app --port 8765
```

Steps that call a model need a credential in the server's environment, either
`ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`. No app module reads them: the Claude
Code CLI that `claude-agent-sdk` spawns inherits the environment and authenticates from
it. Without one the server still boots and serves, and `llm_transform` stages are what
fail.

**Take the tour first.** A browser that has not run it gets it in place of the project
list at <http://localhost:8765>. It seeds a seven-stage workflow over real, sourced
advocacy records and runs it for real, so the first workflow you read is one you
watched run.

### Your own question

1. **＋ New project**, and paste your write-up of how the investigation works. The
   data model is generated from it as a live chat turn, and you land on that chat.
2. Author the stages by talking to the agent in the same chat. The project's five
   sections — Overview, Document, Terms, Workflow, Runs — are the left sidebar.
3. **Get your data file onto the server.** A run reads its inputs off the server's
   disk by absolute path, so the run form's Browse… posts the file and hands the path
   back. [Getting a data file in](docs/self-hosting.md#getting-a-data-file-in) covers
   the endpoint, which takes any caller that can run `curl`.
4. **▶ Run workflow**, from the Workflow section or a stored version's page. The run
   form is where a version is picked and each input is pointed at its file.
5. Read the run: the walkthrough, the issue index, and a panel per step showing what
   that step changed. Then export the review packet and hand it to whoever checks it.

Running once from the command line, against the newest stored version:

```
uv run python -m app.cli <project>
```

Local state lives in `~/.carbonpaper/`, so every checkout and worktree reads and writes
the one store. [docs/self-hosting.md](docs/self-hosting.md) says what is in there and
how to repoint it.

## Where the rest is

- What & why: [docs/overview.md](docs/overview.md) — the mission, the locked
  vocabulary, and the three features.
- Code map: [docs/architecture.md](docs/architecture.md)
- Running your own instance: [docs/self-hosting.md](docs/self-hosting.md) — the file
  store and its quotas, the environment variables, and the Fly.io deploy.
- Contributor guide / conventions: [AGENTS.md](AGENTS.md)
