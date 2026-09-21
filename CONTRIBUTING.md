# Contributing

## Change Boundary

Keep runtime producers, parsers, prompts, fixtures, and tests on one artifact contract. Stable machine tokens such as `strong-accept`, `accept-w-rev`, `reject`, overlap values, category values, IDs, and TSV field order require coordinated changes rather than prose-only edits.

`.ai-ideas/history.sqlite3` is canonical operational history. `ledger.tsv` and
`tmp/ledger.good` are replayable projections of one immutable database
snapshot. Preserve row order, historical seven- and eight-column shapes,
dates, sources, verdicts, evidence classifications, URLs, numeric claims, and
category semantics. [`PROGRAM.md`](PROGRAM.md) is the canonical loop and
schema contract.

Backend work must retain explicit provider selection. No default, fallback,
hook, test, worker, or orchestration path may start any provider through an
unselected shell fallback such as `${CMD:-provider ...}`. Explicit selection
(`HUNT_PROVIDER=claude`, `AWR_PROVIDER=claude`, `./claude-worker.sh`) is valid.

## Local Validation

Run focused gates while editing:

```bash
python3 tests/history_store_smoke.py
python3 tests/history_store_export_slice_smoke.py
python3 tests/history_project_registry_smoke.py
python3 tests/history_projection_smoke.py
python3 tests/history_budget_smoke.py
python3 tests/history_retrieval_smoke.py
python3 tests/history_retrieval_adversarial.py
python3 tests/direction_contract_smoke.py
python3 tests/history_runtime_smoke.py
bash tests/history_runtime_smoke.sh
bash tests/hunt_project_mode_regression.sh
python3 tests/verify_product_contract.py runtime
python3 tests/verify_product_contract.py fixtures
bash tests/runtime_abi_smoke.sh
bash tests/calibration_abi_smoke.sh
```

The smoke tests use fake backends and do not require an external model. Run the complete content contract before integration:

```bash
python3 tests/verify_product_contract.py all
git diff --check
```

Recover both ledger projections from the canonical database before an offline run:

```bash
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 reconcile-ledger
```

Fresh bootstrap treats `ledger.tsv` as the operator baseline and does not read
the legacy near-SA queue by default. Import a validated snapshot explicitly:

```bash
HISTORY_NEAR_SA=tmp/near-sa-queue.tsv ./hunt.sh
```

The bootstrap transaction rejects a missing, symlinked, special, ambiguous, or
semantically mismatched queue before any agent starts. A stale queue whose
stories no longer resolve against the current ledger must remain untouched;
retry with `HISTORY_NEAR_SA` unset to migrate the ledger alone. The database
then owns canonical near-SA observations, and later startup validates the
sealed bootstrap provenance rather than rereading the legacy file.

Build and resolve a bounded internal-history comparison from JSON artifacts:

```bash
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 retrieve \
  --query tmp/candidate.json --intent duplicate_search \
  --comparator-role tmp/history-compare-role.md \
  --comparator-role-identity roles/history-compare.md \
  --output tmp/retrieval_pack.json
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 finalize-comparison \
  --pack tmp/retrieval_pack.json --comparison tmp/history-comparison.json \
  --output tmp/history_receipt.json
python3 lib/history_cli.py --db .ai-ideas/history.sqlite3 replay-receipt \
  --pack tmp/retrieval_pack.json --receipt tmp/history_receipt.json
```

`hunt.sh` runs generation, internal comparison, and every review seat
through the portable-v2 runtime: registered provider request profiles built
by `lib/history_audit_cli.py provider-command` and executed by
`lib/portable_stage.py`. Selector, prescreen, external prior-work
research, and report assembly run from disposable mirrors and return only
their declared bounded artifacts.

The canonical stage roles are `roles/generate.md`, `roles/meta.md`, and
`roles/review.md`. Routine hunt rounds do not invoke the optional meta stage;
structured failure counts enter generation through the database-backed brief.

Shadow mode is the default and never mounts internal-history evidence into
research or review. Enforcement requires both
`HISTORY_CALIBRATION_CAPABILITY` and
`HISTORY_PRODUCTION_TRUST_ROOT`; production entrypoints reject synthetic test
authorities and repository fixture backends. Nonpermanent enforcement
statuses remain sealed abstentions and create no research task or ledger row.
`materialize-research` is the sole producer of the external research
`ideas.tsv`/`ideas.md` view and its eligible enforcement summaries.

Only `complete_match` and `complete_no_match` receipts permit a permanent
internal-history conclusion. Receipt replay is bound to the policy, projection
generation, source watermark, comparator version, pack hash, evidence IDs, and
the SHA-256 values of the host-owned canonical rank trace and exact comparator
preflight. Pack publications are append-only.

Shell changes also require `bash -n` on every touched script. `hunt.sh` must
not append `ledger.tsv`, copy either TSV projection over the other, or add a
test-mode production escape. Litwatch behavior is covered by
`bash litwatch_test.sh`; its live-network probe may report an intentional skip
when network access is unavailable.

Documentation changes must keep relative links valid. Tracked text may contain only sparse Han, such as historical artifact tokens; dense Han still fails the product contract. The product hero path is `assets/ai-ideas-hero.png`.

## Branches and Commits

Work on a feature branch or isolated worktree. Configure the repository hook before pushing:

```bash
git config core.hooksPath .githooks
```

Name branches and commits for the product behavior or contract they establish, such as `feat/runtime-contract`, `fix/archive-recovery`, or `docs/operator-guide`. Keep each commit independently reviewable, stage only intended paths, and describe the shipped surface rather than mechanical rewrite activity.

Direct `main` pushes are blocked by the local pre-push hook unless an operator deliberately sets `ALLOW_MAIN_PUSH=1`. Routine generated output remains limited to `ideas/` and `ledger.tsv`; other pull-request paths cause the auto-merge workflow to skip the merge.
