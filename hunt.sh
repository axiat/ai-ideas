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
# 轮级机器可读指标追加写 tmp/hunt.metrics.tsv(阶段失败/空产出/预筛 fail-open/聚合定谳各一行,字段见 metrics_write 头注)。
# 每轮固定 run_id(启动时间+pid+轮次,candidate_id=<run_id>/I<n>);轮终点(fail/empty/verdict/
# report-missing/published/publish-failed)把 tmp/round 全量产物+manifest+ledger 增量归档 $RUNS_DIR/<run_id>/,
# ledger 只留摘要行(字段见 archive_round 头注);各阶段起止/rc 记 tmp/round/stages.tsv,逐阶段日志在 tmp/round/logs/。
# 归档是审计载体,默认 RUNS_DIR 在仓库外($HOME 下):三个可信后端(claude allowlist / codex·grok OS
# sandbox 写域=仓库)都写不到,只由 hunt.sh 写。唯一够得到的是 agy(不可信、同用户、可写 $HOME)——
# 这不是重定位能关的(同用户 untrusted 后端的本性),只能 best-effort 兜底(见 RUNS_DIR 定义处)。
# 归档失败按终态分级:有 SA 将发布的轮,裁决归档失败即停机(exit 2),绝不发布审计链断裂的 SA;非发布轮只告警。
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
#     AGENT_CMD='./grok-worker.sh' ./hunt.sh
#       # grok 无头适配:--always-approve + --sandbox workspace + 固定层/ledger 的 Edit|Write deny;
#       # 须走 worker(不能直接 AGENT_CMD='grok -p …',见 grok-worker.sh 头注)。ledger 仍靠 ledger.good 兜底。
#   FRONT_CMD 覆盖前段(生成+查重)、BACK_CMD 覆盖后段(打分+报告);二者都默认回落到 AGENT_CMD,不设则行为与原来逐字节一致。
#   Level 1.5(agy 跑便宜前段,claude/codex/grok 跑可信后段):
#     FRONT_CMD='./agy-worker.sh' BACK_CMD='claude -p --strict-mcp-config' ./hunt.sh
#     FRONT_CMD='./agy-worker.sh' BACK_CMD='./grok-worker.sh' ./hunt.sh
#       # 前段用 agy:便宜、可错——错误 idea 由下游独立裁判 + SA 硬门槛毙掉,只会多重试几轮,不污染 verdict。
#       # 后段(verdict/报告)与 publish 全走可信席:claude/codex/grok。agy 不碰 publish(其 CLI sandbox 可读写 $HOME,不能当边界)。
#   REV_CMD_1..REV_CMD_N 逐席位覆盖裁判命令(不设回落 BACK_CMD);REV_STAGGER_SEC 裁判错峰起跑秒数(默认 0)。
#   混合面板示例(1 codex + 1 grok + 1 agy):
#     REV_CMD_1='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' \
#     REV_CMD_2='./grok-worker.sh' REV_CMD_3='./agy-worker.sh' REV_STAGGER_SEC=15 ./hunt.sh
#       # 取最低票 + SA 需全票 ⇒ 便宜裁判只能否决不能放水,SA 决定权仍在可信席位;至少留 1 席 claude/codex/grok。
#       # agy 快速重复调起会触发登录验证;agy-worker.sh 内置启动闸门(AGY_LAUNCH_GAP_SEC,默认 60s)
#       # 自动错峰所有 agy 席位,REV_STAGGER_SEC 可再减少闸门排队。仍不要把全部裁判席交给 agy(须留可信席位)。
#   前段空产出按"便宜可错"短重试:EMPTY_MAX 次内随机等 NO_HIT 区间,连续达 EMPTY_MAX 次才升级长睡(默认 3);
#   预筛(生成与深查之间,FRONT_CMD 跑,便宜可错、只杀不保):只杀"单篇工作直接占据头条"的 direct hit,
#     被杀 idea 由本脚本立即按 reject 入账、overlap=high(防下轮重生成);存活按优先级取 SHORT_MAX 个
#     (默认 3)进深查——复查/进化 > 删承重假设 > 低存量主题(ledger 同主题行数升序)> 生成顺序,
#     防 FIFO 把排位靠后的稀缺候选随机丢掉;超额 keep 丢弃不入账(下轮可重新生成),全灭走空产出短重试;
#     结构失败 fail-open 不废轮:prescreen.md 缺失/判定非法/kill 佐证不全一律按 keep 兜底进 shortlist
#     (无效 kill 不入账;调起 rc≠0 仍走异常重试);
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
cd "$(dirname "$0")" || exit 2
git config core.hooksPath .githooks   # 激活 pre-push 守卫:禁止直推 main

AGENT_CMD=${AGENT_CMD:-claude -p --strict-mcp-config}
# 分段 agent:前段(生成+查重)便宜且可错,可换 agy;后段(打分+报告)决定 verdict 与发布产物,须可信(claude/codex/grok)。
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
RESEARCH_RETRY=${RESEARCH_RETRY:-1}   # 检索不完整(结构未达门槛)对同一 shortlist 定向补查的次数上限;耗尽才整轮作废
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
NONSA_CLASS=tmp/nonsa-class.tsv                                  # 非 SA 四分类观测(item #4;tmp/ 持久,不入固定 ledger schema)
NEAR_SA_QUEUE=tmp/near-sa-queue.tsv                             # near-SA 修订队列(item #5;design-fixable 且有 SA 票,generate 优先取)
LOCK=tmp/hunt.lock
METRICS=tmp/hunt.metrics.tsv
# 裁决归档失败停机时落此哨兵:SA 行已在 ledger.good 但其归档缺失=审计链断裂的孤儿 SA。
# 直接重启会把它当已达标发布(见 SA_TARGET/孤儿路径),归档仍缺——故启动即拦,逼人工先处理再删哨兵。
# 放仓库内(非 $RUNS_DIR):归档目录不可写正是常见停机因,哨兵不能和它同命。
HALT_MARK=tmp/HALTED-ARCHIVE-FAIL
# 按运行审计归档默认放在仓库外:codex/grok 的 OS sandbox 写域=仓库工作树,claude allowlist 只含
# ideas/tmp/(不含 $HOME),故仓库外目录三个可信后端都写不到——归档只由 hunt.sh(编排器,无沙箱)写。
# 例外是 agy:同用户、不可信、可写 $HOME,归档在 $HOME 下它就够得到,重定位关不掉(同用户 untrusted
# 后端本性如此)。兜底只有 best-effort:agy-worker prompt 明令禁写本目录,且 agy 永不担任 verdict 席
# (归档的可还原性只在有 SA 的可信裁决轮才被依赖)——真正的隔离要把 agy 放独立 uid/容器。
# RUNS_DIR 可覆盖;若覆盖回仓库内(如 tmp/runs),连可信后端也触及得到,审计边界整体退回 best-effort。
RUNS_DIR=${RUNS_DIR:-$HOME/.ai-ideas-runs/$(basename "$PWD")}

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
  is_uint "$RESEARCH_RETRY" || { log "RESEARCH_RETRY 必须是非负整数: $RESEARCH_RETRY"; exit 2; }
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

# 调一次 agent(串行阶段用),rc 作为返回值;$1=命令 $2=prompt $3=阶段名。
# 输出除 hunt.log 外另 tee 一份进 $RD/logs/<阶段>.log(按运行归档后可单独还原该阶段的检索/失败现场);
# 起止时间与 rc 追加 $RD/stages.tsv(stage start end rc)。
run_stage() {
  local cmd=$1 rc t0 t1
  m_stage=$3
  log "调起 [$3]: $cmd"
  mkdir -p "$RD/logs"
  t0=$(date '+%F %T')
  $cmd "$2" 2>&1 | tee -a "$RD/logs/$3.log" >> "$LOG"
  rc=${PIPESTATUS[0]}
  t1=$(date '+%F %T')
  printf '%s\t%s\t%s\t%s\n' "$3" "$t0" "$t1" "$rc" >> "$RD/stages.tsv"
  return "$rc"
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
  metrics_write fail "$m_stage" '-'
  archive_round "fail:${m_stage}"
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
  metrics_write empty "$m_stage" '-'
  archive_round "empty:${m_stage}"
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

# 文件非空行数;文件不存在输出 -(区别于存在但为空的 0)
count_lines() { if [ -f "$1" ]; then grep -c . "$1" || true; else echo '-'; fi; }

# 轮级机器可读指标(append-only tmp/hunt.metrics.tsv):阶段异常(fail)、空产出作废(empty)、
# 预筛 fail-open(failopen)、聚合定谳(verdict)各追加一行,调参不再翻 hunt.log/ledger prose。
# 计数列由 tmp/round 文件即时派生,
# -=文件不存在:gen 生成全集、kill 预筛杀、keep 预筛存活、short 入深查 shortlist(drop=keep-short);
# pw_links/pw_api 查重块链接行/结构化 API 检索行总数。verdicts 每 idea 一段 id=票,票,..->终判,
# 票为 rank(2=SA 1=aWr 0=rej -=缺票);全票 2 却 ->reject 即 SA 硬门槛降级。
# 末列 run_id 关联 tmp/runs/<run_id>/ 归档(2026-07-12 起;此前旧行 12 列无此列,消费端按列名或前 12 列位置解析)。
metrics_write() {   # $1=outcome $2=阶段名(-=不适用) $3=verdicts 串(-=不适用)
  local n_gen n_kill n_keep n_short pw_links pw_api
  [ -s "$METRICS" ] || printf 'ts\tround\toutcome\tstage\tlens\tgen\tkill\tkeep\tshort\tpw_links\tpw_api\tverdicts\trun_id\n' > "$METRICS"
  n_gen=$(count_lines "$RD/ideas.all.tsv"); [ "$n_gen" = '-' ] && n_gen=$(count_lines "$RD/ideas.tsv")
  n_kill=$(count_lines "$RD/kills.tsv"); n_keep=$(count_lines "$RD/keeps.tsv"); n_short=$(count_lines "$RD/ideas.tsv")
  if [ -f "$RD/priorwork.md" ]; then
    pw_links=$(grep -cE 'https?://' "$RD/priorwork.md" || true)
    pw_api=$(grep -cE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org' "$RD/priorwork.md" || true)
  else pw_links='-'; pw_api='-'; fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$(date '+%F %T')" "$round" "$1" "$2" "$m_lens" "$n_gen" "$n_kill" "$n_keep" "$n_short" \
    "$pw_links" "$pw_api" "$3" "${run_id:--}" >> "$METRICS"
}

# 按运行归档:把本轮 tmp/round 全量产物(ideas/priorwork/预筛/三席票据与完整评审/阶段日志与起止时间)
# 连同 manifest(来源/backend/policy 版本/退出原因/票向量)、ledger 增量行固化到 tmp/runs/<run_id>/;
# ledger 本体只留摘要行,任一结论可由归档还原输入与判定过程。candidate_id = <run_id>/I<n>。
# 同一 run_id 重复调用整体刷新(verdict 后 report 再失败,exit_reason 覆盖为 fail:report,产物照最新拷)。
# rc:0=归档完整,1=任一环节(建目录/manifest/拷贝)失败。调用方按终态分级——
# 有 SA 将发布的轮要求 rc=0 才放行发布(见 verdict 归档处),非发布轮失败只告警。
archive_round() {   # $1=退出原因(fail:<stage>|empty:<stage>|verdict|report-missing|published|publish-failed)
  local dst rev_cmds r v frozen rc=0
  [ -n "${run_id:-}" ] && [ "$run_id" != '-' ] || return 0
  dst="$RUNS_DIR/$run_id"
  # 首次归档(fail/empty/verdict)全量拷入 tmp/round=「裁判当时所见」。二次归档(verdict 之后的
  # published/report-missing/publish-failed 复用同一 run_id)只刷新 manifest 与观测产物,绝不覆盖
  # 已冻结的评审输入(ideas/priorwork/rev)——report 阶段对 tmp/round 的任何改写都不得篡改裁决快照。
  if [ -d "$dst/round" ]; then
    frozen=1
  else
    frozen=0; rm -rf "$dst"
    mkdir -p "$dst/round" || { log "归档失败:${run_id} 无法建 $dst/round(磁盘满/不可写?)"; return 1; }
  fi
  # ledger 增量=本轮产出了哪些 ledger 行,是审计载体的一部分;写失败须进 rc(SA 轮据此停机),
  # 不能像观测日志那样吞掉。fail/empty 轮增量可能为空,那是合法的(空文件写成功,rc 不动)。
  if ! tail -n +"$((ledger_base_lines + 1))" "$LEDGER_GOOD" > "$dst/ledger.delta.tsv" 2>/dev/null; then
    log "归档失败:${run_id} ledger 增量写入失败"; rc=1
  fi
  rev_cmds=""
  for r in $(seq 1 "$REVIEWERS"); do
    v="REV_CMD_$r"; rev_cmds="${rev_cmds:+$rev_cmds | }${!v:-$BACK_CMD}"
  done
  if ! {
    printf 'run_id\t%s\n' "$run_id"
    printf 'date\t%s\n' "$today"
    printf 'source\thunt\n'
    printf 'round\t%s\n' "$round"
    printf 'lens\t%s\n' "$m_lens"
    printf 'exit_reason\t%s\n' "$1"
    printf 'sa_count\t%s\n' "${sa_count:--}"
    printf 'reviewers\t%s\n' "$REVIEWERS"
    printf 'front_cmd\t%s\n' "$FRONT_CMD"
    printf 'back_cmd\t%s\n' "$BACK_CMD"
    printf 'rev_cmds\t%s\n' "$rev_cmds"
    printf 'git_head\t%s\n' "${before:--}"
    printf 'policy_sha\t%s\n' "$(cat brainstorming_policy.md rubric.md roles/*.md 2>/dev/null | shasum -a 256 | cut -c1-12)"
    printf 'verdicts\t%s\n' "${m_verdicts:--}"
    printf 'archived_at\t%s\n' "$(date '+%F %T')"
  } > "$dst/manifest.tsv" 2>/dev/null; then
    log "归档失败:${run_id} manifest 写入失败"; rc=1
  fi
  if [ "$frozen" = 1 ]; then
    # 冻结评审输入不动,只补观测产物(stages/logs 现含 report 阶段)
    cp "$RD/stages.tsv" "$dst/round/stages.tsv" 2>/dev/null || rc=1
    if [ -d "$RD/logs" ]; then
      mkdir -p "$dst/round/logs" 2>/dev/null
      cp -R "$RD/logs/." "$dst/round/logs/" 2>/dev/null || { log "归档警告:${run_id} 阶段日志补拷失败"; rc=1; }
    fi
  elif ! cp -R "$RD/." "$dst/round/" 2>/dev/null; then
    log "归档失败:${run_id} 产物拷贝失败(磁盘满/不可写?)"; rc=1
  fi
  return "$rc"
}

rank_of() { case "$1" in strong-accept) echo 2 ;; accept-w-rev) echo 1 ;; *) echo 0 ;; esac; }
verdict_of() { case "$1" in 2) echo strong-accept ;; 1) echo accept-w-rev ;; *) echo reject ;; esac; }
# 非 SA 四分类(item #4;$1=降级前最低票 0/1/2、$2=是否全票 SA 但硬门槛降级(1/0)、$3=overlap):
#   evidence-incomplete 票够但证据不完整(全票 SA 被硬门槛降级)——应补证重评,不是判死
#   novelty-dead        头条已被占据(overlap=high)——按当前 PROGRAM 永久,不复活
#   design-fixable      accept-w-rev 且 overlap=low——实验设计类可修,合法进化父本
#   ceiling-limited     accept-w-rev 但 novelty 被近邻封顶(overlap≠low)——上限受限,搁置
# 注:overlap≠high 的裸 reject(min=0,CRITICAL/≥2 MAJOR)当前归 novelty-dead(PROGRAM 下 reject 恒不复活);
# 「只 direct-hit/CRITICAL 永久禁、其余留复查」需改 PROGRAM step6/12,见 P1-PROGRAM-DRAFT.md(item #6),本轮不动。
classify_nonsa() {
  local raw_min=$1 downgraded=$2 overlap=$3
  [ "$downgraded" -eq 1 ] && { echo evidence-incomplete; return; }
  [ "$overlap" = high ] && { echo novelty-dead; return; }
  { [ "$raw_min" -eq 1 ] && [ "$overlap" = low ]; } && { echo design-fixable; return; }
  [ "$raw_min" -eq 1 ] && { echo ceiling-limited; return; }
  echo novelty-dead
}

# 当日(hunt 源)Strong Accept 计数:只数 bash 定谳基线 $LEDGER_GOOD,不信工作树 ledger
sa_today() {
  awk -F'\t' -v d="$today" '$1==d && $2=="hunt" && $5=="strong-accept"{n++} END{print n+0}' "$LEDGER_GOOD" 2>/dev/null || echo 0
}

# 当日报告文件数:同日多份报告(-2/-3 后缀)时,报告是否写出须按"数量新增"判,存在性检查会被旧报告蹭过
reports_today() {
  local f n=0
  for f in "ideas/${today}"_hunt*.md; do
    [ -e "$f" ] || continue                        # glob 未命中时字面量本身会进循环
    n=$((n + 1))
  done
  echo "$n"
}

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

# 预筛判定读取:$1=id → stdout 输出 kill|keep|空(块缺失/判定缺失或非法)。
# 首条判定行整行严格匹配「判定:kill|keep」(容忍空白与全/半角冒号)才算数;
# 附加词(not kill/kill? keep/killed)一律非法 → 空 → 调用方 fail-open keep,
# 防子串抽词把含糊判定变成永久 reject 入账。
prescreen_dec() {
  awk -v id="$1" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/prescreen.md" 2>/dev/null \
    | grep -m1 '^判定' | grep -xE '判定[[:space:]]*[:：][[:space:]]*(kill|keep)[[:space:]]*' \
    | grep -oE 'kill|keep'
}

# kill 佐证校验:块须有 ≥1 条结构化 API 检索记录 + 非 API 占位链接,通过则输出占位链接、rc=0。
# kill 是永久 reject 入账(overlap=high),佐证不全的 kill 一律由调用方降级 keep,防幻觉/缺失
# 链接污染 ledger;keep 侧无校验——keep 本就是 fail-open 的兜底方向(只杀不保)。
kill_evidence() {   # $1=id → stdout 占位链接;rc≠0=佐证不全
  local block url
  block=$(awk -v id="$1" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/prescreen.md" 2>/dev/null)
  printf '%s\n' "$block" | grep -qE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org' || return 1
  url=$(printf '%s\n' "$block" | grep -oE 'https?://[^ )|,;>]+' \
        | grep -vE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org' | head -1)
  [ -n "$url" ] || return 1
  printf '%s\n' "$url"
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

# 预筛 keep 的优先级 rank(0 最高):0=复查/进化(与进化共享每轮至多 1 个名额的稀缺候选)
# 1=删承重假设块 2=普通;标记行格式由 roles/generate.md 模板钉死(复查:/进化自:行首)。
keep_rank() {   # $1=id(查 ideas.all.md 块)
  if awk -v id="$1" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/ideas.all.md" \
       | grep -qE '^[-* ]*(复查|进化自)[::]'; then echo 0
  elif is_axiom_idea "$1" "$RD/ideas.all.md"; then echo 1
  else echo 2; fi
}

# shortlist 选取:keeps.tsv(rank 主题存量 生成序 id story theme)排序取前 SHORT_MAX 个写
# ideas.tsv/ideas.md,溢出照旧丢弃(不深查、不入账)。替代按生成顺序 FIFO——复查/进化/删公理
# 排位靠后时曾被随机丢掉(hunt.log 至 07-07 已 51 次)。设全局 kept。
select_shortlist() {
  local rank tcount oidx id story theme
  kept=0
  [ -s "$RD/keeps.tsv" ] || return 0
  sort -t$'\t' -k1,1n -k2,2n -k3,3n -o "$RD/keeps.tsv" "$RD/keeps.tsv"
  while IFS=$'\t' read -r rank tcount oidx id story theme; do
    if [ "$kept" -lt "$SHORT_MAX" ]; then
      printf '%s\t%s\t%s\n' "$id" "$story" "$theme" >> "$RD/ideas.tsv"
      awk -v id="$id" '$1=="##"{f=($2==id)} f' "$RD/ideas.all.md" >> "$RD/ideas.md"
      printf '\n' >> "$RD/ideas.md"
      kept=$((kept + 1))
    else
      log "预筛:${id} keep 但超出 SHORT_MAX=${SHORT_MAX}(rank=${rank} 主题存量=${tcount}),本轮不深查、不入账(下轮可重新生成)"
    fi
  done < "$RD/keeps.tsv"
}

mkdir -p "$(dirname "$LEDGER_GOOD")"                             # tmp/ 在干净 checkout 里不存在,先建(否则种子/重置全失败)
# metrics 老文件一次性升级:header 缺 run_id 列则补上(仅改首行;旧数据行保持 12 列,消费端按前 12 列位置解析)
if [ -s "$METRICS" ] && ! head -1 "$METRICS" | grep -q 'run_id'; then
  awk 'NR==1{print $0 "\trun_id"; next} {print}' "$METRICS" > "$METRICS.mig" && mv "$METRICS.mig" "$METRICS"
fi
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

mkdir -p "$RUNS_DIR" 2>/dev/null || { log "无法创建归档目录 $RUNS_DIR(权限?),停机"; exit 2; }
log "按运行归档目录: $RUNS_DIR(仓库外,agent 后端不可写)"

# 上次因裁决归档失败停机则拦启动:否则重启会沿孤儿 SA 路径把无归档的 SA 发布掉,重开 #10 的洞。
# 人工须先补回该 run 的归档,或从 ledger.good 删掉那条孤儿 SA 行让它重新查重,再删本哨兵。
if [ -e "$HALT_MARK" ]; then
  log "检测到审计链断裂哨兵 $HALT_MARK(上次裁决归档失败停机):存在无归档的孤儿 SA,直接启动会把它当已达标发布。"
  log "先按哨兵内说明处理(补归档 或 从 $LEDGER_GOOD 删该 SA 行),再删 $HALT_MARK 重启。停机。"
  exit 2
fi

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
run_id='-'
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
  m_stage='-'; m_lens='-'
  # 运行标识与轮级观测:run_id 稳定唯一(启动时间+pid+轮次),candidate_id = <run_id>/I<n>;
  # ledger_base_lines 记本轮开局基线行数,归档时据此提取本轮 ledger 增量(基线轮内只追加)。
  run_id="$(date +%Y%m%dT%H%M%S)-p$$-r${round}"
  sa_count='-'; m_verdicts=''
  before=$(git rev-parse HEAD)
  cp "$LEDGER_GOOD" ledger.tsv                       # 重置到上一轮定谳后的良好 ledger,抹掉任何遗留篡改(F2)
  pre_dirty=$(git status --porcelain | cut -c4- | sort -u)

  front_resumed=0
  if [ "$resume_front" = "1" ]; then
    # 前段续跑:沿用遗留 ideas.tsv/ideas.md/priorwork.md(启动时已过门槛),直接进评审。
    # 评审及以后的残留必须清除——遗留 rev/ 里的票据与评审块可能是前段伪造,verdict 永不续用。
    # stages.tsv/logs/ 保留:它们是中断前段的真实现场,随本轮一起归档。
    resume_front=0
    front_resumed=1
    rm -rf "$RD/rev"
    rm -f "$RD/rev_rc" "$RD/accepted.tsv" "$RD/rejects.tsv" "$RD/meta.txt"
    empties=0
    log "续跑:沿用中断遗留的前段产物,跳过生成/预筛/查重,直接进入评审"
  else
    rm -rf "$RD"; mkdir -p "$RD"
  fi
  # 空文件时 grep -c 输出 0 且 rc=1,不能接 || echo 0(会双行);缺文件才回落 0
  ledger_base_lines=$(grep -c '' "$LEDGER_GOOD" 2>/dev/null || true)
  [ -n "$ledger_base_lines" ] || ledger_base_lines=0

  if [ "$front_resumed" = "0" ]; then
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
    m_lens=${lens:--}
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
    # 结构失败不废轮(废轮浪费整轮生成+透镜抽取):prescreen.md 缺失、判定缺失/非法、kill 佐证
    # 不全,一律 fail-open 按 keep 进优先级 shortlist——多花深查钱,由深查重+裁判+SA 门槛兜底;
    # 调起 rc≠0 仍走 fail_and_wait(后端系统性故障,fail-open 只会让下一阶段接着失败)。
    ps_missing=0
    if [ ! -s "$RD/prescreen.md" ]; then
      ps_missing=1
      log "预筛 fail-open:prescreen.md 缺失/为空,本轮不记 kill、全体按 keep 进优先级选取"
    fi
    : > "$RD/ideas.tsv"; : > "$RD/ideas.md"; : > "$RD/kills.tsv"; : > "$RD/keeps.tsv"
    oidx=0; failopen=0
    while IFS=$'\t' read -r id story theme; do
      [ -z "$id" ] && continue
      oidx=$((oidx + 1))
      dec=$(prescreen_dec "$id")
      if [ "$dec" = "kill" ] && kill_url=$(kill_evidence "$id"); then
        printf '%s\t%s\t%s\t%s\n' "$id" "$story" "$theme" "$kill_url" >> "$RD/kills.tsv"
      else
        if [ "$dec" != "keep" ]; then
          failopen=$((failopen + 1))
          [ "$ps_missing" -eq 0 ] && log "预筛 fail-open:${id} 判定缺失/非法或 kill 佐证不全(API 记录/占位链接),按 keep"
        fi
        # 主题存量必须 grep -Fx 字节比较:BSD awk/sort 的字符串 == 走 strcoll,
        # en_US.UTF-8 下 CJK 串会互判相等(动作表征==效率与系统),计数全错
        tcount=$(cut -f3 "$LEDGER_GOOD" 2>/dev/null | grep -Fxc -- "$theme" || true)
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(keep_rank "$id")" "$tcount" "$oidx" "$id" "$story" "$theme" >> "$RD/keeps.tsv"
      fi
    done < "$RD/ideas.all.tsv"
    # kill 行立即 bash 定谳入账(verdict=reject,overlap=high):防同类 idea 下轮重生成;
    # 预筛错杀只损失单一 idea 族(多花几轮重试),不污染 verdict,可接受。
    if [ -s "$RD/kills.tsv" ]; then
      cp "$LEDGER_GOOD" ledger.tsv
      while IFS=$'\t' read -r id story theme kill_url; do
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$today" "hunt" "$theme" "$story" "reject" "预筛直接占位: $kill_url" "high" "novelty-dead" >> ledger.tsv
      done < "$RD/kills.tsv"
      cp ledger.tsv "$LEDGER_GOOD"
      log "预筛:$(grep -c . "$RD/kills.tsv") 个 direct hit 已按 reject 入账"
    fi
    select_shortlist
    if [ "$failopen" -gt 0 ]; then
      log "预筛 fail-open:${failopen} 个候选按 keep 兜底(无效 kill 不入账)"
      metrics_write failopen prescreen '-'
    fi
    if [ "$kept" -eq 0 ]; then
      log "预筛全灭:本轮候选头条均被直接占位,作废重试"; fails=0
      empty_and_wait; continue
    fi
    log "预筛:${kept} 个按优先级进深查(复查/进化>删公理>低存量主题): $(cut -f1 "$RD/ideas.tsv" | tr '\n' ' ')"

    # 2) 对抗式深查重(禁写 ideas/;只查预筛存活的 shortlist,每个 idea 5-8 篇实读)
    # 检索不完整(产物薄/缺块/裂缝核验不足)不是「低重叠」定论,而是「没查完」:对同一 shortlist
    # 定向补查(重跑本阶段),不废本轮生成/透镜;补查 RESEARCH_RETRY 次仍不达门槛才退回整轮作废。
    research_try=0
    while :; do
      rm -f "$RD/priorwork.md"                        # 补查前清旧产物,防新旧块混算门槛
      run_stage "$FRONT_CMD" "读 roles/research.md,按其执行" research; rc=$?; guard 0
      [ "$rc" -ne 0 ] && break                        # 调起失败:属基础设施故障,不算不完整,走 fail 分支
      { [ -s "$RD/priorwork.md" ] && priorwork_ok && cracks_ok; } && break
      [ "$research_try" -ge "$RESEARCH_RETRY" ] && break
      research_try=$((research_try + 1))
      log "查重不完整(结构未达门槛),对同一 shortlist 定向补查(第 ${research_try}/${RESEARCH_RETRY} 次,不废本轮生成)"
    done
    if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
    if [ ! -s "$RD/priorwork.md" ] || ! priorwork_ok || ! cracks_ok; then
      log "查重补查 ${RESEARCH_RETRY} 次后结构(含裂缝核验)仍不达门槛,本轮作废重试"; fails=0
      empty_and_wait; continue
    fi
    empties=0                                        # 前段两阶段产物齐备,空产出计数清零
  fi

  # 3) N 位裁判,并行 + 各自独立输入目录(开跑时看不到彼此产出)(F3);禁写 ideas/
  m_stage=review
  rev_t0=$(date '+%F %T')
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
    printf 'review\t%s\t%s\t1\n' "$rev_t0" "$(date '+%F %T')" >> "$RD/stages.tsv"
    log "有裁判异常退出或缺席:$(tr '\n' ' ' < "$RD/rev_rc")"; fail_and_wait; continue
  fi
  printf 'review\t%s\t%s\t0\n' "$rev_t0" "$(date '+%F %T')" >> "$RD/stages.tsv"

  # 4) 聚合(bash 定谳):取最低票,SA 需全票 + 过 SA 硬门槛(F4);缺失/无法解析的票一律当 reject(失败关闭)。
  cp "$LEDGER_GOOD" ledger.tsv                       # 干净基线,本轮增量只来自下面 bash 追加
  : > "$RD/accepted.tsv"; : > "$RD/rejects.tsv"
  sa_count=0
  m_verdicts=""
  while IFS=$'\t' read -r id story theme; do
    [ -z "$id" ] && continue
    min=2; reason=""; votes=""; sa_votes=0
    for r in $(seq 1 "$REVIEWERS"); do
      line=$(awk -F'\t' -v id="$id" '$1==id{print; exit}' "$RD/rev/$r/verdict.tsv" 2>/dev/null || true)
      v=$(printf '%s' "$line" | cut -f2); rs=$(printf '%s' "$line" | cut -f4)
      rank=$(rank_of "$v")
      if [ -n "$v" ]; then votes="${votes:+$votes,}${rank}"; else votes="${votes:+$votes,}-"; fi
      [ "$rank" -eq 2 ] && sa_votes=$((sa_votes + 1))
      if [ "$rank" -lt "$min" ]; then min=$rank; reason=$rs; fi
      [ -z "$reason" ] && reason=$rs
    done
    raw_min=$min; downgraded=0                        # 记降级前最低票,供非 SA 分类区分「票够但证据不完整」
    if [ "$min" -eq 2 ] && ! sa_gate_ok "$id"; then
      min=0; downgraded=1; reason="全票 SA 但硬门槛不达标(实读<${MIN_READ}、缺查重块、缺最小否证实验、无完整评审或删公理裂缝核验相符不足),orchestrator 硬降级"
      log "SA 硬门槛未过,${id} 降级 reject"
    fi
    verdict=$(verdict_of "$min")
    m_verdicts="${m_verdicts:+${m_verdicts};}${id}=${votes}->${verdict}"
    [ -z "$reason" ] && reason="(无理由,按最严处理)"
    [ -z "$theme" ] && theme="未标注"
    # overlap 列:取独立查重的「重叠判定」(high/medium/low),供进化父本资格的机械筛选。
    # 锚定行首:查重块其它行可能路过「重叠判定」字样(如"不作重叠判定依据"),非锚定会抓错行
    overlap=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$RD/priorwork.md" 2>/dev/null \
              | grep -m1 '^重叠判定' | grep -oE 'high|medium|low' | head -1)
    [ -z "$overlap" ] && overlap="未知"
    # category(ledger 第 8 列):SA 行留 -,非 SA 走四分类(见 classify_nonsa)
    if [ "$min" -eq 2 ]; then cat="-"; else cat=$(classify_nonsa "$raw_min" "$downgraded" "$overlap"); fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$today" "hunt" "$theme" "$story" "$verdict" "$reason" "$overlap" "$cat" >> ledger.tsv
    if [ "$min" -eq 2 ]; then
      printf '%s\t%s\n' "$id" "$story" >> "$RD/accepted.tsv"; sa_count=$((sa_count + 1))
    else
      printf '%s\t%s\t%s\n' "$id" "$story" "$reason" >> "$RD/rejects.tsv"
      # item #4:非 SA 四分类同步落 tmp/ 观测(与 ledger category 列同源;cid=<run_id>/<id>)
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$today" "${run_id}/${id}" "$cat" "$verdict" "$overlap" "${votes:--}" "$story" >> "$NONSA_CLASS"
      # item #5:design-fixable 且有 SA 票(near-SA)进修订队列,generate 优先取它做进化父本。
      # 去重按 story 精确匹配(BSD CJK strcoll 会误判相等,故用 grep -Fxq),防同一 idea 跨轮堆积。
      if [ "$cat" = design-fixable ] && [ "$sa_votes" -ge 1 ] \
         && ! cut -f3 "$NEAR_SA_QUEUE" 2>/dev/null | grep -Fxq -- "$story"; then
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$today" "${run_id}/${id}" "$story" "$theme" "$overlap" "${votes:--}" "$cat" >> "$NEAR_SA_QUEUE"
        log "near-SA 入队:${id}(票 ${votes},overlap=${overlap},design-fixable)——下轮 generate 优先修订"
      fi
    fi
  done < "$RD/ideas.tsv"
  cp ledger.tsv "$LEDGER_GOOD"                       # 定谳:把本轮 bash 追加固化为新的良好基线(F2)
  guard 0
  log "本轮聚合:${sa_count} 个 Strong Accept(全票且过硬门槛),已记账 ledger.tsv"
  metrics_write verdict '-' "${m_verdicts:--}"
  # 裁决归档=SA 可还原性承诺的载体,须在发布前落定。有 SA 将发布时归档失败即停机(exit 2),
  # 绝不发布一个审计链断裂的 SA。注意:重启不会自动补回本 run 的归档——SA 行已在 ledger.good,
  # 重启走的是孤儿 SA 路径(新 run_id、以已达标发布),原 run 的归档永不回填。故落哨兵拦下次启动,
  # 逼人工二选一:补回 ${run_id} 归档,或从 ledger.good 删该 SA 行重查。无 SA 轮归档失败只告警(无发布物)。
  if ! archive_round verdict && [ "$sa_count" -gt 0 ]; then
    printf '%s\trun_id=%s\tsa_count=%s\treason=verdict-archive-failed\n' \
      "$(date '+%F %T')" "$run_id" "$sa_count" > "$HALT_MARK" 2>/dev/null || true
    log "本轮 ${sa_count} 个 SA 已记入 ledger.good 但裁决归档失败——审计链断裂,停机人工处理。"
    log "重启不会补归档(会以新 run_id 把孤儿 SA 当已达标发布)。修复 $RUNS_DIR 后须:补回 ${run_id} 的归档,"
    log "或从 $LEDGER_GOOD 删掉该 SA 行让其重新查重;然后删哨兵 $HALT_MARK 再启动。"
    exit 2
  fi

  # 5) 达标轮 → 组装报告并发布;当日累计达 SA_TARGET 才停,否则继续攒
  if [ "$sa_count" -gt 0 ]; then
    printf '尝试轮数: %s\n评审日期: %s\n裁判数: %s\n' "$round" "$today" "$REVIEWERS" > "$RD/meta.txt"
    reports_before=$(reports_today)
    run_stage "$BACK_CMD" "读 roles/report.md,按其执行" report; rc=$?; guard 1   # 仅此阶段允许写 ideas/
    if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
    if [ "$(reports_today)" -gt "$reports_before" ]; then
      cp "$LEDGER_GOOD" ledger.tsv   # 发布前再确保 ledger = 定谳 good,抹掉 report 阶段对 ledger 的任何擅改
      ./publish.sh >> "$LOG" 2>&1 || { log "publish.sh 失败,见 hunt.log;停机人工处理"; archive_round publish-failed; exit 2; }
      archive_round published
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
    archive_round report-missing
  fi

  fails=0
  log "运行完成但无达标报告(本轮 idea 未拿到全票 Strong Accept)"
  sleep_min=$(random_no_hit_sleep_min)
  log "正常无达标,随机等待 ${NO_HIT_SLEEP_MIN_LO}-${NO_HIT_SLEEP_MIN_HI} 分钟;本次 ${sleep_min} 分钟后重试"
  sleep "$((sleep_min * 60))"
done
