#!/bin/sh
# Container start: bring the volume's schema up to date, then become the server.
#
# Migration runs HERE, not in fly.toml's release_command, because a release
# command runs on a temporary machine with no volume attached — it could not
# reach /data/app.db. The machine that owns the volume is the only one that can
# migrate it. `set -e` makes a failed migration a failed boot rather than a
# server answering against a stale schema.
set -e

mkdir -p "$(dirname "$CARBON_PAPER_DB_PATH")" "$CARBON_PAPER_PROJECTS_DIR"

alembic upgrade head

exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
