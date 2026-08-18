# Getting started

```
git clone <repo> && cd carbonpaper && ./start
```

That is the whole install. `./start` prints `http://127.0.0.1:8765` when the
server is up; `./start -p 9000` moves it, `./start --no-reload` stops the file
watching.

The machine needs `git`, `curl` and `bash`, and nothing else — not a particular
Python. `./start` installs [uv](https://docs.astral.sh/uv/) into `~/.local/bin`
if it is missing, and uv builds `.venv` from `uv.lock`, fetching its own CPython
3.12 when the system has no 3.12 of its own. Dependencies are declared in
`pyproject.toml` and pinned in `uv.lock`; there is no requirements.txt. Re-run
`./start` after a `git pull` — a sync that is already current costs about a
second.

## Signing in, so the LLM stages run

The server boots and serves with no credential at all. What fails without one is
`llm_transform` stages: `claude-agent-sdk` spawns the **Claude Code CLI**, which
holds the credential, and no app module reads a key itself.

`./start` installs that CLI if it is missing (with a y/n prompt — it is a large
install and `npm install -g @anthropic-ai/claude-code` may already have put one
there), then reports which credential the stages will reach for. Sign in once:

```
claude auth login
```

That is a browser OAuth flow against a Claude subscription, and it is the only
step `./start` will not do for you. `claude auth status` shows what it wrote.
Once signed in there is nothing to export and nothing to put in a file — the
CLI the SDK spawns finds its own credential.

Two environment variables override that CLI login, and both outrank it:

| | |
|---|---|
| `ANTHROPIC_API_KEY` | The metered API, billed per token. Outranks everything below. |
| `CLAUDE_CODE_OAUTH_TOKEN` | A one-year token from `claude setup-token`, on a subscription. For a machine with no browser — CI, a container. Never set it beside the API key: the key wins and the subscription silently goes unused. |

## Where your state lives

Everything is under `~/.carbonpaper/`, outside the checkout, so every clone and
every git worktree reads and writes the one store:

| | |
|---|---|
| `app.db` | The document store: projects, workflow versions, runs, chats. |
| `frames/` | Stage outputs, as parquet. |
| `examples/` | The project working copies. |
| `files/` | Uploaded input data, kept under the hash of its own contents. |

`./start` runs `alembic upgrade head` on every boot, which is what keeps that
store current as you pull. `CARBON_PAPER_DB_PATH` and
`CARBON_PAPER_PROJECTS_DIR` repoint it — that is how the Fly deploy pins
everything to its volume (see [self-hosting.md](self-hosting.md)).

Nothing authenticates the server, so leave it on `127.0.0.1`.

## Your first run needs a data file

A run reads its inputs off the server's disk by absolute path, so a file has to
get there first. The run form's Browse… posts it and stores it;
[self-hosting.md](self-hosting.md#getting-a-data-file-in) covers the
plain-multipart endpoint underneath, which any `curl` can post to.

The product tour on the home page seeds a small worked project instead, if you
would rather read one than build one.

## Working on the code

```
uv run --frozen python -m pytest tests/ app/    # what CI runs
uv run --frozen ruff check
uv run --frozen mypy
uv run --frozen lint-imports                    # the layering contracts
uv run --frozen python -m app.cli <project>     # run a workflow without the UI
```

`pytest tests/` alone misses the arch tests that live beside the code they
guard, in `app/**/_arch_tests/` — hence both paths. [AGENTS.md](../AGENTS.md)
has the conventions those tests enforce.
