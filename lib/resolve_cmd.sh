# shellcheck shell=bash
# 命令串首词解析,awr-side.sh(SIDE_CMD)与 calib/run_panel.sh(PANEL_CMD)共用单源。
# 曾各持一份逐字拷贝,修 resolver 必须两边同步、必然漂移,抽到这里。被 source,不直接执行。
#
# 语义:相对路径钉成真仓库绝对路径(镜像 CWD 下没有 ./grok-worker.sh);含 .. 路径段一律拒
# (只挡开头 ../ 会被 ./a/../../x 嵌段穿出真仓);裸名须真仓根同名文件可执行才遮蔽 PATH
# (杂散的非可执行同名文件不拦 PATH 上的 claude/codex),否则验过 PATH 后原样交出;
# 空串原样返回(awr-side 以空表示内置 agy);解析出的绝对路径含空白即拒——
# 调起点按 IFS 拆词,含空格的仓库路径过了启动校验也必在调起时拆碎成 127。
#
# 用法: resolve_cmd <真仓绝对路径> <报错前缀(如 'awr-side: SIDE_CMD')> <命令字符串>
#   → stdout 解析后命令串;失败 return 2(调用方须 || exit,留到调起时才失败会绕过熔断)。
resolve_cmd() {
  local repo=$1 label=$2 cmd=$3 first rest cand
  [ -n "$cmd" ] || { echo ""; return 0; }
  # 按任意空白切首词(调起点也按 IFS 再切,tab/换行/多空格须一致对待)。
  # 不能用 read -r:here-string 只读到首个换行,含换行的命令串会静默丢掉其后全部参数。
  cmd=${cmd#"${cmd%%[![:space:]]*}"}
  first=${cmd%%[[:space:]]*}
  rest=${cmd#"$first"}
  case "/$first/" in
    */../*) echo "$label 禁止 .. 路径段: $first" >&2; return 2 ;;
  esac
  case "$first" in
    /*) cand=$first ;;
    ./*) cand="$repo/${first#./}" ;;
    *)
      if [ -f "$repo/$first" ] && [ -x "$repo/$first" ]; then
        cand="$repo/$first"
      else
        command -v "$first" >/dev/null 2>&1 || { echo "$label 首词既不在真仓库(可执行文件)也不在 PATH: $first" >&2; return 2; }
        echo "$cmd"; return 0
      fi
      ;;
  esac
  case "$cand" in *[[:space:]]*)
    echo "$label 解析出的绝对路径含空白(调起点按空白拆词必拆碎它;仓库不能放在含空格的路径下): $cand" >&2; return 2 ;;
  esac
  [ -f "$cand" ] && [ -x "$cand" ] || { echo "$label 首词不存在或不可执行: $first -> $cand" >&2; return 2; }
  echo "${cand}${rest}"
}
