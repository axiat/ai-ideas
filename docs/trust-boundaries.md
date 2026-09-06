# Trust Boundaries

## Filesystem

`hunt.sh` owns the repository host process. The v1 contained runtime
(`HISTORY_RUNTIME_ABI=v1`) was removed; portable-v2 is the only runtime and
setting `HISTORY_RUNTIME_ABI=v2` is equivalent to leaving it unset.

Each internal stage runs as a normal same-user
host process in a disposable portable mirror. The mounted surface contains one
role and declared inputs; it omits `ledger.tsv`, the SQLite database, search
indexes, `.git`, run archives, and unrelated round state. The process retains
the user's host privileges and can access absolute paths, credentials,
configuration, processes, and network resources allowed to that user. The
portable mirror bounds declared inputs and accepted outputs; it is not an OS
or container sandbox.

Selector, prescreen, external research, and report stages still run in disposable repository mirrors with host-validated outputs. Review seats never share sibling output. Prompt instructions, `.claude/settings.json`, `GROK_REPO`, and Codex workspace flags are backend controls, not a host security boundary.

Canonical history writes go only through `lib/history_runtime.py` and `lib/history_store.py`. Backends never own SQLite, `ledger.tsv`, or `tmp/ledger.good`.

Per-run archives default to `$HOME/.ai-ideas-runs/$(basename "$PWD")`, outside the workspace exposed to a workspace-scoped backend. A same-user process can still reach that directory. Setting `RUNS_DIR` inside the repository weakens the separation further.

## Network

The default hunt external commands enable search and workspace network access for selector, prescreen, research, and report. Portable stages use registered command-intent argv and inherit ordinary host network authority. Litwatch fetches OAI records by default and may call its annotation backend. `publish.sh` and `settle.sh` access the Git remote; `publish.sh` also calls GitHub through `gh`.

The frozen-panel default omits retrieval flags; the runner also applies provider-specific controls where available and tells reviewers to use only the supplied evidence. End-to-end calibration enables search and network access. A custom backend remains responsible for enforcing either policy; artifact validation cannot prove network behavior.

## Processes

`tmp/hunt.lock` prevents concurrent main loops in one checkout. `tmp/awr-side.lock` performs the same role for the AwR sidecar. Stale locks are cleared only after the recorded process is absent. These locks do not coordinate other checkouts or isolate subprocesses, credentials, environment variables, network sockets, or same-user processes.

Backend commands execute as local child processes. The runtime resolves a registered executable and closed argv, then launches it directly on the host; the mirror does not restrict same-user process authority. `approval_policy=never` prevents the default Codex commands from pausing for approval; it does not constrain Bash, other providers, or operating-system capabilities.

## Decision and Publication

The host, not a backend, owns minimum-vote aggregation, mechanical gates, SQLite commits, projection materialization, and archive creation. A Strong Accept requires unanimous valid ballots, a complete prior-work block, the paper-read threshold, a substantive falsification experiment, complete reviewer sections, and any form-specific crack evidence.

In shipped `shadow` mode, history receipts are observational for a fixed generated batch and cannot change ledger decisions. In calibrated `enforcement` mode, only complete permanent receipts allow a target to continue; nonpermanent statuses abstain without a ledger row. Internal `complete_no_match` is never academic novelty. Similarity and comparator relations never write lineage or verdict state automatically.

`publish.sh` stages only `ideas/` and `ledger.tsv`, commits on `hunt/<date>` or `weekly/<date>`, pushes to `origin`, and creates or repairs a pull request. It requires repository write access and authenticated `gh`; it is not a local-only operation. Publication failure can leave a daily branch, commit, push, or pull request partially completed for the next idempotent run to repair.

After `core.hooksPath=.githooks` is configured, the local pre-push hook blocks direct pushes to `main` unless `ALLOW_MAIN_PUSH=1` is set. A local hook is bypassable and does not replace remote policy.

## Calibration and enforcement authority

Production enforcement requires a sealed capability that binds policy commitment, pre-held-out receipt, benchmark snapshot, qrels, adjudications, held-out counts, and a trusted-runner witness signature. Synthetic fixtures under `calib/history-retrieval/` are `synthetic_contract_only` and cannot enable production enforcement. The repository ships no production capability or trust root; the committed policy remains `shadow`.

## CI

`.github/workflows/auto-merge-routine.yml` runs only for pull requests whose head repository matches the target repository. It lists changed files and skips auto-merge when any path is outside `ideas/` and `ledger.tsv`. For an allowed path set it attempts `gh pr merge --squash --delete-branch` up to five times.

The workflow attempts a merge with its granted token. It does not establish branch protection, required reviews, status-check policy, fork isolation beyond the head-repository condition, or protection against a compromised workflow or repository credential.
