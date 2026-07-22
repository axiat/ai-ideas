#!/usr/bin/env bash
# Explicit agy adapter for the disposable generation and prior-work stages in hunt.sh.
# Scoring, reporting, and publication use their separately configured backends.
#
# The adapter pins the repository root, supplies agy's native print timeout, and
# serializes launches to avoid repeated-login challenges. A misplaced artifact
# remains a missing tmp/round output and is rejected by hunt.sh.
#
# Usage:
#   FRONT_CMD='./agy-worker.sh' BACK_CMD='claude -p' ./hunt.sh
# Configuration:
#   AGY_MODEL          Full model ID printed by `agy models`; default
#                      `gemini-3.6-flash-high`. Verify the selected-model line
#                      in the CLI log.
#   AGY_PRINT_TIMEOUT  Default 8m.
#   AGY_LAUNCH_GAP_SEC Minimum seconds between launches; default 60, 0 disables.
set -u
repo="$(cd "$(dirname "$0")" && pwd)"
model=${AGY_MODEL:-gemini-3.6-flash-high}
ptimeout=${AGY_PRINT_TIMEOUT:-8m}
gap=${AGY_LAUNCH_GAP_SEC:-60}
prompt=${1:?usage: agy-worker.sh <prompt>}
case "$gap" in ''|*[!0-9]*) echo "agy-worker: AGY_LAUNCH_GAP_SEC must be a nonnegative integer: $gap" >&2; exit 2 ;; esac

# The mkdir lock covers stamp read, wait, and stamp write so concurrent judges
# cannot all pass on the same old timestamp. A dead holder or a lock older than
# gap+60 seconds is stale. A lock without a pid is cleared only by age.
stamp="$repo/tmp/agy.last-launch"
lockd="$repo/tmp/agy.launch.lock"
if [ "$gap" -gt 0 ]; then
  mkdir -p "$repo/tmp"
  while ! mkdir "$lockd" 2>/dev/null; do
    holder=$(cat "$lockd/pid" 2>/dev/null || echo "")
    lock_m=$(stat -f %m "$lockd" 2>/dev/null || echo "")
    if { [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; } \
       || { [ -n "$lock_m" ] && [ $(( $(date +%s) - lock_m )) -gt $((gap + 60)) ]; }; then
      echo "agy-worker: removing stale launch lock (holder=${holder:-none})" >&2
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
    echo "agy-worker: launch gap is ${gap}s; waiting ${wait_s}s" >&2
    sleep "$wait_s"
  fi
  date +%s > "$stamp"
  rm -rf "$lockd"
  trap - EXIT
fi

# AGY_OUT_HINT changes the allowed output location while retaining the launch
# gate. Its default preserves the hunt stage's tmp/round location.
out=${AGY_OUT_HINT:-tmp/round/}
pre="Repository root (absolute path): ${repo}. The current working directory is under this root. Resolve every read and write path (${out}…, roles/…, rubric.md, brainstorming_policy.md, research_context.md, ledger.tsv) relative to this root. Write artifacts only under ${repo}/${out}. Never write to ~/.gemini, any scratch directory, per-run audit archives (~/.ai-ideas-runs/ or any *ai-ideas-runs* path), or any other location under \$HOME."

cd "$repo" || { echo "agy-worker: cannot enter repository root $repo" >&2; exit 1; }
exec agy --model "$model" --add-dir "$repo" --print-timeout "$ptimeout" -p "${pre}

${prompt}"
