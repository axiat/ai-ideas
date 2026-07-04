#!/usr/bin/env bash
# hunt.sh 前段(生成 + 查重)的 agy 适配层。只给「便宜、可错」的上游用;
# 后段(打分/报告)和 publish 绝不走这里——见 hunt.sh 顶部说明。
#
# 治两件事:
#   1) agy 相对路径漂移(有时写到 ~/.gemini/antigravity-cli/scratch 而非仓库):
#      把绝对仓库根钉进 prompt 前缀 + --add-dir,逼它写回仓库。真漂了也只是
#      tmp/round 产物缺失 → hunt.sh 判空重试,不污染 verdict。
#   2) 挂起:用 agy 自带 --print-timeout 兜底(macOS 默认无 GNU timeout)。
#
# 用法(在 hunt.sh 里):
#   FRONT_CMD='./agy-worker.sh' BACK_CMD='claude -p' ./hunt.sh
# 可调:
#   AGY_MODEL          默认 gemini-3.5-flash-low(便宜、已实测可用)
#   AGY_PRINT_TIMEOUT  默认 8m
set -u
repo="$(cd "$(dirname "$0")" && pwd)"
model=${AGY_MODEL:-gemini-3.5-flash-low}
ptimeout=${AGY_PRINT_TIMEOUT:-8m}
prompt=${1:?用法: agy-worker.sh <prompt>}

# 绝对路径前缀:agy 相对路径不稳,显式钉死仓库根,所有读写按此解析。
pre="仓库根(绝对路径)= ${repo}。当前工作目录已在此根下。所有读写路径(tmp/round/… roles/… rubric.md brainstorming_policy.md research_context.md ledger.tsv 等)一律相对该根解析;产物必须落在 ${repo}/tmp/round/,严禁写到 ~/.gemini、任何 scratch 目录或 \$HOME 其它位置。"

cd "$repo" || { echo "agy-worker: 无法进入仓库根 $repo" >&2; exit 1; }
exec agy --model "$model" --add-dir "$repo" --print-timeout "$ptimeout" -p "${pre}

${prompt}"
