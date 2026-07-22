# shellcheck shell=bash
# Shared mirror-boundary preamble for calibration judges and AwR agents.
# Keeping the home-directory restrictions here prevents the two paths from
# drifting. This file is sourced, not executed.
#
# Usage: mirror_pre <absolute-mirror> <allowed-write-target> [extra-denials]
# Prints a prompt preamble; $HOME and ~ remain literal for the backend.
mirror_pre() {
  local extra=""
  [ -n "${3:-}" ] && extra="Never write to $3; "
  printf 'Repository root (absolute path): %s. The current working directory is under this root; resolve every read and write path relative to it. The real repository is not the working directory. This task may write only %s. Never write elsewhere. %sNever write to ~/.gemini, ~/.claude, ~/.codex, ~/.grok, any scratch directory, or any other location under $HOME.' "$1" "$2" "$extra"
}
