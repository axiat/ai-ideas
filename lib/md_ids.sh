# shellcheck shell=bash
# Extract `## I<n>` identifiers from case ideas.md files for both calibration
# entry points. Headings inside CommonMark fences do not count. Fences accept
# backticks or tildes, up to three leading spaces, and a matching close at least
# as long as the opener. An info string containing a backtick is inline code,
# not a backtick fence. Explicit optional spaces avoid BSD awk brace intervals.
#
# Usage: md_idea_ids <ideas.md>
# Prints one identifier per line. An unclosed fence returns 3 so callers can
# fail instead of silently dropping later headings.
md_idea_ids() {
  local rc
  awk '
    fence { if ($0 ~ close_re) fence = 0; next }
    match($0, /^ ? ? ?(```+|~~~+)/) {
      seg = substr($0, RSTART, RLENGTH); sub(/^ +/, "", seg)
      c = substr(seg, 1, 1)
      if (c == "`" && substr($0, RSTART + RLENGTH) ~ /`/) { print; next }
      close_re = "^ ? ? ?" seg c "*[ \t]*$"
      fence = 1; next
    }
    { print }
    END { if (fence) exit 3 }
  ' "$1" | grep -oE '^## I[0-9]+' | awk '{print $2}'
  rc=${PIPESTATUS[0]}
  [ "$rc" -eq 0 ] || return 3
  return 0
}
