#!/bin/bash
# Stands up one tenant: a Fly app with no public IP, a volume, and a cloudflared
# tunnel reached only through that tenant's Cloudflare Access application.
#
# Create the Access application first (docs/per-tenant-deploy.md says how) and pass its
# AUD tag in — this script does not call the Cloudflare API.
#
# Usage:
#   TENANT=alice DOMAIN=example.com \
#   ACCESS_TEAM=yourteam ACCESS_AUD=<aud tag from the Access app> \
#   ANTHROPIC_CREDENTIAL=sk-ant-... scripts/provision_tenant.sh
#
# Re-running is safe: every create step tolerates the resource already existing.
set -euo pipefail

: "${TENANT:?set TENANT to a slug, e.g. alice}"
: "${DOMAIN:?set DOMAIN to the apex domain on Cloudflare, e.g. example.com}"
: "${ACCESS_TEAM:?set ACCESS_TEAM, the <team> in https://<team>.cloudflareaccess.com}"
: "${ACCESS_AUD:?set ACCESS_AUD to the AUD tag of the Access application}"
: "${ANTHROPIC_CREDENTIAL:?set ANTHROPIC_CREDENTIAL to the API key or OAuth token}"

# An API key and a subscription token must never both be set — the key silently
# wins and the subscription goes unused. See docs/getting-started.md.
ANTHROPIC_ENV_NAME="${ANTHROPIC_ENV_NAME:-ANTHROPIC_API_KEY}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="carbonpaper-${TENANT}"
TENANT_HOST="${TENANT}.${DOMAIN}"
TUNNEL_NAME="carbonpaper-${TENANT}"
CONFIG="${REPO_ROOT}/deploy/tenants/${TENANT}.toml"

echo "==> tenant ${TENANT}: ${TENANT_HOST} -> ${APP_NAME}"

# The tunnel comes first: its token must be a secret before the first deploy,
# or the machine boots with no way in.
if ! cloudflared tunnel info "${TUNNEL_NAME}" >/dev/null 2>&1; then
  cloudflared tunnel create "${TUNNEL_NAME}"
fi
cloudflared tunnel route dns --overwrite-dns "${TUNNEL_NAME}" "${TENANT_HOST}"
TUNNEL_TOKEN="$(cloudflared tunnel token "${TUNNEL_NAME}")"

fly apps create "${APP_NAME}" --org personal 2>/dev/null \
  || echo "    app ${APP_NAME} already exists"

if ! fly volumes list -a "${APP_NAME}" 2>/dev/null | grep -q carbonpaper_data; then
  fly volumes create carbonpaper_data --size 10 --region iad -a "${APP_NAME}" --yes
fi

mkdir -p "${REPO_ROOT}/deploy/tenants"
sed -e "s|__APP_NAME__|${APP_NAME}|g" \
    -e "s|__ACCESS_TEAM__|${ACCESS_TEAM}|g" \
    -e "s|__ACCESS_AUD__|${ACCESS_AUD}|g" \
    "${REPO_ROOT}/deploy/fly.tenant.toml.template" > "${CONFIG}"

# --stage holds them for the deploy below rather than restarting a machine that
# has not been built yet.
fly secrets set -a "${APP_NAME}" --stage \
  "TUNNEL_TOKEN=${TUNNEL_TOKEN}" \
  "${ANTHROPIC_ENV_NAME}=${ANTHROPIC_CREDENTIAL}"

fly deploy -a "${APP_NAME}" -c "${CONFIG}"

# A public IP bypasses Access completely, so fail loudly rather than report success.
if fly ips list -a "${APP_NAME}" | grep -qE '^\s*v[46]\s'; then
  echo "REFUSING: ${APP_NAME} holds a public IP; Access can be bypassed." >&2
  echo "Release it:  fly ips release <address> -a ${APP_NAME}" >&2
  exit 1
fi

echo
echo "==> https://${TENANT_HOST} is live, reachable only through Access."
echo "    Runs 24/7 (~\$11.81/mo): a tunnel dials out, so a stopped machine"
echo "    has no tunnel and nothing can wake it."
