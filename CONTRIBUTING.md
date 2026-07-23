# Contributing

## Change Boundary

Keep runtime producers, parsers, prompts, fixtures, and tests on one artifact contract. Stable machine tokens such as `strong-accept`, `accept-w-rev`, `reject`, overlap values, category values, IDs, and TSV field order require coordinated changes rather than prose-only edits.

`ledger.tsv` is append-only operational history. Preserve row order, historical seven- and eight-column shapes, dates, sources, verdicts, evidence classifications, URLs, numeric claims, and category semantics. [`PROGRAM.md`](PROGRAM.md) is the canonical loop and schema contract.

Backend work must retain explicit provider selection. No default, fallback, hook, test, worker, or orchestration path may start Claude unless the current command explicitly selects it.

## Local Validation

Run focused gates while editing:

```bash
python3 tests/history_store_smoke.py
python3 tests/history_projection_smoke.py
python3 tests/history_budget_smoke.py
python3 tests/history_retrieval_smoke.py
python3 tests/history_retrieval_adversarial.py
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

The command is a closed JSON argv array. The host captures and hashes the
registered role, policy, mounted inputs, executable, fixed argv, and canonical
prompt before launch. A registered Codex capability replaces the CLI harness
with the exact preflighted request, disables tools, and exposes only one
declared loopback port to the contained client. The host broker reads the
canonical Codex authentication file without modifying it; an expired session
fails with `auth_refresh_required` and requires a normal operator login before
retry. Darwin Codex execution requires `sandbox-exec`. Linux fixture
containment requires `bwrap`; Codex fails closed there until a loopback-only
network namespace is registered. Absence or activation failure is fatal. A
preflight receipt is durable before launch, copied artifacts are untrusted
without the completion receipt, and the completion receipt is published only
after every declared output passes no-follow, type, size, schema, and
prompt-attestation checks.

Only `complete_match` and `complete_no_match` receipts permit a permanent
internal-history conclusion. Receipt replay is bound to the policy, projection
generation, source watermark, comparator version, pack hash, evidence IDs, and
the SHA-256 values of the host-owned canonical rank trace and exact comparator
preflight. Pack publications are append-only.

Shell changes also require `bash -n` on every touched script. Litwatch behavior is covered by `bash litwatch_test.sh`; its live-network probe may report an intentional skip when network access is unavailable.

Documentation changes must keep relative links valid and human-readable tracked content free of Han characters. The product hero path is `assets/ai-ideas-hero.png`.

## Branches and Commits

Work on a feature branch or isolated worktree. Configure the repository hook before pushing:

```bash
git config core.hooksPath .githooks
```

Name branches and commits for the product behavior or contract they establish, such as `feat/runtime-contract`, `fix/archive-recovery`, or `docs/operator-guide`. Keep each commit independently reviewable, stage only intended paths, and describe the shipped surface rather than mechanical rewrite activity.

Direct `main` pushes are blocked by the local pre-push hook unless an operator deliberately sets `ALLOW_MAIN_PUSH=1`. Routine generated output remains limited to `ideas/` and `ledger.tsv`; other pull-request paths cause the auto-merge workflow to skip the merge.
