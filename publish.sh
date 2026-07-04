#!/usr/bin/env bash
# agent 的唯一发布通道:只把 ideas/ 与 ledger.tsv 提交到 <source>/当日 分支,push 并确保有 PR。
# 用法:./publish.sh [source]   source = hunt(默认)| weekly
# allowlist 只放行本脚本,不直接暴露 git/gh。
set -euo pipefail
cd "$(dirname "$0")"
git config core.hooksPath .githooks

src=${1:-hunt}
today=$(date +%F)
branch="${src}/${today}"

git add ideas ledger.tsv
if git diff --cached --quiet; then
  echo "无待发布改动"
  exit 0
fi

if [ "$(git rev-parse --abbrev-ref HEAD)" != "$branch" ]; then
  git checkout -b "$branch" 2>/dev/null || git checkout "$branch"
fi
git commit -m "${src}: ${today} 报告与台账"
git push -u origin "$branch"

state=$(gh pr view "$branch" --json state -q .state 2>/dev/null || true)
if [ "$state" != "OPEN" ]; then
  gh pr create --fill --head "$branch" || echo "PR 创建失败(分支已推送),人工开 PR: $branch"
fi
