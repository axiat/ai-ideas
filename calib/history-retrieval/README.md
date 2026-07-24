# History-Retrieval Calibration Contract

The synthetic fixture validates schemas, temporal isolation, seal bindings, and metric code. It is `synthetic_contract_only`: its scores are not retrieval-quality evidence and cannot enable enforcement.

Production calibration requires temporal corpus snapshots, two independent human judgments per pooled query-record pair, a third adjudication for every disagreement, and at least 30 independent held-out positives plus 30 independent held-out hard negatives for each automated relation. Unjudged pairs remain unjudged.

The policy commitment is sealed from training and calibration data before held-out execution. It binds the policy, split, query-ID sets, benchmark inputs, selected thresholds, error budgets, depths, latency target, and token budget. It contains no held-out labels, adjudications, outputs, or metrics.

A trusted runner allocates a monotonic nonce and signs the pre-held-out receipt under its journal lock before opening held-out labels or an output path. Every output row binds the commitment SHA, receipt SHA, nonce, and held-out start time. A local timestamp or bare hash is not a production witness.

The capability binds the exact commitment, receipt, policy, temporal snapshot, qrels, adjudications, held-out outputs, relation counts, and canonical seal. Production validation requires an external or trusted-runner witness verifier. The committed test key is public and valid only for offline synthetic tests. This repository ships no production trust root or production capability, so `history/retrieval-policy-v1.json` remains in `shadow` mode.

The four arms isolate retrieval ranking, oracle-pack comparison, end-to-end behavior, and closed-book comparison. Metrics include bounded ranking quality, relation classification, false duplicate and internal no-match rates, abstention, evidence support, latency, tokens, comparator pairs, stable-cutoff ties, and a fixed-seed paired bootstrap.
