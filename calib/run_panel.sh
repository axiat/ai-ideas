#!/usr/bin/env bash
# 裁判面板校准:对一个对照 case(ideas.md + priorwork.md)跑 N 位禁搜裁判,取最低票聚合。
# 与 hunt.sh 的评审阶段同构(独立目录、并行、min-vote),差异仅两点:
#   1. 裁判禁用一切检索——校准对照多为已发表工作,联网检索会变成"被自己占据"的假阴性;
#   2. 不写 ledger、不发布,结果只打印 + 留在 tmp/calib/ 供人工核对。
#
# 用法: ./calib/run_panel.sh calib/cases/<case> [裁判数,默认 3]
#   PANEL_CMD 覆盖裁判命令(默认 'claude -p --strict-mcp-config',不带任何用户级 MCP);结果目录 tmp/calib/<case名>/rev/N
#   grok 校准例: PANEL_CMD='./grok-worker.sh' ./calib/run_panel.sh calib/cases/pos-meanflow
#     禁搜机械层:grok 席自动注入 GROK_DISABLE_WEB=1(禁内建检索),claude 席镜像 settings deny
#     WebSearch/WebFetch;OS 层不限网络(shell curl 拦不住),shell 侧禁搜靠 prompt 铁律+泄漏标记人工核对。
#   codex 例须带 --skip-git-repo-check --ephemeral(镜像不是 git 仓库),禁搜故不开 --search/网络。
#
# 写界(与 awr-side.sh run_agent 同构):每席裁判只见一次性镜像(角色/rubric/policy/case 文件),
#   产物写镜像内 tmp/out/,bash 只拷回 verdict.tsv 与 review.md,镜像即弃;真仓不作为裁判工作目录。
#   镜像只隔 CWD,挡越界写的是后端沙箱(grok/codex/claude),无沙箱命令不要当 PANEL_CMD。
#   不同 case 的面板可并行;同一 case 不可(共享结果目录与镜像清扫)。
#
# 判读:
#   阳性对照(已知 oral/spotlight 的投稿前形态 + 理想 priorwork)——期望 min-vote ≥ accept-w-rev,
#     且出现过 strong-accept 票;若给足理想证据仍无人给 SA,瓶颈在 verdict 逻辑/聚合规则,不在生成与查重。
#   阴性对照(头条被单篇占据,priorwork 如实 high)——期望全票 reject;否则面板放水。
set -u
cd "$(dirname "$0")/.." || exit 2
repo=$(pwd)

CASE=${1:?用法: ./calib/run_panel.sh calib/cases/<case> [裁判数]}
REVIEWERS=${2:-3}
PANEL_CMD=${PANEL_CMD:-claude -p --strict-mcp-config}
name=$(basename "$CASE")
OUT="tmp/calib/$name"

[ -s "$CASE/ideas.md" ] && [ -s "$CASE/priorwork.md" ] || { echo "缺 $CASE/ideas.md 或 priorwork.md"; exit 2; }
# 裁判数校验必须在 rm -rf 清场前:REVIEWERS=0/非数字会让 seq 空转、空 pids 数组在 bash 3.2 + set -u
# 下 wait 直接崩(unbound variable)——若先清场,一次手误参数就毁掉上一轮票据还只留一句天书报错。
case "$REVIEWERS" in ''|*[!0-9]*) echo "run_panel: 裁判数必须是正整数: $REVIEWERS" >&2; exit 2 ;; esac
[ "$REVIEWERS" -ge 1 ] || { echo "run_panel: 裁判数须 ≥1: $REVIEWERS" >&2; exit 2; }

# PANEL_CMD 首词解析(相对路径钉真仓绝对路径、封 .. 路径段、裸名须可执行才遮蔽 PATH):
# 与 awr-side.sh 共用单源 lib/resolve_cmd.sh。同样必须在 rm -rf 清场前——
# 命令拼错属配置错,不应先毁掉上一轮面板的票据再 exit 2。
. "$repo/lib/resolve_cmd.sh"
RESOLVED_CMD=$(resolve_cmd "$repo" "run_panel: PANEL_CMD" "$PANEL_CMD") || exit 2
. "$repo/lib/mirror_pre.sh"

rm -rf "$OUT"; mkdir -p "$OUT"
# 镜像名单射编码(_→_u、.→_d):防 pos 的清扫 glob 前缀命中 pos.1 的活镜像,也防 foo.bar 与 foo_bar 同名互扫
mname=${name//_/_u}; mname=${mname//./_d}
rm -rf "$repo/tmp/panel.$mname."*   # 本 case 上次中断遗留的镜像;按 case 名限定,不伤并行中的其它面板

# 裁判输入先冻结成快照:活 $CASE 只在这一刻读一次,id 清单、各席镜像、事后审计全取自 $OUT 这份——
# 若镜像各自再读活 $CASE,面板启动中 case 被编辑会让各席输入不一致,快照也不再是「裁判当时读到的是什么」,
# 人工复核会把输入变更误读成裁判判错/泄漏。
cp "$CASE/ideas.md" "$CASE/priorwork.md" "$OUT/"

# case 的 id 清单,裁判产物校验(verdict_ok 必备集)与末尾聚合共用。单源取快照 ideas.md 的 `## I<n>`——
# 那正是发进镜像、裁判唯一所见的文件;若另立 ideas.tsv 为源,裁判按 md 投的票会因 id 集不符被 verdict_ok 全判失败、面板必败。
# 围栏感知提取单源 lib/md_ids.sh(run_e2e.sh 共用);未闭合围栏不静默照办,报错让人修 case。
. "$repo/lib/md_ids.sh"
md_idea_ids "$OUT/ideas.md" > "$OUT/ids" \
  || { echo "[calib] $CASE/ideas.md 有未闭合围栏(\`\`\`/~~~ 开栏无同字符等长关栏),其后标题会被吞,先修 case 再跑"; exit 2; }
[ -s "$OUT/ids" ] || { echo "[calib] 从 $CASE/ideas.md 提不出 id 清单(需 '## I<n>' 标题)"; exit 2; }

# $1=拷回时已规范化的 verdict.tsv(剥 CR/trim 见 run_judge)。合格 = 对 $OUT/ids 每个 id
# 恰好一行、verdict 枚举合法、无未知 id、无重复;容忍空行与首个非空行的 header。
verdict_ok() {
  [ -s "$1" ] || return 1
  awk -F'\t' -v idsf="$OUT/ids" '
    BEGIN{ while ((getline l < idsf) > 0) if (l != "") want[l]=1 }
    $1==""{ next }
    !seenline++ && ($1=="id" || $1=="ID" || $1 ~ /^#/){ next }
    !($1 in want){ exit 1 }
    $2!="strong-accept" && $2!="accept-w-rev" && $2!="reject"{ exit 1 }
    { if (++seen[$1] > 1) exit 1 }
    END{ for (k in want) if (seen[k] != 1) exit 1 }
  ' "$1"
}

# $1=席位号。建镜像 → 调起 → 只拷回 verdict.tsv/review.md → 弃镜像。整体在后台跑,一席一镜像。
run_judge() {
  local r=$1 mirror pre rc logf
  logf="$repo/$OUT/rev/$r.log"   # 子 shell 已 cd 进镜像,日志重定向必须用绝对路径
  # 镜像路径含 case 名(清扫互不干扰)且不得含 calib 段(grok-worker 的 **/calib/** 写禁按绝对路径匹配)
  mirror=$(mktemp -d "$repo/tmp/panel.$mname.$r.XXXXXX") || { echo "$r 1" >> "$OUT/rev_rc"; return 1; }
  mkdir -p "$mirror/roles" "$mirror/tmp/out"   # 产物放镜像 tmp/ 下:claude allowlist 只放行 Write(tmp/**)
  cp "$repo/roles/review.md" "$mirror/roles/review.md"
  cp "$repo/rubric.md" "$repo/brainstorming_policy.md" "$mirror/"
  cp "$repo/$OUT/ideas.md" "$repo/$OUT/priorwork.md" "$mirror/tmp/out/"   # 从冻结快照拷,不再读活 $CASE
  # calib 专用 claude 权限:只许写 tmp/**,并机械 deny WebSearch/WebFetch——真仓 allowlist 放行检索,
  # 原样拷入会让 claude 席的禁搜只剩 prompt 层。codex/grok 不读此目录。
  mkdir -p "$mirror/.claude"
  cat > "$mirror/.claude/settings.json" <<'JSON'
{
  "permissions": {
    "allow": ["Edit(tmp/**)", "Write(tmp/**)"],
    "deny": ["WebSearch", "WebFetch"]
  }
}
JSON
  pre=$(mirror_pre "$mirror" "tmp/out/verdict.tsv 与 tmp/out/review.md")   # 隔离预提示单源 lib/mirror_pre.sh
  # GROK_REPO 钉 grok 工作根;GROK_DISABLE_WEB=1 机械落实校准禁搜铁律。其它后端忽略这两个变量。
  ( cd "$mirror" && GROK_REPO="$mirror" GROK_DISABLE_WEB=1 $RESOLVED_CMD "${pre}

读 roles/review.md,按其执行;输入目录 D = tmp/out/(ideas.md 与 priorwork.md),固定层在仓库根(rubric.md、brainstorming_policy.md);verdict 写 tmp/out/verdict.tsv,完整评审写 tmp/out/review.md。校准附加铁律(优先级最高):本次禁用 WebSearch/WebFetch 及任何形式的联网检索,novelty 只依据 tmp/out/priorwork.md;若怀疑某 idea 对应某篇已发表论文,不得据此改判,只在 review.md 末尾加一行「怀疑对应已发表工作:<名>」,verdict 仍严格按所给材料评" \
      < /dev/null > "$logf" 2>&1 )
  rc=$?
  # verdict.tsv 拷回即规范化(剥 BOM/CR、trim 各字段):校验与聚合必须读同一份规范文本,
  # 否则校验层容忍的尾随空白会让聚合层 $1==id 失配,把合法票降成缺票 reject;
  # 带 BOM 的合法票首 id 会变 "\xef\xbb\xbfI1" 被判未知 id,同类隐形字节假失败。
  # LC_ALL=C 让 substr 按字节计(UTF-8 aware awk 把 BOM 当 1 字符,substr(_,4) 会多剥)。
  if [ -f "$mirror/tmp/out/verdict.tsv" ]; then
    LC_ALL=C awk -F'\t' 'BEGIN{OFS="\t"}
      NR==1 && substr($0,1,3) == "\357\273\277" { $0 = substr($0, 4) }
      { sub(/\r$/, ""); for (j=1; j<=NF; j++) gsub(/^[ \t]+|[ \t]+$/, "", $j); print }' \
      "$mirror/tmp/out/verdict.tsv" > "$OUT/rev/$r/verdict.tsv"
  fi
  [ -f "$mirror/tmp/out/review.md" ] && cp "$mirror/tmp/out/review.md" "$OUT/rev/$r/review.md"
  rm -rf "$mirror"
  # rc=0 时逐 id 硬校验 verdict.tsv:否则坏裁判在阴性对照里以「缺票/错 id/非法枚举 = reject」
  # 伪装成正确全 reject。review.md 不作硬校验——全 reject 的裁判按角色约定只写 tsv,无 review.md 是合法形态。
  if [ "$rc" -eq 0 ] && ! verdict_ok "$OUT/rev/$r/verdict.tsv"; then
    echo "[calib] 裁判#$r rc=0 但 verdict.tsv 缺失或不合格(须每 id 恰好一行+枚举合法+无未知 id/重复),按失败计(见 $OUT/rev/$r.log)" >&2
    rc=1
  fi
  echo "$r $rc" >> "$OUT/rev_rc"
  return "$rc"
}

pids=()
for r in $(seq 1 "$REVIEWERS"); do
  mkdir -p "$OUT/rev/$r"
  echo "[calib] 调起裁判#$r(镜像隔离)-> $OUT/rev/$r"
  run_judge "$r" &
  pids+=("$!")
done
wait "${pids[@]}"

# 裁判返回码校验(同 hunt.sh):恰好 REVIEWERS 行、每行 rc=0;缺席/非 0 直接失败退出——
# 崩溃的裁判若按"缺票=reject"聚合,会把 agent 故障误读成"阴性对照全 reject"的假结果。
if ! awk -v n="$REVIEWERS" 'NF==2 && $2==0{ok++} END{exit !(ok==n)}' "$OUT/rev_rc" 2>/dev/null; then
  echo "[calib] 有裁判异常退出或缺席: $(tr '\n' ' ' < "$OUT/rev_rc" 2>/dev/null),校准作废;见 $OUT/rev/*.log" >&2
  exit 2
fi

echo
echo "=== 校准结果: $name(取最低票;SA 需全票)==="
rank_of() { case "$1" in strong-accept) echo 2 ;; accept-w-rev) echo 1 ;; *) echo 0 ;; esac; }
: > "$OUT/aggregate.tsv"   # 机器可读聚合(id、逗号票串、min-vote),run_all.sh 按 expect 断言打分用
while read -r id; do
  [ -z "$id" ] && continue
  min=2; votes=""; vcsv=""
  for r in $(seq 1 "$REVIEWERS"); do
    v=$(awk -F'\t' -v id="$id" '$1==id{print $2; exit}' "$OUT/rev/$r/verdict.tsv" 2>/dev/null)
    # rc 校验+verdict_ok 已保证每 id 恰好一票,这里查不到只能是内部矛盾(读错文件/竞态),
    # 响亮中止;不做「缺票=reject」静默降级——那会把故障伪装成阴性对照的正确全 reject。
    [ -z "$v" ] && { echo "[calib] 内部不一致: 裁判#$r 已过 verdict_ok 却查不到 $id 的票,校准作废" >&2; exit 2; }
    votes="$votes  #$r=$v"
    vcsv="${vcsv:+$vcsv,}$v"
    rk=$(rank_of "$v"); [ "$rk" -lt "$min" ] && min=$rk
  done
  case "$min" in 2) agg=strong-accept ;; 1) agg=accept-w-rev ;; *) agg=reject ;; esac
  echo "$id:$votes  =>  min-vote: $agg"
  printf '%s\t%s\t%s\n' "$id" "$vcsv" "$agg" >> "$OUT/aggregate.tsv"
done < "$OUT/ids"
# 泄漏标记全局打印一次(裁判在 review.md 末尾标,不带 id 归属);放循环内会对每个 id 重复整份、
# 让人把 1 条泄漏读成 N 条。LC_ALL=C:BSD sort 在 UTF-8 locale 下等长不同 CJK 串互判相等,会把多条吞成一条
leaks=$(grep -h '怀疑对应已发表工作' "$OUT"/rev/*/review.md 2>/dev/null | LC_ALL=C sort -u)
[ -n "$leaks" ] && printf '%s\n' "$leaks" | sed 's/^/[泄漏标记] /'
echo "票据与完整评审在 $OUT/rev/*/;裁判输入快照 $OUT/ideas.md、$OUT/priorwork.md;裁判日志 $OUT/rev/*.log"
