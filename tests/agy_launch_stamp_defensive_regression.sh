#!/usr/bin/env bash
set -eu

ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
TEMP_BASE=${TMPDIR:-/tmp}
TEMP_BASE=${TEMP_BASE%/}
CASE_ROOT=$(mktemp -d "$TEMP_BASE/agy-launch-stamp-regression.XXXXXX")

cleanup() {
  case "$CASE_ROOT" in
    "$TEMP_BASE"/agy-launch-stamp-regression.*) rm -rf -- "$CASE_ROOT" ;;
    *) printf 'Refusing to remove unexpected path: %s\n' "$CASE_ROOT" >&2 ;;
  esac
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$CASE_ROOT/bin" "$CASE_ROOT/repo" "$CASE_ROOT/launch/tmp"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "launch\n" >> "$FAKE_AGY_LOG"' \
  > "$CASE_ROOT/bin/agy"
chmod 755 "$CASE_ROOT/bin/agy"

STAMP="$CASE_ROOT/launch/tmp/agy.last-launch"
FAKE_AGY_LOG="$CASE_ROOT/agy.log"
export FAKE_AGY_LOG

run_worker_bounded() {
  label=$1
  env \
    PATH="$CASE_ROOT/bin:$PATH" \
    AGY_REPO="$CASE_ROOT/repo" \
    AGY_LAUNCH_ROOT="$CASE_ROOT/launch" \
    AGY_LAUNCH_GAP_SEC=1 \
    "$ROOT/agy-worker.sh" "$label" \
    > "$CASE_ROOT/$label.stdout" 2> "$CASE_ROOT/$label.stderr" &
  worker_pid=$!
  attempts=0
  while kill -0 "$worker_pid" 2>/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 50 ]; then
      kill "$worker_pid" 2>/dev/null || true
      wait "$worker_pid" 2>/dev/null || true
      printf 'agy-worker blocked while reading %s stamp\n' "$label" >&2
      return 1
    fi
    sleep 0.1
  done
  wait "$worker_pid"
}

mkfifo "$STAMP"
run_worker_bounded fifo
[ -f "$STAMP" ] && [ ! -p "$STAMP" ]
python3 - "$STAMP" <<'PY'
import os
import stat
import sys

state = os.stat(sys.argv[1], follow_symlinks=False)
assert stat.S_ISREG(state.st_mode)
assert state.st_nlink == 1
assert state.st_uid == os.geteuid()
PY
printf 'ok: FIFO launch stamp is read without blocking and replaced\n'

rm -f -- "$STAMP"
printf 'do-not-truncate\n' > "$CASE_ROOT/hardlink-target"
ln "$CASE_ROOT/hardlink-target" "$STAMP"
run_worker_bounded hardlink
printf 'do-not-truncate\n' > "$CASE_ROOT/expected"
cmp -s "$CASE_ROOT/expected" "$CASE_ROOT/hardlink-target"
[ ! "$STAMP" -ef "$CASE_ROOT/hardlink-target" ]
python3 - "$STAMP" <<'PY'
import os
import stat
import sys

state = os.stat(sys.argv[1], follow_symlinks=False)
assert stat.S_ISREG(state.st_mode)
assert state.st_nlink == 1
assert state.st_uid == os.geteuid()
PY
[ "$(wc -l < "$FAKE_AGY_LOG" | tr -d ' ')" -eq 2 ]
printf 'ok: hard-linked launch stamp target is preserved and replaced\n'
