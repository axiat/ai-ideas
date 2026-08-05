# Agy Catalog Probe Reliability

## Problem

AwR v2 validates the base provider and the research, prior-work, and judge roles before queue mutation. When every role inherits the same agy configuration, startup launches four identical `agy models` probes. Each probe currently has a 15-second limit.

Live agy 1.1.10 measurements reproduced the failure: one identical probe completed in 14.583 seconds, and the next reached 15.157 seconds and returned `provider model catalog is unavailable`. A preceding manual `agy models` call cannot help because AwR starts fresh probes and does not consume that output.

## Considered Approaches

1. Increase the catalog timeout only. This removes the observed 15-second cliff but retains four redundant network-backed startup probes.
2. Retry every unavailable catalog. This multiplies latency and can hide non-timeout failures such as nonzero exit, stderr, malformed output, or duplicate model entries.
3. Increase the timeout and deduplicate identical startup diagnostics. This addresses both observed causes while preserving fresh execution checks. This is the selected approach.

## Runtime Contract

- Increase the bounded model-catalog probe timeout from 15 to 30 seconds.
- During `awr_runtime_preflight`, validate each distinct `(provider, model, reasoning)` tuple once. The base tuple is the first entry; inherited role tuples reuse that successful diagnostic. A role override with any different value receives its own diagnostic.
- Deduplication applies only to the early, no-launch startup validation. It does not reuse provider-profile files, catalog evidence for a later attempt, executable identity, or launch authorization.
- `awr_write_role_profile` continues to resolve current catalog evidence for each stage. Portable launch continues to revalidate model authority immediately before `subprocess.Popen`.
- Catalog timeout remains fail-closed at 30 seconds. Nonzero exit, stderr, empty or noncanonical output, invalid UTF-8, duplicates, excessive output, and missing requested models remain immediate failures. There is no automatic retry.
- V1 command behavior and every non-agy provider grammar remain unchanged.

## Verification

- A provider-catalog test must prove that the host passes the 30-second bound to the agy catalog subprocess.
- An AwR ABI test must prove that identical inherited role tuples cause one startup diagnostic, while a distinct role override causes one additional diagnostic.
- Existing catalog drift, malformed catalog, forbidden route, provider grammar, v1/v2 separation, portable runtime ABI, and fake Hunt/AwR end-to-end tests must remain green.
- A live no-workload check may run `agy models` and `provider-command`; it must not execute `awr-side.sh` beyond preflight or start a model request.
