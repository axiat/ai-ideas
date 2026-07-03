#!/usr/bin/env bash
# 外层循环:反复调起 agent CLI 跑 hunt.md,直到当日达标报告出现。
# 全程记录 hunt.log(含 agent 输出);失败分类:异常退出(额度/权限/命令错,连续 MAX_FAILS 次即停)
# vs 正常退出但无达标(继续重试)。固定层守卫:每轮结束校验改动只落在 ideas/ 与 ledger.tsv,
# 越界的已跟踪改动回滚;越界的已提交改动或未跟踪新文件,停止循环留人工处理。
#
# 用法:
#   ./hunt.sh [重试间隔分钟,默认 150]
#   AGENT_CMD 指定 agent CLI(prompt 作为最后一个参数传入),例:
#     AGENT_CMD='claude -p' ./hunt.sh              # 默认;权限走 .claude/settings.json allowlist
#     AGENT_CMD='codex --search -a never exec -s workspace-write' ./hunt.sh   # OS sandbox,写限本仓库
#     AGENT_CMD='opencode run' ./hunt.sh
set -u
cd "$(dirname "$0")"
git config core.hooksPath .githooks   # 激活 pre-push 守卫:禁止直推 main

AGENT_CMD=${AGENT_CMD:-claude -p}
SLEEP_MIN=${1:-150}
PROMPT=${PROMPT:-读 hunt.md,开始}
MAX_FAILS=${MAX_FAILS:-12}
LOG=hunt.log

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

fails=0
while :; do
  today=$(date +%F)
  if ls "ideas/${today}"_hunt*.md >/dev/null 2>&1; then
    log "当日达标报告已存在,结束"
    break
  fi

  before=$(git rev-parse HEAD)
  pre_dirty=$(git status --porcelain | cut -c4- | sort -u)

  log "调起 agent: $AGENT_CMD"
  $AGENT_CMD "$PROMPT" >> "$LOG" 2>&1
  rc=$?
  log "agent 退出 rc=$rc"

  # 固定层守卫:本轮新增改动只允许落在 ideas/ 与 ledger.tsv
  changed=$({ git diff --name-only "$before" HEAD; git status --porcelain | cut -c4-; } | sort -u)
  bad=$(comm -23 <(printf '%s\n' "$changed") <(printf '%s\n' "$pre_dirty") \
        | grep -vE '^(ideas/|ledger\.tsv$)' | grep -v '^$' || true)
  if [ -n "$bad" ]; then
    log "守卫:固定层被改动 -> $(echo "$bad" | tr '\n' ' ')"
    committed_bad=$(git diff --name-only "$before" HEAD | grep -xF -f <(printf '%s\n' "$bad") || true)
    if [ -n "$committed_bad" ]; then
      log "越界改动已进 commit,停止循环,人工处理:git log ${before:0:7}..HEAD"
      exit 2
    fi
    rolled_all=1
    while read -r p; do
      if git checkout -- "$p" 2>/dev/null; then
        log "已回滚: $p"
      else
        log "未跟踪的越界文件: $p"
        rolled_all=0
      fi
    done <<< "$bad"
    if [ "$rolled_all" -eq 0 ]; then
      log "存在未跟踪的越界文件,停止循环,人工处理后再跑"
      exit 2
    fi
  fi

  if ls "ideas/${today}"_hunt*.md >/dev/null 2>&1; then
    log "达标,结束"
    break
  fi

  if [ "$rc" -ne 0 ]; then
    fails=$((fails + 1))
    log "异常退出,连续第 ${fails}/${MAX_FAILS} 次(额度耗尽/权限被拒/命令拼错,见上方 agent 输出)"
    if [ "$fails" -ge "$MAX_FAILS" ]; then
      log "连续失败达上限,停止;检查 AGENT_CMD、额度与权限配置"
      exit 1
    fi
  else
    fails=0
    log "运行完成但无达标报告(本轮 idea 全被拒,或未按 hunt.md 写报告)"
  fi
  log "${SLEEP_MIN} 分钟后重试"
  sleep "$((SLEEP_MIN * 60))"
done
