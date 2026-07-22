# shellcheck shell=bash
# Shared first-token resolver for awr-side.sh and calibration entry points.
# This file is sourced, not executed.
#
# Relative executable paths are pinned to the real repository because mirrors
# do not contain adapters such as ./grok-worker.sh. Any `..` path segment is
# rejected. A bare name is shadowed only by an executable repository file;
# otherwise it must resolve through PATH. Empty commands pass through. Resolved
# executable paths containing whitespace are rejected because call sites split
# command strings with IFS.
#
# Usage: resolve_cmd <absolute-real-repo> <error-label> <command-string>
# Prints the resolved command or returns 2. Callers must stop on failure.
resolve_cmd() {
  local repo=$1 label=$2 cmd=$3 first rest cand
  [ -n "$cmd" ] || { echo ""; return 0; }
  # Split on any whitespace, matching call-site IFS behavior. A here-string
  # read would silently discard everything after the first newline.
  cmd=${cmd#"${cmd%%[![:space:]]*}"}
  first=${cmd%%[[:space:]]*}
  rest=${cmd#"$first"}
  case "/$first/" in
    */../*) echo "$label rejects .. path segments: $first" >&2; return 2 ;;
  esac
  case "$first" in
    /*) cand=$first ;;
    ./*) cand="$repo/${first#./}" ;;
    *)
      if [ -f "$repo/$first" ] && [ -x "$repo/$first" ]; then
        cand="$repo/$first"
      else
        command -v "$first" >/dev/null 2>&1 || { echo "$label executable is neither in the repository nor PATH: $first" >&2; return 2; }
        echo "$cmd"; return 0
      fi
      ;;
  esac
  case "$cand" in *[[:space:]]*)
    echo "$label resolved an executable path containing whitespace: $cand" >&2; return 2 ;;
  esac
  [ -f "$cand" ] && [ -x "$cand" ] || { echo "$label executable does not exist or is not executable: $first -> $cand" >&2; return 2; }
  echo "${cand}${rest}"
}
