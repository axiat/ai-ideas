#!/usr/bin/env bash
# Explicit agy adapter for the disposable generation and prior-work stages in hunt.sh.
# Scoring, reporting, and publication use their separately configured backends.
#
# The adapter pins the repository root, supplies agy's native print timeout, and
# serializes launches to avoid repeated-login challenges. A misplaced artifact
# remains a missing tmp/round output and is rejected by hunt.sh.
#
# Usage:
#   FRONT_CMD='./agy-worker.sh' BACK_CMD='./claude-worker.sh' ./hunt.sh
# Configuration:
#   AGY_REPO           Absolute repository/mirror root; defaults to this script's
#                      directory. hunt.sh sets it to the disposable mirror.
#   AGY_MODEL          Full model ID printed by `agy models`; default
#                      `gemini-3.6-flash-high`. Verify the selected-model line
#                      in the CLI log.
#   AGY_PRINT_TIMEOUT  Default 8m.
#   AGY_LAUNCH_ROOT    Shared launch-gate root; defaults to this adapter's
#                      repository so per-seat AGY_REPO mirrors share one gap.
#   AGY_LAUNCH_GAP_SEC Minimum seconds between launches; default 0, disabled.
set -u
self_dir="$(cd "$(dirname "$0")" && pwd)"
repo=${AGY_REPO:-$self_dir}
case "$repo" in
  /*) ;;
  *) echo "agy-worker: AGY_REPO must be absolute: $repo" >&2; exit 2 ;;
esac
[ -d "$repo" ] || { echo "agy-worker: repository root is not a directory: $repo" >&2; exit 2; }
repo="$(cd "$repo" && pwd)"
launch_root=${AGY_LAUNCH_ROOT:-$self_dir}
case "$launch_root" in
  /*) ;;
  *) echo "agy-worker: AGY_LAUNCH_ROOT must be absolute: $launch_root" >&2; exit 2 ;;
esac
[ -d "$launch_root" ] || { echo "agy-worker: launch root is not a directory: $launch_root" >&2; exit 2; }
launch_root="$(cd "$launch_root" && pwd)"
model=${AGY_MODEL:-gemini-3.6-flash-high}
ptimeout=${AGY_PRINT_TIMEOUT:-8m}
gap=${AGY_LAUNCH_GAP_SEC:-0}
prompt=${1:?usage: agy-worker.sh <prompt>}
case "$gap" in ''|*[!0-9]*) echo "agy-worker: AGY_LAUNCH_GAP_SEC must be a nonnegative integer: $gap" >&2; exit 2 ;; esac

# Read no more than 65 bytes from a nonblocking, no-follow descriptor. Invalid,
# foreign-owned, and multiply linked stamps are ignored. Writes use a fresh
# single-link file and rename it into place so an existing inode is never
# truncated through an attacker-controlled link.
launch_stamp() {
  python3 - "$1" "$stamp" "${2:-}" <<'PY'
import os
import stat
import sys
import tempfile

operation, path, value = sys.argv[1:]

if operation == "read":
    if not hasattr(os, "O_NOFOLLOW"):
        print(0)
        raise SystemExit
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        print(0)
        raise SystemExit
    try:
        state = os.fstat(descriptor)
        if (
            not stat.S_ISREG(state.st_mode)
            or state.st_nlink != 1
            or state.st_uid != os.geteuid()
        ):
            print(0)
            raise SystemExit
        payload = os.read(descriptor, 65)
    finally:
        os.close(descriptor)
    if len(payload) > 64:
        print(0)
        raise SystemExit
    payload = payload.rstrip(b"\n")
    if not payload or not payload.isdigit():
        print(0)
        raise SystemExit
    print(int(payload))
    raise SystemExit

if operation != "write" or not value.isdigit():
    raise SystemExit(2)
directory = os.path.dirname(path)
descriptor, temporary = tempfile.mkstemp(prefix=".agy.last-launch.", dir=directory)
try:
    state = os.fstat(descriptor)
    if (
        not stat.S_ISREG(state.st_mode)
        or state.st_nlink != 1
        or state.st_uid != os.geteuid()
    ):
        raise OSError("unsafe launch-stamp temporary")
    os.fchmod(descriptor, 0o600)
    payload = (value + "\n").encode("ascii")
    while payload:
        written = os.write(descriptor, payload)
        if written <= 0:
            raise OSError("short launch-stamp write")
        payload = payload[written:]
    os.fsync(descriptor)
    os.close(descriptor)
    descriptor = -1
    os.replace(temporary, path)
except BaseException:
    if descriptor >= 0:
        os.close(descriptor)
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
}

# The mkdir lock covers stamp read, wait, and stamp write so concurrent judges
# cannot all pass on the same old timestamp. A dead holder or a lock older than
# gap+60 seconds is stale. A lock without a pid is cleared only by age.
stamp="$launch_root/tmp/agy.last-launch"
lockd="$launch_root/tmp/agy.launch.lock"
if [ "$gap" -gt 0 ]; then
  mkdir -p "$launch_root/tmp"
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
  now=$(date +%s); last=$(launch_stamp read)
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  wait_s=$(( last + gap - now ))
  if [ "$wait_s" -gt 0 ]; then
    echo "agy-worker: launch gap is ${gap}s; waiting ${wait_s}s" >&2
    sleep "$wait_s"
  fi
  now=$(date +%s)
  if ! launch_stamp write "$now"; then
    echo "agy-worker: cannot atomically update launch stamp: $stamp" >&2
    exit 1
  fi
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
