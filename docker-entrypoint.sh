#!/bin/sh
# Container start: bring the volume's schema up to date, then become the server.
#
# Migration runs HERE, not in fly.toml's release_command, because a release
# command runs on a temporary machine with no volume attached — it could not
# reach /data/app.db. The machine that owns the volume is the only one that can
# migrate it. `set -e` makes a failed migration a failed boot rather than a
# server answering against a stale schema.
set -e

mkdir -p "$(dirname "$CARBON_PAPER_DB_PATH")" "$CARBON_PAPER_PROJECTS_DIR" "$CLAUDE_CONFIG_DIR"

alembic upgrade head

# One machine serves this deploy (fly.toml: min_machines_running = 1,
# auto_stop_machines = "off"), so this process is the only executor of its store.
# That is what lets a boot be read as proof that the runs still marked `running`
# lost their executor to the deploy, and be resumed rather than buried.
export CARBON_PAPER_SOLE_EXECUTOR=1

exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
