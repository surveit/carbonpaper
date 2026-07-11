# prototype_one — reviewable AI workflows

Run data/OSINT pipelines as **workflows of typed, schema-validated stages** with
human-review gates and fully persisted runs — testable and reviewable, not a black box.

- What & why: [docs/overview.md](docs/overview.md)
- Code map: [docs/architecture.md](docs/architecture.md)
- Contributor guide / conventions: [AGENTS.md](AGENTS.md)

```
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8765     # web UI
python -m app.runtime.runner examples/<name>   # run a project's workflow from the CLI
```
