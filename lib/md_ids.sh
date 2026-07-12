# shellcheck shell=bash
# 从 case ideas.md 提取 `## I<n>` id 清单,calib/run_panel.sh 与 calib/run_e2e.sh 共用单源。
# 围栏代码块内不算:idea 正文若引用 `## I<n>` 样例,幻影 id 会让票据/块校验向产物索要不存在的行;
# 反向错翻吞真标题同样致命。按 CommonMark 认栏:```/~~~ 都算、容 ≤3 前导空格、关栏须同字符且
# 长度 ≥ 开栏且除尾随空白无它;反引号开栏行 info 串含反引号是行内代码不是栏。不写 {0,3} 区间——
# BSD awk 对 brace 区间支持不稳,用 " ? ? ?"。被 source,不直接执行。
#
# 用法: md_idea_ids <ideas.md 路径>
#   → stdout 每行一个 id(I<n>);未闭合围栏(其后真标题会被静默吞)return 3,调用方须响亮报错。
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
