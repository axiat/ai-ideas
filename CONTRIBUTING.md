# Contributing

## Change Boundary

Keep runtime producers, parsers, prompts, fixtures, and tests on one artifact contract. Stable machine tokens such as `strong-accept`, `accept-w-rev`, `reject`, overlap values, category values, IDs, and TSV field order require coordinated changes rather than prose-only edits.

`.ai-ideas/history.sqlite3` is canonical operational history. `ledger.tsv` and
`tmp/ledger.good` are replayable projections of one immutable database
snapshot. Preserve row order, historical seven- and eight-column shapes,
dates, sources, verdicts, evidence classifications, URLs, numeric claims, and
category semantics. [`PROGRAM.md`](PROGRAM.md) is the canonical loop and
schema contract.

Backend work must retain explicit provider selection. No default, fallback, hook, test, worker, or orchestration path may start Claude unless the current command explicitly selects it.

## Local Validation

Run focused gates while editing:

```bash
python3 tests/history_store_smoke.py
python3 tests/history_projection_smoke.py
python3 tests/history_budget_smoke.py
python3 tests/history_retrieval_smoke.py
python3 tests/history_retrieval_adversarial.py
python3 tests/direction_contract_smoke.py
python3 tests/history_runtime_smoke.py
bash tests/history_runtime_smoke.sh
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

Generation, internal comparison, review, and bounded failure distillation use
the same contained stage ABI:

```bash
python3 lib/history_stage.py run \
  --stage generate \
  --manifest /absolute/run/generate-manifest.json \
  --command '["/absolute/path/to/codex","-m","gpt-5.3-codex-spark","-c","model_reasoning_effort=xhigh"]'
```

The `xhigh` command is the registered target configuration, not an online
availability check. It runs only when the local platform, Codex `0.146.x`
CLI version family, model, reasoning setting, adapter, canonicalizer, response
schemas, policy bounds, owner-only authentication file, and Darwin
`sandbox-exec` profile match the audited capability. Any drift fails before
backend launch. Linux fixture containment requires `bwrap`; Codex fails closed
there until a loopback-only network namespace is registered.

Upgrading the contained Codex CLI: adapt
`lib/history_stage_proxy.py` to the new wire shape (normalize volatile
CLI-assigned fields such as message `id`s rather than loosening the exact
preflight comparison), bump `CODEX_CLI_VERSION` in `lib/history_stage.py`,
then re-register the capability in
`history/codex-adapter-capabilities-v2.json` by recomputing
`_codex_profile_bytes` per stage identity with the new version family and
appending the entry. Finish by running the installed-binary loopback tests
in `tests/history_stage_proxy_smoke.py`. Version detection that succeeds
never falls back to the static pin; an unregistered minor family fails
closed.

The command is a closed JSON argv array. The host captures and hashes the
registered role, policy, mounted inputs, executable, fixed argv, and canonical
prompt before launch. The adapter applies fixed CPU, address-space, file-size,
descriptor, process, and core-dump limits before backend `exec`. The pinned
proxy disables tools, exposes one declared loopback port, enforces one absolute
upstream deadline, and cancels active sockets on shutdown. The host broker
reads the canonical Codex authentication file without modifying it; an expired
session fails with `auth_refresh_required` and requires a normal operator login
before retry. A preflight receipt is durable before launch, copied artifacts
are untrusted without the completion receipt, and the completion receipt is
published only after every declared output passes no-follow, type, size,
schema, and prompt-attestation checks.

`hunt.sh` consumes the same five-argument registered Codex prefix through
`CONTAINED_AGENT_CMD_JSON`. The stage adapter owns the fixed noninteractive
`exec` tail, network-disabled mirror, response schema, output paths, and
completion receipt. Per-seat overrides use
`CONTAINED_REV_CMD_<N>_JSON`. Selector, prescreen, external prior-work
research, and report assembly run from disposable mirrors and return only
their declared bounded artifacts.

The canonical contained roles are `roles/generate.md`, `roles/meta.md`, and
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

Documentation changes must keep relative links valid and human-readable tracked content free of Han characters. The product hero path is `assets/ai-ideas-hero.png`.

## Branches and Commits

Work on a feature branch or isolated worktree. Configure the repository hook before pushing:

```bash
git config core.hooksPath .githooks
```

Name branches and commits for the product behavior or contract they establish, such as `feat/runtime-contract`, `fix/archive-recovery`, or `docs/operator-guide`. Keep each commit independently reviewable, stage only intended paths, and describe the shipped surface rather than mechanical rewrite activity.

Direct `main` pushes are blocked by the local pre-push hook unless an operator deliberately sets `ALLOW_MAIN_PUSH=1`. Routine generated output remains limited to `ideas/` and `ledger.tsv`; other pull-request paths cause the auto-merge workflow to skip the merge.
