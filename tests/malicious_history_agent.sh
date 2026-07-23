#!/usr/bin/env bash
set -euo pipefail

[ "$#" -eq 1 ] || {
  printf 'usage: malicious_history_agent.sh <canonical-prompt>\n' >&2
  exit 64
}

script_dir=${BASH_SOURCE[0]%/*}
exec /Applications/Xcode.app/Contents/Developer/usr/bin/python3 \
  "$script_dir/malicious_history_agent.py" "$1"
