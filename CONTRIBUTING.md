# Contributing

## Change Boundary

Keep runtime producers, parsers, prompts, fixtures, and tests on one artifact contract. Stable machine tokens such as `strong-accept`, `accept-w-rev`, `reject`, overlap values, category values, IDs, and TSV field order require coordinated changes rather than prose-only edits.

`ledger.tsv` is append-only operational history. Preserve row order, historical seven- and eight-column shapes, dates, sources, verdicts, evidence classifications, URLs, numeric claims, and category semantics. [`PROGRAM.md`](PROGRAM.md) is the canonical loop and schema contract.

Backend work must retain explicit provider selection. No default, fallback, hook, test, worker, or orchestration path may start Claude unless the current command explicitly selects it.

## Local Validation

Run focused gates while editing:

```bash
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

Shell changes also require `bash -n` on every touched script. Litwatch behavior is covered by `bash litwatch_test.sh`; its live-network probe may report an intentional skip when network access is unavailable.

Documentation changes must keep relative links valid and human-readable tracked content free of Han characters. The product hero path is `assets/ai-ideas-hero.png`.

## Branches and Commits

Work on a feature branch or isolated worktree. Configure the repository hook before pushing:

```bash
git config core.hooksPath .githooks
```

Name branches and commits for the product behavior or contract they establish, such as `feat/runtime-contract`, `fix/archive-recovery`, or `docs/operator-guide`. Keep each commit independently reviewable, stage only intended paths, and describe the shipped surface rather than mechanical rewrite activity.

Direct `main` pushes are blocked by the local pre-push hook unless an operator deliberately sets `ALLOW_MAIN_PUSH=1`. Routine generated output remains limited to `ideas/` and `ledger.tsv`; other pull-request paths cause the auto-merge workflow to skip the merge.
