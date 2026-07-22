# Literature Watch Architecture Record

Snapshot: 2026-07-14, preserving the original `agy-worker.sh` annotation path. The active backend contract is owned by `litwatch.sh`.

Literature Watch runs outside the hunt loop and occupies none of the FRONT, BACK, or REV seats. It prefetches recent work across the themes in `research_context.md`, stores an optional local cache, and gives the research stage nearby-paper seeds. Cache absence or refresh failure does not block `hunt.sh` or change its uncached behavior.

The service collects evidence only. It does not issue verdicts, assess novelty, assign overlap, or reject ideas. Those decisions remain with the independent research and review stages under `PROGRAM.md` invariants 4/7.

## 1. Data acquisition

`lib/litwatch.py harvest` uses the Python standard library to query arXiv OAI-PMH at `https://oaipmh.arxiv.org/oai`. It fetches recent records by set, with `cs` as the default, then filters locally by the `cs.RO`, `cs.LG`, `cs.AI`, `cs.CV`, `cs.CL`, and `stat.ML` category allowlist and the configured theme keywords. The deterministic parser is the only source of paper IDs, titles, abstracts, URLs, and dates.

`LITWATCH_SOURCES` retains two per-query alternatives: Semantic Scholar S2, which requires `LITWATCH_S2_KEY`, and the arXiv search API through `_arxiv_search_query`. S2 returned 429 without a key. The arXiv search API returned severe 429 responses and timeouts for specific themes from both Mac and xyh IPs; the stable term-soup query sorted by date produced off-topic results. OAI-PMH replaced that path as the default. Network timeouts and connection failures are skipped cleanly, while retry handling honors `Retry-After`.

## 2. Relevance annotation

In the snapshot, `agy-worker.sh` read deterministic staging records and wrote relevance annotations through `roles/litwatch.md`. The themes came from `litwatch.sh` `default_themes` or `LITWATCH_THEMES_FILE`. Inferring query terms from `ledger.tsv` death reasons was not part of the design.

Annotations may only cite IDs present in staging. `ingest` discards out-of-set IDs, non-string IDs, malformed rows, and duplicates, recording them in `drops.jsonl`. An annotation can therefore select or describe a fetched record but cannot create one.

## 3. Isolation and verification

The annotator reads a staging copy and writes annotations under `tmp/litwatch/agy/`. Trusted `staging.jsonl` remains outside that directory, and `ingest` reads the trusted copy. The resulting index therefore consists only of parsed API records joined to admitted annotations.

The path restriction in the annotator prompt is not an OS sandbox. An untrusted process running under the same user could deliberately reach a parent path and modify `staging.jsonl`. The cache consumer in `roles/research.md` supplies the second boundary: every cached ID is checked live, including at least one live API record and the full five-paper reading floor. A poisoned cache can waste a retrieval pass, but it cannot establish a verdict by itself.

## 4. Cache

The optional cache is `tmp/litwatch/index.jsonl`, grouped by theme with `{id, title, abstract, url, date, theme, agy_note}` records. `litwatch.sh` is best-effort: annotation failure leaves the hunt loop operational and does not make the cache authoritative.

## 5. Launch throttling

Annotation launches passed through `agy-worker.sh`, sharing its directory lock, timestamp, and `AGY_LAUNCH_GAP_SEC` gate with other agy use. This prevented burst launches from triggering login verification.

## 6. Scheduling

A cron or schedule routine may run the service every N hours; a sleeping laptop simply leaves an older optional cache.

## Validation record: 2026-07-14

- OAI-PMH reduced 2600 recent `cs` records to about 83 relevant papers after local category and keyword filtering, with correct theme labels.
- A real agy pass over 24 relevant papers produced 7 useful annotations in about 1 minute. Every ID came from staging, each theme retained at most 5 papers, and ingest recorded 0 drops.
- Earlier arXiv search API input was mostly noise; agy correctly emitted no annotations for that input. The OAI-PMH run established relevance on substantive input.
- S2 without `LITWATCH_S2_KEY` returned 429 and remained disabled by default.
- 429 responses, timeouts, connection errors, and `Retry-After` handling were reproduced and verified.

The implementation surface consists of `litwatch.sh`, `lib/litwatch.py`, prompt files under `roles/`, and the cache-consumption contract in `roles/research.md`. Generated state remains under `tmp/`. The cache is read-only from the hunt loop and never touches `ledger.tsv` or publication state.
