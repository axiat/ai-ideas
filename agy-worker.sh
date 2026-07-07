#!/usr/bin/env bash
# hunt.sh 前段(生成 + 查重)的 agy 适配层。只给「便宜、可错」的上游用;
# 后段(打分/报告)和 publish 绝不走这里——见 hunt.sh 顶部说明。
#
# 治三件事:
#   1) agy 相对路径漂移(有时写到 ~/.gemini/antigravity-cli/scratch 而非仓库):
#      把绝对仓库根钉进 prompt 前缀 + --add-dir,逼它写回仓库。真漂了也只是
#      tmp/round 产物缺失 → hunt.sh 判空重试,不污染 verdict。
#   2) 挂起:用 agy 自带 --print-timeout 兜底(macOS 默认无 GNU timeout)。
#   3) 快速重复调起触发登录验证:mkdir 锁 + 时间戳闸门,顺序与并发调起统一错峰(见下方临界区注释)。
#
# 用法(在 hunt.sh 里):
#   FRONT_CMD='./agy-worker.sh' BACK_CMD='claude -p' ./hunt.sh
# 可调:
#   AGY_MODEL          默认 'Gemini 3.5 Flash (High)'。只认 `agy models` 打印的完整展示名;连字符形式
#                      (如 gemini-3.5-flash-high)会被静默忽略、回落服务端默认 Gemini 3.5 Flash (Medium)
#                      ——2026-07-07 查 CLI 日志证实历史 352 次解析全部回落,此前旧默认从未生效,实跑 Medium
#                      (顺带作废早先 flash-low/high 对比结论:两边实为同一 Medium)。是否生效看
#                      ~/.gemini/antigravity-cli/log/ 的 "Propagating selected model override" 行,勿信模型自报。
#   AGY_PRINT_TIMEOUT  默认 8m
#   AGY_LAUNCH_GAP_SEC 默认 60:与上一次 agy 启动的最小间隔秒数(戳文件 tmp/agy.last-launch)。
#                      快速重复调起会触发登录验证;顺序阶段与多 agy 裁判席都被此闸门错峰。0 关闭。
set -u
repo="$(cd "$(dirname "$0")" && pwd)"
model=${AGY_MODEL:-Gemini 3.5 Flash (High)}
ptimeout=${AGY_PRINT_TIMEOUT:-8m}
gap=${AGY_LAUNCH_GAP_SEC:-60}
prompt=${1:?用法: agy-worker.sh <prompt>}
case "$gap" in ''|*[!0-9]*) echo "agy-worker: AGY_LAUNCH_GAP_SEC 必须是非负整数秒: $gap" >&2; exit 2 ;; esac

# 启动闸门:距上次 agy 启动不足 gap 秒则等待,防快速重复调起触发登录验证。
# mkdir 锁覆盖「读 stamp → 等待 → 写 stamp」整个临界区——hunt.sh 的裁判是并行调起的,
# 不持锁会多个 worker 同读旧戳一起放行。锁内等待 ≤ gap 秒,故锁龄 > gap+60s 视为陈旧;
# 持锁进程已死同样自清重抢(pid 文件缺失时只认锁龄,防误清刚建、还没写 pid 的锁)。
stamp="$repo/tmp/agy.last-launch"
lockd="$repo/tmp/agy.launch.lock"
if [ "$gap" -gt 0 ]; then
  mkdir -p "$repo/tmp"
  while ! mkdir "$lockd" 2>/dev/null; do
    holder=$(cat "$lockd/pid" 2>/dev/null || echo "")
    lock_m=$(stat -f %m "$lockd" 2>/dev/null || echo "")
    if { [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; } \
       || { [ -n "$lock_m" ] && [ $(( $(date +%s) - lock_m )) -gt $((gap + 60)) ]; }; then
      echo "agy-worker: 清理陈旧闸门锁(holder=${holder:-无})" >&2
      rm -rf "$lockd"
      continue
    fi
    sleep 1
  done
  echo $$ > "$lockd/pid"
  trap 'rm -rf "$lockd"' EXIT
  now=$(date +%s); last=$(cat "$stamp" 2>/dev/null || echo 0)
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  wait_s=$(( last + gap - now ))
  if [ "$wait_s" -gt 0 ]; then
    echo "agy-worker: 距上次 agy 启动不足 ${gap}s,等待 ${wait_s}s 再起" >&2
    sleep "$wait_s"
  fi
  date +%s > "$stamp"
  rm -rf "$lockd"
  trap - EXIT
fi

# 绝对路径前缀:agy 相对路径不稳,显式钉死仓库根,所有读写按此解析。
pre="仓库根(绝对路径)= ${repo}。当前工作目录已在此根下。所有读写路径(tmp/round/… roles/… rubric.md brainstorming_policy.md research_context.md ledger.tsv 等)一律相对该根解析;产物必须落在 ${repo}/tmp/round/,严禁写到 ~/.gemini、任何 scratch 目录或 \$HOME 其它位置。"

cd "$repo" || { echo "agy-worker: 无法进入仓库根 $repo" >&2; exit 1; }
exec agy --model "$model" --add-dir "$repo" --print-timeout "$ptimeout" -p "${pre}

${prompt}"
