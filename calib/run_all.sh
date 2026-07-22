#!/usr/bin/env bash
# Run every selected gold case through run_panel.sh and grade it against its expect assertions.
# Calibration accuracy is pass / (pass + fail). Probe and panel-fail cases are excluded because infrastructure
# failures and unscored probes cannot support a calibration conclusion. Append one row per case to tmp/calib/summary.tsv.
#
# expect DSL, one assertion per line with # comments; every assertion applies independently to every case ID:
#   min_vote>=accept-w-rev | min_vote>=strong-accept    minimum aggregate vote
#   sa_votes>=N | sa_votes=N                            Strong Accept vote count
#   reject_votes>=N                                     reject vote count
#   all_votes=reject                                    every seat rejects
#   probe                                               run without scoring; see calib/README.md
# Thresholds assume the default 3 reviewers. Recheck fixtures when changing reviewer count. Unknown assertions fail.
#
# Usage: ./calib/run_all.sh [reviewers, default 3] [case directories...]
# With no case arguments, run every directory under calib/cases/ that contains expect.
# PANEL_CMD is exported to run_panel.sh. Cases run serially to avoid multiplying reviewer load and backend launch-gate contention.
# Exit codes: 0 all green; 1 fail or panel-fail; 2 configuration or usage error.
set -u
cd "$(dirname "$0")/.." || exit 2

PANEL_CMD=${PANEL_CMD:-codex -c approval_policy=never exec -s workspace-write --skip-git-repo-check --ephemeral}
export PANEL_CMD

REVIEWERS=3
if [ $# -ge 1 ] && [ ! -d "$1" ]; then
  case "$1" in ''|*[!0-9]*) echo "run_all: first argument must be a reviewer count or case directory: $1" >&2; exit 2 ;; esac
  REVIEWERS=$1; shift
fi
[ "$REVIEWERS" -ge 1 ] || { echo "run_all: reviewers must be at least 1: $REVIEWERS" >&2; exit 2; }

cases=()
if [ $# -ge 1 ]; then
  cases=("$@")
else
  for d in calib/cases/*/; do
    [ -f "${d}expect" ] && cases+=("${d%/}")
  done
fi
[ "${#cases[@]}" -ge 1 ] || { echo "run_all: no cases with expect files" >&2; exit 2; }

SUMMARY=tmp/calib/summary.tsv
mkdir -p tmp/calib
[ -s "$SUMMARY" ] || printf 'ts\tcase\treviewers\tpanel_cmd\tgrade\tvotes\tfailed_checks\n' > "$SUMMARY"

rank_of() { case "$1" in strong-accept) echo 2 ;; accept-w-rev) echo 1 ;; *) echo 0 ;; esac; }

# Evaluate one assertion for one ID: $1 assertion, $2 min-vote, $3 SA count, $4 reject count, $5 total votes.
check_one() {
  local want n
  case "$1" in
    'min_vote>='*)
      want=${1#min_vote>=}
      case "$want" in strong-accept|accept-w-rev) ;; *) echo "run_all: invalid min_vote assertion: $1" >&2; exit 2 ;; esac
      [ "$(rank_of "$2")" -ge "$(rank_of "$want")" ] ;;
    'sa_votes>='*)
      n=${1#sa_votes>=}; case "$n" in ''|*[!0-9]*) echo "run_all: invalid assertion: $1" >&2; exit 2 ;; esac
      [ "$3" -ge "$n" ] ;;
    'sa_votes='*)
      n=${1#sa_votes=}; case "$n" in ''|*[!0-9]*) echo "run_all: invalid assertion: $1" >&2; exit 2 ;; esac
      [ "$3" -eq "$n" ] ;;
    'reject_votes>='*)
      n=${1#reject_votes>=}; case "$n" in ''|*[!0-9]*) echo "run_all: invalid assertion: $1" >&2; exit 2 ;; esac
      [ "$4" -ge "$n" ] ;;
    'all_votes=reject')
      [ "$4" -eq "$5" ] ;;
    *) echo "run_all: unknown assertion: $1" >&2; exit 2 ;;
  esac
}

pass=0; fail=0; probe=0; panelfail=0; configerr=0
for c in ${cases[@]+"${cases[@]}"}; do
  name=$(basename "$c")
  expectf="$c/expect"
  # An explicitly requested case without expect is a configuration error, not a skip.
  [ -f "$expectf" ] || { echo "run_all: $c is missing expect; grade=config-error" >&2; configerr=$((configerr + 1)); continue; }
  echo
  echo "########## run_all: $name ($REVIEWERS reviewers) ##########"
  grade=pass; failed=""
  if ! ./calib/run_panel.sh "$c" "$REVIEWERS"; then
    grade=panel-fail
    panelfail=$((panelfail + 1))
  else
    agg_file="tmp/calib/$name/aggregate.tsv"
    [ -s "$agg_file" ] || { echo "run_all: $name panel succeeded without aggregate.tsv" >&2; exit 2; }
    is_probe=0; checks=0
    while IFS= read -r line; do
      line=${line%%#*}
      line=$(printf '%s' "$line" | tr -d '[:space:]')
      [ -z "$line" ] && continue
      [ "$line" = "probe" ] && { is_probe=1; continue; }
      # Evaluate every assertion independently for every ID.
      while IFS=$'\t' read -r id vcsv agg; do
        [ -z "$id" ] && continue
        sa=$(printf '%s' "$vcsv" | tr ',' '\n' | grep -cx 'strong-accept' || true)
        rej=$(printf '%s' "$vcsv" | tr ',' '\n' | grep -cx 'reject' || true)
        total=$(printf '%s' "$vcsv" | tr ',' '\n' | grep -c . || true)
        checks=$((checks + 1))
        if ! check_one "$line" "$agg" "$sa" "$rej" "$total"; then
          failed="${failed:+$failed;}${id}:${line}"
        fi
      done < "$agg_file"
    done < "$expectf"
    if [ "$is_probe" = "1" ]; then
      grade=probe; probe=$((probe + 1))
    elif [ "$checks" -eq 0 ]; then
      # Empty or comment-only expect files are configuration errors.
      grade=config-error; configerr=$((configerr + 1))
      echo "run_all: $name expect has no effective assertions; grade=config-error" >&2
    elif [ -n "$failed" ]; then
      grade=fail; fail=$((fail + 1))
    else
      pass=$((pass + 1))
    fi
  fi
  # Never read aggregate.tsv after panel-fail; an early configuration failure may leave ballots from an earlier run.
  if [ "$grade" = "panel-fail" ]; then
    votes='-'
  else
    votes=$(awk -F'\t' '{printf "%s%s=%s->%s", (NR>1?";":""), $1, $2, $3} END{if(!NR)printf "-"}' "tmp/calib/$name/aggregate.tsv" 2>/dev/null)
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$(date '+%F %T')" "$name" "$REVIEWERS" "$PANEL_CMD" \
    "$grade" "${votes:--}" "${failed:--}" >> "$SUMMARY"
  echo "run_all: $name => $grade${failed:+(failed checks: $failed)}"
done

echo
graded=$((pass + fail))
echo "=== run_all summary: pass=$pass fail=$fail probe=$probe panel-fail=$panelfail config-error=$configerr ==="
if [ "$graded" -gt 0 ]; then
  echo "Calibration accuracy: $pass/$graded (probe and panel-fail excluded; per-case rows in $SUMMARY)"
else
  echo "Calibration accuracy unavailable: no scored cases"
fi
# Green requires no fail, panel-fail, or config-error and at least one completed pass or probe.
[ "$fail" -eq 0 ] && [ "$panelfail" -eq 0 ] && [ "$configerr" -eq 0 ] && [ "$((pass + probe))" -ge 1 ]
