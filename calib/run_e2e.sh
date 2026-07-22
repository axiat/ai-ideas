#!/usr/bin/env bash
# End-to-end retrieval-recall calibration for direct-hit negative controls.
# Frozen calibration fixes ideas and prior work to evaluate verdict logic and aggregation. This runner instead invokes
# roles/research.md with a retrieval-capable backend and checks whether known occupants are recalled and graded honestly.
# Positive controls have no E2E mode because live retrieval would find their source papers and create false negatives.
# Effectiveness boundary: assertions validate artifact structure, occupant hits, and neighbor/API density. Text alone cannot
# prove that retrieval occurred. E2E is a regression gate against thin, empty, or occupant-missing output, not proof against
# an adversarial backend that hardcodes the expected evidence.
#
# e2e.expect DSL, one assertion per line with # comments; every assertion applies to every ID:
#   overlap=high|medium|low        exact Overlap value in the prior-work block
#   url_contains=<substring>       known occupant arXiv ID or URL fragment in the block
#
# Usage: ./calib/run_e2e.sh calib/cases/<case>
# E2E_CMD overrides the retrieval backend. Unlike frozen panels, this command must enable search and network access.
# The runner uses the same disposable-mirror boundary as run_panel.sh and copies back only priorwork.md.
# Results append to tmp/calib/summary.tsv with an e2e: case prefix.
# Exit codes: 0 assertions pass; 1 assertion failure; 2 configuration or backend failure.
set -u
cd "$(dirname "$0")/.." || exit 2
repo=$(pwd)

CASE=${1:?usage: ./calib/run_e2e.sh calib/cases/<case>}
E2E_CMD=${E2E_CMD:-codex --search -c approval_policy=never -c sandbox_workspace_write.network_access=true exec -s workspace-write --skip-git-repo-check --ephemeral}
E2E_MIN_LINKS=${E2E_MIN_LINKS:-5}   # Minimum non-API neighbor links per idea, matching the hunt research gate.
name=$(basename "$CASE")
OUT="tmp/calib/e2e-$name"
SUMMARY=tmp/calib/summary.tsv

[ -s "$CASE/ideas.md" ] || { echo "run_e2e: missing $CASE/ideas.md" >&2; exit 2; }
[ -s "$CASE/e2e.expect" ] || { echo "run_e2e: missing $CASE/e2e.expect" >&2; exit 2; }

. "$repo/lib/resolve_cmd.sh"
RESOLVED_CMD=$(resolve_cmd "$repo" "run_e2e: E2E_CMD" "$E2E_CMD") || exit 2
. "$repo/lib/mirror_pre.sh"
. "$repo/lib/md_ids.sh"

rm -rf "$OUT"; mkdir -p "$OUT"
mkdir -p tmp/calib
[ -s "$SUMMARY" ] || printf 'ts\tcase\treviewers\tpanel_cmd\tgrade\tvotes\tfailed_checks\n' > "$SUMMARY"
# Encode mirror names as in run_panel.sh and limit cleanup to this case.
mname=${name//_/_u}; mname=${mname//./_d}
rm -rf "$repo/tmp/e2e.$mname."*

# Freeze one ideas.md snapshot for ID extraction, the mirror, and later inspection.
cp "$CASE/ideas.md" "$OUT/"
md_idea_ids "$OUT/ideas.md" > "$OUT/ids" \
  || { echo "run_e2e: $CASE/ideas.md has an unclosed Markdown fence" >&2; exit 2; }
[ -s "$OUT/ids" ] || { echo "run_e2e: no IDs found in $CASE/ideas.md; expected ## I<n> headings" >&2; exit 2; }

mirror=$(mktemp -d "$repo/tmp/e2e.$mname.XXXXXX") || { echo "run_e2e: failed to create mirror" >&2; exit 2; }
mkdir -p "$mirror/roles" "$mirror/tmp/round"
cp "$repo/roles/research.md" "$mirror/roles/"
cp "$OUT/ideas.md" "$mirror/tmp/round/ideas.md"
mkdir -p "$mirror/.claude"
# Explicit Claude seats receive E2E-only settings that allow retrieval and restrict writes to mirror/tmp/**.
# Do not copy repository settings because their absolute temporary-directory grants would permit persistent writes outside the mirror.
cat > "$mirror/.claude/settings.json" <<'JSON'
{
  "permissions": {
    "allow": ["Edit(tmp/**)", "Write(tmp/**)", "WebSearch", "WebFetch"]
  }
}
JSON
pre=$(mirror_pre "$mirror" "tmp/round/priorwork.md")
logf="$repo/$OUT/research.log"   # The backend changes CWD, so the log path must be absolute.
( cd "$mirror" && GROK_REPO="$mirror" $RESOLVED_CMD "${pre}

Read roles/research.md and follow it." < /dev/null > "$logf" 2>&1 )
rc=$?
[ -f "$mirror/tmp/round/priorwork.md" ] && cp "$mirror/tmp/round/priorwork.md" "$OUT/priorwork.md"
rm -rf "$mirror"
if [ "$rc" -ne 0 ] || [ ! -s "$OUT/priorwork.md" ]; then
  echo "run_e2e: research failed (rc=$rc) or produced no priorwork.md; see $OUT/research.log" >&2
  printf '%s\te2e:%s\t-\t%s\tagent-fail\t-\t-\n' "$(date '+%F %T')" "$name" "$E2E_CMD" >> "$SUMMARY"
  exit 2
fi

failed=""; detail=""
while read -r id; do
  [ -z "$id" ] && continue
  block=$(awk -v id="$id" '$1=="##"&&$2==id{f=1;next} $1=="##"{if(f)exit} f' "$OUT/priorwork.md")
  if [ -z "$block" ]; then
    failed="${failed:+$failed;}${id}:missing-block"
    detail="${detail:+$detail;}${id}=missing"
    continue
  fi
# Parse only the first anchored Overlap: line and accept an enum immediately after the label.
# Later commentary containing high, medium, or low cannot supply the verdict.
  ov=$(printf '%s\n' "$block" | awk '
    /^Overlap:/ {
      if ($0 ~ /^Overlap:[[:space:]]*(high|medium|low)([[:space:]]|$)/) {
        value=$0
        sub(/^Overlap:[[:space:]]*/, "", value)
        sub(/[[:space:]].*$/, "", value)
        print value
      }
      exit
    }
  ')
  # Count non-API neighbor links separately from structured API query records, matching hunt.sh.
  links=$(printf '%s\n' "$block" | grep -E '^- .*https?://' \
          | grep -cvE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org' || true)
  api=$(printf '%s\n' "$block" | grep -cE 'export\.arxiv\.org/api/query|api\.semanticscholar\.org' || true)
  detail="${detail:+$detail;}${id}=${ov:-missing},links=${links},api=${api}"
  # Independent of e2e.expect, require E2E_MIN_LINKS non-API neighbors and one structured API query URL.
  # This proves artifact completeness only. E2E conclusions assume the configured backend actually performed retrieval.
  if [ "$links" -lt "$E2E_MIN_LINKS" ] || [ "$api" -lt 1 ]; then
    failed="${failed:+$failed;}${id}:retrieval-thin(links=${links}<${E2E_MIN_LINKS} or api=${api}<1)"
  fi
  while IFS= read -r line; do
    line=${line%%#*}
    line=$(printf '%s' "$line" | tr -d '[:space:]')
    [ -z "$line" ] && continue
    case "$line" in
      'overlap='*)
        want=${line#overlap=}
        case "$want" in high|medium|low) ;; *) echo "run_e2e: invalid assertion: $line" >&2; exit 2 ;; esac
        [ "${ov:-}" = "$want" ] || failed="${failed:+$failed;}${id}:${line}" ;;
      'url_contains='*)
        want=${line#url_contains=}
        [ -n "$want" ] || { echo "run_e2e: url_contains assertion is empty" >&2; exit 2; }
        printf '%s\n' "$block" | grep -qF "$want" || failed="${failed:+$failed;}${id}:${line}" ;;
      *) echo "run_e2e: unknown assertion: $line" >&2; exit 2 ;;
    esac
  done < "$CASE/e2e.expect"
done < "$OUT/ids"

grade=pass; [ -n "$failed" ] && grade=fail
printf '%s\te2e:%s\t-\t%s\t%s\t%s\t%s\n' \
  "$(date '+%F %T')" "$name" "$E2E_CMD" "$grade" "${detail:--}" "${failed:--}" >> "$SUMMARY"
echo "run_e2e: $name => $grade${failed:+(failed checks: $failed)}"
echo "Artifact: $OUT/priorwork.md; backend log: $OUT/research.log; input snapshot: $OUT/ideas.md"
[ "$grade" = pass ]
