#!/usr/bin/env bash
# 外层循环:每轮把一批 idea 走完「生成 → 对抗式查重 → N 位裁判打分」,
# 由本脚本(而非任何 agent)聚合 verdict、写 ledger、发布。达当日达标(≥1 Strong Accept)即停。
#
# 反串通设计:
#   - 生成 / 查重 / 打分是互不共享 context 的独立进程;裁判并行跑、各用独立输入目录,开跑时看不到彼此产出。
#   - 每个 idea 取 N 位裁判的「最低」verdict —— Strong Accept 需全票,且过 orchestrator 的 SA 硬门槛。
#   - verdict、ledger、publish 全由本脚本决定。ledger 以 tmp/ledger.good 为单一可信基线,只被 bash 聚合更新;
#     每轮开局重置、聚合后固化,agent 对 ledger 的任何擅改都会在下一轮开局被抹掉。
#   - 分阶段守卫:生成/查重/评审阶段禁写 ideas/(防伪造达标报告绕过全票);仅 report 阶段可写 ideas/。
#
# 全程记录 hunt.log。失败分类:某阶段异常退出(额度/权限/命令错,连续 MAX_FAILS 次即停)
# vs 正常跑完但无达标(继续重试)。
#
# 用法:
#   ./hunt.sh [重试间隔分钟,默认 150]
#   REVIEWERS 裁判票数(默认 3);MIN_READ SA 门槛要求的最少实读篇数(默认 3);
#   AGENT_CMD 指定 agent CLI(prompt 作为最后一个参数传入),例:
#     AGENT_CMD='claude -p' ./hunt.sh              # 默认;权限走 .claude/settings.json allowlist
#     AGENT_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' ./hunt.sh
#       # OS sandbox 写限本仓库;approval never + 放行网络(publish.sh 的 push/gh 需联网)。codex exec 不吃 -a,须用 -c approval_policy=
set -u
cd "$(dirname "$0")"
git config core.hooksPath .githooks   # 激活 pre-push 守卫:禁止直推 main

AGENT_CMD=${AGENT_CMD:-claude -p}
SLEEP_MIN=${1:-150}
MAX_FAILS=${MAX_FAILS:-12}
REVIEWERS=${REVIEWERS:-3}
MIN_READ=${MIN_READ:-3}
LOG=hunt.log
RD=tmp/round
LEDGER_GOOD=tmp/ledger.good

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# 调一次 agent(串行阶段用),rc 作为返回值;$1=prompt $2=阶段名
run_stage() {
  log "调起 [$2]: $AGENT_CMD"
  $AGENT_CMD "$1" >> "$LOG" 2>&1
  return $?
}

# 固定层守卫:本轮相对 before/pre_dirty 的新增已跟踪改动只允许落在指定路径。
# $1=是否允许 ideas/(1/0)。ledger.tsv 恒视为在界内——其完整性由 ledger.good 重置保证,不靠此守卫。
guard() {
  local allow_ideas=${1:-0} pat changed bad committed_bad rolled_all p
  if [ "$allow_ideas" = "1" ]; then pat='^(ideas/|ledger\.tsv$)'; else pat='^(ledger\.tsv$)'; fi
  changed=$({ git diff --name-only "$before" HEAD; git status --porcelain | cut -c4-; } | sort -u)
  bad=$(comm -23 <(printf '%s\n' "$changed") <(printf '%s\n' "$pre_dirty") \
        | grep -vE "$pat" | grep -v '^$' || true)
  [ -z "$bad" ] && return 0
  log "守卫:越界改动 -> $(echo "$bad" | tr '\n' ' ')"
  committed_bad=$(git diff --name-only "$before" HEAD | grep -xF -f <(printf '%s\n' "$bad") || true)
  if [ -n "$committed_bad" ]; then
    log "越界改动已进 commit,停机,人工处理:git log ${before:0:7}..HEAD"
    exit 2
  fi
  rolled_all=1
  while read -r p; do
    [ -z "$p" ] && continue
    if git checkout -- "$p" 2>/dev/null; then log "已回滚: $p"; else log "未跟踪越界文件: $p"; rolled_all=0; fi
  done <<< "$bad"
  [ "$rolled_all" -eq 0 ] && { log "存在未跟踪越界文件(疑似伪造报告/越权写入),停机,人工处理后再跑"; exit 2; }
  return 0
}

# 阶段异常退出:计失败数,达上限停机,否则睡眠(调用方随后 continue)
fail_and_wait() {
  fails=$((fails + 1))
  log "阶段异常退出,连续第 ${fails}/${MAX_FAILS} 次(额度耗尽/权限被拒/命令拼错,见上方 agent 输出)"
  if [ "$fails" -ge "$MAX_FAILS" ]; then
    log "连续失败达上限,停止;检查 AGENT_CMD、额度与权限配置"
    exit 1
  fi
  log "${SLEEP_MIN} 分钟后重试"
  sleep "$((SLEEP_MIN * 60))"
}

rank_of() { case "$1" in strong-accept) echo 2 ;; accept-w-rev) echo 1 ;; *) echo 0 ;; esac; }
verdict_of() { case "$1" in 2) echo strong-accept ;; 1) echo accept-w-rev ;; *) echo reject ;; esac; }

# SA 硬门槛:$1=id。要求 priorwork.md 有该 idea 的查重块、实读篇数≥MIN_READ、
# 且每位裁判都写了该 idea 的完整评审块(全票 SA 本就意味着人人判 strong-accept、理应各附评审;
# 要求全员有块,report 死读 rev/1 才必然有料)。
sa_gate_ok() {
  local id=$1 block n r
  [ -s "$RD/priorwork.md" ] || return 1
  block=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/priorwork.md")
  [ -n "$block" ] || return 1
  n=$(printf '%s\n' "$block" | grep '实读篇数' | grep -oE '[0-9]+' | head -1)
  [ -n "$n" ] && [ "$n" -ge "$MIN_READ" ] || return 1
  for r in $(seq 1 "$REVIEWERS"); do
    grep -qE "^##[[:space:]]+${id}([[:space:]]|$)" "$RD/rev/$r/review.md" 2>/dev/null || return 1
  done
  return 0
}

mkdir -p "$(dirname "$LEDGER_GOOD")"                             # tmp/ 在干净 checkout 里不存在,先建(否则种子/重置全失败)
# 启动瞬间的工作树 ledger 视为人工/operator 基线;后续阶段中 agent 的任何擅改都只会被重置回此基线。
if [ -f ledger.tsv ]; then
  if ! git diff --quiet -- ledger.tsv 2>/dev/null; then
    log "启动基线: ledger.tsv 有未提交改动,按当前工作树作为人工基线"
  fi
  cp ledger.tsv "$LEDGER_GOOD"
else
  git show "HEAD:ledger.tsv" > "$LEDGER_GOOD" 2>/dev/null || : > "$LEDGER_GOOD"
fi
# 任何退出(含 MAX_FAILS、守卫停机、Ctrl-C)都把 ledger.tsv 还原到最近一次 bash 定谳的 good,绝不留篡改
trap 'cp "$LEDGER_GOOD" ledger.tsv 2>/dev/null || true' EXIT

fails=0
round=0
while :; do
  today=$(date +%F)
  if ls "ideas/${today}"_hunt*.md >/dev/null 2>&1; then
    log "当日达标报告已存在,结束"
    break
  fi

  round=$((round + 1))
  before=$(git rev-parse HEAD)
  cp "$LEDGER_GOOD" ledger.tsv                       # 重置到上一轮定谳后的良好 ledger,抹掉任何遗留篡改(F2)
  pre_dirty=$(git status --porcelain | cut -c4- | sort -u)
  rm -rf "$RD"; mkdir -p "$RD"

  # 1) 生成(禁写 ideas/)
  run_stage "读 roles/generate.md,按其执行" generate; rc=$?; guard 0
  if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
  if [ ! -s "$RD/ideas.tsv" ]; then
    log "生成阶段未产出 ideas.tsv,本轮作废重试"; fails=0
    log "${SLEEP_MIN} 分钟后重试"; sleep "$((SLEEP_MIN * 60))"; continue
  fi

  # 2) 对抗式查重(禁写 ideas/)
  run_stage "读 roles/research.md,按其执行" research; rc=$?; guard 0
  if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
  if [ ! -s "$RD/priorwork.md" ]; then
    log "查重阶段未产出 priorwork.md,本轮作废重试"; fails=0
    log "${SLEEP_MIN} 分钟后重试"; sleep "$((SLEEP_MIN * 60))"; continue
  fi

  # 3) N 位裁判,并行 + 各自独立输入目录(开跑时看不到彼此产出)(F3);禁写 ideas/
  : > "$RD/rev_rc"; pids=()
  for r in $(seq 1 "$REVIEWERS"); do
    d="$RD/rev/$r"; mkdir -p "$d"
    cp "$RD/ideas.md" "$RD/priorwork.md" "$d/"
    log "调起 [review#${r}] (并行,独立目录 ${d}): $AGENT_CMD"
    ( $AGENT_CMD "读 roles/review.md,按其执行;输入只在 ${d}/(ideas.md 与 priorwork.md)+ 仓库根 rubric.md、brainstorming_policy.md;verdict 写 ${d}/verdict.tsv,完整评审写 ${d}/review.md" \
        >> "$RD/rev/${r}.log" 2>&1; printf '%s %s\n' "$r" "$?" >> "$RD/rev_rc" ) &
    pids+=("$!")
  done
  wait "${pids[@]}"
  guard 0
  # 需恰好 REVIEWERS 行、每行 rc=0;缺席/非0 均视为失败重试(BSD grep 的 -qv 组合不可靠,用 awk 判)
  if ! awk -v n="$REVIEWERS" 'NF==2 && $2==0{ok++} END{exit !(ok==n)}' "$RD/rev_rc"; then
    log "有裁判异常退出或缺席:$(tr '\n' ' ' < "$RD/rev_rc")"; fail_and_wait; continue
  fi

  # 4) 聚合(bash 定谳):取最低票,SA 需全票 + 过 SA 硬门槛(F4);缺失/无法解析的票一律当 reject(失败关闭)。
  cp "$LEDGER_GOOD" ledger.tsv                       # 干净基线,本轮增量只来自下面 bash 追加
  : > "$RD/accepted.tsv"; : > "$RD/rejects.tsv"
  sa_count=0
  while IFS=$'\t' read -r id story; do
    [ -z "$id" ] && continue
    min=2; reason=""
    for r in $(seq 1 "$REVIEWERS"); do
      line=$(awk -F'\t' -v id="$id" '$1==id{print; exit}' "$RD/rev/$r/verdict.tsv" 2>/dev/null || true)
      v=$(printf '%s' "$line" | cut -f2); rs=$(printf '%s' "$line" | cut -f4)
      rank=$(rank_of "$v")
      if [ "$rank" -lt "$min" ]; then min=$rank; reason=$rs; fi
      [ -z "$reason" ] && reason=$rs
    done
    if [ "$min" -eq 2 ] && ! sa_gate_ok "$id"; then
      min=0; reason="全票 SA 但查重/评审记录不达标(实读<${MIN_READ}、缺查重块或无完整评审),orchestrator 硬降级"
      log "SA 硬门槛未过,${id} 降级 reject"
    fi
    verdict=$(verdict_of "$min")
    [ -z "$reason" ] && reason="(无理由,按最严处理)"
    printf '%s\t%s\t%s\t%s\t%s\n' "$today" "hunt" "$story" "$verdict" "$reason" >> ledger.tsv
    if [ "$min" -eq 2 ]; then
      printf '%s\t%s\n' "$id" "$story" >> "$RD/accepted.tsv"; sa_count=$((sa_count + 1))
    else
      printf '%s\t%s\t%s\n' "$id" "$story" "$reason" >> "$RD/rejects.tsv"
    fi
  done < "$RD/ideas.tsv"
  cp ledger.tsv "$LEDGER_GOOD"                       # 定谳:把本轮 bash 追加固化为新的良好基线(F2)
  guard 0
  log "本轮聚合:${sa_count} 个 Strong Accept(全票且过硬门槛),已记账 ledger.tsv"

  # 5) 达标 → 组装报告并发布;否则继续循环
  if [ "$sa_count" -gt 0 ]; then
    printf '尝试轮数: %s\n评审日期: %s\n裁判数: %s\n' "$round" "$today" "$REVIEWERS" > "$RD/meta.txt"
    run_stage "读 roles/report.md,按其执行" report; rc=$?; guard 1   # 仅此阶段允许写 ideas/
    if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
    if ls "ideas/${today}"_hunt*.md >/dev/null 2>&1; then
      cp "$LEDGER_GOOD" ledger.tsv   # 发布前再确保 ledger = 定谳 good,抹掉 report 阶段对 ledger 的任何擅改
      ./publish.sh >> "$LOG" 2>&1 && { log "已发布,结束"; break; }
      log "publish.sh 失败,见 hunt.log;停机人工处理"; exit 2
    fi
    log "报告阶段声称达标但未写出 ideas/${today}_hunt*.md,本轮作废重试"
  fi

  fails=0
  log "运行完成但无达标报告(本轮 idea 未拿到全票 Strong Accept)"
  log "${SLEEP_MIN} 分钟后重试"
  sleep "$((SLEEP_MIN * 60))"
done
