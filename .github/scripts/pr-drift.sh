#!/usr/bin/env bash
# Emits one TSV row per open pull request: number, base ref, drift, band, label.
#
# "Drift" is the number of commits the base branch has gained since the pull
# request forked from it -- i.e. `git rev-list --count <merge-base>..<base>`.
# It is NOT the age of the branch and NOT the number of commits on the branch.
#
# The bands come from replaying 212 merged pull requests in this repository and
# recording, for each, whether its merge produced a textual conflict:
#
#   drift 0-2 commits  -> 24% conflicted
#   drift 3-7 commits  -> 41% conflicted
#   drift 8-19 commits -> 72% conflicted
#   drift 20+ commits  -> 72% conflicted
#
# 0-2 and 3-7 are folded into one "drift:0-7" band because the actionable break
# is at 8, where the rate roughly doubles and then plateaus -- past 8 more drift
# does not make things meaningfully worse, so a finer high-end split would carry
# no decision value. 20+ is kept separate only as a staleness marker.
#
# Requires: git (with the base and head commits present locally) and gh.
set -euo pipefail

REPO_ARG=()
if [[ -n "${DRIFT_REPO:-}" ]]; then
  REPO_ARG=(--repo "$DRIFT_REPO")
fi

name_drift_band() {
  local drift="$1"
  if (( drift < 8 )); then
    echo "drift:0-7"
  elif (( drift < 20 )); then
    echo "drift:8-19"
  else
    echo "drift:20+"
  fi
}

# Counts commits on `base` that the pull request has never seen. Prints nothing
# and returns non-zero when either commit is missing from the local object store.
count_drift() {
  local base_sha="$1" head_sha="$2" merge_base
  merge_base="$(git merge-base "$base_sha" "$head_sha")" || return 1
  git rev-list --count "$merge_base..$base_sha"
}

main() {
  printf 'number\tbase\tdrift\tband\n'
  gh "${REPO_ARG[@]}" pr list --state open --limit 200 \
    --json number,baseRefName,headRefOid \
    --jq '.[] | [.number, .baseRefName, .headRefOid] | @tsv' \
  | while IFS=$'\t' read -r number base head_sha; do
      base_sha="$(git rev-parse "refs/remotes/origin/$base" 2>/dev/null || true)"
      if [[ -z "$base_sha" ]]; then
        printf '%s\t%s\tNA\tbase-ref-missing\n' "$number" "$base"
        continue
      fi
      drift="$(count_drift "$base_sha" "$head_sha" || true)"
      if [[ -z "$drift" ]]; then
        printf '%s\t%s\tNA\tcommit-missing\n' "$number" "$base"
        continue
      fi
      printf '%s\t%s\t%s\t%s\n' "$number" "$base" "$drift" "$(name_drift_band "$drift")"
    done
}

main "$@"
