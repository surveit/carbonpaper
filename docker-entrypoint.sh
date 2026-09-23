#!/bin/bash
# Container start: bring the volume's schema up to date, then become the server.
#
# Migration runs HERE, not in fly.toml's release_command, because a release
# command runs on a temporary machine with no volume attached — it could not
# reach /data/app.db. The machine that owns the volume is the only one that can
# migrate it. `set -e` makes a failed migration a failed boot rather than a
# server answering against a stale schema.
#
# bash, not sh, for `wait -n`: a per-tenant machine runs cloudflared beside the
# server and must die when EITHER exits. A live server behind a dead tunnel takes
# no traffic and Fly would keep it up forever, unreachable and still billing.
set -e

mkdir -p "$(dirname "$CARBON_PAPER_DB_PATH")" "$CARBON_PAPER_PROJECTS_DIR" "$CLAUDE_CONFIG_DIR"

alembic upgrade head

if [ -z "$TUNNEL_TOKEN" ]; then
  exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
fi

# A per-tenant machine has no public IP; the tunnel dials out and is the only
# way in. --no-autoupdate keeps the pinned binary from replacing itself.
cloudflared tunnel --no-autoupdate run --token "$TUNNEL_TOKEN" &
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 &

wait -n
exit $?
