#!/usr/bin/env bash
# AwR 复活 sidecar:主环之外用多轮 agent(默认 agy,便宜可错;可换 claude/codex/grok)把 ledger 中
# accept-w-rev 的 idea 改进成可复审的成品。研究员检索补缺口、出修订稿(roles/awr.md);裁判按主环 rubric 判
# 「SA-可能/还不行」(roles/awr-judge.md);判还不行则缺陷回灌任务文件,研究员下轮继续改;
# MAX_ROUNDS 轮反馈用尽则带最后修订稿收尾。成品 tmp/awr-side/awr/<key>.md 给人/claude 复审,
# verdict 不进主环。只读 ledger 基线、roles/awr*.md、rubric.md、brainstorming_policy.md;
# 只写 tmp/awr-side/awr/;不碰 tmp/round/、ideas/、不 git(prompt 约定+后端沙箱写域,非机械封进程;
# grok 席细节见 grok-worker.sh 头注 3/3b)。与 hunt.sh 可同时跑。
#
# 对 agy 三个已知弱点的对策:
#   能力弱 → 每次调起只做一件小事(一条 AwR 的一轮研究或一次判定);研究只要可点 URL 的证据;
#            裁判失败关闭(不确定一律判还不行);
#   爱早停 → 两类产物各有机械校验(check_draft/check_judge,含末行 AGY-DONE 早停探测),
#            不合格存 .badN 重跑,同 key 累计 MAX_BAD 次拉黑(删 .badN 文件解除);
#            研究员写新稿到 .new.md,校验通过才顶替旧草稿,烂稿不吞好稿;
#   连发触发登录验证 → 与 agy-worker.sh 共享启动闸门戳(tmp/agy.last-launch),默认间隔 120s,
#            主环将来加 agy 席也自动互相错峰;
#   配额用尽/登录失效 → 熔断:连续 3 次调起连产物文件都没写出(与内容烂的 .badN 分开计)视为
#            调起端本体故障,冷却 COOLDOWN 秒再试,防超限后不停调起危及账号。
#
# 每 key 状态全由文件派生,无状态文件,中断随便杀:
#   <key>.md 成品(终态)  <key>.task.md 任务+历轮反馈  <key>.draft.md 现行草稿  <key>.judge.md 最新判定
#   草稿比任务新 → 待判;任务比草稿新(有新反馈)→ 待修订;任务里反馈节数 = 已完成轮数。
#
# 用法: caffeinate -is ./awr-side.sh        # 常驻:队列全终态后每 POLL 秒重扫(等主环产新 AwR)
#
# 接入 claude/codex/grok(与 hunt.sh AGENT_CMD 同约定:命令字符串按空白切分,prompt 作最后一个参数传入):
#   SIDE_CMD           两席统一覆盖(不设=内置 agy,行为与原来一致)
#   SIDE_RESEARCH_CMD  研究员席单独覆盖(不设回落 SIDE_CMD)
#   SIDE_JUDGE_CMD     裁判席单独覆盖(不设回落 SIDE_CMD)
# 例:
#   SIDE_JUDGE_CMD='claude -p --strict-mcp-config' ./awr-side.sh   # agy 研究(便宜可错)+ claude 裁判(可信),推荐
#   SIDE_CMD='claude -p --strict-mcp-config' ./awr-side.sh         # 两席全 claude(与 hunt.sh 同:不加载 MCP)
#   SIDE_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write --skip-git-repo-check --ephemeral' ./awr-side.sh
#       # codex 以沙箱镜像为 workspace,写限镜像;镜像无 .git,须 --skip-git-repo-check;--ephemeral 避免写会话
#   SIDE_CMD='./grok-worker.sh' ./awr-side.sh                      # 两席全 grok(见 grok-worker.sh)
#   SIDE_JUDGE_CMD='./grok-worker.sh' ./awr-side.sh                # agy 研究 + grok 裁判
# 注意:自定义命令不走 agy 启动闸门(闸门专治 agy 连发触发登录验证),但走随机节流(禁背靠背,
#       默认调起间隔 1-10min 随机,见 SIDE_GAP_MIN/MAX_SEC,全后端一视同仁);模型/超时由命令
#       自身携带(AGY_MODEL/AGY_PRINT_TIMEOUT 仅作用于内置 agy;claude/codex/grok 无 agy 挂起兜底,
#       与 hunt.sh 同);机械校验、.badN、熔断对所有后端一视同仁(产物末行仍须 AGY-DONE)。
#       相对路径 SIDE_CMD(如 ./grok-worker.sh)启动时解析为真仓库绝对路径(失败即退出);
#       调起注入 GROK_REPO=<镜像> 钉 grok 工作根。
#
# 可调: AGY_MODEL(默认 'Gemini 3.5 Flash (High)';只认 `agy models` 的完整展示名,连字符形式会被
#              静默忽略、回落服务端默认 Flash (Medium)——详见 agy-worker.sh 头注释)
#       AGY_PRINT_TIMEOUT(默认 10m,均仅内置 agy)
#       SIDE_GAP_SEC(默认 120,0 关闭;agy 启动闸门,仅内置 agy)
#       SIDE_GAP_MIN_SEC/SIDE_GAP_MAX_SEC(默认 60/600,随机节流区间,禁背靠背,全后端;MAX=0 关闭)
#       SIDE_POLL_SEC(默认 9000=150min,0=队列全终态后退出)
#       SIDE_MAX_BAD(默认 3) SIDE_MAX_ROUNDS(默认 3,收尾前允许的反馈轮数)
#       SIDE_COOLDOWN_SEC(默认 3600,熔断后的冷却秒数;0=熔断直接退出)
set -u
repo="$(cd "$(dirname "$0")" && pwd)"
model=${AGY_MODEL:-Gemini 3.5 Flash (High)}
ptimeout=${AGY_PRINT_TIMEOUT:-10m}
side_cmd=${SIDE_CMD:-}                       # 空=内置 agy
research_cmd=${SIDE_RESEARCH_CMD:-$side_cmd}
judge_cmd=${SIDE_JUDGE_CMD:-$side_cmd}
gap=${SIDE_GAP_SEC:-120}
gap_min=${SIDE_GAP_MIN_SEC:-60}              # 随机节流下限(禁背靠背,全后端)
gap_max=${SIDE_GAP_MAX_SEC:-600}             # 随机节流上限;0 关闭随机节流
poll=${SIDE_POLL_SEC:-9000}                  # 队列全终态后重扫间隔,默认 150min
max_bad=${SIDE_MAX_BAD:-3}
max_rounds=${SIDE_MAX_ROUNDS:-3}
cooldown=${SIDE_COOLDOWN_SEC:-3600}
statedir="$repo/tmp/awr-side"
outdir="$statedir/awr"
sidelock="$repo/tmp/awr-side.lock"
gate_stamp="$repo/tmp/agy.last-launch"
gate_lock="$repo/tmp/agy.launch.lock"
for v in "$gap" "$gap_min" "$gap_max" "$poll" "$max_bad" "$max_rounds" "$cooldown"; do
  case "$v" in ''|*[!0-9]*) echo "awr-side: GAP/GAP_MIN/GAP_MAX/POLL/MAX_BAD/MAX_ROUNDS/COOLDOWN 必须是非负整数: $v" >&2; exit 2 ;; esac
done
[ "$gap_max" -eq 0 ] || [ "$gap_min" -le "$gap_max" ] || { echo "awr-side: SIDE_GAP_MIN_SEC($gap_min) 不能大于 SIDE_GAP_MAX_SEC($gap_max)" >&2; exit 2; }

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$outdir/side.log"; }

# 改名迁移:agy-side.sh 时代的状态目录整体搬家,队列状态(成品/草稿/任务/.badN)无损续跑。
if [ -d "$repo/tmp/agy-side" ] && [ ! -d "$statedir" ]; then
  mv "$repo/tmp/agy-side" "$statedir"
fi
mkdir -p "$outdir"

# 实例锁:双开会对同一条 AwR 重复起 agy、互踩 .badN 计数与草稿。持锁进程已死则自清重抢。
while ! mkdir "$sidelock" 2>/dev/null; do
  holder=$(cat "$sidelock/pid" 2>/dev/null || echo "")
  if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
    log "清理陈旧实例锁(原持有 pid $holder 已不在)"; rm -rf "$sidelock"; continue
  fi
  echo "awr-side: 已有实例在跑(pid ${holder:-未知}),退出。确认无实例可 rm -rf $sidelock" >&2; exit 1
done
echo $$ > "$sidelock/pid"
trap 'rm -rf "$sidelock"; [ "$(cat "$gate_lock/pid" 2>/dev/null)" = "$$" ] && rm -rf "$gate_lock"' EXIT
rm -rf "$statedir"/run.* 2>/dev/null || true   # 上次被 kill 遗留的调用镜像;已持实例锁,无并发在用

# 启动闸门:与 agy-worker.sh 同一戳、同一锁、同一陈旧判据(锁内等待 ≤ gap,锁龄 > gap+60s 视为陈旧)。
gate() {
  [ "$gap" -gt 0 ] || return 0
  local holder lock_m now last wait_s
  while ! mkdir "$gate_lock" 2>/dev/null; do
    holder=$(cat "$gate_lock/pid" 2>/dev/null || echo "")
    lock_m=$(stat -f %m "$gate_lock" 2>/dev/null || echo "")
    if { [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; } \
       || { [ -n "$lock_m" ] && [ $(( $(date +%s) - lock_m )) -gt $((gap + 60)) ]; }; then
      log "清理陈旧闸门锁(holder=${holder:-无})"; rm -rf "$gate_lock"; continue
    fi
    sleep 1
  done
  echo $$ > "$gate_lock/pid"
  now=$(date +%s); last=$(cat "$gate_stamp" 2>/dev/null || echo 0)
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  wait_s=$(( last + gap - now ))
  if [ "$wait_s" -gt 0 ]; then log "距上次 agy 启动不足 ${gap}s,等 ${wait_s}s"; sleep "$wait_s"; fi
  date +%s > "$gate_stamp"
  rm -rf "$gate_lock"
}

# 随机节流:禁止背靠背调起。每次调起(本进程首次、及每次空闲重扫后首次除外)前睡 gap_min..gap_max
# 的随机秒数,制造调起间 1-10min(默认)的间隔。与 agy 启动闸门正交——闸门治 agy 连发登录验证并与
# agy-worker 错峰(仅 agy),本节流对所有后端一视同仁(claude/codex 也慢下来)。gap_max=0 关闭。
throttle_first=1
throttle() {
  [ "$gap_max" -gt 0 ] || return 0
  if [ "$throttle_first" = 1 ]; then throttle_first=0; return 0; fi
  local span r
  span=$((gap_max - gap_min + 1))
  r=$((gap_min + RANDOM % span))
  log "节流: 距上次调起随机等待 ${r}s(禁背靠背)"
  sleep "$r"
}

# 熔断计数:连续调起连产物文件都没写出的次数(各后端合计)。配额用尽/登录失效/断网时 agent
# 报错即退、不产文件,.badN(按 key 计内容烂)不涨,没有这层会无限重试超限调起。
nofile=0

# SIDE_CMD 首词解析(相对路径钉真仓绝对路径、封 .. 路径段、裸名须可执行才遮蔽 PATH):
# 与 calib/run_panel.sh 共用单源 lib/resolve_cmd.sh。
# 两席命令启动时一次性解析;失败=配置错,立即退出——若留到调起时才失败,early-return 会绕过
# nofile 熔断且不产 .badN,坏命令下无限空转。
. "$repo/lib/resolve_cmd.sh"
. "$repo/lib/mirror_pre.sh"
# 报错前缀按值的实际来源命名(与 :- 回落同判据):报错一律写 SIDE_CMD 会把
# SIDE_JUDGE_CMD 拼错的用户支去查一个没设或没错的变量。
research_label="awr-side: SIDE_CMD"; [ -n "${SIDE_RESEARCH_CMD:-}" ] && research_label="awr-side: SIDE_RESEARCH_CMD"
judge_label="awr-side: SIDE_CMD"; [ -n "${SIDE_JUDGE_CMD:-}" ] && judge_label="awr-side: SIDE_JUDGE_CMD"
research_cmd=$(resolve_cmd "$repo" "$research_label" "$research_cmd") || exit 2
judge_cmd=$(resolve_cmd "$repo" "$judge_label" "$judge_cmd") || exit 2

# $1=命令字符串(空=内置 agy) $2=唯一允许写的文件 $3=prompt 正文 $4=原始输出日志。
# agent 只看 tmp/awr-side 下的临时镜像;合格与否由外层机械校验决定,主仓库只接收指定输出文件。
# 启动闸门只罩 agy 后端(专治 agy 连发触发登录验证),claude/codex/grok 直起、不动共享戳。
# 自定义后端命令已在启动时解析为绝对路径;GROK_REPO=镜像 让 grok-worker 不钉回真仓。
run_agent() {
  local cmd=$1 target=$2 prompt=$3 logf=$4 first sandbox rel target_in_sandbox prompt_in_sandbox pre rc
  throttle                                          # 随机 1-10min 节流,禁背靠背(全后端)
  read -r first _ <<<"$cmd"                         # 与 resolver/调起点同口径按任意空白切首词(tab 也算)
  case "$first" in ''|agy|*/agy) gate ;; esac       # agy 另加启动闸门(治连发登录验证 + 与 agy-worker 错峰)
  sandbox=$(mktemp -d "$statedir/run.XXXXXX") || return 1
  rel=${target#"$repo"/}
  target_in_sandbox="$sandbox/$rel"
  mkdir -p "$sandbox/roles" "$sandbox/tmp/awr-side/awr" "$(dirname "$target_in_sandbox")"
  cp "$repo/roles/awr.md" "$sandbox/roles/awr.md"
  cp "$repo/roles/awr-judge.md" "$sandbox/roles/awr-judge.md"
  cp "$repo/rubric.md" "$sandbox/rubric.md"
  cp "$repo/brainstorming_policy.md" "$sandbox/brainstorming_policy.md"
  cp -R "$repo/.claude" "$sandbox/.claude" 2>/dev/null || true   # claude -p 在镜像内的 allowlist(Write(tmp/**) 等)
  cp "$outdir"/*.md "$sandbox/tmp/awr-side/awr/" 2>/dev/null || true
  rm -f "$target" "$target_in_sandbox"
  prompt_in_sandbox=${prompt//$repo/$sandbox}
  pre=$(mirror_pre "$sandbox" "$target_in_sandbox" "tmp/round/、ideas/、ledger.tsv")   # 隔离预提示单源 lib/mirror_pre.sh
  if [ -z "$cmd" ]; then
    ( cd "$sandbox" && agy --model "$model" --add-dir "$sandbox" --print-timeout "$ptimeout" \
        -p "${pre}

${prompt_in_sandbox}" < /dev/null >> "$logf" 2>&1 )
  else
    # GROK_REPO=镜像:grok-worker 以镜像为工作根(不按脚本 dirname 逃回真仓);其它后端忽略。
    ( cd "$sandbox" && GROK_REPO="$sandbox" $cmd "${pre}

${prompt_in_sandbox}" < /dev/null >> "$logf" 2>&1 )
  fi
  rc=$?
  if [ -e "$target_in_sandbox" ]; then cp "$target_in_sandbox" "$target"; fi
  rm -rf "$sandbox"
  if [ -e "$target" ]; then
    nofile=0
  else
    nofile=$((nofile + 1))
    if [ "$nofile" -ge 3 ]; then
      if [ "$cooldown" -gt 0 ]; then
        log "熔断: 连续 ${nofile} 次调起无产物(疑似配额用尽/登录失效/断网,rc=$rc,详见各 .agy.log),冷却 ${cooldown}s 再试"
        sleep "$cooldown"; nofile=0
      else
        log "熔断: 连续 ${nofile} 次调起无产物(疑似配额用尽/登录失效/断网,rc=$rc,详见各 .agy.log),退出"
        exit 3
      fi
    fi
  fi
  return "$rc"
}

# 研究稿机械校验(早停/糊弄检测):修订版 idea 节、≥3 条带 URL 检索记录、末行 AGY-DONE。失败时 stdout 给原因。
check_draft() {
  local f=$1 n
  [ -s "$f" ] || { echo "空产物"; return 1; }
  grep -qE '^## 修订版 idea' "$f" || { echo "缺「## 修订版 idea」节"; return 1; }
  n=$(grep -cE '^- .*https?://' "$f" || true)
  [ "${n:-0}" -ge 3 ] || { echo "带 URL 的检索记录不足(${n:-0}<3)"; return 1; }
  [ "$(grep -v '^[[:space:]]*$' "$f" | tail -1)" = "AGY-DONE" ] || { echo "缺 AGY-DONE 末行(疑似早停)"; return 1; }
}

# 判定机械校验:判定行二选一、判还不行须附 ≥1 条缺陷、末行 AGY-DONE。
check_judge() {
  local f=$1 dec
  [ -s "$f" ] || { echo "空产物"; return 1; }
  dec=$(grep -E '^判定[::]' "$f" | head -1 | sed -E 's/^判定[::][[:space:]]*//')
  case "$dec" in
    SA-可能*) ;;
    还不行*) grep -qE '^- 缺陷[::]' "$f" || { echo "判还不行但无缺陷条目"; return 1; } ;;
    *) echo "判定行缺失或非法"; return 1 ;;
  esac
  [ "$(grep -v '^[[:space:]]*$' "$f" | tail -1)" = "AGY-DONE" ] || { echo "缺 AGY-DONE 末行(疑似早停)"; return 1; }
}

# 收尾成终态 <key>.md:bash 写头 + 现行草稿 + 最新判定。$1=key $2=状态描述(其余用外层变量)。
finalize() {
  { printf '# AwR 复活成品 %s\n- 状态: %s\n- 原始 idea: %s\n- 过程档: %s.task.md(含历轮反馈)\n\n' "$1" "$2" "$idea" "$1"
    cat "$draft"
    if [ -s "$judgef" ]; then printf '\n---\n## 最后裁判意见\n'; cat "$judgef"; fi
  } > "$out"
}

cd "$repo" || { echo "awr-side: 无法进入仓库根 $repo" >&2; exit 1; }
log "awr-side 启动: 研究=${research_cmd:-agy(内置,$model)} 裁判=${judge_cmd:-agy(内置,$model)} throttle=${gap_min}-${gap_max}s gap=${gap}s poll=${poll}s max_bad=$max_bad max_rounds=$max_rounds cooldown=${cooldown}s"

while :; do
  # 只信 bash 定谳基线(主环运行期间工作树 ledger.tsv 可能被 agent 篡改);快照后再遍历,避开读写窗口。
  src="$repo/tmp/ledger.good"; [ -s "$src" ] || src="$repo/ledger.tsv"
  snap="$outdir/.ledger.snap"; cp "$src" "$snap"
  did=0; pending=0
  while IFS=$'\t' read -r d source theme idea verdict reason _overlap <&3; do
    [ "$source" = "hunt" ] && [ "$verdict" = "accept-w-rev" ] || continue
    [ -n "$idea" ] || continue
    key=$(printf '%s' "$idea" | md5 | cut -c1-12)
    out="$outdir/$key.md"
    [ -s "$out" ] && continue                                  # 终态
    nbad=0
    for badf in "$outdir/$key".*.bad*; do
      [ -e "$badf" ] || continue                               # glob 未命中时字面量本身会进循环
      nbad=$((nbad + 1))
    done
    if [ "$nbad" -ge "$max_bad" ]; then continue; fi           # 拉黑
    pending=1
    task="$outdir/$key.task.md"; draft="$outdir/$key.draft.md"
    judgef="$outdir/$key.judge.md"; new="$outdir/$key.new.md"; alog="$outdir/$key.agy.log"
    if [ ! -s "$task" ]; then
      { printf '# AwR 任务 %s\n' "$key"
        printf -- '- 日期: %s\n- 主题: %s\n- idea: %s\n- reason(缺口来源): %s\n' "$d" "$theme" "$idea" "$reason"
      } > "$task"
    fi
    rounds=$(grep -c '^## 裁判反馈' "$task" 2>/dev/null) || rounds=0
    # 研究/修订:无草稿,或任务(含新反馈)比草稿新
    if ! { [ -s "$draft" ] && [ "$draft" -nt "$task" ]; }; then
      hint=""; [ -s "$draft" ] && hint=",既有草稿在 ${draft}(只读,在其基础上改进,不推倒重来)"
      log "调起 [研究:$key 第$((rounds + 1))轮]: $theme"
      run_agent "$research_cmd" "$new" "读 ${repo}/roles/awr.md,按其执行;任务输入在 ${task}${hint};产物写 ${new}。" "$alog"; rc=$?
      if why=$(check_draft "$new"); then
        mv -f "$new" "$draft"
      else
        mv -f "$new" "$outdir/$key.research.bad$((nbad + 1))" 2>/dev/null || true
        log "作废 [研究:$key](agy rc=$rc): ${why}$([ $((nbad + 1)) -ge "$max_bad" ] && printf ',已达 %s 次,拉黑' "$max_bad")"
        continue
      fi
    fi
    # 反馈轮数用尽:草稿已按最后一轮反馈修订过,不再评,直接收尾
    if [ "$rounds" -ge "$max_rounds" ]; then
      finalize "$key" "未达标(${max_rounds} 轮反馈用尽;末节裁判意见针对修订前草稿,缺陷已回灌并修订)"
      log "收尾 [awr:$key]: 未达标,${max_rounds} 轮反馈用尽"
      did=1; continue
    fi
    # 判定
    log "调起 [裁判:$key 第$((rounds + 1))轮]"
    run_agent "$judge_cmd" "$judgef" "读 ${repo}/roles/awr-judge.md,按其执行;待评草稿在 ${draft},任务背景在 ${task},评分标准在 ${repo}/rubric.md 与 ${repo}/brainstorming_policy.md;判定写 ${judgef}(覆盖旧内容)。" "$alog"; rc=$?
    if ! why=$(check_judge "$judgef"); then
      mv -f "$judgef" "$outdir/$key.judge.bad$((nbad + 1))" 2>/dev/null || true
      log "作废 [裁判:$key](agy rc=$rc): ${why}$([ $((nbad + 1)) -ge "$max_bad" ] && printf ',已达 %s 次,拉黑' "$max_bad")"
      continue
    fi
    if grep -qE '^判定[::][[:space:]]*SA-可能' "$judgef"; then
      finalize "$key" "达标(裁判判 SA-可能,第 $((rounds + 1)) 轮)"
      log "收尾 [awr:$key]: SA-可能,第 $((rounds + 1)) 轮"
    else
      { printf '\n## 裁判反馈 第%s轮\n' "$((rounds + 1))"; grep -E '^- 缺陷[::]' "$judgef"; } >> "$task"
      log "反馈 [awr:$key 第$((rounds + 1))轮]: 还不行,$(grep -cE '^- 缺陷[::]' "$judgef") 条缺陷回灌"
    fi
    did=1
  done 3< "$snap"
  if [ "$pending" = 0 ]; then
    [ "$poll" -gt 0 ] || { log "队列全终态,单遍模式退出"; exit 0; }
    log "队列全终态,${poll}s 后重扫"; sleep "$poll"; throttle_first=1
  elif [ "$did" = 0 ] && [ "$poll" -eq 0 ]; then
    log "剩余任务本遍全部作废,单遍模式退出"; exit 1
  fi
done
