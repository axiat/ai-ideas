#!/usr/bin/env bash
# Explicit Grok adapter for hunt.sh, awr-side.sh, and calibration panels.
#
# CLI contract: one positional prompt enters this wrapper; every option is set
# through GROK_* variables. The wrapper supplies `-p` last and explicitly uses
# unattended approval, a workspace sandbox, and no subagents.
#
# Security boundary: the deny rules cover direct file-tool writes and shell
# writes whose targets Grok can identify statically. Indirect writes can bypass
# those rules. The workspace sandbox does not block processes or network access,
# and user-level hooks, plugins, and MCP configuration remain inherited. Ledger
# integrity therefore rests on hunt.sh's ledger.good snapshot and loop guard.
# Calibration and AwR add filesystem/CWD isolation by running in mirrors; they do
# not isolate processes or network. Strong isolation requires an outer OS sandbox.
# GROK_REPO must name the mirror for AwR or this wrapper would resolve its own real
# repository. File-system sandbox deny entries cannot include readable role input.
#
# Usage:
#   AGENT_CMD='./grok-worker.sh' ./hunt.sh
#   FRONT_CMD='./agy-worker.sh' BACK_CMD='./grok-worker.sh' ./hunt.sh
#   FRONT_CMD='./grok-worker.sh' BACK_CMD='claude -p --strict-mcp-config' ./hunt.sh
#   PANEL_CMD='./grok-worker.sh' ./calib/run_panel.sh calib/cases/pos-meanflow
#   SIDE_CMD='./grok-worker.sh' ./awr-side.sh
# Configuration:
#   GROK_REPO          Absolute work root; defaults to this script's directory.
#   GROK_MODEL         Default grok-4.5.
#   GROK_MAX_TURNS     Positive integer; default 80.
#   GROK_SANDBOX       `workspace` (default) or explicit `off`; unknown profiles
#                      fail because Grok otherwise warns and continues unsandboxed.
#   GROK_DISABLE_WEB   Boolean; disables built-in search, not shell networking.
#   GROK_BIN           Executable resolved through PATH; default grok.
set -u
self_dir="$(cd "$(dirname "$0")" && pwd)"
repo=${GROK_REPO:-$self_dir}
# Multiple arguments indicate misplaced CLI flags and must fail closed.
[ "$#" -eq 1 ] || { echo "grok-worker: expected one prompt argument, received $#; configure options through GROK_* variables" >&2; exit 2; }
prompt=$1
model=${GROK_MODEL:-grok-4.5}
max_turns=${GROK_MAX_TURNS:-80}
sandbox=${GROK_SANDBOX:-workspace}
bin=${GROK_BIN:-grok}
disable_web=${GROK_DISABLE_WEB:-0}

case "$max_turns" in ''|*[!0-9]*) echo "grok-worker: GROK_MAX_TURNS must be a positive integer: $max_turns" >&2; exit 2 ;; esac
[ "$max_turns" -ge 1 ] || { echo "grok-worker: GROK_MAX_TURNS must be at least 1: $max_turns" >&2; exit 2; }
# Search disablement is a safety switch and rejects unknown values.
case "$disable_web" in
  ''|0|false|no|off) disable_web=0 ;;
  1|true|yes|on)     disable_web=1 ;;
  *) echo "grok-worker: GROK_DISABLE_WEB accepts only 0/1/true/false/yes/no/on/off: $disable_web" >&2; exit 2 ;;
esac
case "$repo" in
  /*) ;;
  *) echo "grok-worker: GROK_REPO must be an absolute path: $repo" >&2; exit 2 ;;
esac
[ -d "$repo" ] || { echo "grok-worker: work root does not exist: $repo" >&2; exit 2; }
# Unknown sandbox profiles would run without isolation, so only built-in
# workspace and explicit off are accepted.
case "$sandbox" in
  workspace|off) ;;
  *) echo "grok-worker: GROK_SANDBOX accepts only workspace/off: $sandbox" >&2; exit 2 ;;
esac

cd "$repo" || { echo "grok-worker: cannot enter work root $repo" >&2; exit 1; }
command -v "$bin" >/dev/null 2>&1 || { echo "grok-worker: Grok executable not found: $bin" >&2; exit 2; }

export GROK_DISABLE_AUTOUPDATER=1

# Best-effort file-tool write denials cover the ledger, fixed inputs, entry
# scripts, calibration tree, and publication/orchestration files. Relative,
# absolute, and recursive patterns are all needed because tool paths vary.
denies=()
deny_write_edit() {
  # $1 is passed unchanged to Write and Edit deny rules.
  local g=$1
  denies+=(--deny "Write($g)" --deny "Edit($g)")
}
deny_file() {
  # $1 is a file path relative to the work root.
  local p=$1 base
  base=$(basename "$p")
  deny_write_edit "$p"
  deny_write_edit "$repo/$p"
  deny_write_edit "**/$base"
}
deny_tree() {
  # $1 is a directory relative to the work root, without a trailing slash.
  local d=$1
  deny_write_edit "$d/**"
  deny_write_edit "**/$d/**"
  deny_write_edit "$repo/$d/**"
}

deny_file 'ledger.tsv'
deny_file 'tmp/ledger.good'   # Trusted ledger baseline sits outside the later loop guard.
deny_tree 'tmp/runs'          # Bash-owned immutable per-run audit archive.
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

# -p must be the final option immediately before its prompt value.
exec "$bin" "${args[@]}" -p "$prompt"
