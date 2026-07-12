#!/usr/bin/env bash
# gold set 批跑与机器判读:对每个带 expect 的 case 跑 run_panel.sh,按 expect 断言打分,
# 打印校准正确率(= pass / (pass+fail),probe 与 panel-fail 不计入分母——面板基础设施失败
# 不得形成校准结论),结果逐 case 追加 tmp/calib/summary.tsv。
#
# expect DSL(calib/cases/<case>/expect,每行一条,# 注释;断言对 case 内每个 id 都须成立):
#   min_vote>=accept-w-rev | min_vote>=strong-accept    聚合最低票下限
#   sa_votes>=N | sa_votes=N                            strong-accept 票数
#   reject_votes>=N                                     reject 票数
#   all_votes=reject                                    全票 reject
#   probe                                               只跑不打分(判读表见 calib/README.md)
# 票数阈值按默认 3 裁判书写;换裁判数时须一并复核 expect。未知断言响亮退出,不静默跳过。
#
# 用法: ./calib/run_all.sh [裁判数,默认 3] [case 目录...]   # 缺省 = calib/cases/ 下全部带 expect 的
#   PANEL_CMD 透传 run_panel.sh(裁判后端、禁搜与镜像隔离见彼处头注)。
#   case 间串行:并行会同时压起多份 N 裁判面板,agy/grok 后端还会互踩启动闸门。
# 退出码: 0=全过;1=存在 fail 或 panel-fail;2=配置/用法错误。
set -u
cd "$(dirname "$0")/.." || exit 2

REVIEWERS=3
if [ $# -ge 1 ] && [ ! -d "$1" ]; then
  case "$1" in ''|*[!0-9]*) echo "run_all: 首参须是裁判数或 case 目录: $1" >&2; exit 2 ;; esac
  REVIEWERS=$1; shift
fi
[ "$REVIEWERS" -ge 1 ] || { echo "run_all: 裁判数须 ≥1: $REVIEWERS" >&2; exit 2; }

cases=()
if [ $# -ge 1 ]; then
  cases=("$@")
else
  for d in calib/cases/*/; do
    [ -f "${d}expect" ] && cases+=("${d%/}")
  done
fi
[ "${#cases[@]}" -ge 1 ] || { echo "run_all: 没有带 expect 的 case 可跑" >&2; exit 2; }

SUMMARY=tmp/calib/summary.tsv
mkdir -p tmp/calib
[ -s "$SUMMARY" ] || printf 'ts\tcase\treviewers\tpanel_cmd\tgrade\tvotes\tfailed_checks\n' > "$SUMMARY"

rank_of() { case "$1" in strong-accept) echo 2 ;; accept-w-rev) echo 1 ;; *) echo 0 ;; esac; }

# 对一个 id 的票评一条断言;$1=断言 $2=聚合 min-vote $3=SA 票数 $4=reject 票数 $5=总票数
check_one() {
  local want n
  case "$1" in
    'min_vote>='*)
      want=${1#min_vote>=}
      case "$want" in strong-accept|accept-w-rev) ;; *) echo "run_all: 非法 min_vote 断言: $1" >&2; exit 2 ;; esac
      [ "$(rank_of "$2")" -ge "$(rank_of "$want")" ] ;;
    'sa_votes>='*)
      n=${1#sa_votes>=}; case "$n" in ''|*[!0-9]*) echo "run_all: 非法断言: $1" >&2; exit 2 ;; esac
      [ "$3" -ge "$n" ] ;;
    'sa_votes='*)
      n=${1#sa_votes=}; case "$n" in ''|*[!0-9]*) echo "run_all: 非法断言: $1" >&2; exit 2 ;; esac
      [ "$3" -eq "$n" ] ;;
    'reject_votes>='*)
      n=${1#reject_votes>=}; case "$n" in ''|*[!0-9]*) echo "run_all: 非法断言: $1" >&2; exit 2 ;; esac
      [ "$4" -ge "$n" ] ;;
    'all_votes=reject')
      [ "$4" -eq "$5" ] ;;
    *) echo "run_all: 未知断言(不静默跳过): $1" >&2; exit 2 ;;
  esac
}

pass=0; fail=0; probe=0; panelfail=0
for c in ${cases[@]+"${cases[@]}"}; do
  name=$(basename "$c")
  expectf="$c/expect"
  [ -f "$expectf" ] || { echo "run_all: $c 缺 expect,跳过" >&2; continue; }
  echo
  echo "########## run_all: $name(${REVIEWERS} 裁判)##########"
  grade=pass; failed=""
  if ! ./calib/run_panel.sh "$c" "$REVIEWERS"; then
    grade=panel-fail
    panelfail=$((panelfail + 1))
  else
    agg_file="tmp/calib/$name/aggregate.tsv"
    [ -s "$agg_file" ] || { echo "run_all: $name 面板成功但缺 aggregate.tsv(run_panel 版本过旧?)" >&2; exit 2; }
    is_probe=0
    while IFS= read -r line; do
      line=${line%%#*}
      line=$(printf '%s' "$line" | tr -d '[:space:]')
      [ -z "$line" ] && continue
      [ "$line" = "probe" ] && { is_probe=1; continue; }
      # 断言对每个 id 独立评,任一 id 不满足即记失败
      while IFS=$'\t' read -r id vcsv agg; do
        [ -z "$id" ] && continue
        sa=$(printf '%s' "$vcsv" | tr ',' '\n' | grep -cx 'strong-accept' || true)
        rej=$(printf '%s' "$vcsv" | tr ',' '\n' | grep -cx 'reject' || true)
        total=$(printf '%s' "$vcsv" | tr ',' '\n' | grep -c . || true)
        if ! check_one "$line" "$agg" "$sa" "$rej" "$total"; then
          failed="${failed:+$failed;}${id}:${line}"
        fi
      done < "$agg_file"
    done < "$expectf"
    if [ "$is_probe" = "1" ]; then
      grade=probe; probe=$((probe + 1))
    elif [ -n "$failed" ]; then
      grade=fail; fail=$((fail + 1))
    else
      pass=$((pass + 1))
    fi
  fi
  votes=$(awk -F'\t' '{printf "%s%s=%s->%s", (NR>1?";":""), $1, $2, $3} END{if(!NR)printf "-"}' "tmp/calib/$name/aggregate.tsv" 2>/dev/null)
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$(date '+%F %T')" "$name" "$REVIEWERS" "${PANEL_CMD:-claude -p --strict-mcp-config}" \
    "$grade" "${votes:--}" "${failed:--}" >> "$SUMMARY"
  echo "run_all: $name => $grade${failed:+(未过: $failed)}"
done

echo
graded=$((pass + fail))
echo "=== run_all 汇总: pass=$pass fail=$fail probe=$probe panel-fail=$panelfail ==="
if [ "$graded" -gt 0 ]; then
  echo "校准正确率: $pass/$graded(probe 与 panel-fail 不计入;逐 case 见 $SUMMARY)"
else
  echo "无计分 case(全为 probe/panel-fail),校准正确率不适用"
fi
[ "$fail" -eq 0 ] && [ "$panelfail" -eq 0 ]
