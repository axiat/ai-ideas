#!/usr/bin/env bash
# Explicit Claude adapter for hunt.sh, awr-side.sh, and calibration panels.
#
# CLI contract: one positional prompt enters this wrapper; every option is set
# through CLAUDE_* variables. The wrapper supplies `-p` last and uses bare
# non-interactive print with unattended permissions.
#
# Security boundary: deny rules cover direct file-tool writes whose targets are
# statically identifiable. Indirect writes can bypass those rules. Ledger
# integrity therefore rests on hunt.sh's ledger.good snapshot and loop guard.
# Calibration and AwR add filesystem/CWD isolation by running in mirrors; they
# do not isolate processes or network.
#
# Usage:
#   AGENT_CMD='./claude-worker.sh' ./hunt.sh
#   CLAUDE_MODEL=sonnet CLAUDE_REASONING_EFFORT=high \
#     AGENT_CMD='./claude-worker.sh' ./hunt.sh
#   FRONT_CMD='./agy-worker.sh' BACK_CMD='./claude-worker.sh' ./hunt.sh
#   PANEL_CMD='./claude-worker.sh' ./calib/run_panel.sh calib/cases/pos-meanflow
#   SIDE_CMD='./claude-worker.sh' ./awr-side.sh
# Configuration:
#   CLAUDE_REPO          Absolute work root; defaults to this script's directory.
#   CLAUDE_MODEL         Optional model passed with `--model`; omission preserves
#                        the Claude CLI's current default.
#   CLAUDE_REASONING_EFFORT
#                        Optional effort; accepted explicit values are
#                        low|medium|high|xhigh|max. Omission preserves the CLI
#                        default.
#   CLAUDE_BIN           Executable resolved through PATH; default claude.
set -u
self_dir="$(cd "$(dirname "$0")" && pwd)"
repo=${CLAUDE_REPO:-$self_dir}
# Multiple arguments indicate misplaced CLI flags and must fail closed.
[ "$#" -eq 1 ] || { echo "claude-worker: expected one prompt argument, received $#; configure options through CLAUDE_* variables" >&2; exit 2; }
prompt=$1
model=${CLAUDE_MODEL-}
reasoning=${CLAUDE_REASONING_EFFORT-}
bin=${CLAUDE_BIN:-claude}

case "$reasoning" in
  ''|low|medium|high|xhigh|max) ;;
  *) echo "claude-worker: CLAUDE_REASONING_EFFORT accepts only low|medium|high|xhigh|max: $reasoning" >&2; exit 2 ;;
esac
case "$repo" in
  /*) ;;
  *) echo "claude-worker: CLAUDE_REPO must be an absolute path: $repo" >&2; exit 2 ;;
esac
[ -d "$repo" ] || { echo "claude-worker: work root does not exist: $repo" >&2; exit 2; }

cd "$repo" || { echo "claude-worker: cannot enter work root $repo" >&2; exit 1; }
command -v "$bin" >/dev/null 2>&1 || { echo "claude-worker: Claude executable not found: $bin" >&2; exit 2; }

# Best-effort file-tool write denials cover the ledger, fixed inputs, entry
# scripts, calibration tree, and publication/orchestration files.
denies=()
deny_write_edit() {
  local g=$1
  denies+=(--disallowedTools "Write($g)" --disallowedTools "Edit($g)")
}
deny_file() {
  local p=$1 base
  base=$(basename "$p")
  deny_write_edit "$p"
  deny_write_edit "$repo/$p"
  deny_write_edit "**/$base"
}
deny_tree() {
  local d=$1
  deny_write_edit "$d/**"
  deny_write_edit "**/$d/**"
  deny_write_edit "$repo/$d/**"
}

deny_file 'ledger.tsv'
deny_file 'tmp/ledger.good'
deny_tree 'tmp/runs'
for p in \
  PROGRAM.md rubric.md brainstorming_policy.md research_context.md \
  hunt.sh publish.sh settle.sh agy-worker.sh grok-worker.sh claude-worker.sh awr-side.sh
do
  deny_file "$p"
done
for d in roles calib lib .claude .githooks .github; do
  deny_tree "$d"
done

args=(
  --bare
  --dangerously-skip-permissions
  --add-dir "$repo"
)
[ -z "$model" ] || args+=(--model "$model")
[ -z "$reasoning" ] || args+=(--effort "$reasoning")
args+=("${denies[@]}")

# -p must be the final option immediately before its prompt value.
exec "$bin" "${args[@]}" -p "$prompt"
