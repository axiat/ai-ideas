#!/usr/bin/env bash
# Verify that local publication residue already exists on origin/main, align the
# local main branch, and remove safely merged routine branches.
# Usage:
#   ./settle.sh
#   DRY_RUN=1 ./settle.sh
#   ./settle.sh 'ledger.tsv' 'ideas/*_hunt*.md'
#
# Default accepted local residue:
#   ledger.tsv
#   ideas/*_hunt*.md
#   ideas/*_weekly*.md
set -euo pipefail
cd "$(dirname "$0")"

remote=${REMOTE:-origin}
base=${BASE:-main}
upstream="${remote}/${base}"
dry_run=${DRY_RUN:-0}
tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/ai-ideas-settle.XXXXXX")
trap 'rm -rf "$tmpdir"' EXIT
allowed_patterns=("ledger.tsv" "ideas/*_hunt*.md" "ideas/*_weekly*.md")
if [[ "$#" -gt 0 ]]; then
  allowed_patterns=("$@")
fi

allowed_residue() {
  local path=$1
  local pattern
  for pattern in "${allowed_patterns[@]}"; do
    [[ "$path" == $pattern ]] && return 0
  done
  return 1
}

run() {
  if [[ "$dry_run" == "1" ]]; then
    printf 'DRY_RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

echo "fetch ${remote}..."
git fetch "$remote" --prune

git rev-parse --verify "$upstream" >/dev/null

current_branch=$(git rev-parse --abbrev-ref HEAD)
if [[ "$current_branch" != "$base" ]]; then
  if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    echo "Current branch is ${current_branch} and the worktree is dirty; resolve it before switching to ${base}" >&2
    exit 2
  fi
  run git switch "$base"
fi

local_head=$(git rev-parse "$base")
upstream_head=$(git rev-parse "$upstream")
merge_base=$(git merge-base "$base" "$upstream")
if [[ "$local_head" != "$upstream_head" && "$merge_base" != "$local_head" ]]; then
  echo "Local ${base} diverged from ${upstream}; stopping to preserve local commits" >&2
  exit 2
fi

dirty_list="${tmpdir}/dirty"
git status --porcelain --untracked-files=all | sed 's/^...//' | sort -u > "$dirty_list"
while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  if ! allowed_residue "$path"; then
    echo "Non-routine residue found: ${path}" >&2
    echo "Allowed patterns:" >&2
    printf '  %s\n' "${allowed_patterns[@]}" >&2
    exit 2
  fi
done < "$dirty_list"

while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  if [[ ! -f "$path" ]]; then
    echo "Local residue is not a regular file: ${path}" >&2
    exit 2
  fi
  if ! git cat-file -e "${upstream}:${path}" 2>/dev/null; then
    echo "${upstream} does not contain ${path}; publication cannot be verified" >&2
    exit 2
  fi
  if ! git show "${upstream}:${path}" | cmp -s - "$path"; then
    echo "Local ${path} differs from ${upstream}; stopping" >&2
    exit 2
  fi
  echo "verified: ${path}"
done < "$dirty_list"

run git reset --hard "$upstream"

delete_local_branch_if_safe() {
  local branch=$1
  if git merge-base --is-ancestor "$branch" "$upstream" 2>/dev/null ||
     git diff --quiet "$branch" "$upstream" --; then
    run git branch -D "$branch"
  else
    echo "skip local branch with unmerged diff: ${branch}"
  fi
}

delete_remote_branch_if_safe() {
  local branch=$1
  local ref="${remote}/${branch}"
  if git merge-base --is-ancestor "$ref" "$upstream" 2>/dev/null ||
     git diff --quiet "$ref" "$upstream" --; then
    run git push "$remote" --delete "$branch"
  else
    echo "skip remote branch with unmerged diff: ${branch}"
  fi
}

local_branch_list="${tmpdir}/local-branches"
git branch --format='%(refname:short)' --list 'hunt/*' 'weekly/*' > "$local_branch_list"
while IFS= read -r branch; do
  [[ -z "$branch" ]] && continue
  delete_local_branch_if_safe "$branch"
done < "$local_branch_list"

remote_branch_list="${tmpdir}/remote-branches"
git for-each-ref --format='%(refname:short)' \
  "refs/remotes/${remote}/hunt/*" \
  "refs/remotes/${remote}/weekly/*" |
sed "s#^${remote}/##" > "$remote_branch_list"
while IFS= read -r branch; do
  [[ -z "$branch" ]] && continue
  delete_remote_branch_if_safe "$branch"
done < "$remote_branch_list"

git status --short --branch
