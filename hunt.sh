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
# vs 正常跑完但无达标(随机短间隔继续重试)。
#
# 用法:
#   ./hunt.sh [异常重试间隔分钟,默认 150]
#   NO_HIT_SLEEP_MIN_LO/HI 正常无达标后的随机重试区间(默认 1-8 分钟,默认最小为 1);
#   ALLOW_ZERO_NO_HIT_SLEEP=1 仅用于测试,允许正常无达标后 0 分钟重试;
#   REVIEWERS 裁判票数(默认 3);MIN_READ SA 门槛要求的最少实读篇数(默认 3);
#   AGENT_CMD 指定 agent CLI(prompt 作为最后一个参数传入),例:
#     AGENT_CMD='claude -p' ./hunt.sh              # 默认;权限走 .claude/settings.json allowlist
#     AGENT_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' ./hunt.sh
#       # OS sandbox 写限本仓库;approval never + 放行网络(publish.sh 的 push/gh 需联网)。codex exec 不吃 -a,须用 -c approval_policy=
#   FRONT_CMD 覆盖前段(生成+查重)、BACK_CMD 覆盖后段(打分+报告);二者都默认回落到 AGENT_CMD,不设则行为与原来逐字节一致。
#   Level 1.5(agy 跑便宜前段,claude/codex 跑可信后段):
#     FRONT_CMD='./agy-worker.sh' BACK_CMD='claude -p' ./hunt.sh
#       # 前段用 agy:便宜、可错——错误 idea 由下游独立裁判 + SA 硬门槛毙掉,只会多重试几轮,不污染 verdict。
#       # 后段(verdict/报告)与 publish 全走 claude/codex:可信、可并行。agy 不碰 publish(其 CLI sandbox 可读写 $HOME,不能当边界)。
#   REV_CMD_1..REV_CMD_N 逐席位覆盖裁判命令(不设回落 BACK_CMD);REV_STAGGER_SEC 裁判错峰起跑秒数(默认 0)。
#   混合面板示例(1 codex + 1 claude + 1 agy):
#     REV_CMD_1='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' \
#     REV_CMD_2='claude -p' REV_CMD_3='./agy-worker.sh' REV_STAGGER_SEC=15 ./hunt.sh
#       # 取最低票 + SA 需全票 ⇒ 便宜裁判只能否决不能放水,SA 决定权仍在可信席位;至少留 1 席 claude/codex。
#       # agy 全席并发(3 个)认证会挂;混席时 agy ≤1 且配 REV_STAGGER_SEC 错峰。
#   前段空产出按"便宜可错"短重试:EMPTY_MAX 次内随机等 NO_HIT 区间,连续达 EMPTY_MAX 次才升级长睡(默认 3);
#   PRIOR_MIN_LINKS 查重结构门槛,每个 idea 块须有 ≥N 条带链接近邻,不达标视同空产出重试(默认 3);
#   PRIOR_MIN_API 查重结构门槛之二,每个 idea 块须有 ≥N 条结构化 API 检索记录(arXiv/Semantic Scholar query URL),
#     0 关闭(默认 1)——API 召回可复现、可审计,判定仍靠实读;近邻链接与 API 记录分开计数,互不充数;
#   THEME_MIN_LOW 主题门槛:本轮须有 ≥N 个 idea 落在 ledger 存量最少的三个主题(并列一并计入)内,
#     0 关闭分布校验(默认 2);theme 必须属 policy 主题词表,词表解析不出则跳过整项校验;
#   META_EVERY 每 N 轮做一次死因蒸馏(roles/meta.md → tmp/deathlist.md,默认 6),
#   META_MIN_REJECTS 拒行少于 N 时跳过蒸馏(默认 5)。蒸馏是可错阶段,失败只记日志不阻塞。
set -u
cd "$(dirname "$0")"
git config core.hooksPath .githooks   # 激活 pre-push 守卫:禁止直推 main

AGENT_CMD=${AGENT_CMD:-claude -p}
# 分段 agent:前段(生成+查重)便宜且可错,可换 agy;后段(打分+报告)决定 verdict 与发布产物,须可信(claude/codex)。
# 两者默认回落 AGENT_CMD——都不设时行为与原来完全一致。
FRONT_CMD=${FRONT_CMD:-$AGENT_CMD}
BACK_CMD=${BACK_CMD:-$AGENT_CMD}
FAIL_SLEEP_MIN=${FAIL_SLEEP_MIN:-${1:-150}}
NO_HIT_SLEEP_MIN_LO=${NO_HIT_SLEEP_MIN_LO:-1}
NO_HIT_SLEEP_MIN_HI=${NO_HIT_SLEEP_MIN_HI:-8}
ALLOW_ZERO_NO_HIT_SLEEP=${ALLOW_ZERO_NO_HIT_SLEEP:-0}
MAX_FAILS=${MAX_FAILS:-12}
REVIEWERS=${REVIEWERS:-3}
MIN_READ=${MIN_READ:-3}
REV_STAGGER_SEC=${REV_STAGGER_SEC:-0}
EMPTY_MAX=${EMPTY_MAX:-3}
PRIOR_MIN_LINKS=${PRIOR_MIN_LINKS:-3}
PRIOR_MIN_API=${PRIOR_MIN_API:-1}
THEME_MIN_LOW=${THEME_MIN_LOW:-2}
META_EVERY=${META_EVERY:-6}
META_MIN_REJECTS=${META_MIN_REJECTS:-5}
LOG=hunt.log
RD=tmp/round
LEDGER_GOOD=tmp/ledger.good
DEATHLIST=tmp/deathlist.md

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

is_uint() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }

validate_sleep_config() {
  is_uint "$FAIL_SLEEP_MIN" || { log "FAIL_SLEEP_MIN 必须是非负整数分钟: $FAIL_SLEEP_MIN"; exit 2; }
  is_uint "$NO_HIT_SLEEP_MIN_LO" || { log "NO_HIT_SLEEP_MIN_LO 必须是非负整数分钟: $NO_HIT_SLEEP_MIN_LO"; exit 2; }
  is_uint "$NO_HIT_SLEEP_MIN_HI" || { log "NO_HIT_SLEEP_MIN_HI 必须是非负整数分钟: $NO_HIT_SLEEP_MIN_HI"; exit 2; }
  case "$ALLOW_ZERO_NO_HIT_SLEEP" in
    0|1) ;;
    *) log "ALLOW_ZERO_NO_HIT_SLEEP 只能是 0 或 1: $ALLOW_ZERO_NO_HIT_SLEEP"; exit 2 ;;
  esac
  if [ "$ALLOW_ZERO_NO_HIT_SLEEP" != "1" ]; then
    if [ "$NO_HIT_SLEEP_MIN_LO" -lt 1 ] || [ "$NO_HIT_SLEEP_MIN_HI" -lt 1 ]; then
      log "NO_HIT_SLEEP_MIN_LO/HI 默认必须 >=1 分钟;测试需显式 ALLOW_ZERO_NO_HIT_SLEEP=1"
      exit 2
    fi
  fi
  if [ "$NO_HIT_SLEEP_MIN_LO" -gt "$NO_HIT_SLEEP_MIN_HI" ]; then
    log "NO_HIT_SLEEP_MIN_LO 不能大于 NO_HIT_SLEEP_MIN_HI: ${NO_HIT_SLEEP_MIN_LO}-${NO_HIT_SLEEP_MIN_HI}"
    exit 2
  fi
  is_uint "$REV_STAGGER_SEC" || { log "REV_STAGGER_SEC 必须是非负整数秒: $REV_STAGGER_SEC"; exit 2; }
  is_uint "$EMPTY_MAX" && [ "$EMPTY_MAX" -ge 1 ] || { log "EMPTY_MAX 必须是 >=1 的整数: $EMPTY_MAX"; exit 2; }
  is_uint "$PRIOR_MIN_LINKS" || { log "PRIOR_MIN_LINKS 必须是非负整数: $PRIOR_MIN_LINKS"; exit 2; }
  is_uint "$PRIOR_MIN_API" || { log "PRIOR_MIN_API 必须是非负整数: $PRIOR_MIN_API"; exit 2; }
  is_uint "$THEME_MIN_LOW" || { log "THEME_MIN_LOW 必须是非负整数: $THEME_MIN_LOW"; exit 2; }
  is_uint "$META_EVERY" && [ "$META_EVERY" -ge 1 ] || { log "META_EVERY 必须是 >=1 的整数: $META_EVERY"; exit 2; }
  is_uint "$META_MIN_REJECTS" || { log "META_MIN_REJECTS 必须是非负整数: $META_MIN_REJECTS"; exit 2; }
}

# 发散透镜:从 policy 的「## 发散透镜」小节随机抽一条(随机性在 bash 层,agent 不得自选);
# 小节缺失或为空则输出空串,本轮不注入。
pick_lens() {
  local n total
  total=$(awk '/^## 发散透镜/{f=1;next} /^## /{f=0} f&&/^- /' brainstorming_policy.md | grep -c . || true)
  [ "$total" -gt 0 ] || { echo ""; return 0; }
  n=$((RANDOM % total + 1))
  awk '/^## 发散透镜/{f=1;next} /^## /{f=0} f&&/^- /' brainstorming_policy.md | sed -n "${n}p" | sed 's/^- //'
}

sleep_minutes() {
  local minutes=$1
  log "${minutes} 分钟后重试"
  sleep "$((minutes * 60))"
}

random_no_hit_sleep_min() {
  echo $((NO_HIT_SLEEP_MIN_LO + RANDOM % (NO_HIT_SLEEP_MIN_HI - NO_HIT_SLEEP_MIN_LO + 1)))
}

# 调一次 agent(串行阶段用),rc 作为返回值;$1=命令 $2=prompt $3=阶段名
run_stage() {
  local cmd=$1
  log "调起 [$3]: $cmd"
  $cmd "$2" >> "$LOG" 2>&1
  return $?
}

# 固定层守卫:本轮相对 before/pre_dirty 的新增已跟踪改动只允许落在指定路径。
# $1=是否允许 ideas/(1/0)。ledger.tsv 恒视为在界内——其完整性由 ledger.good 重置保证,不靠此守卫。
guard() {
  local allow_ideas=${1:-0} pat changed bad committed_bad rolled_all p bad_after
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
    if git cat-file -e "$before:$p" 2>/dev/null; then
      if git restore --source="$before" --staged --worktree -- "$p" 2>/dev/null; then
        log "已回滚: $p"
      elif git reset -q "$before" -- "$p" 2>/dev/null && git checkout "$before" -- "$p" 2>/dev/null; then
        log "已回滚: $p"
      else
        log "未能回滚越界文件: $p"
        rolled_all=0
      fi
    else
      git reset -q HEAD -- "$p" 2>/dev/null || true
      log "未跟踪越界文件: $p"
      rolled_all=0
    fi
  done <<< "$bad"
  changed=$({ git diff --name-only "$before" HEAD; git status --porcelain | cut -c4-; } | sort -u)
  bad_after=$(comm -23 <(printf '%s\n' "$changed") <(printf '%s\n' "$pre_dirty") \
        | grep -vE "$pat" | grep -v '^$' || true)
  if [ "$rolled_all" -eq 0 ] || [ -n "$bad_after" ]; then
    log "存在未跟踪或未清干净的越界文件,停机,人工处理后再跑: $(echo "$bad_after" | tr '\n' ' ')"
    exit 2
  fi
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
  sleep_minutes "$FAIL_SLEEP_MIN"
}

# 前段(生成/查重)空产出:前段定位"便宜可错",EMPTY_MAX 次内走 NO_HIT 短随机重试;
# 连续达 EMPTY_MAX 次(疑似认证挂了等真故障)才升级为 FAIL_SLEEP 长睡,防空转死循环。
empty_and_wait() {
  local m
  empties=$((empties + 1))
  if [ "$empties" -ge "$EMPTY_MAX" ]; then
    log "前段连续 ${empties} 次空产出,疑似非偶发(认证/命令问题),升级长间隔重试"
    empties=0
    sleep_minutes "$FAIL_SLEEP_MIN"
  else
    m=$(random_no_hit_sleep_min)
    log "前段空产出(连续第 ${empties}/${EMPTY_MAX} 次),短重试 ${m} 分钟"
    sleep "$((m * 60))"
  fi
}

rank_of() { case "$1" in strong-accept) echo 2 ;; accept-w-rev) echo 1 ;; *) echo 0 ;; esac; }
verdict_of() { case "$1" in 2) echo strong-accept ;; 1) echo accept-w-rev ;; *) echo reject ;; esac; }

# SA 硬门槛:$1=id。要求 priorwork.md 有该 idea 的查重块、实读篇数≥MIN_READ、
# ideas.md 该 idea 块含「最小否证实验」(feasibility 的非叙事锚点,裁判只认它),
# 且每位裁判都写了该 idea 的完整评审块(全票 SA 本就意味着人人判 strong-accept、理应各附评审;
# 要求全员有块,report 死读 rev/1 才必然有料)。
sa_gate_ok() {
  local id=$1 block iblock n r fal
  [ -s "$RD/priorwork.md" ] || return 1
  block=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/priorwork.md")
  [ -n "$block" ] || return 1
  n=$(printf '%s\n' "$block" | grep '实读篇数' | grep -oE '[0-9]+' | head -1)
  [ -n "$n" ] && [ "$n" -ge "$MIN_READ" ] || return 1
  iblock=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/ideas.md" 2>/dev/null)
  # 字段须存在且冒号后有实内容(≥30 字节,防空字段/占位蹭过);语义真伪由裁判把关
  fal=$(printf '%s\n' "$iblock" | grep '最小否证实验' | head -1 | sed -E 's/^.*最小否证实验[[:space:]]*[::]?[[:space:]]*//')
  [ "$(printf '%s' "$fal" | wc -c | tr -d ' ')" -ge 30 ] || return 1
  for r in $(seq 1 "$REVIEWERS"); do
    grep -qE "^##[[:space:]]+${id}([[:space:]]|$)" "$RD/rev/$r/review.md" 2>/dev/null || return 1
  done
  return 0
}

# 查重结构门槛(前段机械校验,与 SA 门槛无关):每个 idea 在 priorwork.md 有块,
# 且块内 ≥PRIOR_MIN_LINKS 条带链接的近邻工作。不达标视同空产出——裁判"novelty 只认 priorwork",
# 查重太薄会让裁判在缺证据下瞎判,不如直接重跑前段。
priorwork_ok() {
  local id rest block links api
  while IFS=$'\t' read -r id rest; do
    [ -z "$id" ] && continue
    block=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/priorwork.md")
    if [ -z "$block" ]; then log "查重门槛:priorwork.md 缺 ${id} 块"; return 1; fi
    # 近邻只计「- 」bullet 且排除 API URL——API 记录不得给近邻数充数
    links=$(printf '%s\n' "$block" | grep -E '^- .*https?://' \
            | grep -cvE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org' || true)
    if [ "$links" -lt "$PRIOR_MIN_LINKS" ]; then
      log "查重门槛:${id} 带链接近邻不足(${links} < ${PRIOR_MIN_LINKS})"; return 1
    fi
    # 结构化 API 检索记录(arXiv / Semantic Scholar query URL):召回可复现、可审计
    if [ "$PRIOR_MIN_API" -gt 0 ]; then
      api=$(printf '%s\n' "$block" | grep -cE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org' || true)
      if [ "$api" -lt "$PRIOR_MIN_API" ]; then
        log "查重门槛:${id} 结构化 API 检索记录不足(${api} < ${PRIOR_MIN_API})"; return 1
      fi
    fi
  done < "$RD/ideas.tsv"
  return 0
}

# 主题门槛(生成阶段机械校验):theme 必须属 policy 主题词表(防生成端乱造标签污染台账);
# 且本轮 ≥THEME_MIN_LOW 个 idea 落在 ledger 存量最少的三个主题内(阈值取第三低存量,并列一并计入;
# 冷启动全零时全员达标)。词表从 policy「## 主题词表」小节首个非空行解析,解析不出则跳过整项校验。
themes_ok() {
  local vfile id rest theme low_hits
  vfile="$RD/themes.vocab"
  awk '/^## 主题词表/{f=1;next} /^## /{f=0} f&&NF' brainstorming_policy.md | head -1 \
    | tr '/' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$' > "$vfile" || true
  if [ ! -s "$vfile" ]; then
    log "主题门槛:policy 未解析出主题词表,跳过校验"; return 0
  fi
  while IFS=$'\t' read -r id rest theme; do
    [ -z "$id" ] && continue
    if ! grep -qxF "$theme" "$vfile"; then
      log "主题门槛:${id} 主题不在词表: '${theme}'"; return 1
    fi
  done < "$RD/ideas.tsv"
  [ "$THEME_MIN_LOW" -gt 0 ] || return 0
  low_hits=$(awk -F'\t' -v led=ledger.tsv '
    NR==FNR { cnt[$0]=0; next }
    FILENAME==led { if ($3 in cnt) cnt[$3]++; next }
    $1!="" { th[FNR]=$3 }
    END {
      n=0; for (t in cnt) v[++n]=cnt[t]
      for (i=1;i<=n;i++) for (j=i+1;j<=n;j++) if (v[j]<v[i]) { x=v[i]; v[i]=v[j]; v[j]=x }
      thresh = (n>=3 ? v[3] : v[n])
      hits=0
      for (k in th) if (th[k] in cnt && cnt[th[k]]<=thresh) hits++
      print hits
    }' "$vfile" ledger.tsv "$RD/ideas.tsv")
  if [ "$low_hits" -lt "$THEME_MIN_LOW" ]; then
    log "主题门槛:低存量主题覆盖不足(${low_hits} < ${THEME_MIN_LOW}),疑似跨轮模式坍缩"; return 1
  fi
  return 0
}

mkdir -p "$(dirname "$LEDGER_GOOD")"                             # tmp/ 在干净 checkout 里不存在,先建(否则种子/重置全失败)
validate_sleep_config
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
empties=0
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

  # 0) 死因蒸馏(可错,失败不阻塞):每 META_EVERY 轮、拒行足量时,让独立进程把 ledger 拒因
  #    归纳成 tmp/deathlist.md,生成阶段据此避开高频失败模式(Co-Scientist meta-review 的廉价版)。
  rejects_now=$(awk -F'\t' '$5=="reject"' ledger.tsv 2>/dev/null | grep -c . || true)
  if [ $(( (round - 1) % META_EVERY )) -eq 0 ] && [ "$rejects_now" -ge "$META_MIN_REJECTS" ]; then
    run_stage "$FRONT_CMD" "读 roles/meta.md,按其执行" meta; rc=$?; guard 0
    if [ "$rc" -ne 0 ] || [ ! -s "$DEATHLIST" ]; then
      log "死因蒸馏失败或无产出,忽略并继续(沿用旧清单或无清单)"
    else
      log "死因清单已更新: $DEATHLIST(基于 ${rejects_now} 行拒记录)"
    fi
  fi

  # 1) 生成(禁写 ideas/);发散透镜由 bash 随机抽取注入,对抗跨轮模式坍缩
  lens=$(pick_lens)
  gen_prompt="读 roles/generate.md,按其执行"
  if [ -n "$lens" ]; then
    gen_prompt="${gen_prompt};本轮发散透镜(orchestrator 随机指定,不得替换):${lens}"
    log "本轮发散透镜: ${lens}"
  fi
  run_stage "$FRONT_CMD" "$gen_prompt" generate; rc=$?; guard 0
  if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
  if [ ! -s "$RD/ideas.tsv" ] || ! themes_ok; then
    log "生成阶段未产出 ideas.tsv 或主题结构不达标,本轮作废重试"; fails=0
    empty_and_wait; continue
  fi

  # 2) 对抗式查重(禁写 ideas/)
  run_stage "$FRONT_CMD" "读 roles/research.md,按其执行" research; rc=$?; guard 0
  if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
  if [ ! -s "$RD/priorwork.md" ] || ! priorwork_ok; then
    log "查重阶段未产出 priorwork.md 或结构不达标,本轮作废重试"; fails=0
    empty_and_wait; continue
  fi
  empties=0                                          # 前段两阶段产物齐备,空产出计数清零

  # 3) N 位裁判,并行 + 各自独立输入目录(开跑时看不到彼此产出)(F3);禁写 ideas/
  : > "$RD/rev_rc"; pids=()
  for r in $(seq 1 "$REVIEWERS"); do
    d="$RD/rev/$r"; mkdir -p "$d"
    cp "$RD/ideas.md" "$RD/priorwork.md" "$d/"
    rev_cmd_var="REV_CMD_$r"; rev_cmd=${!rev_cmd_var:-$BACK_CMD}   # 混合面板:逐席位覆盖,不设回落 BACK_CMD
    log "调起 [review#${r}] (并行,独立目录 ${d}): $rev_cmd"
    ( if [ "$r" -gt 1 ] && [ "$REV_STAGGER_SEC" -gt 0 ]; then sleep "$(( (r - 1) * REV_STAGGER_SEC ))"; fi
      $rev_cmd "读 roles/review.md,按其执行;输入只在 ${d}/(ideas.md 与 priorwork.md)+ 仓库根 rubric.md、brainstorming_policy.md;verdict 写 ${d}/verdict.tsv,完整评审写 ${d}/review.md" \
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
  while IFS=$'\t' read -r id story theme; do
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
      min=0; reason="全票 SA 但硬门槛不达标(实读<${MIN_READ}、缺查重块、缺最小否证实验或无完整评审),orchestrator 硬降级"
      log "SA 硬门槛未过,${id} 降级 reject"
    fi
    verdict=$(verdict_of "$min")
    [ -z "$reason" ] && reason="(无理由,按最严处理)"
    [ -z "$theme" ] && theme="未标注"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$today" "hunt" "$theme" "$story" "$verdict" "$reason" >> ledger.tsv
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
    run_stage "$BACK_CMD" "读 roles/report.md,按其执行" report; rc=$?; guard 1   # 仅此阶段允许写 ideas/
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
  sleep_min=$(random_no_hit_sleep_min)
  log "正常无达标,随机等待 ${NO_HIT_SLEEP_MIN_LO}-${NO_HIT_SLEEP_MIN_HI} 分钟;本次 ${sleep_min} 分钟后重试"
  sleep "$((sleep_min * 60))"
done
