#!/usr/bin/env bash
set -eu

ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
TEMP_BASE=${TMPDIR:-/tmp}
TEMP_BASE=${TEMP_BASE%/}
CASE_ROOT=$(mktemp -d "$TEMP_BASE/grok-worker-smoke.XXXXXX")

cleanup() {
  case "$CASE_ROOT" in
    "$TEMP_BASE"/grok-worker-smoke.*) rm -rf -- "$CASE_ROOT" ;;
    *) printf 'Refusing to remove unexpected path: %s\n' "$CASE_ROOT" >&2 ;;
  esac
}
trap cleanup EXIT HUP INT TERM

FAKE_GROK="$CASE_ROOT/grok"
ARGS_LOG="$CASE_ROOT/args"
ENV_LOG="$CASE_ROOT/environment"
LAUNCH_LOG="$CASE_ROOT/launched"

# The single-quoted lines are the generated fake executable, not expressions
# evaluated by this smoke process.
# shellcheck disable=SC2016
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  ': "${FAKE_GROK_ARGS:?}" "${FAKE_GROK_ENV:?}" "${FAKE_GROK_LAUNCH:?}"' \
  ': > "$FAKE_GROK_LAUNCH"' \
  'printf "%s\n" "$@" > "$FAKE_GROK_ARGS"' \
  'printf "%s\n" "GROK_CLAUDE_SKILLS_ENABLED=$GROK_CLAUDE_SKILLS_ENABLED" > "$FAKE_GROK_ENV"' \
  'printf "%s\n" "GROK_CLAUDE_RULES_ENABLED=$GROK_CLAUDE_RULES_ENABLED" >> "$FAKE_GROK_ENV"' \
  'printf "%s\n" "GROK_CLAUDE_MCPS_ENABLED=$GROK_CLAUDE_MCPS_ENABLED" >> "$FAKE_GROK_ENV"' \
  'printf "%s\n" "GROK_CLAUDE_HOOKS_ENABLED=$GROK_CLAUDE_HOOKS_ENABLED" >> "$FAKE_GROK_ENV"' \
  'printf "%s\n" "GROK_CLAUDE_SESSIONS_ENABLED=$GROK_CLAUDE_SESSIONS_ENABLED" >> "$FAKE_GROK_ENV"' \
  > "$FAKE_GROK"
chmod 755 "$FAKE_GROK"

assert_pair() {
  flag=$1
  value=$2
  file=$3
  awk -v flag="$flag" -v value="$value" '
    $0 == flag {
      count += 1
      if ((getline actual) <= 0 || actual != value) bad = 1
    }
    END { exit ! (count == 1 && bad != 1) }
  ' "$file"
}

assert_singleton() {
  value=$1
  file=$2
  [ "$(grep -cxF -- "$value" "$file")" -eq 1 ]
}

assert_final_prompt() {
  value=$1
  file=$2
  awk -v value="$value" '
    { before_last=last; last=$0 }
    END { exit ! (before_last == "-p" && last == value) }
  ' "$file"
}

assert_absent() {
  value=$1
  file=$2
  if grep -qxF -- "$value" "$file"; then
    printf 'unexpected argument: %s\n' "$value" >&2
    exit 1
  fi
}

assert_compatibility_sources_disabled() {
  expected="$CASE_ROOT/environment.expected"
  printf '%s\n' \
    'GROK_CLAUDE_SKILLS_ENABLED=false' \
    'GROK_CLAUDE_RULES_ENABLED=false' \
    'GROK_CLAUDE_MCPS_ENABLED=false' \
    'GROK_CLAUDE_HOOKS_ENABLED=false' \
    'GROK_CLAUDE_SESSIONS_ENABLED=false' \
    > "$expected"
  cmp -s "$expected" "$ENV_LOG"
}

run_wrapper() {
  GROK_CLAUDE_SKILLS_ENABLED=true \
  GROK_CLAUDE_RULES_ENABLED=true \
  GROK_CLAUDE_MCPS_ENABLED=true \
  GROK_CLAUDE_HOOKS_ENABLED=true \
  GROK_CLAUDE_SESSIONS_ENABLED=true \
  GROK_REPO="$ROOT" \
  GROK_BIN="$FAKE_GROK" \
  FAKE_GROK_ARGS="$ARGS_LOG" \
  FAKE_GROK_ENV="$ENV_LOG" \
  FAKE_GROK_LAUNCH="$LAUNCH_LOG" \
  bash "$ROOT/grok-worker.sh" "$@"
}

(
  unset GROK_MODEL GROK_REASONING_EFFORT
  run_wrapper prompt-default
)
[ -e "$LAUNCH_LOG" ]
assert_absent -m "$ARGS_LOG"
assert_absent --reasoning-effort "$ARGS_LOG"
assert_singleton --always-approve "$ARGS_LOG"
assert_pair --sandbox workspace "$ARGS_LOG"
assert_pair -p prompt-default "$ARGS_LOG"
assert_final_prompt prompt-default "$ARGS_LOG"
assert_compatibility_sources_disabled
printf 'ok: Grok wrapper preserves omitted model and reasoning defaults\n'

rm -f -- "$LAUNCH_LOG"
GROK_MODEL=grok-4.5 GROK_REASONING_EFFORT=high run_wrapper prompt-explicit
[ -e "$LAUNCH_LOG" ]
assert_pair -m grok-4.5 "$ARGS_LOG"
assert_pair --reasoning-effort high "$ARGS_LOG"
assert_singleton --always-approve "$ARGS_LOG"
assert_pair --sandbox workspace "$ARGS_LOG"
assert_pair -p prompt-explicit "$ARGS_LOG"
assert_final_prompt prompt-explicit "$ARGS_LOG"
assert_compatibility_sources_disabled
printf 'ok: Grok wrapper passes explicit model and reasoning\n'

rm -f -- "$LAUNCH_LOG" "$ARGS_LOG" "$ENV_LOG"
if GROK_REASONING_EFFORT=medium run_wrapper prompt-invalid \
  > "$CASE_ROOT/invalid.stdout" 2> "$CASE_ROOT/invalid.stderr"
then
  printf 'grok-worker accepted unsupported GROK_REASONING_EFFORT\n' >&2
  exit 1
fi
[ ! -e "$LAUNCH_LOG" ]
grep -q 'GROK_REASONING_EFFORT accepts only high' "$CASE_ROOT/invalid.stderr"
printf 'ok: Grok wrapper rejects unsupported reasoning before launch\n'
