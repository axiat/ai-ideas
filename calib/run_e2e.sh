#!/usr/bin/env bash
# 端到端(真实检索)校准——检索侧。与 run_panel.sh 的冻结校准分开:冻结校准固定 ideas+priorwork,
# 验 verdict 逻辑/聚合规则;本脚本放开检索,验「查重进程能否召回已知占位并如实定级」。
# 对带 e2e.expect 的 case(direct-hit 阴性)镜像跑 roles/research.md,机械断言 priorwork 产物。
# 阳性对照没有端到端跑法:已发表工作会被真检索找到、判成"被自己占据"的假阴性(见 calib/README.md)。
# 效力边界:机械断言只查产物结构(重叠判定、占位命中、近邻/API 密度),无法从纯文本证明真发生了检索——
# 效力来自用真·联网后端跑 research 角色。故 E2E 只作回归门(拦薄/空/漏占位),不作对抗性造假的证明。
#
# e2e.expect DSL(calib/cases/<case>/e2e.expect,每行一条,# 注释;对每个 id 都须成立):
#   overlap=high|medium|low        priorwork 块的重叠判定
#   url_contains=<substr>          priorwork 块内出现该子串(已知占位的 arXiv 编号/URL 片段)
#
# 用法: ./calib/run_e2e.sh calib/cases/<case>
#   E2E_CMD 覆盖 agent 命令(默认 'claude -p --strict-mcp-config')。与面板相反,后端必须放行检索:
#   grok 席不注入 GROK_DISABLE_WEB;codex 席须开 --search 与网络。镜像隔离与写界同 run_panel
#   (一次性镜像、bash 只拷回 priorwork.md、E2E_CMD 必须自带沙箱)。
# 结果追加 tmp/calib/summary.tsv(case 名带 e2e: 前缀)。
# 退出码: 0=断言全过;1=断言未过;2=配置错或 agent 调起失败(基础设施失败,不构成校准结论)。
set -u
cd "$(dirname "$0")/.." || exit 2
repo=$(pwd)

CASE=${1:?用法: ./calib/run_e2e.sh calib/cases/<case>}
E2E_CMD=${E2E_CMD:-claude -p --strict-mcp-config}
E2E_MIN_LINKS=${E2E_MIN_LINKS:-5}   # 每 idea 块非 API 近邻链接下限,同 hunt.sh PRIOR_MIN_LINKS 默认;检索证据硬门槛
name=$(basename "$CASE")
OUT="tmp/calib/e2e-$name"
SUMMARY=tmp/calib/summary.tsv

[ -s "$CASE/ideas.md" ] || { echo "run_e2e: 缺 $CASE/ideas.md" >&2; exit 2; }
[ -s "$CASE/e2e.expect" ] || { echo "run_e2e: 缺 $CASE/e2e.expect(端到端断言)" >&2; exit 2; }

. "$repo/lib/resolve_cmd.sh"
RESOLVED_CMD=$(resolve_cmd "$repo" "run_e2e: E2E_CMD" "$E2E_CMD") || exit 2
. "$repo/lib/mirror_pre.sh"
. "$repo/lib/md_ids.sh"

rm -rf "$OUT"; mkdir -p "$OUT"
mkdir -p tmp/calib
[ -s "$SUMMARY" ] || printf 'ts\tcase\treviewers\tpanel_cmd\tgrade\tvotes\tfailed_checks\n' > "$SUMMARY"
# 镜像名单射编码同 run_panel(_→_u、.→_d),清扫按 case 名限定
mname=${name//_/_u}; mname=${mname//./_d}
rm -rf "$repo/tmp/e2e.$mname."*

# 输入冻结成快照:id 清单、镜像、事后审计全取自这一份
cp "$CASE/ideas.md" "$OUT/"
md_idea_ids "$OUT/ideas.md" > "$OUT/ids" \
  || { echo "run_e2e: $CASE/ideas.md 有未闭合围栏(其后标题会被吞),先修 case 再跑" >&2; exit 2; }
[ -s "$OUT/ids" ] || { echo "run_e2e: 从 $CASE/ideas.md 提不出 id 清单(需 '## I<n>' 标题)" >&2; exit 2; }

mirror=$(mktemp -d "$repo/tmp/e2e.$mname.XXXXXX") || { echo "run_e2e: 建镜像失败" >&2; exit 2; }
mkdir -p "$mirror/roles" "$mirror/tmp/round"
cp "$repo/roles/research.md" "$mirror/roles/"
cp "$OUT/ideas.md" "$mirror/tmp/round/ideas.md"
mkdir -p "$mirror/.claude"
# E2E 专用 claude 权限:放行检索(与冻结面板相反,E2E 要真检索)+ 只写镜像内 tmp/**;
# 不拷真仓 settings——那含 Write(//tmp/**)、Write(//private/tmp/**),会让 research 席把持久写留在镜像外。
cat > "$mirror/.claude/settings.json" <<'JSON'
{
  "permissions": {
    "allow": ["Edit(tmp/**)", "Write(tmp/**)", "WebSearch", "WebFetch"]
  }
}
JSON
pre=$(mirror_pre "$mirror" "tmp/round/priorwork.md")
logf="$repo/$OUT/research.log"   # 子 shell 已 cd 进镜像,日志重定向必须用绝对路径
( cd "$mirror" && GROK_REPO="$mirror" $RESOLVED_CMD "${pre}

读 roles/research.md,按其执行" < /dev/null > "$logf" 2>&1 )
rc=$?
[ -f "$mirror/tmp/round/priorwork.md" ] && cp "$mirror/tmp/round/priorwork.md" "$OUT/priorwork.md"
rm -rf "$mirror"
if [ "$rc" -ne 0 ] || [ ! -s "$OUT/priorwork.md" ]; then
  echo "run_e2e: research 调起失败(rc=$rc)或未产出 priorwork.md,见 $OUT/research.log" >&2
  printf '%s\te2e:%s\t-\t%s\tagent-fail\t-\t-\n' "$(date '+%F %T')" "$name" "$E2E_CMD" >> "$SUMMARY"
  exit 2
fi

failed=""; detail=""
while read -r id; do
  [ -z "$id" ] && continue
  block=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$OUT/priorwork.md")
  if [ -z "$block" ]; then
    failed="${failed:+$failed;}${id}:missing-block"
    detail="${detail:+$detail;}${id}=missing"
    continue
  fi
  # 锚定行首:块内其它行可能提及「重叠判定」字样(如 API 召回说明"不作重叠判定依据"),
  # 非锚定 grep -m1 会抓错行、把真实判定 high 误读成路过词
  ov=$(printf '%s\n' "$block" | grep -m1 '^重叠判定' | grep -oE 'high|medium|low' | head -1)
  # 近邻链接排除 API URL(与 hunt.sh priorwork_ok 同口径);API 检索记录单独计数
  links=$(printf '%s\n' "$block" | grep -E '^- .*https?://' \
          | grep -cvE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org' || true)
  api=$(printf '%s\n' "$block" | grep -cE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org' || true)
  detail="${detail:+$detail;}${id}=${ov:-无判定},links=${links},api=${api}"
  # 检索结构门槛(独立于 e2e.expect):要求 ≥E2E_MIN_LINKS 条非 API 近邻 + ≥1 条结构化 API query URL
  # (同 hunt.sh priorwork_ok 与 roles/research.md 口径),不足即判 retrieval-thin。
  # 界定:这只证明「产物结构完整」,不证明「发生了真检索」——纯文本无法证伪一个硬编码这些字符串、
  # 甚至明写「未检索」的离线 agent。E2E 的效力来自用真·联网后端(claude/codex/grok+web)跑 research 角色,
  # 门槛只拦薄/空产物这类回归,不是防对抗性造假的机械证明。判读 E2E 结果时须默认后端确实联网。
  if [ "$links" -lt "$E2E_MIN_LINKS" ] || [ "$api" -lt 1 ]; then
    failed="${failed:+$failed;}${id}:retrieval-thin(links=${links}<${E2E_MIN_LINKS} 或 api=${api}<1)"
  fi
  while IFS= read -r line; do
    line=${line%%#*}
    line=$(printf '%s' "$line" | tr -d '[:space:]')
    [ -z "$line" ] && continue
    case "$line" in
      'overlap='*)
        want=${line#overlap=}
        case "$want" in high|medium|low) ;; *) echo "run_e2e: 非法断言: $line" >&2; exit 2 ;; esac
        [ "${ov:-}" = "$want" ] || failed="${failed:+$failed;}${id}:${line}" ;;
      'url_contains='*)
        want=${line#url_contains=}
        [ -n "$want" ] || { echo "run_e2e: url_contains 断言为空" >&2; exit 2; }
        printf '%s\n' "$block" | grep -qF "$want" || failed="${failed:+$failed;}${id}:${line}" ;;
      *) echo "run_e2e: 未知断言(不静默跳过): $line" >&2; exit 2 ;;
    esac
  done < "$CASE/e2e.expect"
done < "$OUT/ids"

grade=pass; [ -n "$failed" ] && grade=fail
printf '%s\te2e:%s\t-\t%s\t%s\t%s\t%s\n' \
  "$(date '+%F %T')" "$name" "$E2E_CMD" "$grade" "${detail:--}" "${failed:--}" >> "$SUMMARY"
echo "run_e2e: $name => $grade${failed:+(未过: $failed)}"
echo "产物: $OUT/priorwork.md;agent 日志 $OUT/research.log;输入快照 $OUT/ideas.md"
[ "$grade" = pass ]
