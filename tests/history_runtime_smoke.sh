#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# Keep the protocol-shape check ahead of the slower Python suite.  A legacy
# hunt must fail here instead of looking green because only the library tests
# passed (or looking red because a nested sandbox is unavailable).
for helper in \
  history_startup \
  history_freeze_batch \
  history_observe_round \
  history_seal_selection \
  history_compare_targets \
  history_materialize_research \
  history_seal_resume \
  history_seal_review_plan \
  history_run_review_matrix \
  history_build_aggregation \
  history_commit_round \
  history_materialize_ledger
do
  grep -q "^${helper}()" "$ROOT/hunt.sh" || {
    printf 'history_runtime_smoke: missing %s runtime cutover helper\n' "$helper" >&2
    exit 1
  }
done

if grep -Eq 'META_EVERY|META_MIN_REJECTS|Read roles/meta\.md and follow it' "$ROOT/hunt.sh"; then
  printf 'history_runtime_smoke: routine meta path remains\n' >&2
  exit 1
fi

if grep -Eq 'cp[[:space:]]+(ledger\.tsv|"?\\$LEDGER_GOOD"?)' "$ROOT/hunt.sh"; then
  printf 'history_runtime_smoke: hunt still uses cp-based ledger ownership\n' >&2
  exit 1
fi

# The removed contained-v1 executor must not remain selectable anywhere.
if grep -Eq 'HUNT_RUNTIME_ABI|--reviewer-command|run-stage|run_contained_stage' "$ROOT/hunt.sh"; then
  printf 'history_runtime_smoke: contained-v1 executor remains in hunt.sh\n' >&2
  exit 1
fi
grep -q 'CONTAINED_AGENT_CMD_JSON was removed with the v1 runtime' "$ROOT/hunt.sh" || {
  printf 'history_runtime_smoke: hunt.sh lost the contained-v1 rejection\n' >&2
  exit 1
}
if grep -Eq 'AWR_RUNTIME_ABI|resolve_cmd' "$ROOT/awr-side.sh"; then
  printf 'history_runtime_smoke: contained-v1 executor remains in awr-side.sh\n' >&2
  exit 1
fi
grep -q 'was removed with the v1 runtime' "$ROOT/awr-side.sh" || {
  printf 'history_runtime_smoke: awr-side.sh lost the contained-v1 rejection\n' >&2
  exit 1
}

relative_observation_failed=0
if ! python3 - "$ROOT/lib/history_runtime.py" <<'PY'
import pathlib
import sys

source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
body = source.split("def observe_frozen_batch(", 1)[1].split(
    "def _portable_comparator_runner(", 1
)[0]
if (
    "root = pathlib.Path(artifact_root)" in body
    or '"batch_path": str(pathlib.Path(batch_path))' in body
):
    raise SystemExit(1)
PY
then
  relative_observation_failed=1
fi

if [ "${HISTORY_RUNTIME_SKIP_PYTHON:-0}" != 1 ]; then
  python3 "$ROOT/tests/history_runtime_smoke.py"
fi

[ "$relative_observation_failed" -eq 0 ] || {
  printf 'history_runtime_smoke: relative observation paths cannot be replayed by seal-selection\n' >&2
  exit 1
}

printf 'ok: bounded history runtime contract\n'
