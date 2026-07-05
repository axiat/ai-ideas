#!/usr/bin/env bash
# 裁判面板校准:对一个对照 case(ideas.md + priorwork.md)跑 N 位禁搜裁判,取最低票聚合。
# 与 hunt.sh 的评审阶段同构(独立目录、并行、min-vote),差异仅两点:
#   1. 裁判禁用一切检索——校准对照多为已发表工作,联网检索会变成"被自己占据"的假阴性;
#   2. 不写 ledger、不发布,结果只打印 + 留在 tmp/calib/ 供人工核对。
#
# 用法: ./calib/run_panel.sh calib/cases/<case> [裁判数,默认 3]
#   PANEL_CMD 覆盖裁判命令(默认 'claude -p');结果目录 tmp/calib/<case名>/rev/N
#
# 判读:
#   阳性对照(已知 oral/spotlight 的投稿前形态 + 理想 priorwork)——期望 min-vote ≥ accept-w-rev,
#     且出现过 strong-accept 票;若给足理想证据仍无人给 SA,瓶颈在 verdict 逻辑/聚合规则,不在生成与查重。
#   阴性对照(头条被单篇占据,priorwork 如实 high)——期望全票 reject;否则面板放水。
set -u
cd "$(dirname "$0")/.."

CASE=${1:?用法: ./calib/run_panel.sh calib/cases/<case> [裁判数]}
REVIEWERS=${2:-3}
PANEL_CMD=${PANEL_CMD:-claude -p}
name=$(basename "$CASE")
OUT="tmp/calib/$name"

[ -s "$CASE/ideas.md" ] && [ -s "$CASE/priorwork.md" ] || { echo "缺 $CASE/ideas.md 或 priorwork.md"; exit 2; }
rm -rf "$OUT"; mkdir -p "$OUT"

pids=()
for r in $(seq 1 "$REVIEWERS"); do
  d="$OUT/rev/$r"; mkdir -p "$d"
  cp "$CASE/ideas.md" "$CASE/priorwork.md" "$d/"
  echo "[calib] 调起裁判#$r -> $d"
  ( $PANEL_CMD "读 roles/review.md,按其执行;输入只在 ${d}/(ideas.md 与 priorwork.md)+ 仓库根 rubric.md、brainstorming_policy.md;verdict 写 ${d}/verdict.tsv,完整评审写 ${d}/review.md。校准附加铁律(优先级最高):本次禁用 WebSearch/WebFetch 及任何形式的联网检索,novelty 只依据 ${d}/priorwork.md;若怀疑某 idea 对应某篇已发表论文,不得据此改判,只在 review.md 末尾加一行「怀疑对应已发表工作:<名>」,verdict 仍严格按所给材料评" \
      > "$OUT/rev/$r.log" 2>&1; echo "$r $?" >> "$OUT/rev_rc" ) &
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
awk -F'\t' '{print $1}' "$CASE"/ideas.tsv 2>/dev/null > "$OUT/ids" || grep -oE '^## I[0-9]+' "$CASE/ideas.md" | awk '{print $2}' > "$OUT/ids"
rank_of() { case "$1" in strong-accept) echo 2 ;; accept-w-rev) echo 1 ;; *) echo 0 ;; esac; }
while read -r id; do
  [ -z "$id" ] && continue
  min=2; votes=""
  for r in $(seq 1 "$REVIEWERS"); do
    v=$(awk -F'\t' -v id="$id" '$1==id{print $2; exit}' "$OUT/rev/$r/verdict.tsv" 2>/dev/null)
    [ -z "$v" ] && v="缺票(计 reject)"
    votes="$votes  #$r=$v"
    rk=$(rank_of "$v"); [ "$rk" -lt "$min" ] && min=$rk
  done
  case "$min" in 2) agg=strong-accept ;; 1) agg=accept-w-rev ;; *) agg=reject ;; esac
  echo "$id:$votes  =>  min-vote: $agg"
  grep -h '怀疑对应已发表工作' "$OUT"/rev/*/review.md 2>/dev/null | sed 's/^/  [泄漏标记] /' | sort -u
done < "$OUT/ids"
echo "票据与完整评审在 $OUT/rev/*/;裁判日志 $OUT/rev/*.log"
