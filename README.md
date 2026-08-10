# carbonpaper — reviewable AI workflows

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
