#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

[ -f "$ROOT/lib/history_stage.py" ] || {
  printf 'history_mirror_smoke: missing lib/history_stage.py\n' >&2
  exit 1
}

python3 "$ROOT/tests/history_stage_smoke.py"
