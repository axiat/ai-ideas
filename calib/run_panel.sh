#!/usr/bin/env bash
# Run N retrieval-disabled reviewers on one frozen calibration case and aggregate by minimum vote.
# The topology matches the hunt review stage: isolated directories, parallel seats, and minimum-vote aggregation.
# Calibration reviewers differ in two ways:
#   1. Retrieval is disabled because many controls are reconstructed from published work; live search would create false negatives by finding the source paper.
#   2. The panel neither writes the ledger nor publishes. Results remain under tmp/calib/ for inspection.
#
# Usage: ./calib/run_panel.sh calib/cases/<case> [reviewers, default 3]
#   PANEL_CMD overrides the trusted reviewer command. Results are written to tmp/calib/<case>/rev/N.
#   Example: PANEL_CMD='./grok-worker.sh' ./calib/run_panel.sh calib/cases/pos-robomme
#   Grok seats receive GROK_DISABLE_WEB=1. The mirror settings deny WebSearch and WebFetch for explicit Claude seats.
#   The operating-system network remains available, so shell-side retrieval control still depends on the prompt and the leak marker.
#   Codex commands need --skip-git-repo-check --ephemeral because the mirror is not a Git repository. Frozen panels do not enable --search or network access.
#
# Write boundary, matching awr-side.sh run_agent: each seat receives a disposable mirror containing only the role,
# rubric, policy, and frozen case. The backend writes under mirror/tmp/out/. Bash copies back only verdict.tsv and
# review.md, then discards the mirror. The real repository is never the reviewer working directory.
# The mirror isolates the file tree and CWD, not processes or network. PANEL_CMD must provide its own sandbox.
# Different cases may run concurrently; the same case may not because it shares the result directory and cleanup prefix.
#
# Interpretation:
#   Strong Accept capability controls reconstruct a known paper before publication and pair it with ideal prior work.
#   They require min-vote >= accept-w-rev plus at least one strong-accept vote.
#   Acceptance-floor boundary controls require min-vote >= accept-w-rev without requiring a strong-accept vote.
#   Negative controls have a single-paper direct hit and honest high overlap. Every seat must reject.
set -u
cd "$(dirname "$0")/.." || exit 2
repo=$(pwd)

CASE=${1:?usage: ./calib/run_panel.sh calib/cases/<case> [reviewers]}
REVIEWERS=${2:-3}
PANEL_CMD=${PANEL_CMD:-codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral}
name=$(basename "$CASE")
OUT="tmp/calib/$name"

[ -s "$CASE/ideas.md" ] && [ -s "$CASE/priorwork.md" ] || { echo "run_panel: missing $CASE/ideas.md or priorwork.md"; exit 2; }
# Validate REVIEWERS before cleanup. Zero or nonnumeric input makes seq empty, and Bash 3.2 with set -u then fails on an empty pids array.
# Validation before rm -rf also preserves the previous ballots when configuration is invalid.
case "$REVIEWERS" in ''|*[!0-9]*) echo "run_panel: reviewers must be a positive integer: $REVIEWERS" >&2; exit 2 ;; esac
[ "$REVIEWERS" -ge 1 ] || { echo "run_panel: reviewers must be at least 1: $REVIEWERS" >&2; exit 2; }

# Resolve the first PANEL_CMD token through the shared resolver. Relative paths are pinned to the real repository,
# parent traversal is rejected, and a bare repository file shadows PATH only when executable. Resolve before cleanup.
. "$repo/lib/resolve_cmd.sh"
RESOLVED_CMD=$(resolve_cmd "$repo" "run_panel: PANEL_CMD" "$PANEL_CMD") || exit 2
. "$repo/lib/mirror_pre.sh"

rm -rf "$OUT"; mkdir -p "$OUT"
# Encode mirror names (_ -> _u, . -> _d) so a cleanup prefix cannot match a live sibling case or collide across names.
mname=${name//_/_u}; mname=${mname//./_d}
rm -rf "$repo/tmp/panel.$mname."*   # Remove only stale mirrors for this case.

# Freeze reviewer input once. ID extraction, every seat, and later inspection use this snapshot.
# Reading the live case per seat would allow edits during launch to give reviewers different evidence.
cp "$CASE/ideas.md" "$CASE/priorwork.md" "$OUT/"

# The snapshot's ## I<n> headings are the single ID source for validation and aggregation.
# Fence-aware extraction is shared with run_e2e.sh; an unclosed fence is a configuration error.
. "$repo/lib/md_ids.sh"
md_idea_ids "$OUT/ideas.md" > "$OUT/ids" \
  || { echo "[calib] $CASE/ideas.md has an unclosed Markdown fence; repair the case before running the panel"; exit 2; }
[ -s "$OUT/ids" ] || { echo "[calib] no IDs found in $CASE/ideas.md; expected ## I<n> headings"; exit 2; }

# $1 is a normalized verdict.tsv. Valid output has exactly one four-column row per known ID,
# a valid verdict enum, numeric MAJOR count, and nonempty reason. Blank lines and one leading header are allowed.
verdict_ok() {
  [ -s "$1" ] || return 1
  awk -F'\t' -v idsf="$OUT/ids" '
    BEGIN{ while ((getline l < idsf) > 0) if (l != "") want[l]=1 }
    $0==""{ next }
    !seenline++ && ($1=="id" || $1=="ID" || $1 ~ /^#/){ next }
    NF!=4{ exit 1 }
    !($1 in want){ exit 1 }
    $2!="strong-accept" && $2!="accept-w-rev" && $2!="reject"{ exit 1 }
    $3!~/^[0-9]+$/{ exit 1 }
    $4==""{ exit 1 }
    { if (++seen[$1] > 1) exit 1 }
    END{ for (k in want) if (seen[k] != 1) exit 1 }
  ' "$1"
}

accepted_reviews_ok() {
  local verdictf=$1 reviewf=$2 id
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    [ -s "$reviewf" ] || return 1
    grep -qE "^##[[:space:]]+${id}([[:space:]]|$)" "$reviewf" || return 1
  done < <(awk -F'\t' '$2=="strong-accept" || $2=="accept-w-rev"{print $1}' "$verdictf")
}

# $1 is the seat number. Build one mirror, invoke the backend, copy approved artifacts, and discard the mirror.
run_judge() {
  local r=$1 mirror pre rc logf
  logf="$repo/$OUT/rev/$r.log"   # The backend changes CWD, so the log path must be absolute.
  # The mirror path includes the encoded case name and intentionally excludes a calib path segment used by Grok write guards.
  mirror=$(mktemp -d "$repo/tmp/panel.$mname.$r.XXXXXX") || { echo "$r 1" >> "$OUT/rev_rc"; return 1; }
  mkdir -p "$mirror/roles" "$mirror/tmp/out"
  cp "$repo/roles/review.md" "$mirror/roles/review.md"
  cp "$repo/rubric.md" "$repo/brainstorming_policy.md" "$mirror/"
  cp "$repo/$OUT/ideas.md" "$repo/$OUT/priorwork.md" "$mirror/tmp/out/"
  # Explicit Claude seats receive a calibration-only policy that limits writes to tmp/** and denies WebSearch/WebFetch.
  # Codex and Grok ignore this directory.
  mkdir -p "$mirror/.claude"
  cat > "$mirror/.claude/settings.json" <<'JSON'
{
  "permissions": {
    "allow": ["Edit(tmp/**)", "Write(tmp/**)"],
    "deny": ["WebSearch", "WebFetch"]
  }
}
JSON
  pre=$(mirror_pre "$mirror" "tmp/out/verdict.tsv and tmp/out/review.md")
  # GROK_REPO pins the Grok working root; GROK_DISABLE_WEB=1 enforces frozen-panel retrieval policy for Grok.
  ( cd "$mirror" && GROK_REPO="$mirror" GROK_DISABLE_WEB=1 $RESOLVED_CMD "${pre}

Read roles/review.md and follow it. Use D = tmp/out/ for ideas.md and priorwork.md, with rubric.md and brainstorming_policy.md at the repository root. Write verdicts to tmp/out/verdict.tsv and complete accepted reviews to tmp/out/review.md. Calibration override: do not use WebSearch, WebFetch, or any network retrieval. Judge novelty only from tmp/out/priorwork.md. If an idea appears to match a published work, do not change the verdict from memory; append exactly one line to review.md in the form suspected published counterpart: <name>." \
      < /dev/null > "$logf" 2>&1 )
  rc=$?
# Normalize verdict.tsv on copy-back: remove BOM/CR and trim every field. Validation and aggregation must read the same bytes.
# LC_ALL=C makes substr byte-oriented when removing the UTF-8 BOM.
  if [ -f "$mirror/tmp/out/verdict.tsv" ]; then
    LC_ALL=C awk -F'\t' 'BEGIN{OFS="\t"}
      NR==1 && substr($0,1,3) == "\357\273\277" { $0 = substr($0, 4) }
      { sub(/\r$/, ""); for (j=1; j<=NF; j++) gsub(/^[ \t]+|[ \t]+$/, "", $j); print }' \
      "$mirror/tmp/out/verdict.tsv" > "$OUT/rev/$r/verdict.tsv"
  fi
  [ -f "$mirror/tmp/out/review.md" ] && cp "$mirror/tmp/out/review.md" "$OUT/rev/$r/review.md"
  rm -rf "$mirror"
# A successful backend must still satisfy the verdict ABI. Treating missing or malformed votes as reject would make broken
# reviewers look correct on negative controls. Accepted votes additionally require their corresponding review block.
  if [ "$rc" -eq 0 ] && ! verdict_ok "$OUT/rev/$r/verdict.tsv"; then
    echo "[calib] reviewer #$r exited 0 but verdict.tsv is missing or invalid; see $OUT/rev/$r.log" >&2
    rc=1
  fi
  if [ "$rc" -eq 0 ] && ! accepted_reviews_ok "$OUT/rev/$r/verdict.tsv" "$OUT/rev/$r/review.md"; then
    echo "[calib] reviewer #$r returned an accepted verdict without a matching ## I<n> review block; see $OUT/rev/$r.log" >&2
    rc=1
  fi
  echo "$r $rc" >> "$OUT/rev_rc"
  return "$rc"
}

pids=()
for r in $(seq 1 "$REVIEWERS"); do
  mkdir -p "$OUT/rev/$r"
    echo "[calib] launch reviewer #$r in an isolated mirror -> $OUT/rev/$r"
  run_judge "$r" &
  pids+=("$!")
done
wait "${pids[@]}"

# Require one successful status row per reviewer. Missing or failed seats invalidate the panel.
if ! awk -v n="$REVIEWERS" 'NF==2 && $2==0{ok++} END{exit !(ok==n)}' "$OUT/rev_rc" 2>/dev/null; then
  echo "[calib] reviewer failure or missing seat: $(tr '\n' ' ' < "$OUT/rev_rc" 2>/dev/null); panel invalid, see $OUT/rev/*.log" >&2
  exit 2
fi

echo
echo "=== Calibration result: $name (minimum vote; Strong Accept requires unanimity) ==="
rank_of() { case "$1" in strong-accept) echo 2 ;; accept-w-rev) echo 1 ;; *) echo 0 ;; esac; }
: > "$OUT/aggregate.tsv"   # id, comma-separated votes, minimum vote
while read -r id; do
  [ -z "$id" ] && continue
  min=2; votes=""; vcsv=""
  for r in $(seq 1 "$REVIEWERS"); do
    v=$(awk -F'\t' -v id="$id" '$1==id{print $2; exit}' "$OUT/rev/$r/verdict.tsv" 2>/dev/null)
    # verdict_ok guarantees one vote per ID. A missing lookup here is an internal inconsistency, never an implicit reject.
    [ -z "$v" ] && { echo "[calib] internal inconsistency: reviewer #$r passed validation but has no vote for $id" >&2; exit 2; }
    votes="$votes  #$r=$v"
    vcsv="${vcsv:+$vcsv,}$v"
    rk=$(rank_of "$v"); [ "$rk" -lt "$min" ] && min=$rk
  done
  case "$min" in 2) agg=strong-accept ;; 1) agg=accept-w-rev ;; *) agg=reject ;; esac
  echo "$id:$votes  =>  min-vote: $agg"
  printf '%s\t%s\t%s\n' "$id" "$vcsv" "$agg" >> "$OUT/aggregate.tsv"
done < "$OUT/ids"
# Print unique memory-leak markers once for the panel rather than once per idea.
leaks=$(grep -hE '^suspected published counterpart: .+$' "$OUT"/rev/*/review.md 2>/dev/null | LC_ALL=C sort -u)
[ -n "$leaks" ] && printf '%s\n' "$leaks" | sed 's/^/[leak marker] /'
echo "Ballots and reviews: $OUT/rev/*/; frozen inputs: $OUT/ideas.md and $OUT/priorwork.md; logs: $OUT/rev/*.log"
