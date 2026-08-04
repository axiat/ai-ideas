# Scalable History Runtime Final Evidence

Audited implementation HEAD: `100f252a4dd5ab5e3f4978ca06507c5e22f4e117`

Independent verification:

- [Independent test](scalable-history-runtime-independent-test.md): **PASS**
- [Independent whole-branch code audit](scalable-history-runtime-independent-code-audit.md): **PASS**

Both reviews are fixed to the same implementation HEAD. Verification used host logic and fake providers; no real model workload or Claude invocation occurred.

## Fixed-HEAD gates

| Gate | Result |
| --- | --- |
| Provider adapters, model authority, portable boundary, stage runtime | 78/78 passed |
| Pre-call catalog drift | Rejected before render, `Popen`, or state-root creation |
| Catalog drift during input copy | Second authority probe rejected before `Popen`; attempt state was cleaned |
| History runtime | 102/102 passed |
| Portable Hunt/AwR fake E2E and v1/v2 ABI | Passed |
| Product contract | `ok: all` |
| OpenSpec strict validation | Passed |
| Python compile and shell syntax | 71 Python files and 23 shell files passed |

The complete baseline, migration, recovery, router, semantic-release, retrieval, and static-policy matrices are recorded in the two independent reports. Litwatch reported 14 passed, 0 failed, and one network-dependent smoke skipped; simulated retry cases passed.

## Production release boundary

```text
real provider capacity qualified = false
production qrels qualified       = false
production complete_no_match     = VETO
currency price accuracy          = unclaimed
portable mirror OS containment   = unclaimed
```

`history/capacity-profiles-v1.json` keeps `safe-24k-v1` and every registered real provider (`codex`, `kimi`, `grok`, `opencode`, `agy`) in `unbudgetable`. Only `fake-safe-24k-v1` has `hard-complete-test-only` status.

`history/production-evidence-roots-v1.json` has empty `fault_reports`, `replay_reports`, and `semantic_evaluation_reports`. No current real evidence root can authorize a production no-match release.

Focused release-gate verification:

```sh
python3 tests/history_audit_plan_authority_smoke.py \
  HistoryAuditPlanAuthoritySmoke.test_registered_real_capacity_remains_unbudgetable

python3 tests/history_audit_eval_smoke.py \
  QrelsAndReleaseTests.test_repository_synthetic_qrels_cannot_self_label_real_and_mint \
  QrelsAndReleaseTests.test_forged_qualified_boolean_cannot_mint_complete_no_match \
  StorageReleaseAuthorizationTests.test_direct_complete_no_match_insert_requires_durable_authorization
```

Results: 1/1 and 3/3 passed. Synthetic qrels, a forged qualification boolean, and a direct storage insert cannot mint production `complete_no_match`.
