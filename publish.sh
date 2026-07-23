#!/usr/bin/env bash
# Sole publication path for agent output. It commits only ideas/ and ledger.tsv
# on a daily <source>/<date> branch, pushes it, and ensures an open PR exists.
# Reruns repair interruptions between commit, push, and PR creation.
# Usage: ./publish.sh [source], where source defaults to hunt or may be weekly.
set -euo pipefail
cd "$(dirname "$0")"
git config core.hooksPath .githooks

src=${1:-hunt}
today=$(date +%F)
branch="${src}/${today}"

git add ideas ledger.tsv
if ! git diff --cached --quiet; then
  staged=$(git diff --cached --name-only)
  bad=$(printf '%s\n' "$staged" | grep -vE '^(ideas/|ledger\.tsv$)' || true)
  if [ -n "$bad" ]; then
    echo "publish refused: staged paths extend beyond ideas/ and ledger.tsv:" >&2
    echo "$bad" >&2
    exit 2
  fi
  if [ "$(git rev-parse --abbrev-ref HEAD)" != "$branch" ]; then
    git checkout -b "$branch" 2>/dev/null || git checkout "$branch"
  fi
  git commit -m "${src}: publish ${today} report and ledger" -- ideas ledger.tsv
elif ! git rev-parse --verify -q "refs/heads/${branch}" >/dev/null; then
  echo "No publication changes"
  exit 0
fi

# At this point a new commit or an existing daily branch needs idempotent push/PR repair.
git push -u origin "$branch"

state=$(gh pr view "$branch" --json state -q .state 2>/dev/null || true)
if [ "$state" != "OPEN" ]; then
  gh pr create --fill --head "$branch" || echo "PR creation failed after branch push; open it manually: $branch"
fi
