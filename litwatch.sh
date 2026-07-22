#!/usr/bin/env bash
# Recent-work monitor, independent from the hunt.sh loop. Each pass fetches API
# records into trusted staging, optionally annotates a sandbox copy, then admits
# only annotations whose identifiers already exist in staging. Backend failure
# is best-effort: deterministic ingest still produces the index.
#
# Configuration:
#   LITWATCH_DIR          Output directory; default tmp/litwatch.
#   LITWATCH_MAX          Per-theme, per-source limit; default 25.
#   LITWATCH_WINDOW       Recent-day window; default 60, 0 disables.
#   LITWATCH_SOURCES      Space-separated oai/arxiv/s2; default oai.
#   LITWATCH_OAI_DAYS/SETS/MAXPAGES/CATS configure OAI harvesting.
#   LITWATCH_S2_KEY       Semantic Scholar key passed as x-api-key.
#   LITWATCH_SORT         submittedDate (default) or relevance.
#   LITWATCH_THEMES_FILE  Query themes, or `|`-separated OAI keyword groups.
#   LITWATCH_FETCH_GAP    Seconds between arxiv/s2 fetches; default 3.
#   LITWATCH_CMD          Annotation backend; defaults to sandboxed Codex.
#   LITWATCH_AGY_CMD      Legacy override only when LITWATCH_CMD is unset.
#   LITWATCH_NO_AGY=1     Compatibility switch that skips annotation.
#   LITWATCH_PREBUILT_STAGING bypasses fetch for offline runs.
#   LITWATCH_LOOP_SEC     0 for one pass; positive seconds for a foreground loop.
set -u
litwatch_cmd_was_set=${LITWATCH_CMD+x}
LITWATCH_CMD=${LITWATCH_CMD:-codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral}
annotation_cmd=$LITWATCH_CMD
if [ -z "$litwatch_cmd_was_set" ] && [ -n "${LITWATCH_AGY_CMD:-}" ]; then
  annotation_cmd=$LITWATCH_AGY_CMD
fi
repo="$(cd "$(dirname "$0")" && pwd)"; cd "$repo" || { echo "litwatch: cannot enter repository root $repo" >&2; exit 1; }
py="$repo/lib/litwatch.py"
dir=${LITWATCH_DIR:-tmp/litwatch}
max=${LITWATCH_MAX:-25}
window=${LITWATCH_WINDOW:-60}
sources=${LITWATCH_SOURCES:-oai}
sortby=${LITWATCH_SORT:-submittedDate}
gap=${LITWATCH_FETCH_GAP:-3}
# OAI-PMH batch-harvest configuration.
oai_days=${LITWATCH_OAI_DAYS:-4}
oai_sets=${LITWATCH_OAI_SETS:-cs}
oai_maxpages=${LITWATCH_OAI_MAXPAGES:-8}
oai_cats=${LITWATCH_OAI_CATS:-cs.RO,cs.LG,cs.AI,cs.CV,cs.CL,stat.ML}
mkdir -p "$dir"
log(){ printf '[litwatch %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# Search-query themes for s2 and arxiv.
default_query_themes(){ cat <<'EOF'
vision language action reinforcement learning post-training
world model latent dynamics model-based reinforcement learning robot
VLM reward shaping reinforcement learning manipulation
inference-time steering frozen policy backbone
long-horizon robot manipulation reinforcement learning
flow matching action policy robot learning
EOF
}
# Local OAI themes use `|` for equivalent case-insensitive substrings.
default_oai_themes(){ cat <<'EOF'
vision-language-action|vision language action|VLA policy|VLA model
world model|latent dynamics|model-based reinforcement
diffusion policy|flow matching policy|flow matching action
reward shaping|VLM reward|reward model reinforcement|dense reward
long-horizon manipulation|long horizon manipulation|multi-step manipulation
robot manipulation|robotic manipulation|manipulation policy
inference-time steering|noise-space steering|frozen backbone|test-time steering
EOF
}
query_themes_stream(){
  if [ -n "${LITWATCH_THEMES_FILE:-}" ] && [ -f "${LITWATCH_THEMES_FILE}" ]; then
    cat "$LITWATCH_THEMES_FILE"
  else
    default_query_themes
  fi
}

staging="$dir/staging.jsonl"       # Trusted fetch output and sole ingest input.
agydir="$dir/agy"                  # Backend workspace isolated from trusted staging.
ann="$agydir/annotations.jsonl"
index="$dir/index.jsonl"
drop="$dir/drops.jsonl"
mkdir -p "$agydir"

loop_sec=${LITWATCH_LOOP_SEC:-0}
case "$loop_sec" in ''|*[!0-9]*) echo "litwatch: LITWATCH_LOOP_SEC must be a nonnegative integer: $loop_sec" >&2; exit 2 ;; esac

# One pass: fetch, best-effort annotation, deterministic ingest.
one_pass(){
# 1) Fetch or copy prebuilt staging.
if [ -n "${LITWATCH_PREBUILT_STAGING:-}" ]; then
  cp "$LITWATCH_PREBUILT_STAGING" "$staging" || { log "Failed to copy prebuilt staging: $LITWATCH_PREBUILT_STAGING"; return 1; }
  log "Using prebuilt staging; fetch skipped: $LITWATCH_PREBUILT_STAGING"
else
  : > "$staging"
  for src in $sources; do
    case "$src" in
      oai)
        tf="${LITWATCH_THEMES_FILE:-}"
        if [ -z "$tf" ] || [ ! -f "$tf" ]; then tf="$dir/oai_themes.txt"; default_oai_themes > "$tf"; fi
        log "OAI harvest: days=$oai_days sets=$oai_sets cats=$oai_cats maxpages=$oai_maxpages"
        python3 "$py" harvest --days "$oai_days" --sets "$oai_sets" --max-pages "$oai_maxpages" \
          --cats "$oai_cats" --themes-file "$tf" >> "$staging" || log "OAI harvest failed; continuing"
        ;;
      s2|arxiv)
        while IFS= read -r theme; do
          [ -n "$theme" ] || continue
          if python3 "$py" fetch --source "$src" --query "$theme" --max "$max" \
               --window-days "$window" --sort "$sortby" --theme "$theme" >> "$staging"; then :; else
            log "Fetch failed; continuing: src=$src theme=$theme"
          fi
          [ "$gap" = "0" ] || sleep "$gap"
        done < <(query_themes_stream)
        ;;
      *) log "Unknown source skipped: $src" ;;
    esac
  done
fi

# 2) Best-effort annotation. The backend receives a copy under agydir. Ingest
# still reads trusted staging, so mutations to the backend copy cannot add records.
: > "$ann"
if [ "${LITWATCH_NO_AGY:-0}" != "1" ] && [ -s "$staging" ]; then
  cp "$staging" "$agydir/staging.jsonl"
  log "Annotation backend: $annotation_cmd (AGY_OUT_HINT=$agydir)"
  if AGY_OUT_HINT="$agydir" $annotation_cmd "Read roles/litwatch.md and follow it. Read ${agydir}/staging.jsonl and write only ${ann}."; then
    log "Annotation backend returned 0"
  else
    log "Annotation failed; deterministic ingest continues"
  fi
fi

# 3) Admit annotations only for fetched IDs; log malformed, duplicate, and
# out-of-set entries.
python3 "$py" ingest --staging "$staging" --annotations "$ann" \
  --drop-log "$drop" --out "$index" || { log "Ingest failed"; return 1; }
n=$(grep -c '' "$index" 2>/dev/null); n=${n:-0}
nd=$(grep -c '' "$drop" 2>/dev/null); nd=${nd:-0}
log "Index ready: $index ($n records; $nd dropped annotations in $drop)"
}

# Run once by default or remain in the foreground at the configured interval.
while :; do
  one_pass; rc=$?
  [ "$loop_sec" -gt 0 ] || exit "$rc"
  log "Pass finished (rc=$rc); rerunning in ${loop_sec}s (Ctrl-C stops)"
  sleep "$loop_sec"
done
