# Trust Boundaries

## Filesystem

`hunt.sh` runs serial stages in the repository and checks new tracked, untracked, and committed changes after each stage. Only `ledger.tsv` is allowed during ordinary stages; the report stage also allows `ideas/`. Newly changed out-of-scope tracked files are restored when possible, committed out-of-scope changes abort the run, and unresolved untracked files abort the run. Pre-existing dirty paths are excluded from this stage-delta check.

Review seats receive separate copies of `ideas.md` and `priorwork.md`, but their processes still run from the repository. The copies prevent shared ballot inputs; they do not create an operating-system sandbox.

AwR and calibration use disposable repository mirrors, copy back only expected artifact paths, and accept them only after structural validation. A backend with the same user identity can still access paths outside a mirror unless its own sandbox or container prevents it. Prompt instructions, `.claude/settings.json`, `GROK_REPO`, and Codex workspace flags are backend controls, not a host security boundary.

Per-run archives default to `$HOME/.ai-ideas-runs/$(basename "$PWD")`, outside the workspace exposed to a workspace-scoped backend. A same-user process can still reach that directory. Setting `RUNS_DIR` inside the repository weakens the separation further.

## Network

The default hunt and AwR Codex commands enable search and workspace network access. Litwatch fetches OAI records by default and may call its annotation backend. `publish.sh` and `settle.sh` access the Git remote; `publish.sh` also calls GitHub through `gh`.

The frozen-panel default omits retrieval flags; the runner also applies provider-specific controls where available and tells reviewers to use only the supplied evidence. End-to-end calibration enables search and network access. A custom backend remains responsible for enforcing either policy; artifact validation cannot prove network behavior.

## Processes

`tmp/hunt.lock` prevents concurrent main loops in one checkout. `tmp/awr-side.lock` performs the same role for the AwR sidecar. Stale locks are cleared only after the recorded process is absent. These locks do not coordinate other checkouts or isolate subprocesses, credentials, environment variables, network sockets, or same-user processes.

Backend commands execute as local child processes. `approval_policy=never` prevents the default Codex commands from pausing for approval; it does not constrain Bash, other providers, or operating-system capabilities.

## Decision and Publication

Bash, not a backend, owns minimum-vote aggregation, mechanical gates, ledger mutation, and archive creation. A Strong Accept requires unanimous valid ballots, a complete prior-work block, the paper-read threshold, a substantive falsification experiment, complete reviewer sections, and any form-specific crack evidence.

`publish.sh` stages only `ideas/` and `ledger.tsv`, commits on `hunt/<date>` or `weekly/<date>`, pushes to `origin`, and creates or repairs a pull request. It requires repository write access and authenticated `gh`; it is not a local-only operation. Publication failure can leave a daily branch, commit, push, or pull request partially completed for the next idempotent run to repair.

The local pre-push hook blocks direct pushes to `main` unless `ALLOW_MAIN_PUSH=1` is set. A local hook is bypassable and does not replace remote policy.

## CI

`.github/workflows/auto-merge-routine.yml` runs only for pull requests whose head repository matches the target repository. It lists changed files and skips auto-merge when any path is outside `ideas/` and `ledger.tsv`. For an allowed path set it attempts `gh pr merge --squash --delete-branch` up to five times.

The workflow attempts a merge with its granted token. It does not establish branch protection, required reviews, status-check policy, fork isolation beyond the head-repository condition, or protection against a compromised workflow or repository credential.
