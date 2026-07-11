#!/usr/bin/env bash
# hunt.sh / awr-side.sh / calib 的 grok 适配层。可当前段、后段或裁判席(与 claude/codex 同级接入)。
#
# 治:
#   1) CLI 形态:grok 的 -p/--single 必须带 prompt 值;裸 positional 在无 TTY 下失败。
#      hunt.sh 的 run_stage 是 `$cmd "$prompt"`,故不能写 AGENT_CMD='grok -p --flags'
#      (flags 会被 -p 吞掉)。本脚本只收单一 prompt 参数(多参报错,配置全走 GROK_* 环境变量),
#      再拼成合法无头调用。
#   2) 无人值守:显式 --always-approve(不依赖用户 ~/.grok/config.toml 的 permission_mode)。
#   3) 写界(边界只到「文件写域」,不含进程/网络,且有已知绕过——勿当强隔离):
#      --sandbox workspace → OS 写限 CWD + tmp + ~/.grok(类 codex workspace-write);
#      另对 ledger/固定层/脚本层/calib 发 Edit|Write deny。deny 挡的是 file tools,也拦得住
#      能被静态识别为写目标的 shell 命令(tee/cp/mv/重定向实测被拒);但间接写绕得过——
#      实测 `python3 -c 'open(path,"a").write(...)'` 直接改写 deny 名单内文件。因此:
#        · ledger 完整性最终靠 hunt.sh 的 ledger.good 快照 + 回路守卫兜底,不靠本层 deny;
#        · 校准面板靠 run_panel.sh 镜像执行(裁判不见真仓,只回传 verdict/review)——隔的是
#          文件树与 CWD,不隔进程与网络。
#      注意:不能把 ledger/roles 放进 sandbox deny 列表——kernel deny 是读+写,角色要读它们。
#   3b) sandbox workspace 不禁网络、不禁进程:terminal 实测可跑 git/gh/curl 并联网(HTTP 200)。
#      grok 还继承用户级 ~/.claude+~/.grok 的 hooks/plugins/MCP(inspect 实见 12 hooks、cowart MCP
#      起 bash、数十 skills),镜像隔不掉全局配置。这些外部副作用当前无机械闸,只靠 prompt 铁律与
#      角色约定;真要机械封网/封进程须把 grok 再包一层 OS 沙箱(sandbox-exec/容器),本脚本不做。
#   4) 工作根:默认脚本所在目录。AwR 临时镜像须设 GROK_REPO=<镜像绝对路径>,否则
#      dirname($0) 会钉回真仓库、拆掉物理隔离(见 awr-side.sh run_agent)。
#   5) 减噪:--no-subagents(禁派生子代理)。勿用 --disallowed-tools Agent——当前 grok 0.2.x
#      会在 agent 构建期炸 run_terminal_cmd 的 schema 约束(auto_background_on_timeout)。
#      GROK_DISABLE_AUTOUPDATER=1 抑更新检查。
#   6) 不碰 publish/git/gh:prompt 约定,非机械保证(见 3b,terminal 实际可跑 git/gh)。
#      发布只由 bash 调 publish.sh;hunt.sh 回路守卫在 agent 返回后才比对,挡不住运行中的外发。
#
# 用法:
#   AGENT_CMD='./grok-worker.sh' ./hunt.sh
#   FRONT_CMD='./agy-worker.sh' BACK_CMD='./grok-worker.sh' ./hunt.sh
#   FRONT_CMD='./grok-worker.sh' BACK_CMD='claude -p --strict-mcp-config' ./hunt.sh
#   PANEL_CMD='./grok-worker.sh' ./calib/run_panel.sh calib/cases/pos-meanflow
#   SIDE_CMD='./grok-worker.sh' ./awr-side.sh   # awr-side 会解析为绝对路径并注入 GROK_REPO=镜像
# 可调:
#   GROK_REPO          工作根(绝对路径);不设=脚本所在目录。AwR 镜像必设。
#   GROK_MODEL         默认 grok-4.5(见 `grok models`)
#   GROK_MAX_TURNS     默认 80;查重/裁判轮次多,过小会 max_turns 中途死
#   GROK_SANDBOX       固定枚举 workspace(默认)/off(关闭 OS sandbox,不推荐),其它值 exit 2
#                      (grok 对未知 profile 只告警就无沙箱跑,不能放行;不支持 sandbox.toml 自定义名)
#   GROK_DISABLE_WEB   设为 1 时加 --disable-web-search(校准面板用,禁内建检索;shell curl 不在此列)
#   GROK_BIN           默认 grok(PATH 解析)
set -u
self_dir="$(cd "$(dirname "$0")" && pwd)"
repo=${GROK_REPO:-$self_dir}
# AGENT_CMD 契约下 prompt 是唯一参数;多参=命令串里塞了 flags,静默吞掉会用错配置跑完全程,报错退出
[ "$#" -eq 1 ] || { echo "grok-worker: 只接受 1 个参数(prompt),收到 $# 个;模型/轮数等一律经 GROK_* 环境变量配置" >&2; exit 2; }
prompt=$1
model=${GROK_MODEL:-grok-4.5}
max_turns=${GROK_MAX_TURNS:-80}
sandbox=${GROK_SANDBOX:-workspace}
bin=${GROK_BIN:-grok}
disable_web=${GROK_DISABLE_WEB:-0}

case "$max_turns" in ''|*[!0-9]*) echo "grok-worker: GROK_MAX_TURNS 必须是正整数: $max_turns" >&2; exit 2 ;; esac
[ "$max_turns" -ge 1 ] || { echo "grok-worker: GROK_MAX_TURNS 须 ≥1: $max_turns" >&2; exit 2; }
# 禁搜是安全开关,不能 fail-open:枚举校验,认不出的值直接退出而非默默留检索开着(与上面 MAX_TURNS 同风格)
case "$disable_web" in
  ''|0|false|no|off) disable_web=0 ;;
  1|true|yes|on)     disable_web=1 ;;
  *) echo "grok-worker: GROK_DISABLE_WEB 只接受 0/1/true/false/yes/no/on/off: $disable_web" >&2; exit 2 ;;
esac
case "$repo" in
  /*) ;;
  *) echo "grok-worker: GROK_REPO 必须是绝对路径: $repo" >&2; exit 2 ;;
esac
[ -d "$repo" ] || { echo "grok-worker: 工作根不存在: $repo" >&2; exit 2; }
# 沙箱同为安全开关,不能 fail-open:grok 0.2.x 对认不出的 profile 只打 warning 就无沙箱跑完全程(实测)。
# 固定枚举 workspace(内建)/off(显式关),不支持 sandbox.toml 自定义 profile——
# 放行自定义名就得可靠判定「toml 里真有这个 table」,grep 级检查会被注释里的同名串骗过(fail-open),
# 真做对要 TOML parser + profile 名转义,为一个无人在用的形态不值;要用自定义 profile 再来改这里。
case "$sandbox" in
  workspace|off) ;;
  *) echo "grok-worker: GROK_SANDBOX 只接受 workspace/off(grok 对认不出的 profile 只告警就无沙箱跑,不放行): $sandbox" >&2; exit 2 ;;
esac

cd "$repo" || { echo "grok-worker: 无法进入工作根 $repo" >&2; exit 1; }
command -v "$bin" >/dev/null 2>&1 || { echo "grok-worker: 找不到 grok 可执行: $bin" >&2; exit 2; }

export GROK_DISABLE_AUTOUPDATER=1

# file-tool 写禁:台账、固定层、入口脚本、校准树、发布/编排。读仍允许。best-effort,非气密(见头注 3):
# 挡 file tools 与可被静态识别的 shell 写(tee/cp/mv/重定向),但间接写(python open().write() 等)绕得过。
# Grok file tools 常传绝对路径;目录 glob 须用 **/dir/**,仅 dir/** 在部分路径形态下匹配失败。
# 单文件:相对 + 绝对 + **/name;目录:相对 dir/** + **/dir/** + 绝对 $repo/dir/**。
# ledger 完整性最终靠 hunt ledger.good 快照兜底;校准面板靠 run_panel.sh 镜像执行(隔文件树/CWD,不隔网/进程)。
denies=()
deny_write_edit() {
  # $1=glob 或路径(原样塞进 Write/Edit)
  local g=$1
  denies+=(--deny "Write($g)" --deny "Edit($g)")
}
deny_file() {
  # $1=相对工作根的文件名(可含路径)
  local p=$1 base
  base=$(basename "$p")
  deny_write_edit "$p"
  deny_write_edit "$repo/$p"
  deny_write_edit "**/$base"
}
deny_tree() {
  # $1=相对工作根的目录名(无尾 /)
  local d=$1
  deny_write_edit "$d/**"
  deny_write_edit "**/$d/**"
  deny_write_edit "$repo/$d/**"
}

deny_file 'ledger.tsv'
deny_file 'tmp/ledger.good'   # hunt.sh 唯一可信台账基线:tmp/ 对 file-tool 可写且在守卫视野外,毒它=毒下次聚合
for p in \
  PROGRAM.md rubric.md brainstorming_policy.md research_context.md \
  hunt.sh publish.sh settle.sh agy-worker.sh grok-worker.sh awr-side.sh
do
  deny_file "$p"
done
for d in roles calib lib .claude .githooks .github; do
  deny_tree "$d"
done

args=(
  --always-approve
  --no-subagents
  --max-turns "$max_turns"
  -m "$model"
  "${denies[@]}"
)
[ "$sandbox" = "off" ] || args+=(--sandbox "$sandbox")
[ "$disable_web" = "1" ] && args+=(--disable-web-search)

# -p 必须在最后一组:其后紧跟 prompt 字符串
exec "$bin" "${args[@]}" -p "$prompt"
