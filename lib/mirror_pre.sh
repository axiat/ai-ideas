# shellcheck shell=bash
# 镜像执行的隔离预提示,calib/run_panel.sh(run_judge)与 awr-side.sh(run_agent)共用单源。
# 曾各持一份同构文本且已实际漂移:awr 侧列了 ~/.gemini 等家目录禁写,面板侧没有;
# 后端新增禁写点必须两边手工同步,漏哪边哪边的隔离就弱一档,抽到这里。被 source,不直接执行。
#
# 用法: mirror_pre <镜像绝对路径> <允许写点描述> [额外禁写清单(顿号分隔)]
#   → stdout 预提示文本。$HOME/~ 按字面输出,由 agent 端解释。
mirror_pre() {
  local extra=""
  [ -n "${3:-}" ] && extra="严禁写 $3;"
  printf '仓库根(绝对路径)= %s。当前工作目录已在此根下,所有读写路径一律相对该根解析。真实仓库未作为工作目录提供;本次任务只允许写 %s,严禁写其它任何位置;%s严禁写 ~/.gemini、~/.claude、~/.codex、~/.grok、任何 scratch 目录或 $HOME 其它位置。' "$1" "$2" "$extra"
}
