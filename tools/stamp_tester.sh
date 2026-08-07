#!/bin/sh
# Stamp one carbonpaper Fly.io app for one tester (see docs/deploy.md):
# register the app under the tester's name from the repo's fly.toml, set the
# tester's Anthropic API key as the app's secret, deploy, print the URL.
#
# Usage: tools/stamp_tester.sh <tester-name> <anthropic-api-key>
#
# The key is passed to `fly secrets set` and never printed by this script;
# run it from a shell without command echoing (no `sh -x`).
set -eu

if [ "$#" -ne 2 ] || [ -z "$1" ] || [ -z "$2" ]; then
    echo "usage: $0 <tester-name> <anthropic-api-key>" >&2
    echo "  tester-name  becomes the app name: carbonpaper-<tester-name>" >&2
    echo "  api-key      a per-tester key from a spend-capped Console workspace" >&2
    exit 2
fi

tester="$1"
api_key="$2"
app_name="carbonpaper-${tester}"

# Register the app without deploying: --copy-config takes the repo's fly.toml
# as-is (only the name changes), --no-deploy defers the build until the secret
# below exists, --yes keeps it non-interactive.
fly launch --no-deploy --copy-config --name "$app_name" --yes

# Set before the first deploy so the first boot already holds its key.
# --stage records the secret without cycling machines (none exist yet).
fly secrets set --app "$app_name" --stage "ANTHROPIC_API_KEY=${api_key}"

fly deploy --app "$app_name"

echo "deployed: https://${app_name}.fly.dev"
