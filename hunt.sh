#!/usr/bin/env bash
# Runs the complete idea-hunt pipeline: generate, rank, prescreen, research,
# independent review, deterministic aggregation, ledger append, report, publish.
# A Strong Accept requires unanimous reviewer votes and every mechanical evidence
# gate. Bash owns verdict aggregation, ledger mutation, publication, and archives.
# Agents run in separate processes; review seats receive isolated input copies.
# Before every round, ledger.tsv is restored from tmp/ledger.good. Stage guards
# reject out-of-scope writes, and only the report stage may write ideas/.
#
# Every round has a stable run_id and archives tmp/round, a manifest, and its
# ledger delta under RUNS_DIR/<run_id>. An archive failure blocks publication for
# any round with a Strong Accept. Stage logs live in tmp/round/logs/, stage timing
# in tmp/round/stages.tsv, and machine-readable metrics in tmp/hunt.metrics.tsv.
#
# Usage:
#   ./hunt.sh [failure retry delay in minutes; default: 150]
#
# Primary controls:
#   NO_HIT_SLEEP_MIN_LO/HI  Retry range after a complete round without a report.
#   ALLOW_ZERO_NO_HIT_SLEEP Allow zero-delay retries in tests only.
#   REVIEWERS               Independent review seats; default: 3.
#   MIN_READ                Minimum papers read for the SA gate; default: 5.
#   SA_TARGET               Daily hunt SA target; default: 1, 0 means unbounded.
#   AGENT_CMD               Backend command; the prompt is the final argument.
#   FRONT_CMD/BACK_CMD      Optional overrides for front and decision stages.
#   REV_CMD_1..REV_CMD_N    Optional per-seat review commands.
#   REV_STAGGER_SEC         Review-seat launch stagger; default: 0.
#   EMPTY_MAX               Empty front-stage retries before long cooldown; 3.
#   SHORT_MAX               Maximum candidates sent to deep research; 3.
#   PRIOR_MIN_LINKS         Neighbor-link minimum per candidate; 5.
#   PRIOR_MIN_API           Reproducible API-query minimum per candidate; 1.
#   RESEARCH_RETRY          Targeted research retries before discarding a round; 1.
#   AXIOM_MIN_CRACKS        Crack-evidence and verification minimum; 2.
#   THEME_MIN_LOW           Candidates required from the least-used themes; 2.
#   META_EVERY              Failure-pattern digest interval in rounds; 6.
#   META_MIN_REJECTS        Failed rows required before a digest; 5.
#   RESUME_FRONT            Reuse mechanically valid interrupted front artifacts; 1.
#
# Backend examples:
#   AGENT_CMD='codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write' ./hunt.sh
#   AGENT_CMD='./grok-worker.sh' ./hunt.sh
#   FRONT_CMD='./agy-worker.sh' BACK_CMD='./grok-worker.sh' ./hunt.sh
#   AGENT_CMD='claude -p --strict-mcp-config' ./hunt.sh  # explicit opt-in only
#
# Prescreen kills only a single-work direct hit and records it as reject/high.
# Structural prescreen failures fail open to keep. Shortlist priority is lineage,
# assumption removal, selector rank, low theme inventory, then generation order.
# Excess keeps are neither researched nor recorded and may return in a later round.
# Empty front artifacts retry quickly; backend failures count toward MAX_FAILS.
# RESUME_FRONT never reuses review ballots or aggregated verdicts.
set -u
cd "$(dirname "$0")" || exit 2
git config core.hooksPath .githooks   # Activate the guard against direct main pushes.

AGENT_CMD=${AGENT_CMD:-codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write}
# Front and decision stages default to the trusted backend above.
FRONT_CMD=${FRONT_CMD:-$AGENT_CMD}
BACK_CMD=${BACK_CMD:-$AGENT_CMD}
FAIL_SLEEP_MIN=${FAIL_SLEEP_MIN:-${1:-150}}
NO_HIT_SLEEP_MIN_LO=${NO_HIT_SLEEP_MIN_LO:-1}
NO_HIT_SLEEP_MIN_HI=${NO_HIT_SLEEP_MIN_HI:-8}
ALLOW_ZERO_NO_HIT_SLEEP=${ALLOW_ZERO_NO_HIT_SLEEP:-0}
MAX_FAILS=${MAX_FAILS:-12}
REVIEWERS=${REVIEWERS:-3}
MIN_READ=${MIN_READ:-5}
SA_TARGET=${SA_TARGET:-1}
REV_STAGGER_SEC=${REV_STAGGER_SEC:-0}
EMPTY_MAX=${EMPTY_MAX:-3}
PRIOR_MIN_LINKS=${PRIOR_MIN_LINKS:-5}
PRIOR_MIN_API=${PRIOR_MIN_API:-1}
RESEARCH_RETRY=${RESEARCH_RETRY:-1}   # Targeted retries for structurally incomplete research.
NEAR_SA_MAX=${NEAR_SA_MAX:-30}        # Bounded revision queue, pruned before generation.
SHORT_MAX=${SHORT_MAX:-3}
THEME_MIN_LOW=${THEME_MIN_LOW:-2}
AXIOM_MIN_CRACKS=${AXIOM_MIN_CRACKS:-2}
META_EVERY=${META_EVERY:-6}
META_MIN_REJECTS=${META_MIN_REJECTS:-5}
RESUME_FRONT=${RESUME_FRONT:-1}
LOG=hunt.log
RD=tmp/round
LEDGER_GOOD=tmp/ledger.good
DEATHLIST=tmp/deathlist.md
NONSA_CLASS=tmp/nonsa-class.tsv                                  # Non-SA classification observations.
NEAR_SA_QUEUE=tmp/near-sa-queue.tsv                              # Bounded near-SA revision queue.
LOCK=tmp/hunt.lock
METRICS=tmp/hunt.metrics.tsv
# Sentinel for a Strong Accept recorded without a complete decision archive.
# It lives inside the repository so an unavailable RUNS_DIR cannot hide the halt.
HALT_MARK=tmp/HALTED-ARCHIVE-FAIL
# Per-run archives default outside the workspace so workspace-sandboxed trusted
# backends cannot modify them. A same-user untrusted backend can still reach HOME;
# isolate that backend by uid or container when the archive is a hard boundary.
# Overriding RUNS_DIR to a repository path reduces the boundary to best effort.
RUNS_DIR=${RUNS_DIR:-$HOME/.ai-ideas-runs/$(basename "$PWD")}

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

is_uint() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }

validate_sleep_config() {
  is_uint "$FAIL_SLEEP_MIN" || { log "FAIL_SLEEP_MIN must be a nonnegative integer number of minutes: $FAIL_SLEEP_MIN"; exit 2; }
  is_uint "$NO_HIT_SLEEP_MIN_LO" || { log "NO_HIT_SLEEP_MIN_LO must be a nonnegative integer number of minutes: $NO_HIT_SLEEP_MIN_LO"; exit 2; }
  is_uint "$NO_HIT_SLEEP_MIN_HI" || { log "NO_HIT_SLEEP_MIN_HI must be a nonnegative integer number of minutes: $NO_HIT_SLEEP_MIN_HI"; exit 2; }
  case "$ALLOW_ZERO_NO_HIT_SLEEP" in
    0|1) ;;
    *) log "ALLOW_ZERO_NO_HIT_SLEEP must be 0 or 1: $ALLOW_ZERO_NO_HIT_SLEEP"; exit 2 ;;
  esac
  if [ "$ALLOW_ZERO_NO_HIT_SLEEP" != "1" ]; then
    if [ "$NO_HIT_SLEEP_MIN_LO" -lt 1 ] || [ "$NO_HIT_SLEEP_MIN_HI" -lt 1 ]; then
      log "NO_HIT_SLEEP_MIN_LO/HI must be at least 1 unless tests set ALLOW_ZERO_NO_HIT_SLEEP=1"
      exit 2
    fi
  fi
  if [ "$NO_HIT_SLEEP_MIN_LO" -gt "$NO_HIT_SLEEP_MIN_HI" ]; then
    log "NO_HIT_SLEEP_MIN_LO cannot exceed NO_HIT_SLEEP_MIN_HI: ${NO_HIT_SLEEP_MIN_LO}-${NO_HIT_SLEEP_MIN_HI}"
    exit 2
  fi
  is_uint "$REV_STAGGER_SEC" || { log "REV_STAGGER_SEC must be a nonnegative integer number of seconds: $REV_STAGGER_SEC"; exit 2; }
  is_uint "$EMPTY_MAX" && [ "$EMPTY_MAX" -ge 1 ] || { log "EMPTY_MAX must be an integer of at least 1: $EMPTY_MAX"; exit 2; }
  is_uint "$PRIOR_MIN_LINKS" || { log "PRIOR_MIN_LINKS must be a nonnegative integer: $PRIOR_MIN_LINKS"; exit 2; }
  is_uint "$PRIOR_MIN_API" || { log "PRIOR_MIN_API must be a nonnegative integer: $PRIOR_MIN_API"; exit 2; }
  is_uint "$RESEARCH_RETRY" || { log "RESEARCH_RETRY must be a nonnegative integer: $RESEARCH_RETRY"; exit 2; }
  is_uint "$NEAR_SA_MAX" && [ "$NEAR_SA_MAX" -ge 1 ] || { log "NEAR_SA_MAX must be an integer of at least 1: $NEAR_SA_MAX"; exit 2; }
  is_uint "$SHORT_MAX" && [ "$SHORT_MAX" -ge 1 ] || { log "SHORT_MAX must be an integer of at least 1: $SHORT_MAX"; exit 2; }
  is_uint "$THEME_MIN_LOW" || { log "THEME_MIN_LOW must be a nonnegative integer: $THEME_MIN_LOW"; exit 2; }
  is_uint "$AXIOM_MIN_CRACKS" && [ "$AXIOM_MIN_CRACKS" -ge 1 ] || { log "AXIOM_MIN_CRACKS must be an integer of at least 1: $AXIOM_MIN_CRACKS"; exit 2; }
  is_uint "$META_EVERY" && [ "$META_EVERY" -ge 1 ] || { log "META_EVERY must be an integer of at least 1: $META_EVERY"; exit 2; }
  is_uint "$META_MIN_REJECTS" || { log "META_MIN_REJECTS must be a nonnegative integer: $META_MIN_REJECTS"; exit 2; }
  is_uint "$SA_TARGET" || { log "SA_TARGET must be a nonnegative integer; 0 means unbounded: $SA_TARGET"; exit 2; }
  case "$RESUME_FRONT" in
    0|1) ;;
    *) log "RESUME_FRONT must be 0 or 1: $RESUME_FRONT"; exit 2 ;;
  esac
}

# Randomly select one policy lens plus three implicit blank cards.
pick_lens() {
  local n total
  total=$(awk '/^## Divergence Lenses/{f=1;next} /^## /{f=0} f&&/^- /' brainstorming_policy.md | grep -c . || true)
  [ "$total" -gt 0 ] || { echo ""; return 0; }
  n=$((RANDOM % (total + 3) + 1))
  [ "$n" -le "$total" ] || { echo ""; return 0; }
  awk '/^## Divergence Lenses/{f=1;next} /^## /{f=0} f&&/^- /' brainstorming_policy.md | sed -n "${n}p" | sed 's/^- //'
}

sleep_minutes() {
  local minutes=$1
  log "Retrying in ${minutes} minutes"
  sleep "$((minutes * 60))"
}

random_no_hit_sleep_min() {
  echo $((NO_HIT_SLEEP_MIN_LO + RANDOM % (NO_HIT_SLEEP_MIN_HI - NO_HIT_SLEEP_MIN_LO + 1)))
}

# Run one serial stage and record its log, timestamps, and return code.
run_stage() {
  local cmd=$1 rc t0 t1
  m_stage=$3
  log "Starting [$3]: $cmd"
  mkdir -p "$RD/logs"
  t0=$(date '+%F %T')
  $cmd "$2" 2>&1 | tee -a "$RD/logs/$3.log" >> "$LOG"
  rc=${PIPESTATUS[0]}
  t1=$(date '+%F %T')
  printf '%s\t%s\t%s\t%s\n' "$3" "$t0" "$t1" "$rc" >> "$RD/stages.tsv"
  return "$rc"
}

# Reject new tracked changes outside ledger.tsv and, when allowed, ideas/.
guard() {
  local allow_ideas=${1:-0} pat changed bad committed_bad rolled_all p bad_after
  if [ "$allow_ideas" = "1" ]; then pat='^(ideas/|ledger\.tsv$)'; else pat='^(ledger\.tsv$)'; fi
  changed=$({ git diff --name-only "$before" HEAD; git status --porcelain | cut -c4-; } | sort -u)
  bad=$(comm -23 <(printf '%s\n' "$changed") <(printf '%s\n' "$pre_dirty") \
        | grep -vE "$pat" | grep -v '^$' || true)
  [ -z "$bad" ] && return 0
  log "Guard found out-of-scope changes: $(echo "$bad" | tr '\n' ' ')"
  committed_bad=$(git diff --name-only "$before" HEAD | grep -xF -f <(printf '%s\n' "$bad") || true)
  if [ -n "$committed_bad" ]; then
    log "Out-of-scope changes were committed; inspect git log ${before:0:7}..HEAD"
    exit 2
  fi
  rolled_all=1
  while read -r p; do
    [ -z "$p" ] && continue
    if git cat-file -e "$before:$p" 2>/dev/null; then
      if git restore --source="$before" --staged --worktree -- "$p" 2>/dev/null; then
        log "Restored: $p"
      elif git reset -q "$before" -- "$p" 2>/dev/null && git checkout "$before" -- "$p" 2>/dev/null; then
        log "Restored: $p"
      else
        log "Could not restore out-of-scope file: $p"
        rolled_all=0
      fi
    else
      git reset -q HEAD -- "$p" 2>/dev/null || true
      log "Untracked out-of-scope file remains: $p"
      rolled_all=0
    fi
  done <<< "$bad"
  changed=$({ git diff --name-only "$before" HEAD; git status --porcelain | cut -c4-; } | sort -u)
  bad_after=$(comm -23 <(printf '%s\n' "$changed") <(printf '%s\n' "$pre_dirty") \
        | grep -vE "$pat" | grep -v '^$' || true)
  if [ "$rolled_all" -eq 0 ] || [ -n "$bad_after" ]; then
    log "Out-of-scope files remain; resolve before restarting: $(echo "$bad_after" | tr '\n' ' ')"
    exit 2
  fi
  return 0
}

# Count a backend failure, archive it, and stop at MAX_FAILS.
fail_and_wait() {
  metrics_write fail "$m_stage" '-'
  archive_round "fail:${m_stage}"
  fails=$((fails + 1))
  log "Stage failed (${fails}/${MAX_FAILS}); inspect the backend output above"
  if [ "$fails" -ge "$MAX_FAILS" ]; then
    log "Maximum consecutive failures reached; check AGENT_CMD, quota, and permissions"
    exit 1
  fi
  sleep_minutes "$FAIL_SLEEP_MIN"
}

# Retry empty front-stage output quickly, then use the failure cooldown.
empty_and_wait() {
  local m
  metrics_write empty "$m_stage" '-'
  archive_round "empty:${m_stage}"
  empties=$((empties + 1))
  if [ "$empties" -ge "$EMPTY_MAX" ]; then
    log "Front stage returned empty output ${empties} times; switching to the failure cooldown"
    empties=0
    sleep_minutes "$FAIL_SLEEP_MIN"
  else
    m=$(random_no_hit_sleep_min)
    log "Front stage returned empty output (${empties}/${EMPTY_MAX}); retrying in ${m} minutes"
    sleep "$((m * 60))"
  fi
}

# Return a nonempty-line count, or '-' when the file does not exist.
count_lines() { if [ -f "$1" ]; then grep -c . "$1" || true; else echo '-'; fi; }

# Append one round-level metric row. Missing artifacts use '-'. Verdict ranks are
# 2=SA, 1=AwR, 0=reject, and -=missing; a 2->reject transition marks gate failure.
metrics_write() {   # $1=outcome $2=stage $3=verdict vector
  local n_gen n_kill n_keep n_short pw_links pw_api
  [ -s "$METRICS" ] || printf 'ts\tround\toutcome\tstage\tlens\tgen\tkill\tkeep\tshort\tpw_links\tpw_api\tverdicts\trun_id\n' > "$METRICS"
  n_gen=$(count_lines "$RD/ideas.all.tsv"); [ "$n_gen" = '-' ] && n_gen=$(count_lines "$RD/ideas.tsv")
  n_kill=$(count_lines "$RD/kills.tsv"); n_keep=$(count_lines "$RD/keeps.tsv"); n_short=$(count_lines "$RD/ideas.tsv")
  if [ -f "$RD/priorwork.md" ]; then
    pw_links=$(grep -cE 'https?://' "$RD/priorwork.md" || true)
    pw_api=$(grep -cE '^- Query:[[:space:]]*https?://(export\.arxiv\.org/api/query|api\.semanticscholar\.org)' "$RD/priorwork.md" || true)
  else pw_links='-'; pw_api='-'; fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$(date '+%F %T')" "$round" "$1" "$2" "$m_lens" "$n_gen" "$n_kill" "$n_keep" "$n_short" \
    "$pw_links" "$pw_api" "$3" "${run_id:--}" >> "$METRICS"
}

# Archive the round inputs, ballots, logs, manifest, and ledger delta. The first
# archive freezes decision inputs; later calls refresh only manifest and logs.
archive_round() {   # $1=exit reason
  local dst rev_cmds r v frozen rc=0
  [ -n "${run_id:-}" ] && [ "$run_id" != '-' ] || return 0
  dst="$RUNS_DIR/$run_id"
  # Preserve the original review snapshot across later publication updates.
  if [ -d "$dst/round" ]; then
    frozen=1
  else
    frozen=0; rm -rf "$dst"
    mkdir -p "$dst/round" || { log "Archive failed for ${run_id}: cannot create $dst/round"; return 1; }
  fi
  # An empty ledger delta is valid for failed or empty rounds.
  if ! tail -n +"$((ledger_base_lines + 1))" "$LEDGER_GOOD" > "$dst/ledger.delta.tsv" 2>/dev/null; then
    log "Archive failed for ${run_id}: cannot write ledger delta"; rc=1
  fi
  rev_cmds=""
  for r in $(seq 1 "$REVIEWERS"); do
    v="REV_CMD_$r"; rev_cmds="${rev_cmds:+$rev_cmds | }${!v:-$BACK_CMD}"
  done
  if ! {
    printf 'run_id\t%s\n' "$run_id"
    printf 'date\t%s\n' "$today"
    printf 'source\thunt\n'
    printf 'round\t%s\n' "$round"
    printf 'lens\t%s\n' "$m_lens"
    printf 'exit_reason\t%s\n' "$1"
    printf 'sa_count\t%s\n' "${sa_count:--}"
    printf 'reviewers\t%s\n' "$REVIEWERS"
    printf 'front_cmd\t%s\n' "$FRONT_CMD"
    printf 'back_cmd\t%s\n' "$BACK_CMD"
    printf 'rev_cmds\t%s\n' "$rev_cmds"
    printf 'git_head\t%s\n' "${before:--}"
    printf 'policy_sha\t%s\n' "$(cat brainstorming_policy.md rubric.md roles/*.md 2>/dev/null | shasum -a 256 | cut -c1-12)"
    printf 'verdicts\t%s\n' "${m_verdicts:--}"
    printf 'archived_at\t%s\n' "$(date '+%F %T')"
  } > "$dst/manifest.tsv" 2>/dev/null; then
    log "Archive failed for ${run_id}: cannot write manifest"; rc=1
  fi
  if [ "$frozen" = 1 ]; then
    # Refresh observation artifacts without replacing frozen review inputs.
    cp "$RD/stages.tsv" "$dst/round/stages.tsv" 2>/dev/null || rc=1
    if [ -d "$RD/logs" ]; then
      mkdir -p "$dst/round/logs" 2>/dev/null
      cp -R "$RD/logs/." "$dst/round/logs/" 2>/dev/null || { log "Archive warning for ${run_id}: could not refresh stage logs"; rc=1; }
    fi
  elif ! cp -R "$RD/." "$dst/round/" 2>/dev/null; then
    log "Archive failed for ${run_id}: could not copy round artifacts"; rc=1
  fi
  return "$rc"
}

rank_of() { case "$1" in strong-accept) echo 2 ;; accept-w-rev) echo 1 ;; *) echo 0 ;; esac; }
verdict_of() { case "$1" in 2) echo strong-accept ;; 1) echo accept-w-rev ;; *) echo reject ;; esac; }
# Ballots must use the exact verdict vocabulary before rank conversion.
vote_valid() { case "$1" in strong-accept|accept-w-rev|reject) return 0 ;; *) return 1 ;; esac; }
# Two or more reviewer-reported MAJOR findings cap the effective rank at AwR.
major_cap() {   # $1=rank $2=MAJOR field -> effective rank
  local rank=$1 mj
  mj=$(printf '%s' "$2" | grep -oE '[0-9]+' | head -1)
  if [ -n "$mj" ] && [ "$mj" -ge 2 ] && [ "$rank" -gt 1 ]; then echo 1; else echo "$rank"; fi
}
# Classify non-SA rows from pre-gate rank, gate downgrade, and overlap.
# evidence-incomplete is the only reject class eligible for evidence recheck.
classify_nonsa() {
  local raw_min=$1 downgraded=$2 overlap=$3
  [ "$downgraded" -eq 1 ] && { echo evidence-incomplete; return; }
  [ "$overlap" = high ] && { echo novelty-dead; return; }
  { [ "$raw_min" -eq 1 ] && [ "$overlap" = low ]; } && { echo design-fixable; return; }
  [ "$raw_min" -eq 1 ] && { echo ceiling-limited; return; }
  echo novelty-dead
}

# Remove terminal stories already recorded twice, then bound the revision queue.
prune_near_sa_queue() {
  [ -s "$NEAR_SA_QUEUE" ] || return 0
  local line story cnt
  : > "$NEAR_SA_QUEUE.tmp"
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    story=$(printf '%s' "$line" | cut -f3)
    cnt=$(cut -f4 "$LEDGER_GOOD" 2>/dev/null | grep -Fxc -- "$story")
    [ "${cnt:-0}" -lt 2 ] && printf '%s\n' "$line" >> "$NEAR_SA_QUEUE.tmp"
  done < "$NEAR_SA_QUEUE"
  tail -n "$NEAR_SA_MAX" "$NEAR_SA_QUEUE.tmp" > "$NEAR_SA_QUEUE" 2>/dev/null || : > "$NEAR_SA_QUEUE"
  rm -f "$NEAR_SA_QUEUE.tmp"
}

# Count today's hunt Strong Accept rows from the orchestrator baseline only.
sa_today() {
  awk -F'\t' -v d="$today" '$1==d && $2=="hunt" && $5=="strong-accept"{n++} END{print n+0}' "$LEDGER_GOOD" 2>/dev/null || echo 0
}

# Count today's reports so report creation is verified by a strict increment.
reports_today() {
  local f n=0
  for f in "ideas/${today}"_hunt*.md; do
    [ -e "$f" ] || continue                        # An unmatched glob remains literal.
    n=$((n + 1))
  done
  echo "$n"
}

# Strong Accept gate: sufficient papers, a substantive falsification experiment,
# a complete review block from every seat, and verified crack evidence when used.
sa_gate_ok() {
  local id=$1 block iblock n r fal
  [ -s "$RD/priorwork.md" ] || return 1
  block=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"&&$2~/^I[0-9]+$/{if(f)exit} f' "$RD/priorwork.md")
  [ -n "$block" ] || return 1
  n=$(printf '%s\n' "$block" | grep '^Papers Read:' | grep -oE '[0-9]+' | head -1)
  [ -n "$n" ] && [ "$n" -ge "$MIN_READ" ] || return 1
  iblock=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"&&$2~/^I[0-9]+$/{if(f)exit} f' "$RD/ideas.md" 2>/dev/null)
  # Require at least 30 bytes after the label; reviewers assess substance.
  fal=$(printf '%s\n' "$iblock" | grep '^Minimal Falsification Experiment:' | head -1 | sed -E 's/^Minimal Falsification Experiment:[[:space:]]*//')
  [ "$(printf '%s' "$fal" | wc -c | tr -d ' ')" -ge 30 ] || return 1
  for r in $(seq 1 "$REVIEWERS"); do
    grep -qE "^##[[:space:]]+${id}([[:space:]]|$)" "$RD/rev/$r/review.md" 2>/dev/null || return 1
  done
  # Assumption removal requires enough independently supported crack evidence.
  if is_axiom_idea "$id" "$RD/ideas.md"; then
    n=$(printf '%s\n' "$block" | grep -cE 'Verification:[[:space:]]*supports([[:space:]]|$)' || true)
    [ "$n" -ge "$AXIOM_MIN_CRACKS" ] || return 1
  fi
  return 0
}

# Front-stage research gate: every candidate needs enough linked neighbors and
# reproducible API queries before review begins.
priorwork_ok() {
  local id rest block links api
  while IFS=$'\t' read -r id rest; do
    [ -z "$id" ] && continue
    block=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"&&$2~/^I[0-9]+$/{if(f)exit} f' "$RD/priorwork.md")
    if [ -z "$block" ]; then log "Research gate: priorwork.md is missing the ${id} block"; return 1; fi
    # Count only the linked bullets inside Nearest Work.
    links=$(printf '%s\n' "$block" | awk '/^Nearest Work:/{f=1;next} /^Strongest Counterexample:/{f=0} f' \
            | grep -cE '^- .*https?://' || true)
    if [ "$links" -lt "$PRIOR_MIN_LINKS" ]; then
      log "Research gate: ${id} has too few linked neighbors (${links} < ${PRIOR_MIN_LINKS})"; return 1
    fi
    # Structured API queries keep retrieval reproducible and auditable.
    if [ "$PRIOR_MIN_API" -gt 0 ]; then
      api=$(printf '%s\n' "$block" | grep -cE '^- Query:[[:space:]]*https?://(export\.arxiv\.org/api/query|api\.semanticscholar\.org)' || true)
      if [ "$api" -lt "$PRIOR_MIN_API" ]; then
        log "Research gate: ${id} has too few structured API queries (${api} < ${PRIOR_MIN_API})"; return 1
      fi
    fi
  done < "$RD/ideas.tsv"
  return 0
}

# Generation theme gate: labels must come from the policy vocabulary, and enough
# candidates must cover the three least-used inventory levels, including ties.
themes_ok() {
  local tsv vfile id rest theme low_hits
  tsv=${1:-$RD/ideas.tsv}
  vfile="$RD/themes.vocab"
  awk '/^## Theme Vocabulary/{f=1;next} /^## /{f=0} f&&NF' brainstorming_policy.md | head -1 \
    | tr '/' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$' > "$vfile" || true
  if [ ! -s "$vfile" ]; then
    log "Theme gate: no vocabulary could be parsed from policy; skipping this gate"; return 0
  fi
  while IFS=$'\t' read -r id rest theme; do
    [ -z "$id" ] && continue
    if ! grep -qxF "$theme" "$vfile"; then
      log "Theme gate: ${id} uses a theme outside the vocabulary: '${theme}'"; return 1
    fi
  done < "$tsv"
  [ "$THEME_MIN_LOW" -gt 0 ] || return 0
  low_hits=$(awk -F'\t' -v led=ledger.tsv '
    NR==FNR { cnt[$0]=0; next }
    FILENAME==led { if ($3 in cnt) cnt[$3]++; next }
    $1!="" { th[FNR]=$3 }
    END {
      n=0; for (t in cnt) v[++n]=cnt[t]
      for (i=1;i<=n;i++) for (j=i+1;j<=n;j++) if (v[j]<v[i]) { x=v[i]; v[i]=v[j]; v[j]=x }
      thresh = (n>=3 ? v[3] : v[n])
      hits=0
      for (k in th) if (th[k] in cnt && cnt[th[k]]<=thresh) hits++
      print hits
    }' "$vfile" ledger.tsv "$tsv")
  if [ "$low_hits" -lt "$THEME_MIN_LOW" ]; then
    log "Theme gate: low-inventory coverage is insufficient (${low_hits} < ${THEME_MIN_LOW})"; return 1
  fi
  return 0
}

# Return an exact prescreen decision; malformed or missing decisions fail open.
prescreen_dec() {
  awk -v id="$1" '$1=="##"&&$2==id{f=1;next} $1=="##"&&$2~/^I[0-9]+$/{if(f)exit} f' "$RD/prescreen.md" 2>/dev/null \
    | grep -m1 '^Decision:' | grep -xE 'Decision: (kill|keep)[[:space:]]*' \
    | grep -oE 'kill|keep'
}

# A kill requires one structured API query and one anchored Occupant URL.
kill_evidence() {   # $1=id -> direct-hit URL; nonzero means insufficient evidence
  local block url
  block=$(awk -v id="$1" '$1=="##"&&$2==id{f=1;next} $1=="##"&&$2~/^I[0-9]+$/{if(f)exit} f' "$RD/prescreen.md" 2>/dev/null)
  printf '%s\n' "$block" | grep -qE '^- Query:[[:space:]]*https?://(export\.arxiv\.org/api/query|api\.semanticscholar\.org)' || return 1
  url=$(printf '%s\n' "$block" | grep -m1 '^Occupant:' | grep -oE 'https?://[^ )|,;>]+' \
        | grep -vE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org' | head -1)
  [ -n "$url" ] || return 1
  printf '%s\n' "$url"
}

# Mechanical structure gate for the remove-load-bearing-assumption form.
is_axiom_idea() {   # $1=id $2=ideas.md path
  awk -v id="$1" '$1=="##"&&$2==id{f=1;next} $1=="##"&&$2~/^I[0-9]+$/{if(f)exit} f' "$2" 2>/dev/null \
    | grep -q '^Form:[[:space:]]*remove-load-bearing-assumption[[:space:]]*$'
}
axiom_ok() {        # $1=ideas.md $2=ideas.tsv $3=require attempt marker
  local md=$1 tsv=$2 need_marker=${3:-1} marker marker_count m_id id rest block field val urls
  if [ "$need_marker" = "1" ]; then
    marker_count=$(awk '/^## I[0-9]+/{exit} /^Assumption-Removal Attempt:/{n++} END{print n+0}' "$md")
    if [ "$marker_count" -ne 1 ]; then log "Assumption-removal gate: expected exactly one preamble attempt marker"; return 1; fi
    marker=$(awk '/^## I[0-9]+/{exit} /^Assumption-Removal Attempt:/{print; exit}' "$md")
    if printf '%s' "$marker" | grep -q '^Assumption-Removal Attempt: incomplete — '; then
      if ! printf '%s' "$marker" | grep -qE '^Assumption-Removal Attempt: incomplete — .+; blocked by: .+$'; then
        log "Assumption-removal gate: incomplete marker must name the candidate and blocked field"; return 1
      fi
      val=${marker#*incomplete — }
      if [ "$(printf '%s' "$val" | wc -c | tr -d ' ')" -lt 30 ]; then
        log "Assumption-removal gate: an incomplete attempt must name the candidate and blocked field"; return 1
      fi
    else
      m_id=$(printf '%s' "$marker" | grep -E '^Assumption-Removal Attempt: complete I[0-9]+$' | grep -oE 'I[0-9]+' | head -1)
      if [ -z "$m_id" ] || ! is_axiom_idea "$m_id" "$md"; then
        log "Assumption-removal gate: complete marker does not name a valid block (${m_id:-missing id})"; return 1
      fi
    fi
  fi
  while IFS=$'\t' read -r id rest; do
    [ -z "$id" ] && continue
    is_axiom_idea "$id" "$md" || continue
    block=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"&&$2~/^I[0-9]+$/{if(f)exit} f' "$md")
    for field in 'Assumption to Remove' 'Why It Can Be Removed Now' 'Forcing Constraint'; do
      val=$(printf '%s\n' "$block" | grep -m1 "^${field}:" | sed -E "s/^${field}:[[:space:]]*//")
      if [ "$(printf '%s' "$val" | wc -c | tr -d ' ')" -lt 12 ]; then
        log "Assumption-removal gate: ${id} field '${field}' is missing or empty"; return 1
      fi
    done
    urls=$(printf '%s\n' "$block" | grep -cE '^Crack Evidence:.*https?://' || true)
    if [ "$urls" -lt "$AXIOM_MIN_CRACKS" ]; then
      log "Assumption-removal gate: ${id} has too few linked crack-evidence rows (${urls} < ${AXIOM_MIN_CRACKS})"; return 1
    fi
  done < "$tsv"
  return 0
}
# Research must independently classify every required crack-evidence item.
cracks_ok() {
  local id rest block n
  while IFS=$'\t' read -r id rest; do
    [ -z "$id" ] && continue
    is_axiom_idea "$id" "$RD/ideas.md" || continue
    block=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"&&$2~/^I[0-9]+$/{if(f)exit} f' "$RD/priorwork.md")
    if ! printf '%s\n' "$block" | grep -q '^## Crack Evidence Verification$'; then
      log "Research gate: ${id} is missing Crack Evidence Verification"; return 1
    fi
    n=$(printf '%s\n' "$block" | grep -cE '^- https?://.* \| Verification: (supports|partial|contradicts|unreachable) — .+' || true)
    if [ "$n" -lt "$AXIOM_MIN_CRACKS" ]; then
      log "Research gate: ${id} has too few crack-verification rows (${n} < ${AXIOM_MIN_CRACKS})"; return 1
    fi
  done < "$RD/ideas.tsv"
  return 0
}

# Keep priority: lineage first, assumption removal second, ordinary candidates third.
keep_rank() {   # $1=id in ideas.all.md
  if awk -v id="$1" '$1=="##"&&$2==id{f=1;next} $1=="##"&&$2~/^I[0-9]+$/{if(f)exit} f' "$RD/ideas.all.md" \
       | grep -qE '^[-* ]*(Recheck|Evolved from):'; then echo 0
  elif is_axiom_idea "$1" "$RD/ideas.all.md"; then echo 1
  else echo 2; fi
}

# Missing or invalid selector ranks fall back to 999 without discarding the round.
select_rank_of() {   # $1=id
  local r
  [ -s "$RD/select.tsv" ] || { echo 999; return; }
  r=$(awk -F'\t' -v id="$1" '$1==id{print $2; exit}' "$RD/select.tsv" 2>/dev/null)
  case "$r" in ''|*[!0-9]*) echo 999 ;; *) echo "$r" ;; esac
}

# Build the shortlist by keep priority, selector rank, theme inventory, and order.
select_shortlist() {
  local rank srank tcount oidx id story theme
  kept=0
  [ -s "$RD/keeps.tsv" ] || return 0
  sort -t$'\t' -k1,1n -k2,2n -k3,3n -k4,4n -o "$RD/keeps.tsv" "$RD/keeps.tsv"
  while IFS=$'\t' read -r rank srank tcount oidx id story theme; do
    if [ "$kept" -lt "$SHORT_MAX" ]; then
      printf '%s\t%s\t%s\n' "$id" "$story" "$theme" >> "$RD/ideas.tsv"
      awk -v id="$id" '$1=="##"{f=($2==id)} f' "$RD/ideas.all.md" >> "$RD/ideas.md"
      printf '\n' >> "$RD/ideas.md"
      kept=$((kept + 1))
    else
      log "Prescreen: ${id} exceeds SHORT_MAX=${SHORT_MAX} (keep=${rank}, select=${srank}, theme inventory=${tcount}); leaving it unrecorded"
    fi
  done < "$RD/keeps.tsv"
}

mkdir -p "$(dirname "$LEDGER_GOOD")"                             # Seed tmp/ in a clean checkout.
# Add run_id to a legacy metrics header while preserving old 12-column rows.
if [ -s "$METRICS" ] && ! head -1 "$METRICS" | grep -q 'run_id'; then
  awk 'NR==1{print $0 "\trun_id"; next} {print}' "$METRICS" > "$METRICS.mig" && mv "$METRICS.mig" "$METRICS"
fi
validate_sleep_config
if [ "$SA_TARGET" -gt 0 ]; then TARGET_DESC="$SA_TARGET"; else TARGET_DESC="unbounded"; fi

# Atomic instance lock for the shared round directory and ledger baseline.
if ! mkdir "$LOCK" 2>/dev/null; then
  other=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
    log "Another hunt.sh instance is running (pid ${other}); exiting"
    exit 2
  fi
  log "Removing stale instance lock (former pid ${other:-unknown} is absent)"
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || { log "Could not acquire the instance lock; exiting"; exit 2; }
fi
echo $$ > "$LOCK/pid"

mkdir -p "$RUNS_DIR" 2>/dev/null || { log "Cannot create archive directory $RUNS_DIR; stopping"; exit 2; }
log "Per-run archive directory: $RUNS_DIR"

# Block restart after an incomplete Strong Accept decision archive.
if [ -e "$HALT_MARK" ]; then
  log "Found archive-integrity sentinel $HALT_MARK; an unarchived Strong Accept must not be published"
  log "Restore its archive or remove its row from $LEDGER_GOOD, then remove the sentinel and restart"
  exit 2
fi

# Treat the startup working-tree ledger as the operator baseline.
if [ -f ledger.tsv ]; then
  if ! git diff --quiet -- ledger.tsv 2>/dev/null; then
    log "Startup baseline: using the current modified ledger.tsv"
  fi
  cp ledger.tsv "$LEDGER_GOOD"
else
  git show "HEAD:ledger.tsv" > "$LEDGER_GOOD" 2>/dev/null || : > "$LEDGER_GOOD"
fi
# Restore the latest orchestrator baseline and release the lock on every exit.
trap 'cp "$LEDGER_GOOD" ledger.tsv 2>/dev/null || true; rm -rf "$LOCK"' EXIT

# Resume mechanically valid front artifacts, but never reuse ballots or verdicts.
resume_front=0
if [ "$RESUME_FRONT" = "1" ] && [ -s "$RD/ideas.tsv" ] && [ -s "$RD/ideas.md" ] && [ -s "$RD/priorwork.md" ]; then
  # Prefer the full generated set for theme validation; support legacy leftovers.
  themes_src="$RD/ideas.tsv"
  [ -s "$RD/ideas.all.tsv" ] && themes_src="$RD/ideas.all.tsv"
  if themes_ok "$themes_src" && axiom_ok "$RD/ideas.md" "$RD/ideas.tsv" 0 && priorwork_ok && cracks_ok; then
    resume_front=1
    log "Found valid interrupted front artifacts; resuming with fresh reviews"
  else
    log "Interrupted front artifacts failed validation; starting a fresh round"
  fi
fi

fails=0
empties=0
round=0
run_id='-'
while :; do
  today=$(date +%F)
  sa_now=$(sa_today)
  if [ "$SA_TARGET" -gt 0 ] && [ "$sa_now" -ge "$SA_TARGET" ]; then
    if ls "ideas/${today}"_hunt*.md >/dev/null 2>&1; then
      # Publication is idempotent; recover a completed report before exiting.
      if ./publish.sh >> "$LOG" 2>&1; then
        log "Daily Strong Accept count ${sa_now} reached target ${SA_TARGET}; publication confirmed"
        break
      fi
      log "Daily target is met, but publication recovery failed; inspect hunt.log"
      exit 2
    fi
    # A prior process may have stopped between aggregation and report creation.
    log "Daily SA count ${sa_now} reached ${SA_TARGET}, but no report exists; running a recovery round"
  elif [ "$round" -eq 0 ] && ls "ideas/${today}"_hunt*.md >/dev/null 2>&1; then
    # Recover any startup report, then continue toward the higher target.
    ./publish.sh >> "$LOG" 2>&1 || { log "Startup publication recovery failed; inspect hunt.log"; exit 2; }
    log "Daily Strong Accept count ${sa_now}/${TARGET_DESC}; existing reports are published"
  fi

  round=$((round + 1))
  m_stage='-'; m_lens='-'
  prune_near_sa_queue   # Remove terminal stories and apply NEAR_SA_MAX.
  # A stable run_id identifies the round; candidate ids are <run_id>/I<n>.
  # ledger_base_lines anchors the append-only delta archived for this round.
  run_id="$(date +%Y%m%dT%H%M%S)-p$$-r${round}"
  sa_count='-'; m_verdicts=''
  before=$(git rev-parse HEAD)
  cp "$LEDGER_GOOD" ledger.tsv                       # Restore the last Bash-owned baseline.
  pre_dirty=$(git status --porcelain | cut -c4- | sort -u)

  front_resumed=0
  if [ "$resume_front" = "1" ]; then
    # Keep validated front artifacts, but clear every review and verdict artifact.
    resume_front=0
    front_resumed=1
    rm -rf "$RD/rev"
    rm -f "$RD/rev_rc" "$RD/accepted.tsv" "$RD/rejects.tsv" "$RD/meta.txt"
    empties=0
    log "Resuming validated front artifacts with fresh reviews"
  else
    rm -rf "$RD"; mkdir -p "$RD"
  fi
  # grep -c prints 0 with rc=1 for an empty file; do not append another zero.
  ledger_base_lines=$(grep -c '' "$LEDGER_GOOD" 2>/dev/null || true)
  [ -n "$ledger_base_lines" ] || ledger_base_lines=0

  if [ "$front_resumed" = "0" ]; then
    # 0) Periodically distill recurring failures; this stage cannot block a round.
    fails_now=$(awk -F'\t' '$5=="reject" || $5=="accept-w-rev"' ledger.tsv 2>/dev/null | grep -c . || true)
    if [ $(( (round - 1) % META_EVERY )) -eq 0 ] && [ "$fails_now" -ge "$META_MIN_REJECTS" ]; then
      run_stage "$FRONT_CMD" "Read roles/meta.md and follow it" meta; rc=$?; guard 0
      if [ "$rc" -ne 0 ] || [ ! -s "$DEATHLIST" ]; then
        log "Failure digest produced no usable output; continuing with the prior digest or none"
      else
        log "Updated $DEATHLIST from ${fails_now} reject/AwR rows"
      fi
    fi

    # 1) Generate candidates under a Bash-selected divergence lens.
    lens=$(pick_lens)
    m_lens=${lens:--}
    gen_prompt="Read roles/generate.md and follow it"
    if [ -n "$lens" ]; then
      gen_prompt="${gen_prompt}; Divergence Lens (selected by the orchestrator; do not replace): ${lens}"
      log "Divergence lens: ${lens}"
    else
      log "No divergence lens selected"
    fi
    run_stage "$FRONT_CMD" "$gen_prompt" generate; rc=$?; guard 0
    if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
    if [ ! -s "$RD/ideas.tsv" ] || ! themes_ok || ! axiom_ok "$RD/ideas.md" "$RD/ideas.tsv" 1; then
      log "Generation produced no ideas.tsv or failed theme/assumption-removal structure gates"; fails=0
      empty_and_wait; continue
    fi

    # 1.4) Rank without killing; invalid output falls back to generation order.
    run_stage "$FRONT_CMD" "Read roles/select.md and follow it" select; rc=$?; guard 0
    [ "$rc" -ne 0 ] && log "Selection failed (rc=$rc); falling back to generation order"

    # 1.5) Prescreen only single-work direct hits; Bash builds the shortlist.
    mv "$RD/ideas.tsv" "$RD/ideas.all.tsv"
    mv "$RD/ideas.md" "$RD/ideas.all.md"
    run_stage "$FRONT_CMD" "Read roles/prescreen.md and follow it" prescreen; rc=$?; guard 0
    if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
    # Missing or malformed decisions fail open to keep; backend failures do not.
    ps_missing=0
    if [ ! -s "$RD/prescreen.md" ]; then
      ps_missing=1
      log "Prescreen fail-open: prescreen.md is missing or empty; treating all candidates as keep"
    fi
    : > "$RD/ideas.tsv"; : > "$RD/ideas.md"; : > "$RD/kills.tsv"; : > "$RD/keeps.tsv"
    oidx=0; failopen=0
    while IFS=$'\t' read -r id story theme; do
      [ -z "$id" ] && continue
      oidx=$((oidx + 1))
      dec=$(prescreen_dec "$id")
      if [ "$dec" = "kill" ] && kill_url=$(kill_evidence "$id"); then
        printf '%s\t%s\t%s\t%s\n' "$id" "$story" "$theme" "$kill_url" >> "$RD/kills.tsv"
      else
        if [ "$dec" != "keep" ]; then
          failopen=$((failopen + 1))
          [ "$ps_missing" -eq 0 ] && log "Prescreen fail-open: ${id} has an invalid decision or incomplete kill evidence"
        fi
        # Use byte-exact theme counting; locale collation can merge distinct labels.
        tcount=$(cut -f3 "$LEDGER_GOOD" 2>/dev/null | grep -Fxc -- "$theme" || true)
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(keep_rank "$id")" "$(select_rank_of "$id")" "$tcount" "$oidx" "$id" "$story" "$theme" >> "$RD/keeps.tsv"
      fi
    done < "$RD/ideas.all.tsv"
    # Record validated direct hits immediately as reject/high.
    if [ -s "$RD/kills.tsv" ]; then
      cp "$LEDGER_GOOD" ledger.tsv
      while IFS=$'\t' read -r id story theme kill_url; do
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$today" "hunt" "$theme" "$story" "reject" "Prescreen direct hit: $kill_url" "high" "novelty-dead" >> ledger.tsv
      done < "$RD/kills.tsv"
      cp ledger.tsv "$LEDGER_GOOD"
      log "Prescreen recorded $(grep -c . "$RD/kills.tsv") direct hits as reject"
    fi
    select_shortlist
    if [ "$failopen" -gt 0 ]; then
      log "Prescreen fail-open kept ${failopen} candidates; invalid kills were not recorded"
      metrics_write failopen prescreen '-'
    fi
    if [ "$kept" -eq 0 ]; then
      log "Prescreen killed every candidate as a direct hit; discarding the round"; fails=0
      empty_and_wait; continue
    fi
    log "Prescreen sent ${kept} prioritized candidates to deep research: $(cut -f1 "$RD/ideas.tsv" | tr '\n' ' ')"

    # 2) Adversarial research over the shortlist, with bounded targeted retries.
    research_try=0
    while :; do
      rm -f "$RD/priorwork.md"                        # Clear stale evidence before retrying.
      run_stage "$FRONT_CMD" "Read roles/research.md and follow it" research; rc=$?; guard 0
      [ "$rc" -ne 0 ] && break                        # Backend failure follows the failure path.
      { [ -s "$RD/priorwork.md" ] && priorwork_ok && cracks_ok; } && break
      [ "$research_try" -ge "$RESEARCH_RETRY" ] && break
      research_try=$((research_try + 1))
      log "Research is structurally incomplete; targeted retry ${research_try}/${RESEARCH_RETRY}"
    done
    if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
    if [ ! -s "$RD/priorwork.md" ] || ! priorwork_ok || ! cracks_ok; then
      log "Research remained incomplete after ${RESEARCH_RETRY} retries; discarding the round"; fails=0
      empty_and_wait; continue
    fi
    empties=0                                        # Front artifacts are complete.
  fi

  # 3) Run N review seats in parallel with isolated input directories.
  m_stage=review
  rev_t0=$(date '+%F %T')
  : > "$RD/rev_rc"; pids=()
  for r in $(seq 1 "$REVIEWERS"); do
    d="$RD/rev/$r"; mkdir -p "$d"
    cp "$RD/ideas.md" "$RD/priorwork.md" "$d/"
    rev_cmd_var="REV_CMD_$r"; rev_cmd=${!rev_cmd_var:-$BACK_CMD}   # Per-seat override.
    log "Starting [review#${r}] in isolated directory ${d}: $rev_cmd"
    ( if [ "$r" -gt 1 ] && [ "$REV_STAGGER_SEC" -gt 0 ]; then sleep "$(( (r - 1) * REV_STAGGER_SEC ))"; fi
      $rev_cmd "Read roles/review.md and follow it; inputs are ${d}/ideas.md, ${d}/priorwork.md, rubric.md, and brainstorming_policy.md; write verdicts to ${d}/verdict.tsv and complete reviews to ${d}/review.md" \
        >> "$RD/rev/${r}.log" 2>&1; printf '%s %s\n' "$r" "$?" >> "$RD/rev_rc" ) &
    pids+=("$!")
  done
  wait "${pids[@]}"
  guard 0
  # Require exactly one successful return-code row per review seat.
  if ! awk -v n="$REVIEWERS" 'NF==2 && $2==0{ok++} END{exit !(ok==n)}' "$RD/rev_rc"; then
    printf 'review\t%s\t%s\t1\n' "$rev_t0" "$(date '+%F %T')" >> "$RD/stages.tsv"
    log "A review seat failed or is missing: $(tr '\n' ' ' < "$RD/rev_rc")"; fail_and_wait; continue
  fi
  # Validate every candidate-seat ballot before rank conversion or ledger writes.
  bad_vote=""
  while IFS=$'\t' read -r id story theme; do
    [ -z "$id" ] && continue
    for r in $(seq 1 "$REVIEWERS"); do
      v=$(awk -F'\t' -v id="$id" '$1==id{print $2; exit}' "$RD/rev/$r/verdict.tsv" 2>/dev/null)
      vote_valid "$v" || bad_vote="${bad_vote:+$bad_vote }${id}@rev${r}[${v:-missing}]"
    done
  done < "$RD/ideas.tsv"
  if [ -n "$bad_vote" ]; then
    printf 'review\t%s\t%s\t1\n' "$rev_t0" "$(date '+%F %T')" >> "$RD/stages.tsv"
    log "Missing or invalid ballots (${bad_vote}); rerunning without ledger mutation"
    fail_and_wait; continue
  fi
  printf 'review\t%s\t%s\t0\n' "$rev_t0" "$(date '+%F %T')" >> "$RD/stages.tsv"

  # 4) Aggregate the minimum valid rank; Strong Accept remains unanimous and gated.
  cp "$LEDGER_GOOD" ledger.tsv                       # Append only to a clean baseline.
  : > "$RD/accepted.tsv"; : > "$RD/rejects.tsv"
  sa_count=0
  m_verdicts=""
  while IFS=$'\t' read -r id story theme; do
    [ -z "$id" ] && continue
    min=2; reason=""; votes=""; sa_votes=0
    for r in $(seq 1 "$REVIEWERS"); do
      line=$(awk -F'\t' -v id="$id" '$1==id{print; exit}' "$RD/rev/$r/verdict.tsv" 2>/dev/null || true)
      v=$(printf '%s' "$line" | cut -f2); mj=$(printf '%s' "$line" | cut -f3); rs=$(printf '%s' "$line" | cut -f4)
      rank=$(rank_of "$v")
      capped=$(major_cap "$rank" "$mj")               # Enforce the MAJOR cap.
      if [ "$capped" != "$rank" ]; then
        log "MAJOR cap: ${id} reviewer ${r} reported ${mj}; capping strong-accept at accept-w-rev"; rank=$capped
      fi
      # Ballot validation guarantees a vocabulary value; '-' remains defense in depth.
      if [ -n "$v" ]; then votes="${votes:+$votes,}${rank}"; else votes="${votes:+$votes,}-"; fi
      [ "$rank" -eq 2 ] && sa_votes=$((sa_votes + 1))
      if [ "$rank" -lt "$min" ]; then min=$rank; reason=$rs; fi
      [ -z "$reason" ] && reason=$rs
    done
    raw_min=$min; downgraded=0                        # Preserve the pre-gate minimum.
    if [ "$min" -eq 2 ] && ! sa_gate_ok "$id"; then
      min=0; downgraded=1; reason="Unanimous SA failed a mechanical gate: papers read < ${MIN_READ}, missing research block, missing falsification experiment, incomplete review, or insufficient supported crack evidence"
      log "Strong Accept gate failed; downgrading ${id} to reject"
    fi
    verdict=$(verdict_of "$min")
    m_verdicts="${m_verdicts:+${m_verdicts};}${id}=${votes}->${verdict}"
    [ -z "$reason" ] && reason="(No reason supplied; handled conservatively)"
    [ -z "$theme" ] && theme="Unlabeled"
    # Read the independently researched overlap from the candidate block.
    overlap=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"&&$2~/^I[0-9]+$/{if(f)exit} f' "$RD/priorwork.md" 2>/dev/null \
              | sed -E -n 's/^Overlap:[[:space:]]*(high|medium|low)([[:space:]].*)?$/\1/p' | head -1)
    [ -z "$overlap" ] && overlap="unknown"
    # Strong Accept rows use '-', and non-SA rows use the four-way classifier.
    if [ "$min" -eq 2 ]; then cat="-"; else cat=$(classify_nonsa "$raw_min" "$downgraded" "$overlap"); fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$today" "hunt" "$theme" "$story" "$verdict" "$reason" "$overlap" "$cat" >> ledger.tsv
    if [ "$min" -eq 2 ]; then
      printf '%s\t%s\n' "$id" "$story" >> "$RD/accepted.tsv"; sa_count=$((sa_count + 1))
    else
      printf '%s\t%s\t%s\n' "$id" "$story" "$reason" >> "$RD/rejects.tsv"
      # Mirror the non-SA classification into tmp/ observations.
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$today" "${run_id}/${id}" "$cat" "$verdict" "$overlap" "${votes:--}" "$story" >> "$NONSA_CLASS"
      # Queue eligible near-SA candidates once, while the story remains recheckable.
      story_cnt=$(cut -f4 ledger.tsv 2>/dev/null | grep -Fxc -- "$story")
      if { [ "$cat" = design-fixable ] || [ "$cat" = evidence-incomplete ]; } && [ "$sa_votes" -ge 1 ] \
         && [ "${story_cnt:-0}" -lt 2 ] \
         && ! cut -f3 "$NEAR_SA_QUEUE" 2>/dev/null | grep -Fxq -- "$story"; then
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$today" "${run_id}/${id}" "$story" "$theme" "$overlap" "${votes:--}" "$cat" >> "$NEAR_SA_QUEUE"
        log "Queued near-SA ${id} (votes=${votes}, overlap=${overlap}, category=${cat})"
      fi
    fi
  done < "$RD/ideas.tsv"
  cp ledger.tsv "$LEDGER_GOOD"                       # Freeze the Bash-owned ledger result.
  guard 0
  log "Aggregated ${sa_count} unanimous, gate-complete Strong Accept rows into ledger.tsv"
  metrics_write verdict '-' "${m_verdicts:--}"
  # A Strong Accept cannot publish without a complete decision archive.
  if ! archive_round verdict && [ "$sa_count" -gt 0 ]; then
    printf '%s\trun_id=%s\tsa_count=%s\treason=verdict-archive-failed\n' \
      "$(date '+%F %T')" "$run_id" "$sa_count" > "$HALT_MARK" 2>/dev/null || true
    log "Recorded ${sa_count} SA rows, but the decision archive failed; stopping before publication"
    log "Restore archive ${run_id} under $RUNS_DIR or remove its SA rows from $LEDGER_GOOD"
    log "Then remove $HALT_MARK and restart"
    exit 2
  fi

  # 5) Assemble and publish a report, then continue until SA_TARGET is reached.
  if [ "$sa_count" -gt 0 ]; then
    printf 'Rounds Attempted: %s\nReview Date: %s\nReviewers: %s\n' "$round" "$today" "$REVIEWERS" > "$RD/meta.txt"
    reports_before=$(reports_today)
    run_stage "$BACK_CMD" "Read roles/report.md and follow it" report; rc=$?; guard 1   # Only this stage may write ideas/.
    if [ "$rc" -ne 0 ]; then fail_and_wait; continue; fi
    if [ "$(reports_today)" -gt "$reports_before" ]; then
      cp "$LEDGER_GOOD" ledger.tsv   # Remove any report-stage ledger mutation.
      ./publish.sh >> "$LOG" 2>&1 || { log "publish.sh failed; inspect hunt.log"; archive_round publish-failed; exit 2; }
      archive_round published
      sa_now=$(sa_today)
      if [ "$SA_TARGET" -gt 0 ] && [ "$sa_now" -ge "$SA_TARGET" ]; then
        log "Published; daily Strong Accept count ${sa_now} reached ${SA_TARGET}"
        break
      fi
      fails=0
      sleep_min=$(random_no_hit_sleep_min)
      log "Published; daily Strong Accept count ${sa_now}/${TARGET_DESC}; next round in ${sleep_min} minutes"
      sleep "$((sleep_min * 60))"
      continue
    fi
    log "Report stage created no new ideas/${today}_hunt*.md file; discarding the round"
    archive_round report-missing
  fi

  fails=0
  log "Round completed without a unanimous Strong Accept report"
  sleep_min=$(random_no_hit_sleep_min)
  log "No report; retrying in ${sleep_min} minutes (range ${NO_HIT_SLEEP_MIN_LO}-${NO_HIT_SLEEP_MIN_HI})"
  sleep "$((sleep_min * 60))"
done
