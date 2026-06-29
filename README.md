# prototype_one

Reviewable AI methodology DAGs for journalism & institutional accountability — data/OSINT
pipelines as DAGs of typed, schema-validated, human-reviewable nodes.

**Start with [AGENTS.md](AGENTS.md)** — it's the index into the documentation in [`docs/`](docs/):

- [docs/overview.md](docs/overview.md) — what this is and why (product context)
- [docs/architecture.md](docs/architecture.md) — the code map
- [docs/named-schemas.md](docs/named-schemas.md) — the data model (authored before the DAG)
- [docs/run-and-review-ui.md](docs/run-and-review-ui.md) — the run + review UI
- [docs/lobbymap-eval.md](docs/lobbymap-eval.md) — the LobbyMap reproduction project

```
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8765
```
