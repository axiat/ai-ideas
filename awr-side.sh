#!/usr/bin/env bash
# AwR sidecar. It revises `accept-w-rev` ledger entries outside the main hunt
# loop through three independent roles: researcher, prior-work investigator,
# and reviewer. Novelty depends only on the independent prior-work artifact.
# A not-ready decision feeds concrete defects into the next revision round.
# Final artifacts live under `tmp/awr-side/awr/` and never alter verdicts,
# `ledger.tsv`, `ideas/`, or the main loop's `tmp/round/` state.
#
# Each key is file-derived and restartable:
#   <key>.md             final artifact
#   <key>.task.md        source task and feedback history
#   <key>.draft.md       current revision
#   <key>.priorwork.md   evidence for the current revision
#   <key>.judge.md       latest reviewer decision
# A draft newer than its task is ready for review. A task newer than its draft
# needs another revision. Prior work is reused only when newer than the draft.
# Invalid artifacts become `.badN`; MAX_BAD files blacklist the key. Three
# consecutive invocations without any artifact trigger a cooldown circuit.
#
# Usage:
#   caffeinate -is ./awr-side.sh
#
# Backend command strings are split on whitespace and receive the prompt as
# their final argument. Codex is the default. Provider overrides are explicit:
#   SIDE_CMD             all three roles
#   SIDE_RESEARCH_CMD    researcher only; falls back to SIDE_CMD
#   SIDE_JUDGE_CMD       reviewer only; falls back to SIDE_CMD
#   SIDE_PRIORWORK_CMD   prior work only; falls back to SIDE_JUDGE_CMD
#
# Examples:
#   SIDE_CMD=agy ./awr-side.sh
#   SIDE_CMD='./grok-worker.sh' ./awr-side.sh
#   SIDE_CMD='claude -p --strict-mcp-config' ./awr-side.sh
#
# `SIDE_CMD=agy` selects the mirror-local built-in adapter with AGY_MODEL and
# AGY_PRINT_TIMEOUT. Relative custom commands are resolved against the source
# repository before entering the mirror. Every backend receives the same
# artifact validation, random launch throttle, bad-artifact accounting, and
# circuit breaker. `AGY-DONE` remains the required final nonempty line.
#
# Tuning:
#   AGY_MODEL                         default: Gemini 3.5 Flash (High)
#   AGY_PRINT_TIMEOUT                 default: 10m
#   SIDE_GAP_SEC                      default: 120; built-in agy gate, 0 disables
#   SIDE_GAP_MIN_SEC/MAX_SEC          default: 60/600; all-backend throttle
#   SIDE_POLL_SEC                     default: 9000; 0 exits after a terminal scan
#   SIDE_MAX_BAD / SIDE_MAX_ROUNDS    default: 3 / 3
#   SIDE_COOLDOWN_SEC                 default: 3600; 0 exits on circuit break
set -u
repo="$(cd "$(dirname "$0")" && pwd)"
model=${AGY_MODEL:-Gemini 3.5 Flash (High)}
ptimeout=${AGY_PRINT_TIMEOUT:-10m}
SIDE_CMD=${SIDE_CMD:-codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write --skip-git-repo-check --ephemeral}
side_cmd=$SIDE_CMD
research_cmd=${SIDE_RESEARCH_CMD:-$side_cmd}
judge_cmd=${SIDE_JUDGE_CMD:-$side_cmd}
priorwork_cmd=${SIDE_PRIORWORK_CMD:-$judge_cmd}   # Prior work follows the reviewer's trust level.
gap=${SIDE_GAP_SEC:-120}
gap_min=${SIDE_GAP_MIN_SEC:-60}
gap_max=${SIDE_GAP_MAX_SEC:-600}
poll=${SIDE_POLL_SEC:-9000}
max_bad=${SIDE_MAX_BAD:-3}
max_rounds=${SIDE_MAX_ROUNDS:-3}
cooldown=${SIDE_COOLDOWN_SEC:-3600}
statedir="$repo/tmp/awr-side"
outdir="$statedir/awr"
sidelock="$repo/tmp/awr-side.lock"
gate_stamp="$repo/tmp/agy.last-launch"
gate_lock="$repo/tmp/agy.launch.lock"
for v in "$gap" "$gap_min" "$gap_max" "$poll" "$max_bad" "$max_rounds" "$cooldown"; do
  case "$v" in ''|*[!0-9]*) echo "awr-side: GAP/GAP_MIN/GAP_MAX/POLL/MAX_BAD/MAX_ROUNDS/COOLDOWN must be nonnegative integers: $v" >&2; exit 2 ;; esac
done
[ "$gap_max" -eq 0 ] || [ "$gap_min" -le "$gap_max" ] || { echo "awr-side: SIDE_GAP_MIN_SEC($gap_min) exceeds SIDE_GAP_MAX_SEC($gap_max)" >&2; exit 2; }

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$outdir/side.log"; }

# Preserve queue state created under the former `tmp/agy-side` name.
if [ -d "$repo/tmp/agy-side" ] && [ ! -d "$statedir" ]; then
  mv "$repo/tmp/agy-side" "$statedir"
fi
mkdir -p "$outdir"

# A single-instance lock prevents duplicate calls and conflicting `.badN` counts.
while ! mkdir "$sidelock" 2>/dev/null; do
  holder=$(cat "$sidelock/pid" 2>/dev/null || echo "")
  if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
    log "Removing stale instance lock; former PID $holder is absent"; rm -rf "$sidelock"; continue
  fi
  echo "awr-side: another instance is running (PID ${holder:-unknown}); remove $sidelock only after verifying no instance remains" >&2; exit 1
done
echo $$ > "$sidelock/pid"
trap 'rm -rf "$sidelock"; [ "$(cat "$gate_lock/pid" 2>/dev/null)" = "$$" ] && rm -rf "$gate_lock"' EXIT
rm -rf "$statedir"/run.* 2>/dev/null || true   # Mirrors left by an interrupted invocation.

# Built-in agy shares its launch stamp and lock with agy-worker.sh.
gate() {
  [ "$gap" -gt 0 ] || return 0
  local holder lock_m now last wait_s
  while ! mkdir "$gate_lock" 2>/dev/null; do
    holder=$(cat "$gate_lock/pid" 2>/dev/null || echo "")
    lock_m=$(stat -f %m "$gate_lock" 2>/dev/null || echo "")
    if { [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; } \
       || { [ -n "$lock_m" ] && [ $(( $(date +%s) - lock_m )) -gt $((gap + 60)) ]; }; then
      log "Removing stale agy launch lock (holder=${holder:-none})"; rm -rf "$gate_lock"; continue
    fi
    sleep 1
  done
  echo $$ > "$gate_lock/pid"
  now=$(date +%s); last=$(cat "$gate_stamp" 2>/dev/null || echo 0)
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  wait_s=$(( last + gap - now ))
  if [ "$wait_s" -gt 0 ]; then log "Waiting ${wait_s}s to preserve the ${gap}s agy launch gap"; sleep "$wait_s"; fi
  date +%s > "$gate_stamp"
  rm -rf "$gate_lock"
}

# Random throttling prevents back-to-back calls across all backends. The first
# call after startup or an idle scan is immediate. A zero maximum disables it.
throttle_first=1
throttle() {
  [ "$gap_max" -gt 0 ] || return 0
  if [ "$throttle_first" = 1 ]; then throttle_first=0; return 0; fi
  local span r
  span=$((gap_max - gap_min + 1))
  r=$((gap_min + RANDOM % span))
  log "Throttle: waiting ${r}s before the next backend call"
  sleep "$r"
}

# Count consecutive calls that produce no file, separately from bad content.
nofile=0

# Resolve command heads once at startup. Configuration errors must fail before
# the queue loop or they can bypass both bad-artifact and no-file accounting.
. "$repo/lib/resolve_cmd.sh"
. "$repo/lib/mirror_pre.sh"
# Attribute resolution errors to the variable that supplied the command.
research_label="awr-side: SIDE_CMD"; [ -n "${SIDE_RESEARCH_CMD:-}" ] && research_label="awr-side: SIDE_RESEARCH_CMD"
judge_label="awr-side: SIDE_CMD"; [ -n "${SIDE_JUDGE_CMD:-}" ] && judge_label="awr-side: SIDE_JUDGE_CMD"
# Prior-work attribution follows its PRIORWORK -> JUDGE -> CMD fallback chain.
priorwork_label="awr-side: SIDE_CMD"; [ -n "${SIDE_JUDGE_CMD:-}" ] && priorwork_label="awr-side: SIDE_JUDGE_CMD"; [ -n "${SIDE_PRIORWORK_CMD:-}" ] && priorwork_label="awr-side: SIDE_PRIORWORK_CMD"
research_cmd=$(resolve_cmd "$repo" "$research_label" "$research_cmd") || exit 2
judge_cmd=$(resolve_cmd "$repo" "$judge_label" "$judge_cmd") || exit 2
priorwork_cmd=$(resolve_cmd "$repo" "$priorwork_label" "$priorwork_cmd") || exit 2

# $1=command, $2=only writable artifact, $3=prompt, $4=raw backend log.
# Each call sees a disposable mirror. Only the validated target is copied back.
run_agent() {
  local cmd=$1 target=$2 prompt=$3 logf=$4 first sandbox rel target_in_sandbox prompt_in_sandbox pre rc
  throttle
  read -r first _ <<<"$cmd"
  case "$first" in ''|agy|*/agy) gate ;; esac
  sandbox=$(mktemp -d "$statedir/run.XXXXXX") || return 1
  rel=${target#"$repo"/}
  target_in_sandbox="$sandbox/$rel"
  mkdir -p "$sandbox/roles" "$sandbox/tmp/awr-side/awr" "$(dirname "$target_in_sandbox")"
  cp "$repo/roles/awr.md" "$sandbox/roles/awr.md"
  cp "$repo/roles/awr-priorwork.md" "$sandbox/roles/awr-priorwork.md"
  cp "$repo/roles/awr-judge.md" "$sandbox/roles/awr-judge.md"
  cp "$repo/rubric.md" "$sandbox/rubric.md"
  cp "$repo/brainstorming_policy.md" "$sandbox/brainstorming_policy.md"
  cp -R "$repo/.claude" "$sandbox/.claude" 2>/dev/null || true   # Explicit Claude opt-in uses the mirror allowlist.
  cp "$outdir"/*.md "$sandbox/tmp/awr-side/awr/" 2>/dev/null || true
  rm -f "$target" "$target_in_sandbox"
  prompt_in_sandbox=${prompt//$repo/$sandbox}
  pre=$(mirror_pre "$sandbox" "$target_in_sandbox" "tmp/round/, ideas/, or ledger.tsv")
  if [ "$cmd" = "agy" ]; then
    ( cd "$sandbox" && agy --model "$model" --add-dir "$sandbox" --print-timeout "$ptimeout" \
        -p "${pre}

${prompt_in_sandbox}" < /dev/null >> "$logf" 2>&1 )
  else
    # GROK_REPO keeps the explicit Grok adapter inside the mirror; other backends ignore it.
    ( cd "$sandbox" && GROK_REPO="$sandbox" $cmd "${pre}

${prompt_in_sandbox}" < /dev/null >> "$logf" 2>&1 )
  fi
  rc=$?
  if [ -e "$target_in_sandbox" ]; then cp "$target_in_sandbox" "$target"; fi
  rm -rf "$sandbox"
  if [ -e "$target" ]; then
    nofile=0
  else
    nofile=$((nofile + 1))
    if [ "$nofile" -ge 3 ]; then
      if [ "$cooldown" -gt 0 ]; then
        log "Circuit open: ${nofile} consecutive calls produced no artifact (rc=$rc); retrying after ${cooldown}s"
        sleep "$cooldown"; nofile=0
      else
        log "Circuit open: ${nofile} consecutive calls produced no artifact (rc=$rc); exiting"
        exit 3
      fi
    fi
  fi
  return "$rc"
}

# Reject incomplete drafts before replacing the last valid revision.
check_draft() {
  local f=$1 n
  [ -s "$f" ] || { echo "empty artifact"; return 1; }
  grep -qxE '## Revised Idea[[:space:]]*' "$f" || { echo "missing ## Revised Idea"; return 1; }
  n=$(grep -cE '^- .*https?://' "$f" || true)
  [ "${n:-0}" -ge 3 ] || { echo "insufficient linked search records (${n:-0}<3)"; return 1; }
  [ "$(grep -v '^[[:space:]]*$' "$f" | tail -1)" = "AGY-DONE" ] || { echo "missing final AGY-DONE sentinel"; return 1; }
}

# Enforce the independent prior-work structure without grading its substance.
check_priorwork() {
  local f=$1 n
  [ -s "$f" ] || { echo "empty artifact"; return 1; }
  n=$(grep -cE '^- .*https?://' "$f" || true)
  [ "${n:-0}" -ge 5 ] || { echo "insufficient linked neighbors (${n:-0}<5)"; return 1; }
  grep -qE '^- Query:[[:space:]]*https?://' "$f" || { echo "missing reproducible API query URL"; return 1; }
  grep -qE '^Strongest Counterexample:' "$f" || { echo "missing Strongest Counterexample"; return 1; }
  grep -qE '^Overlap:[[:space:]]*(high|medium|low)([[:space:]]|$)' "$f" || { echo "missing or invalid Overlap"; return 1; }
  grep -qE '^Papers Read:[[:space:]]*[0-9]+[[:space:]]*$' "$f" || { echo "missing or invalid Papers Read"; return 1; }
  grep -qE '^arXiv ID Check:' "$f" || { echo "missing arXiv ID Check"; return 1; }
  if grep -qxE '## Crack Evidence Verification[[:space:]]*' "$f"; then
    if ! awk '
      /Verification:/ {
        line = $0
        labels = gsub(/Verification:/, "&", line)
        if (labels != 1) exit 1
        sub(/^.*Verification:[[:space:]]*/, "", line)
        if (line !~ /^(supports|partial|contradicts|unreachable)([[:space:]]|$)/) exit 1
        outcomes++
      }
      END { if (outcomes < 1) exit 1 }
    ' "$f"; then
      echo "invalid crack-evidence verification outcome"
      return 1
    fi
  elif grep -qE 'Verification:' "$f"; then
    echo "verification outcome appears without ## Crack Evidence Verification"
    return 1
  fi
  [ "$(grep -v '^[[:space:]]*$' "$f" | tail -1)" = "AGY-DONE" ] || { echo "missing final AGY-DONE sentinel"; return 1; }
}

# A not-ready decision must include at least one repairable defect.
check_judge() {
  local f=$1 dec n
  [ -s "$f" ] || { echo "empty artifact"; return 1; }
  n=$(grep -cE '^Decision:' "$f" || true)
  [ "${n:-0}" -eq 1 ] || { echo "expected exactly one decision"; return 1; }
  dec=$(sed -nE 's/^Decision:[[:space:]]*//p' "$f")
  case "$dec" in
    SA-possible) ;;
    not-ready) grep -qE '^- Defect:' "$f" || { echo "not-ready decision has no defect"; return 1; } ;;
    *) echo "missing or invalid decision"; return 1 ;;
  esac
  [ "$(grep -v '^[[:space:]]*$' "$f" | tail -1)" = "AGY-DONE" ] || { echo "missing final AGY-DONE sentinel"; return 1; }
}

# Build the terminal artifact from the current draft and latest evidence.
finalize() {
  { printf '# AwR Result %s\nStatus: %s\nOutcome: %s\nOriginal Idea: %s\nProcess Record: %s.task.md\n\n' "$1" "$2" "$3" "$idea" "$1"
    cat "$draft"
    if [ -s "$pwork" ]; then printf '\n---\n## Independent Prior-Work Evidence\n'; cat "$pwork"; fi
    if [ -s "$judgef" ]; then printf '\n---\n## Final Reviewer Decision\n'; cat "$judgef"; fi
  } > "$out"
}

cd "$repo" || { echo "awr-side: cannot enter repository root $repo" >&2; exit 1; }
log "awr-side started: research=$research_cmd priorwork=$priorwork_cmd reviewer=$judge_cmd throttle=${gap_min}-${gap_max}s agy_gap=${gap}s poll=${poll}s max_bad=$max_bad max_rounds=$max_rounds cooldown=${cooldown}s"

while :; do
  # Prefer the shell-approved ledger snapshot, then freeze the input for this scan.
  src="$repo/tmp/ledger.good"; [ -s "$src" ] || src="$repo/ledger.tsv"
  snap="$outdir/.ledger.snap"; cp "$src" "$snap"
  did=0; pending=0
  while IFS=$'\t' read -r d source theme idea verdict reason _overlap <&3; do
    [ "$source" = "hunt" ] && [ "$verdict" = "accept-w-rev" ] || continue
    [ -n "$idea" ] || continue
    key=$(printf '%s' "$idea" | md5 | cut -c1-12)
    out="$outdir/$key.md"
    [ -s "$out" ] && continue                                  # Terminal artifact.
    nbad=0
    for badf in "$outdir/$key".*.bad*; do
      [ -e "$badf" ] || continue
      nbad=$((nbad + 1))
    done
    if [ "$nbad" -ge "$max_bad" ]; then continue; fi
    pending=1
    task="$outdir/$key.task.md"; draft="$outdir/$key.draft.md"
    judgef="$outdir/$key.judge.md"; new="$outdir/$key.new.md"; alog="$outdir/$key.agy.log"
    pwork="$outdir/$key.priorwork.md"; pworknew="$outdir/$key.priorwork.new.md"
    if [ ! -s "$task" ]; then
      { printf '# AwR Task %s\n' "$key"
        printf 'Date: %s\nTheme: %s\nIdea: %s\nReason: %s\n' "$d" "$theme" "$idea" "$reason"
      } > "$task"
    fi
    rounds=$(grep -c '^## Reviewer Feedback$' "$task" 2>/dev/null) || rounds=0
    # Research when no valid draft exists or feedback is newer than the draft.
    if ! { [ -s "$draft" ] && [ "$draft" -nt "$task" ]; }; then
      hint=""; [ -s "$draft" ] && hint=", with the existing draft at ${draft}; improve it in place"
      log "Starting [research:$key round $((rounds + 1))]: $theme"
      run_agent "$research_cmd" "$new" "Read ${repo}/roles/awr.md and follow it. The task is ${task}${hint}. Write the artifact to ${new}." "$alog"; rc=$?
      if why=$(check_draft "$new"); then
        mv -f "$new" "$draft"
      else
        mv -f "$new" "$outdir/$key.research.bad$((nbad + 1))" 2>/dev/null || true
        log "Rejected [research:$key] (backend rc=$rc): ${why}$([ $((nbad + 1)) -ge "$max_bad" ] && printf '; reached %s bad artifacts, blacklisted' "$max_bad")"
        continue
      fi
    fi
    # The last feedback has been incorporated; stop before another review.
    if [ "$rounds" -ge "$max_rounds" ]; then
      finalize "$key" "not-ready" "The revision remained below the acceptance gate after ${max_rounds} feedback rounds; the final reviewer decision targets the preceding draft."
      log "Finalized [awr:$key]: not-ready after ${max_rounds} feedback rounds"
      did=1; continue
    fi
    # Prior work must target this draft. Reuse is limited to crash or reviewer retries.
    if ! { [ -s "$pwork" ] && [ "$pwork" -nt "$draft" ]; }; then
      log "Starting [priorwork:$key round $((rounds + 1))]"
      run_agent "$priorwork_cmd" "$pworknew" "Read ${repo}/roles/awr-priorwork.md and follow it. The draft is ${draft}; use only its ## Revised Idea claim to form queries and do not trust its ## Search Record. The task context is ${task}. Write independent prior-work evidence to ${pworknew}." "$alog"; rc=$?
      if why=$(check_priorwork "$pworknew"); then
        mv -f "$pworknew" "$pwork"
      else
        mv -f "$pworknew" "$outdir/$key.priorwork.bad$((nbad + 1))" 2>/dev/null || true
        log "Rejected [priorwork:$key] (backend rc=$rc): ${why}$([ $((nbad + 1)) -ge "$max_bad" ] && printf '; reached %s bad artifacts, blacklisted' "$max_bad")"
        continue
      fi
    fi
    # Independent review.
    log "Starting [review:$key round $((rounds + 1))]"
    run_agent "$judge_cmd" "$judgef" "Read ${repo}/roles/awr-judge.md and follow it. Review ${draft} using independent prior-work evidence from ${pwork}; novelty depends only on that evidence, not the draft's search record. The task context is ${task}; the criteria are ${repo}/rubric.md and ${repo}/brainstorming_policy.md. Overwrite ${judgef} with the decision." "$alog"; rc=$?
    if ! why=$(check_judge "$judgef"); then
      mv -f "$judgef" "$outdir/$key.judge.bad$((nbad + 1))" 2>/dev/null || true
      log "Rejected [review:$key] (backend rc=$rc): ${why}$([ $((nbad + 1)) -ge "$max_bad" ] && printf '; reached %s bad artifacts, blacklisted' "$max_bad")"
      continue
    fi
    if grep -qxE 'Decision:[[:space:]]*SA-possible' "$judgef"; then
      finalize "$key" "ready" "The reviewer returned SA-possible in round $((rounds + 1))."
      log "Finalized [awr:$key]: ready in round $((rounds + 1))"
    else
      { printf '\n## Reviewer Feedback\nRound: %s\n' "$((rounds + 1))"; grep -E '^- Defect:' "$judgef"; } >> "$task"
      log "Feedback [awr:$key round $((rounds + 1))]: $(grep -cE '^- Defect:' "$judgef") defects queued"
    fi
    did=1
  done 3< "$snap"
  if [ "$pending" = 0 ]; then
    [ "$poll" -gt 0 ] || { log "Queue terminal; exiting one-pass mode"; exit 0; }
    log "Queue terminal; rescanning in ${poll}s"; sleep "$poll"; throttle_first=1
  elif [ "$did" = 0 ] && [ "$poll" -eq 0 ]; then
    log "Every remaining task failed this scan; exiting one-pass mode"; exit 1
  fi
done
