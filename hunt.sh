#!/usr/bin/env bash
# 外层循环:每轮把一批 idea 走完「生成 → 预筛(杀 direct hit)→ 对抗式深查重 → N 位裁判打分」,
# 由本脚本(而非任何 agent)聚合 verdict、写 ledger、发布。当日全票 Strong Accept 累计达 SA_TARGET(默认 1)即停。
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
#   REVIEWERS 裁判票数(默认 3);MIN_READ SA 门槛要求的最少实读篇数(默认 5);
#   SA_TARGET 当日 Strong Accept 目标数,累计达标才停(默认 1,行为同旧版;0=不设上限一直攒,Ctrl-C 手动停);
#     >1 或 0 时同日多份报告按 roles/report.md 加 -2/-3 后缀,publish.sh 幂等追加进同一当日分支与 PR;
#   AGENT_CMD 指定 agent CLI(prompt 作为最后一个参数传入),例:
#     AGENT_CMD='claude -p --strict-mcp-config' ./hunt.sh   # 默认;权限走 .claude/settings.json allowlist
#     AGENT_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' ./hunt.sh
#       # OS sandbox 写限本仓库;approval never + 放行网络(publish.sh 的 push/gh 需联网)。codex exec 不吃 -a,须用 -c approval_policy=
#   FRONT_CMD 覆盖前段(生成+查重)、BACK_CMD 覆盖后段(打分+报告);二者都默认回落到 AGENT_CMD,不设则行为与原来逐字节一致。
#   Level 1.5(agy 跑便宜前段,claude/codex 跑可信后段):
#     FRONT_CMD='./agy-worker.sh' BACK_CMD='claude -p --strict-mcp-config' ./hunt.sh
#       # 前段用 agy:便宜、可错——错误 idea 由下游独立裁判 + SA 硬门槛毙掉,只会多重试几轮,不污染 verdict。
#       # 后段(verdict/报告)与 publish 全走 claude/codex:可信、可并行。agy 不碰 publish(其 CLI sandbox 可读写 $HOME,不能当边界)。
#   REV_CMD_1..REV_CMD_N 逐席位覆盖裁判命令(不设回落 BACK_CMD);REV_STAGGER_SEC 裁判错峰起跑秒数(默认 0)。
#   混合面板示例(1 codex + 1 claude + 1 agy):
#     REV_CMD_1='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' \
#     REV_CMD_2='claude -p --strict-mcp-config' REV_CMD_3='./agy-worker.sh' REV_STAGGER_SEC=15 ./hunt.sh
#       # 取最低票 + SA 需全票 ⇒ 便宜裁判只能否决不能放水,SA 决定权仍在可信席位;至少留 1 席 claude/codex。
#       # agy 快速重复调起会触发登录验证;agy-worker.sh 内置启动闸门(AGY_LAUNCH_GAP_SEC,默认 60s)
#       # 自动错峰所有 agy 席位,REV_STAGGER_SEC 可再减少闸门排队。仍不要把全部裁判席交给 agy(须留可信席位)。
#   前段空产出按"便宜可错"短重试:EMPTY_MAX 次内随机等 NO_HIT 区间,连续达 EMPTY_MAX 次才升级长睡(默认 3);
#   预筛(生成与深查之间,FRONT_CMD 跑,便宜可错、只杀不保):只杀"单篇工作直接占据头条"的 direct hit,
#     被杀 idea 由本脚本立即按 reject 入账、overlap=high(防下轮重生成);存活取前 SHORT_MAX 个(默认 3)
#     进深查,超额 keep 丢弃不入账(下轮可重新生成),全灭走空产出短重试;
#   PRIOR_MIN_LINKS 查重结构门槛,每个 idea 块须有 ≥N 条带链接近邻,不达标视同空产出重试(默认 5);
#   PRIOR_MIN_API 查重结构门槛之二,每个 idea 块须有 ≥N 条结构化 API 检索记录(arXiv/Semantic Scholar query URL),
#     0 关闭(默认 1)——API 召回可复现、可审计,判定仍靠实读;近邻链接与 API 记录分开计数,互不充数;
#   AXIOM_MIN_CRACKS 删承重假设形态门槛(默认 2):生成块自报裂缝证据 URL 行数、查重块核验行数,
#     以及 SA 硬门槛时「核验:相符」行数,均须 ≥N;生成全集另须「删公理尝试」标记行(成 I<n> 或 未成+卡点);
#   THEME_MIN_LOW 主题门槛:本轮须有 ≥N 个 idea 落在 ledger 存量最少的三个主题(并列一并计入)内,
#     0 关闭分布校验(默认 2);theme 必须属 policy 主题词表,词表解析不出则跳过整项校验;
#   META_EVERY 每 N 轮做一次失败蒸馏(roles/meta.md → tmp/deathlist.md,默认 6),
#   META_MIN_REJECTS 失败行(reject+accept-w-rev)少于 N 时跳过蒸馏(默认 5)。蒸馏是可错阶段,失败只记日志不阻塞。
#   中断恢复:tmp/hunt.lock 实例锁防同目录双开(持锁进程已死则自动清陈旧锁);
#   启动时当日 SA 计数(以 ledger 基线为准)已达标则先跑一次幂等的 publish.sh 补发布再退;
#   已有当日报告但未达标(发布后上调 SA_TARGET 重启,或 report 写完 publish 没跑成)则启动先补发布再继续攒;
#   RESUME_FRONT=1(默认)时,中断遗留的前段产物(ideas+priorwork)过机械门槛则首轮跳过生成/预筛/查重续跑;
#   评审票据/聚合残留一律作废、裁判由本进程重新调起——verdict 永不续用,防前段借崩溃伪造票据绕过独立评审。
set -u
cd "$(dirname "$0")"
git config core.hooksPath .githooks   # 激活 pre-push 守卫:禁止直推 main

AGENT_CMD=${AGENT_CMD:-claude -p --strict-mcp-config}
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
MIN_READ=${MIN_READ:-5}
SA_TARGET=${SA_TARGET:-1}
REV_STAGGER_SEC=${REV_STAGGER_SEC:-0}
EMPTY_MAX=${EMPTY_MAX:-3}
PRIOR_MIN_LINKS=${PRIOR_MIN_LINKS:-5}
PRIOR_MIN_API=${PRIOR_MIN_API:-1}
SHORT_MAX=${SHORT_MAX:-3}
THEME_MIN_LOW=${THEME_MIN_LOW:-2}
AXIOM_MIN_CRACKS=${AXIOM_MIN_CRACKS:-2}
META_EVERY=${META_EVERY:-6}
META_MIN_REJECTS=${META_MIN_REJECTS:-5}
RESUME_FRONT=${RESUME_FRONT:-1}
LOG=hunt.log
RD=tmp/round
LEDGER_GOOD=tmp/ledger.good
DEATHLIST=tmp/deathlist.md
LOCK=tmp/hunt.lock

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
  is_uint "$SHORT_MAX" && [ "$SHORT_MAX" -ge 1 ] || { log "SHORT_MAX 必须是 >=1 的整数: $SHORT_MAX"; exit 2; }
  is_uint "$THEME_MIN_LOW" || { log "THEME_MIN_LOW 必须是非负整数: $THEME_MIN_LOW"; exit 2; }
  is_uint "$AXIOM_MIN_CRACKS" && [ "$AXIOM_MIN_CRACKS" -ge 1 ] || { log "AXIOM_MIN_CRACKS 必须是 >=1 的整数: $AXIOM_MIN_CRACKS"; exit 2; }
  is_uint "$META_EVERY" && [ "$META_EVERY" -ge 1 ] || { log "META_EVERY 必须是 >=1 的整数: $META_EVERY"; exit 2; }
  is_uint "$META_MIN_REJECTS" || { log "META_MIN_REJECTS 必须是非负整数: $META_MIN_REJECTS"; exit 2; }
  is_uint "$SA_TARGET" || { log "SA_TARGET 必须是非负整数(0=不设上限): $SA_TARGET"; exit 2; }
  case "$RESUME_FRONT" in
    0|1) ;;
    *) log "RESUME_FRONT 只能是 0 或 1: $RESUME_FRONT"; exit 2 ;;
  esac
}

# 发散透镜:从 policy 的「## 发散透镜」小节随机抽一条(随机性在 bash 层,agent 不得自选);
# 抽签池含 3 张空白牌(与 policy「抽签池」行同步),抽中输出空串、本轮不注入;
# 小节缺失或为空同样输出空串。
pick_lens() {
  local n total
  total=$(awk '/^## 发散透镜/{f=1;next} /^## /{f=0} f&&/^- /' brainstorming_policy.md | grep -c . || true)
  [ "$total" -gt 0 ] || { echo ""; return 0; }
  n=$((RANDOM % (total + 3) + 1))
  [ "$n" -le "$total" ] || { echo ""; return 0; }
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

# 当日(hunt 源)Strong Accept 计数:只数 bash 定谳基线 $LEDGER_GOOD,不信工作树 ledger
sa_today() {
  awk -F'\t' -v d="$today" '$1==d && $2=="hunt" && $5=="strong-accept"{n++} END{print n+0}' "$LEDGER_GOOD" 2>/dev/null || echo 0
}

# 当日报告文件数:同日多份报告(-2/-3 后缀)时,报告是否写出须按"数量新增"判,存在性检查会被旧报告蹭过
reports_today() { ls "ideas/${today}"_hunt*.md 2>/dev/null | grep -c . || true; }

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
  # 删承重假设形态:独立查重实读核验的「相符」行须 ≥AXIOM_MIN_CRACKS(裂缝确属实,防话术合规蹭 SA)
  if is_axiom_idea "$id" "$RD/ideas.md"; then
    n=$(printf '%s\n' "$block" | grep -cE '核验[::][[:space:]]*相符' || true)
    [ "$n" -ge "$AXIOM_MIN_CRACKS" ] || return 1
  fi
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
  local tsv vfile id rest theme low_hits
  tsv=${1:-$RD/ideas.tsv}
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
  done < "$tsv"
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
    }' "$vfile" ledger.tsv "$tsv")
  if [ "$low_hits" -lt "$THEME_MIN_LOW" ]; then
    log "主题门槛:低存量主题覆盖不足(${low_hits} < ${THEME_MIN_LOW}),疑似跨轮模式坍缩"; return 1
  fi
  return 0
}

# 预筛判定读取:$1=id → stdout 输出 kill|keep|空(块缺失/判定非法)
prescreen_dec() {
  awk -v id="$1" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/prescreen.md" 2>/dev/null \
    | grep -m1 '^判定' | grep -oE 'kill|keep' | head -1
}

# 预筛结构门槛(机械校验):ideas.all.tsv 每个 id 在 prescreen.md 有块、块内 ≥1 条结构化 API 检索记录、
# 判定 ∈ {kill,keep}、kill 必附占位工作链接(非 API URL)。预筛定位"便宜可错、只杀不保":
# 结构不达标视同空产出重跑;keep 不构成任何 novelty 结论,深查与裁判照常对抗。
prescreen_ok() {
  local id rest block dec link
  while IFS=$'\t' read -r id rest; do
    [ -z "$id" ] && continue
    block=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/prescreen.md")
    if [ -z "$block" ]; then log "预筛门槛:prescreen.md 缺 ${id} 块"; return 1; fi
    if ! printf '%s\n' "$block" | grep -qE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org'; then
      log "预筛门槛:${id} 缺结构化 API 检索记录"; return 1
    fi
    dec=$(prescreen_dec "$id")
    case "$dec" in
      keep) ;;
      kill)
        link=$(printf '%s\n' "$block" | grep -oE 'https?://[^ )|,;>]+' \
               | grep -vE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org' | head -1)
        if [ -z "$link" ]; then log "预筛门槛:${id} 判 kill 但未附占位链接"; return 1; fi
        ;;
      *) log "预筛门槛:${id} 判定缺失或非法"; return 1 ;;
    esac
  done < "$RD/ideas.all.tsv"
  return 0
}

# 删承重假设形态(机械校验,语义真伪由查重核验与裁判把关):
# - 生成全集须带「删公理尝试」标记行(ideas.md 首个 ## 之前):成 I<n> 或 未成——一句话候选与卡点。
#   配额落在 10 个原料候选;未成标记不是 idea、不进 ideas.tsv,自然不入 ledger。
# - 形态含「删承重假设」的每块须齐三个专属字段行 + 裂缝证据 ≥AXIOM_MIN_CRACKS 条带 URL(自报待核验);
#   第五字段复用「最小否证实验」行,由 SA 硬门槛与裁判把关。
is_axiom_idea() {   # $1=id $2=ideas.md 路径
  awk -v id="$1" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$2" 2>/dev/null \
    | grep -q '^形态[::].*删承重假设'
}
axiom_ok() {        # $1=ideas.md $2=ideas.tsv $3=是否要求标记行(1/0;续跑的 shortlist 无标记行)
  local md=$1 tsv=$2 need_marker=${3:-1} marker m_id id rest block field val urls
  if [ "$need_marker" = "1" ]; then
    marker=$(grep -m1 '^删公理尝试[::]' "$md" || true)
    if [ -z "$marker" ]; then log "删公理门槛:缺「删公理尝试:」标记行(成 I<n> 或 未成+卡点)"; return 1; fi
    if printf '%s' "$marker" | grep -q '未成'; then
      val=${marker#*未成}
      if [ "$(printf '%s' "$val" | wc -c | tr -d ' ')" -lt 30 ]; then
        log "删公理门槛:未成标记须附一句话候选与卡在哪个字段"; return 1
      fi
    else
      m_id=$(printf '%s' "$marker" | grep -oE 'I[0-9]+' | head -1)
      if [ -z "$m_id" ] || ! is_axiom_idea "$m_id" "$md"; then
        log "删公理门槛:成标记未点名有效的删承重假设块(${m_id:-无 id})"; return 1
      fi
    fi
  fi
  while IFS=$'\t' read -r id rest; do
    [ -z "$id" ] && continue
    is_axiom_idea "$id" "$md" || continue
    block=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$md")
    for field in 删哪条承重假设 为何现在能删 'forcing constraint'; do
      val=$(printf '%s\n' "$block" | grep -m1 "^${field}[::]" | sed -E "s/^${field}[::][[:space:]]*//")
      if [ "$(printf '%s' "$val" | wc -c | tr -d ' ')" -lt 12 ]; then
        log "删公理门槛:${id} 字段「${field}」缺失或空洞"; return 1
      fi
    done
    urls=$(printf '%s\n' "$block" | grep -c '^裂缝证据[::].*https\?://' || true)
    if [ "$urls" -lt "$AXIOM_MIN_CRACKS" ]; then
      log "删公理门槛:${id} 裂缝证据带 URL 行不足(${urls} < ${AXIOM_MIN_CRACKS})"; return 1
    fi
  done < "$tsv"
  return 0
}
# 查重侧:删承重假设块须有「裂缝证据核验」节,核验行(相符/部分/不符/不可达)≥AXIOM_MIN_CRACKS。
# 这里只锁结构与核验存在性;「相符」条数到 SA 硬门槛才要求,不符/不可达如实记录即合规。
cracks_ok() {
  local id rest block n
  while IFS=$'\t' read -r id rest; do
    [ -z "$id" ] && continue
    is_axiom_idea "$id" "$RD/ideas.md" || continue
    block=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/priorwork.md")
    if ! printf '%s\n' "$block" | grep -q '裂缝证据核验'; then
      log "查重门槛:${id} 缺「裂缝证据核验」节"; return 1
    fi
    n=$(printf '%s\n' "$block" | grep -cE '核验[::][[:space:]]*(相符|部分|不符|不可达)' || true)
    if [ "$n" -lt "$AXIOM_MIN_CRACKS" ]; then
      log "查重门槛:${id} 裂缝核验行不足(${n} < ${AXIOM_MIN_CRACKS})"; return 1
    fi
  done < "$RD/ideas.tsv"
  return 0
}

mkdir -p "$(dirname "$LEDGER_GOOD")"                             # tmp/ 在干净 checkout 里不存在,先建(否则种子/重置全失败)
validate_sleep_config
if [ "$SA_TARGET" -gt 0 ]; then TARGET_DESC="$SA_TARGET"; else TARGET_DESC="∞(不设上限)"; fi

# 实例锁:同目录双开会互踩(共享 tmp/round 与 ledger 基线、守卫误杀、同日分支撞车)。
# mkdir 原子抢锁;持锁进程已死则清陈旧锁重抢。确认无实例时可手动 rm -rf tmp/hunt.lock。
if ! mkdir "$LOCK" 2>/dev/null; then
  other=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
    log "已有 hunt.sh 实例在跑(pid ${other}),退出"
    exit 2
  fi
  log "清理陈旧实例锁(原持有 pid ${other:-未知} 已不在)"
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || { log "抢锁失败(并发启动?),退出"; exit 2; }
fi
echo $$ > "$LOCK/pid"

# 启动瞬间的工作树 ledger 视为人工/operator 基线;后续阶段中 agent 的任何擅改都只会被重置回此基线。
if [ -f ledger.tsv ]; then
  if ! git diff --quiet -- ledger.tsv 2>/dev/null; then
    log "启动基线: ledger.tsv 有未提交改动,按当前工作树作为人工基线"
  fi
  cp ledger.tsv "$LEDGER_GOOD"
else
  git show "HEAD:ledger.tsv" > "$LEDGER_GOOD" 2>/dev/null || : > "$LEDGER_GOOD"
fi
# 任何退出(含 MAX_FAILS、守卫停机、Ctrl-C)都把 ledger.tsv 还原到最近一次 bash 定谳的 good,绝不留篡改;顺带释放实例锁
trap 'cp "$LEDGER_GOOD" ledger.tsv 2>/dev/null || true; rm -rf "$LOCK"' EXIT

# 前段续跑检测:中断遗留的前段产物(生成+查重)过机械门槛则首轮跳过生成/查重,省掉已花的调用费。
# 只信前段产物(它们本就是 agent 产物、由门槛+裁判消化);评审票据残留在循环内一律清除,verdict 永不续用。
resume_front=0
if [ "$RESUME_FRONT" = "1" ] && [ -s "$RD/ideas.tsv" ] && [ -s "$RD/ideas.md" ] && [ -s "$RD/priorwork.md" ]; then
  # 主题门槛查发散全集(ideas.all.tsv,预筛前的 4-6 个);老格式遗留(无 all 文件)退回查 ideas.tsv
  themes_src="$RD/ideas.tsv"
  [ -s "$RD/ideas.all.tsv" ] && themes_src="$RD/ideas.all.tsv"
  if themes_ok "$themes_src" && axiom_ok "$RD/ideas.md" "$RD/ideas.tsv" 0 && priorwork_ok && cracks_ok; then
    resume_front=1
    log "检测到中断遗留的前段产物且过机械门槛,首轮续跑(跳过生成/预筛/查重,裁判照常重跑)"
  else
    log "中断遗留的前段产物不过门槛,按常规重跑"
  fi
fi

fails=0
empties=0
round=0
while :; do
  today=$(date +%F)
  sa_now=$(sa_today)
  if [ "$SA_TARGET" -gt 0 ] && [ "$sa_now" -ge "$SA_TARGET" ]; then
    if ls "ideas/${today}"_hunt*.md >/dev/null 2>&1; then
      # 报告可能写完但发布被中断:publish.sh 幂等,先补发布再退(确无待发布改动时为空跑)
      if ./publish.sh >> "$LOG" 2>&1; then
        log "当日 Strong Accept 累计 ${sa_now},已达目标 ${SA_TARGET}(已确保发布),结束"
        break
      fi
      log "当日已达标但补发布失败,见 hunt.log;停机人工处理"
      exit 2
    fi
    # 罕见中断态:ledger 已计达标但当日无报告(上次死在聚合后、报告前)。继续跑轮补出报告——
    # 新报告只覆盖新达标轮,孤儿 SA 行留在 ledger 随发布入库;可能超额一轮,可接受。
    log "当日 SA 计数 ${sa_now} 已达目标 ${SA_TARGET} 但缺当日报告(疑似聚合后中断),继续跑轮补报告"
  elif [ "$round" -eq 0 ] && ls "ideas/${today}"_hunt*.md >/dev/null 2>&1; then
    # 启动时未达标但已有当日报告(发布后上调 SA_TARGET 重启,或 report 写完 publish 被中断):
    # 先幂等补发布,再继续攒。仅启动做——运行中每次报告写出后都紧跟 publish,失败即停,不会滞留。
    ./publish.sh >> "$LOG" 2>&1 || { log "启动补发布失败,见 hunt.log;停机人工处理"; exit 2; }
    log "当日 Strong Accept 累计 ${sa_now}/${TARGET_DESC},已有报告已确保发布,继续攒"
  fi

  round=$((round + 1))
  before=$(git rev-parse HEAD)
  cp "$LEDGER_GOOD" ledger.tsv                       # 重置到上一轮定谳后的良好 ledger,抹掉任何遗留篡改(F2)
  pre_dirty=$(git status --porcelain | cut -c4- | sort -u)

  if [ "$resume_front" = "1" ]; then
    # 前段续跑:沿用遗留 ideas.tsv/ideas.md/priorwork.md(启动时已过门槛),直接进评审。
    # 评审及以后的残留必须清除——遗留 rev/ 里的票据与评审块可能是前段伪造,verdict 永不续用。
    resume_front=0
    rm -rf "$RD/rev"
    rm -f "$RD/rev_rc" "$RD/accepted.tsv" "$RD/rejects.tsv" "$RD/meta.txt"
    empties=0
    log "续跑:沿用中断遗留的前段产物,跳过生成/预筛/查重,直接进入评审"
  else
    rm -rf "$RD"; mkdir -p "$RD"

    # 0) 失败蒸馏(可错,失败不阻塞):每 META_EVERY 轮、失败行足量时,让独立进程把 ledger 的
    #    reject 拒因与 accept-w-rev 封顶原因归纳成 tmp/deathlist.md(致命模式/封顶模式/进化候选),
    #    生成阶段据此避开高频失败模式、选对进化父本(Co-Scientist meta-review 的廉价版)。
    fails_now=$(awk -F'\t' '$5=="reject" || $5=="accept-w-rev"' ledger.tsv 2>/dev/null | grep -c . || true)
    if [ $(( (round - 1) % META_EVERY )) -eq 0 ] && [ "$fails_now" -ge "$META_MIN_REJECTS" ]; then
      run_stage "$FRONT_CMD" "读 roles/meta.md,按其执行" meta; rc=$?; guard 0
      if [ "$rc" -ne 0 ] || [ ! -s "$DEATHLIST" ]; then
        log "失败蒸馏无产出或失败,忽略并继续(沿用旧清单或无清单)"
      else
        log "失败清单已更新: $DEATHLIST(基于 ${fails_now} 行 reject/AwR 记录)"
      fi
    fi

    # 1) 生成(禁写 ideas/);发散透镜由 bash 随机抽取注入,对抗跨轮模式坍缩
    lens=$(pick_lens)
    gen_prompt="读 roles/generate.md,按其执行"
    if [ -n "$lens" ]; then
      gen_prompt="${gen_prompt};本轮发散透镜(orchestrator 随机指定,不得替换):${lens}"
      log "本轮发散透镜: ${lens}"
    else
      log "本轮无透镜注入(空白牌或小节缺失),自由发散"
    fi
    run_stage "$FRONT_CMD" "$gen_prompt" generate; rc=$?; guard 0
    if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
    if [ ! -s "$RD/ideas.tsv" ] || ! themes_ok || ! axiom_ok "$RD/ideas.md" "$RD/ideas.tsv" 1; then
      log "生成阶段未产出 ideas.tsv 或主题/删公理结构不达标,本轮作废重试"; fails=0
      empty_and_wait; continue
    fi

    # 1.5) 预筛(禁写 ideas/;便宜可错、只杀不保):深查花钱前杀掉"单篇直接占据头条"的 direct hit。
    #      agent 只给判定与证据链接;shortlist 与 kill 台账由本脚本机械构建,防预筛擅改候选内容。
    mv "$RD/ideas.tsv" "$RD/ideas.all.tsv"
    mv "$RD/ideas.md" "$RD/ideas.all.md"
    run_stage "$FRONT_CMD" "读 roles/prescreen.md,按其执行" prescreen; rc=$?; guard 0
    if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
    if [ ! -s "$RD/prescreen.md" ] || ! prescreen_ok; then
      log "预筛阶段未产出 prescreen.md 或结构不达标,本轮作废重试"; fails=0
      empty_and_wait; continue
    fi
    : > "$RD/ideas.tsv"; : > "$RD/ideas.md"; : > "$RD/kills.tsv"
    kept=0
    while IFS=$'\t' read -r id story theme; do
      [ -z "$id" ] && continue
      if [ "$(prescreen_dec "$id")" = "keep" ]; then
        if [ "$kept" -lt "$SHORT_MAX" ]; then
          printf '%s\t%s\t%s\n' "$id" "$story" "$theme" >> "$RD/ideas.tsv"
          awk -v id="$id" '$1=="##"{f=($2==id)} f' "$RD/ideas.all.md" >> "$RD/ideas.md"
          printf '\n' >> "$RD/ideas.md"
          kept=$((kept + 1))
        else
          log "预筛:${id} keep 但超出 SHORT_MAX=${SHORT_MAX},本轮不深查、不入账(下轮可重新生成)"
        fi
      else
        kill_url=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/prescreen.md" \
                   | grep -oE 'https?://[^ )|,;>]+' \
                   | grep -vE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org' | head -1)
        printf '%s\t%s\t%s\t%s\n' "$id" "$story" "$theme" "$kill_url" >> "$RD/kills.tsv"
      fi
    done < "$RD/ideas.all.tsv"
    # kill 行立即 bash 定谳入账(verdict=reject,overlap=high):防同类 idea 下轮重生成;
    # 预筛错杀只损失单一 idea 族(多花几轮重试),不污染 verdict,可接受。
    if [ -s "$RD/kills.tsv" ]; then
      cp "$LEDGER_GOOD" ledger.tsv
      while IFS=$'\t' read -r id story theme kill_url; do
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$today" "hunt" "$theme" "$story" "reject" "预筛直接占位: $kill_url" "high" >> ledger.tsv
      done < "$RD/kills.tsv"
      cp ledger.tsv "$LEDGER_GOOD"
      log "预筛:$(grep -c . "$RD/kills.tsv") 个 direct hit 已按 reject 入账"
    fi
    if [ "$kept" -eq 0 ]; then
      log "预筛全灭:本轮候选头条均被直接占位,作废重试"; fails=0
      empty_and_wait; continue
    fi
    log "预筛:${kept} 个存活进深查"

    # 2) 对抗式深查重(禁写 ideas/;只查预筛存活的 shortlist,每个 idea 5-8 篇实读)
    run_stage "$FRONT_CMD" "读 roles/research.md,按其执行" research; rc=$?; guard 0
    if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
    if [ ! -s "$RD/priorwork.md" ] || ! priorwork_ok || ! cracks_ok; then
      log "查重阶段未产出 priorwork.md 或结构(含裂缝核验)不达标,本轮作废重试"; fails=0
      empty_and_wait; continue
    fi
    empties=0                                        # 前段两阶段产物齐备,空产出计数清零
  fi

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
      min=0; reason="全票 SA 但硬门槛不达标(实读<${MIN_READ}、缺查重块、缺最小否证实验、无完整评审或删公理裂缝核验相符不足),orchestrator 硬降级"
      log "SA 硬门槛未过,${id} 降级 reject"
    fi
    verdict=$(verdict_of "$min")
    [ -z "$reason" ] && reason="(无理由,按最严处理)"
    [ -z "$theme" ] && theme="未标注"
    # overlap 列:取独立查重的「重叠判定」(high/medium/low),供进化父本资格的机械筛选
    overlap=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/priorwork.md" 2>/dev/null \
              | grep -m1 '重叠判定' | grep -oE 'high|medium|low' | head -1)
    [ -z "$overlap" ] && overlap="未知"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$today" "hunt" "$theme" "$story" "$verdict" "$reason" "$overlap" >> ledger.tsv
    if [ "$min" -eq 2 ]; then
      printf '%s\t%s\n' "$id" "$story" >> "$RD/accepted.tsv"; sa_count=$((sa_count + 1))
    else
      printf '%s\t%s\t%s\n' "$id" "$story" "$reason" >> "$RD/rejects.tsv"
    fi
  done < "$RD/ideas.tsv"
  cp ledger.tsv "$LEDGER_GOOD"                       # 定谳:把本轮 bash 追加固化为新的良好基线(F2)
  guard 0
  log "本轮聚合:${sa_count} 个 Strong Accept(全票且过硬门槛),已记账 ledger.tsv"

  # 5) 达标轮 → 组装报告并发布;当日累计达 SA_TARGET 才停,否则继续攒
  if [ "$sa_count" -gt 0 ]; then
    printf '尝试轮数: %s\n评审日期: %s\n裁判数: %s\n' "$round" "$today" "$REVIEWERS" > "$RD/meta.txt"
    reports_before=$(reports_today)
    run_stage "$BACK_CMD" "读 roles/report.md,按其执行" report; rc=$?; guard 1   # 仅此阶段允许写 ideas/
    if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
    if [ "$(reports_today)" -gt "$reports_before" ]; then
      cp "$LEDGER_GOOD" ledger.tsv   # 发布前再确保 ledger = 定谳 good,抹掉 report 阶段对 ledger 的任何擅改
      ./publish.sh >> "$LOG" 2>&1 || { log "publish.sh 失败,见 hunt.log;停机人工处理"; exit 2; }
      sa_now=$(sa_today)
      if [ "$SA_TARGET" -gt 0 ] && [ "$sa_now" -ge "$SA_TARGET" ]; then
        log "已发布;当日 Strong Accept 累计 ${sa_now},达目标 ${SA_TARGET},结束"
        break
      fi
      fails=0
      sleep_min=$(random_no_hit_sleep_min)
      log "已发布;当日 Strong Accept 累计 ${sa_now}/${TARGET_DESC},${sleep_min} 分钟后继续下一轮"
      sleep "$((sleep_min * 60))"
      continue
    fi
    log "报告阶段声称达标但未新增 ideas/${today}_hunt*.md 报告,本轮作废重试"
  fi

  fails=0
  log "运行完成但无达标报告(本轮 idea 未拿到全票 Strong Accept)"
  sleep_min=$(random_no_hit_sleep_min)
  log "正常无达标,随机等待 ${NO_HIT_SLEEP_MIN_LO}-${NO_HIT_SLEEP_MIN_HI} 分钟;本次 ${sleep_min} 分钟后重试"
  sleep "$((sleep_min * 60))"
done
