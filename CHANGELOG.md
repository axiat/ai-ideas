# CHANGELOG

## 2026-07-19 Calibration: embodied-domain axiom-removal positive (`pos-axiom-torque`, 3/3 SA)

- Added `calib/cases/pos-axiom-torque`, a pre-publication reconstruction of the RSS 2026 Outstanding Systems Paper NeuralActuator (arXiv 2607.11734, post-cutoff). The candidate removes the assumption that actuator learning requires ground-truth torque supervision. Nine neighbors were verified through the arXiv API; an 18+8-paper window scan found no direct hit; 2/2 crack-evidence items matched on full reading; and the force-aware axis occupant, analytical DOB 2507.06174, was recorded. The panel returned 3/3 SA, so the `pos-axiom-*` interpretation table marks the channel operational. Ballots are recorded in `calib/results-2026-07-19.md`.
- Added `pos-axiom-torque` to `calib/README.md` and changed `pos-axiom-*` from unselected to established. The RSS 2026 official award page still returned 2025 content on 07-19; the result was cross-checked through Instagram and the arXiv identifier.

## 2026-07-19 Harness: incremental SA payoff and report evidence boundaries

- `brainstorming_policy.md` and `rubric.md`: Strong Accept counts only new, attributable payoff over the nearest payoff occupant, or over the strongest current baseline for the same metric and setting when a bounded search finds no occupant. Published or already-realized gains cannot be counted again. Protocol change; pending PR review.
- `roles/research.md` and `roles/review.md`: prior-work analysis must name the payoff occupant or document a bounded zero-hit result. A published anomaly enters scores, flaws, verdicts, the Integrity gate, or actions only when the exact arm, setting, and single-variable estimand align. Protocol change; pending PR review.
- `roles/report.md` and `ideas/2026-07-17_hunt.md`: reports may state only the panel-wide unanimous verdict. Reviewer 1 text and prior work are copied contiguously for the accepted ID; the report does not merge CRITICAL or MAJOR findings across seats or recompute literature comparisons. The 07-17 report now carries the A2World correction, `+1.2` and `+2.7`, at the top. Frozen historical ballots were not rewritten. Re-running the report alone from the old archive still reproduces its frozen `1.6%` sentence; the published correction is authoritative.
- `ledger.tsv`: with explicit authorization, the official 07-17 I3 ledger row was corrected once from `strong-accept` to `accept-w-rev`. Its reason is the sentence returned by three fresh ballots over byte-identical frozen input, and its category is `design-fixable` under the current aggregation rule. Frozen verdict and review artifacts remain unchanged; later `PROGRAM.md` rules remain append-only.
- Validation reran the frozen 07-17 I3 archive independently. All three fresh ballots returned Accept-w-Rev and none repeated the invalid anomaly-derived value. The formal suite reported 4 pass + 1 probe and 0 fail. Both checks used sandboxed Codex. The default Claude backend timed out before producing valid final ballots, so its partial output was not mixed into the result.

## 2026-07-15 AwR independent prior-work seat

Using agy for both research and judgment had allowed overly broad approvals because the judge treated the draft's self-reported retrieval section as novelty evidence. AwR now follows the hunt invariant that novelty depends on independent prior-work analysis: each round has separate research, prior-work, and judgment seats.

- Added `roles/awr-priorwork.md`. It reads only the claims under `## Revised Idea`, derives search terms, and produces factual prior work with 5-8 papers, a reproducible API query, `Strongest Counterexample:`, and `Crack Evidence Verification`. It cannot trust or copy retrieval claims supplied by the draft. Protocol addition; pending PR review.
- `roles/awr-judge.md`: novelty depends only on `priorwork.md`; the judge no longer searches. Targeted retrieval and crack verification use that file, while the minimal falsification experiment remains a draft check. Missing or thin prior work forces `Decision: not-ready`. Protocol change; pending PR review.
- `awr-side.sh`: inserted a prior-work gate before judgment. `SIDE_PRIORWORK_CMD` falls back to `SIDE_JUDGE_CMD`, keeping evidence and judgment at the same trust tier. `<key>.priorwork.md` is reusable only when newer than the draft, which covers crash and judgment retries but forces a new search after a rewritten draft. `check_priorwork` requires at least 5 linked neighbors, a URL query line, `Strongest Counterexample:`, and final `AGY-DONE`; failures become `.priorwork.badN` and count toward the existing `$key.*.bad*` circuit breaker. Final artifacts include independent prior-work evidence.
- The recommended split changed from agy research plus Claude judgment to agy research plus Claude prior-work and judgment. An all-agy setup still uses separate search and judgment invocations.
- Validation: `bash -n` passed; `check_priorwork` passed 6/6 cases covering valid output, too few neighbors, missing query, missing strongest counterexample, missing `AGY-DONE`, and empty output. Fake-agent end-to-end checks preserved research < prior-work < judgment ordering on the SA path. With `max_rounds=2` and repeated not-ready outcomes, the system ran 2 searches and 2 judgments, returned final not-ready status, and fed back both rounds without regressing `check_draft` or `check_judge`. No live agy run was performed.

## 2026-07-14 Literature Watch foreground loop (`LITWATCH_LOOP_SEC`)

- `litwatch.sh`: moved stages 1/2/3 into `one_pass()`. `LITWATCH_LOOP_SEC=0`, the default, performs one pass and exits; values `>0` sleep that many seconds between passes until Ctrl-C. OAI-PMH acquisition uses about 8 requests per pass with a 3s page interval. Recent-work metadata changes daily, so multi-hour intervals are sufficient. Ingest failure changed from `exit 1` to `return 1`, preserving the loop.
- `README.md`: added `caffeinate -is env LITWATCH_LOOP_SEC=21600 ./litwatch.sh` for a 6h foreground refresh that prevents sleep.
- Validation: `bash -n` and `py_compile` passed; `litwatch_test.sh` passed 15/15, including one-shot T7/T8 after the `one_pass()` refactor. A live loop completed 3 passes at a 2s interval and shut down cleanly.

## 2026-07-14 Literature Watch OAI-PMH acquisition

Theme-specific queries against the arXiv search API produced severe 429 responses and timeouts from both Mac and xyh IPs. The only stable term-soup query sorted by date returned off-topic noise. OAI-PMH batch acquisition with local filtering replaced it as the default; the S2 path remains available.

- `lib/litwatch.py`: added `parse_oai`, `harvest_oai`, `load_themes`, `filter_tag`, and the `harvest` subcommand. It reads OAI-PMH from `oaipmh.arxiv.org/oai`; `export.arxiv.org/oai2` redirects there with 301. Records are fetched by set and date range with bounded `resumptionToken` pagination, filtered through the category allowlist and inline `|` keyword tags, and deduplicated keep-first. Only parsed OAI responses can create records.
- `litwatch.sh`: defaulted `LITWATCH_SOURCES=oai`. Stage 1 runs one harvest for `oai` and per-query acquisition for `s2` or `arxiv`. Added `LITWATCH_OAI_DAYS`, `LITWATCH_OAI_SETS`, `LITWATCH_OAI_MAXPAGES`, and `LITWATCH_OAI_CATS`, with separate OAI and query theme defaults. `LITWATCH_S2_KEY` and `LITWATCH_SORT` remain supported.
- Live validation on 2026-07-14 fetched 2600 `cs` records on Mac without OAI rate limiting and filtered them to 83 relevant papers with correct themes. A real agy pass over 24 papers produced 7 useful annotations, at most 5 per theme, all from staging, with 0 ingest drops. This established that agy worked on substantive input.
- `py_compile`, `bash -n`, and `litwatch_test.sh` 15/15 passed. T14 covers `parse_oai`; T15 covers `filter_tag`; the suite also covers the trust boundary, zero-regression behavior, backoff, and deduplication.

## 2026-07-14 Literature Watch service foundation

`LITWATCH-DRAFT.md` established Literature Watch as a resident process outside the hunt loop. It uses spare agy capacity to prefetch recent domain papers into an optional cache used only as nearest-neighbor seeds. agy was deliberately excluded from the main loop because its failure must neither delay nor contaminate verdict formation. Two independent adversarial test reviews were completed.

- Added `lib/litwatch.py` as the deterministic acquisition and admission core. `fetch` queries arXiv or Semantic Scholar APIs, `parse` handles local responses for offline tests, and `ingest` joins annotations only when their ID exactly exists in staging. Out-of-set IDs, non-string IDs, malformed rows, and duplicates are dropped to `drops.jsonl`. Empty or malformed input returns `[]` without a traceback. `_norm_arxiv_id` retains legacy category prefixes such as `cs/...`, which are required for reachable URLs.
- Added `litwatch.sh` with acquisition, annotation, and admission stages. It is best-effort: annotation failure still lets the deterministic core produce the index. agy reads a staging copy and writes annotations under `tmp/litwatch/agy/`; `ingest` reads trusted `staging.jsonl` outside that directory, so forged records created inside the annotation directory cannot enter the index.
- Added `roles/litwatch.md`. The annotator may write only under `tmp/litwatch/agy/`, may select only IDs present in staging, and may assess relevance only. It cannot judge novelty, overlap, or verdict.
- `agy-worker.sh`: changed the fixed output location to `${AGY_OUT_HINT:-tmp/round/}`. Literature Watch reuses the directory lock and `AGY_LAUNCH_GAP_SEC` gate while directing output to its annotation directory. Behavior is byte-identical when `AGY_OUT_HINT` is unset.
- `roles/research.md`: added the cache-consumption contract. Cached entries accelerate cold-start retrieval but never reduce the live API, full-reading, or verification requirements. Cache absence preserves uncached behavior. Protocol change; pending PR review.
- `LITWATCH-DRAFT.md`: aligned acquisition with Python stdlib rather than a literal curl path and described the safety property as structural isolation plus independent live verification, not an absolute structural guarantee. Inferring themes from `ledger.tsv` death reasons remained outside this version.
- Boundary: the path rule in the agy prompt is not an OS sandbox. A deliberately hostile same-user process could reach a parent path and modify `staging.jsonl`. `roles/research.md` therefore rechecks every cache ID live and retains every structure gate; a poisoned cache can waste one search but cannot establish an incorrect verdict.
- Validation: `bash -n`, `py_compile`, and `litwatch_test.sh` 12/12 passed, including offline units, orchestration, one live arXiv smoke, and `OUT_HINT` placement. The first adversarial review found that type-invalid annotations crashed ingest, violating zero regression; T9/T10 locked the fix. The second found an injection path when staging shared the agy write directory; separation plus T12 locked the fix. The final review passed.
- The initial live check on 2026-07-14 saw burst rate limits, empty responses, and timeouts from arXiv; `fetch` skipped them without traceback. S2 returned 429 without a key, so it remained disabled by default and gained `LITWATCH_S2_KEY`. A real agy dry run completed in about 46s without a login stall, read sandbox staging, emitted a clean empty annotation set under the uncertain-input rule, and left the index valid. At this point relevance was still unproven because the default loose `all:` OR query plus date sorting was noisy. The later OAI-PMH entry above closed that evidence gap.

## 2026-07-12 Review aggregation hardening: B2 missing votes and B3 MAJOR cap

The P1 re-review confirmed two aggregation gaps in `hunt.sh`. B2 enforces the `roles/review.md` requirement that every ID has one valid row. B3 mechanically enforces the rule that at least 2 MAJOR findings cap a ballot at Accept-w-Rev. No protocol or role file changed.

- B2: the old `rank_of $(cut -f2)` mapped missing and out-of-vocabulary votes to rank 0, indistinguishable from a real `reject`, so `min=0` could enter `novelty-dead` and permanently suppress a candidate. Added `vote_valid`, requiring exact membership in `{strong-accept,accept-w-rev,reject}` for every ID and seat before ledger mutation. Any invalid vote sends the full round through `fail_and_wait` as a review failure and logs `I<n>@rev<r>[vote]`. Real reject ballots remain valid.
- B3: the third `verdict.tsv` field, MAJOR count, had not affected aggregation. Added `major_cap`: rank 2 with a reported integer at least 2 becomes rank 1 before vote vectors, `sa_votes`, and the minimum are computed. The cap therefore propagates through every downstream path. Parsing takes the first integer; an unparseable MAJOR field falls back to the verdict instead of invalidating the round.
- Boundary: B2 validates only the verdict token. B3 does not reject an unparseable MAJOR field, so an extreme `strong-accept` plus malformed MAJOR value can escape this cross-check, but it still requires unanimous SA and the `sa_gate` evidence checks. `awr-side.sh` uses separate aggregation.
- Validation: `bash -n` passed. 36 extracted-function tests covered valid and invalid vote forms, `major_cap` cases `(2,3)->1`, `(2,0)->2`, `(1,3)->1`, `(0,3)->0`, embedded integer parsing, unparseable fallback, `rank_of`, and `classify_nonsa`. Twenty-one sandboxed integration checks covered unanimous SA publication, MAJOR=3 capping to AwR with votes `1,1,1`, MAJOR=0 preserving `2,2,1` and near-SA admission, unanimous reject, a missing reviewer-3 row causing a review failure with no `ledger.good` row, and uppercase `Reject` causing the same failure.

## 2026-07-12 Near-SA lifecycle and classification fixes: A1, A2, B1

An eight-item P1 re-review confirmed that A1 and A2 interacted: a broad `design-fixable` classifier admitted AwR+low rows that were not eligible for evolution or recheck, while an append-only queue and a mandatory head-first rule let one ineligible row block the only evolution slot.

- A1: added `prune_near_sa_queue` before each generation round. It removes terminal stories already present at least 2 times in `ledger.good`, then retains the newest `NEAR_SA_MAX` entries, default 30. Admission now requires `story_cnt<2`. `generate.md` selects the first still-eligible row and skips invalid rows instead of blocking on the queue head.
- A2: `classify_nonsa` uses only raw minimum rank and overlap, while true evolution eligibility depends on free-text reasons. `design-fixable` is therefore documented as a coarse queue label. `generate.md` reads the ledger reason to distinguish evolution from evidence recheck and skips mismatches; pruning ages out residual ineligible rows.
- B1: corrected the claim that every non-downgraded reject had a mechanically verified CRITICAL or direct hit. Mapping such rejects to `novelty-dead` was a fail-closed approximation based on rank 0, not proof of a CRITICAL finding. The corresponding `PROGRAM.md` category text was reserved for the later documentation PR.
- Validation: `bash -n` passed. Tests covered removal at count >=2, retention below 2, newest-N capping, queue order, the end-to-end count gate, and archive-stop scenarios A/B/C.

## 2026-07-12 P1 #1: independent selector

Implemented item #1 from `P1-PROGRAM-DRAFT.md` with authorization to change `PROGRAM.md` and policy. Generation previously diverged to 10 candidates and self-filtered to 4-6 in one context; selection now belongs to a separate process. This completed #4-schema, #6, and #1, after which `P1-PROGRAM-DRAFT.md` was removed as specified.

- Added `roles/select.md`. It ranks the complete divergent set under an independent, cheap, non-killing context using proposition strength, clear-accept ceiling, minimal falsification quality, and executability, then writes `tmp/round/select.tsv`. Because selection precedes prior-work research, its novelty dimension measures falsifiable proposition strength rather than claiming that no one has published the idea.
- `roles/generate.md`: changed 10 candidates followed by self-filtering into about 10 candidates with no self-filtering. Lens and axiom-removal quotas now refer to selection into the deep-research slots, and occupied-mechanism avoidance applies during divergence.
- `hunt.sh`: inserted `select` between generation and prescreen using `FRONT_CMD`. A nonzero selector return code warns and continues. `select_rank_of` returns rank from `select.tsv` or 999 for missing or invalid data. The `keeps.tsv` schema gained selector rank. `select_shortlist` sorts by `keep_rank`, selector rank, low theme occupancy, then generation order. Missing selector output falls back to generation order.
- `PROGRAM.md`: step 1 now diverges to about 10 candidates without self-filtering, step 1.4 performs independent ranking, and step 9 describes selector plus prescreen reduction. The canonical axiom-removal quota in `brainstorming_policy.md` and the README flow were aligned.
- Validation: `bash -n` passed. With selector order I3,I1,I2 and `SHORT_MAX=2`, the shortlist was I3,I1 and logs recorded I2 overflow. Invocation order was generate -> select -> prescreen -> research and `stages.tsv` contained `select`. `STUB_NO_SELECT` fell back to I1,I2. Archive-stop scenarios A/B/C remained green.

## 2026-07-12 P1 #6: evidence-bounded candidate revival

Implemented item #6 from `P1-PROGRAM-DRAFT.md` with authorization to change the canonical revival policy in `brainstorming_policy.md`. The previous blanket ban on reviving reject rows also blocked candidates whose unanimous SA ballots had been downgraded only because evidence gates were incomplete.

- `PROGRAM.md` invariant 6 and `brainstorming_policy.md`: `novelty-dead` candidates with a direct hit, `overlap=high`, or CRITICAL finding remain permanently barred. Only `evidence-incomplete` reject rows may receive one evidence re-review. The block records its source and revival condition; the same story can be retried once, then becomes permanently barred if still below threshold.
- `roles/generate.md`: recheck eligibility now includes reject rows with `category=evidence-incomplete`. Evolution remains limited to `accept-w-rev`; reject revival uses the recheck path. Queue guidance maps `design-fixable` to evolution and `evidence-incomplete` to recheck.
- `hunt.sh`: near-SA admission changed from `design-fixable` to `design-fixable || evidence-incomplete`, with `sa_votes>=1` for both.
- Correction to the draft taxonomy: `design-fixable` and `ceiling-limited` are Accept-w-Rev categories, not revivable reject categories. A reject at minimum rank 0 can be only `novelty-dead` or `evidence-incomplete`; therefore `evidence-incomplete` is the only reject class opened for re-review.
- Validation: `bash -n` passed. A stub with votes `2,2,2` and empty `review.md` was downgraded by `sa_gate` to `verdict=reject, category=evidence-incomplete` and entered the near-SA queue. Archive-stop scenarios A/B/C remained green.

## 2026-07-12 P1 #4 schema: persistent non-SA categories

Implemented item #4-schema from `P1-PROGRAM-DRAFT.md` under temporary authorization to change `PROGRAM.md`. Non-SA categories had existed only in `tmp/nonsa-class.tsv`; ledger persistence made them available across runs and enabled item #6.

- `PROGRAM.md`: expanded the ledger from 7 to 8 columns. The final `category` field accepts `novelty-dead`, `evidence-incomplete`, `design-fixable`, `ceiling-limited`, or `-`. Legacy 7-column rows treat the missing value as unknown, matching the overlap migration rule.
- `hunt.sh`: both ledger writers gained the eighth field. Aggregated SA rows write `-`; non-SA rows use `classify_nonsa`; prescreen direct hits write `novelty-dead`. Positional reads of theme, verdict, and overlap were unchanged.
- `roles/generate.md`, `roles/meta.md`, and `trigger.md`: replaced references to the final overlap field with explicit column 7 for overlap and column 8 for category.
- Not included in this change: #6 revival and #1 selector required policy changes beyond the temporary `PROGRAM.md` authorization. Their canonical locations remained `brainstorming_policy.md` lines 7 and 8 and were implemented in the later entries above.
- Validation: `bash -n` passed. A near-SA stub wrote 8 fields with `design-fixable` in column 8. Archive-stop scenarios A/B/C remained green; the published SA row had 8 fields with `-` in column 8, and `publish.sh` and `settle.sh` completed normally.

## 2026-07-12 P1 candidate-quality instrumentation

Implemented the agent-editable subset of the near-SA quality program: #2 research facts, #3 targeted retrieval retry, #4 observable classification, and #5 near-SA queue. #1 selector, #6 revival, and persistent #4 schema required human-owned policy or `PROGRAM.md` changes and remained in `P1-PROGRAM-DRAFT.md` at this point.

- #2: `roles/research.md` no longer judges whether the strongest-counterexample difference is enough for clear accept. It reports only the concrete difference; the independent reviewer decides the ceiling from prior work.
- #3: mechanical retrieval failure previously sent the whole round through `empty_and_wait`. Added `RESEARCH_RETRY`, default 1, to rerun research for the same shortlist after `rm priorwork.md`, preventing old and new blocks from jointly satisfying gates. Exhaustion still invalidates the round. `roles/research.md` requires an honest incomplete marker instead of labeling unread neighbors low-overlap to satisfy the floor.
- #4: added `classify_nonsa` over pre-downgrade minimum rank, hard-gate downgrade status, and overlap. Categories were `evidence-incomplete`, `novelty-dead`, `design-fixable`, and `ceiling-limited`, recorded in `tmp/nonsa-class.tsv` without changing the fixed ledger schema at this stage.
- #5: candidates with `design-fixable` and at least one SA vote entered `tmp/near-sa-queue.tsv`, deduplicated by exact story with `grep -Fxq`. `generate.md` priority became near-SA queue, deathlist evolution candidate, then ledger scan. The single evolution block gained a `delta:` line describing the concrete change and why it addresses the prior ceiling. Existing Accept-w-Rev+low eligibility and the one-slot limit remained intact.
- Validation: `bash -n hunt.sh` and three stub scenarios passed.

## 2026-07-12 Re-review fix 4: archive recovery, delta return code, and claim alignment

The fourth re-review returned 6/10 PASS and exposed four remaining gaps. Source labels were `PARTIAL` for #10 and #1 and `FAIL` for #3 and #5.

- #10 partial, recovery semantics and swallowed delta failure: stopping text claimed that repairing `$RUNS_DIR` and restarting would backfill the original archive. In fact, restart created a new run ID and treated the orphan SA as already eligible for publication. Added `tmp/HALTED-ARCHIVE-FAIL` with run ID, SA count, and reason. Startup exits 2 while it exists, requiring the original run archive to be restored or the orphan SA row removed from `ledger.good` before the sentinel is cleared. A failed `ledger.delta.tsv` write now sets rc=1 instead of being swallowed by `|| true`; the delta is part of the audit artifact.
- #3 fail, E2E naming: source lines 98-100 were already qualified, but script headers, `calib/README.md`, and output still claimed live retrieval. They now describe end-to-end retrieval-recall calibration whose validity depends on a network-enabled backend. The README states that mechanical assertions validate recall structure but do not prove network access. `run_e2e.sh` uses the same boundary.
- #5 fail, score-count wording: bullets 720/723 still implied count-to-verdict rules. They now state that all dimensions <=4 are a thin-idea projection rather than a reject rule, and that no dimension at 7+ prevents clear accept by definition but does not independently choose between AwR and Reject.
- #1 partial, agy archive reach: moving the archive cannot isolate an untrusted same-user agy process with `$HOME` access. `hunt.sh` and `$RUNS_DIR` comments now state that only agy can reach the location and that hard isolation requires another UID or a container. The `agy-worker.sh` prompt denies writes to `~/.ai-ideas-runs/`. Archive restoration is relied upon only for trusted adjudication rounds with SA; agy never occupies a verdict seat.
- Validation: all shell files passed `bash -n`. Scenario A, broken archive plus SA, exited 2, wrote a complete sentinel, and emitted no report. Scenario B, sentinel present after directory repair, exited 2 before the loop with 0 aggregation logs and 0 new manifests. Scenario C, sentinel removed after deleting the orphan row and repairing storage, exited 0 and published with manifest, report, and `ledger.delta.tsv`.

## 2026-07-12 Re-review fix 3: halt SA publication on archive failure

The prior P2-5 change still warned and continued when archival copy failed. A probe made `$RUNS_DIR` unwritable mid-run; the archive disappeared while the hunt published SA with rc=0, silently breaking the P0 restoration guarantee.

- `archive_round` now returns failure when directory creation, manifest writing, or copying fails. For an SA round awaiting publication, adjudication archive failure exits 2 and blocks publication. At this revision, the expected recovery was to repair `$RUNS_DIR`, restart, backfill the archive, and publish; the next entry corrected that assumption with `tmp/HALTED-ARCHIVE-FAIL`.
- Archive failure remains a warning for non-SA rounds and the post-publication refresh. Non-SA rounds have no published artifact; a published run already has the complete adjudication archive and can tolerate a missing report-log refresh.
- Validation: a stub made the archive directory read-only during review with unanimous SA; hunt exited 2 without a report or publication. The writable control exited 0 with one manifest and one published report.

## 2026-07-12 Archive placement, E2E scope, AwR gates, and rubric semantics

Re-review of the preceding changes found four incomplete items. Report freezing and the `run_all`, publication, and copy P2 checks passed.

- #1 archive write domain: changed the default from repository `tmp/runs` to `$HOME/.ai-ideas-runs/<repo>`. Claude allowlists and Codex or grok repository sandboxes cannot write there; only the unsandboxed `hunt.sh` orchestrator creates archives. Startup logs the location. agy remained a known same-user exception. Existing grok and Claude denies for `tmp/runs` remain as fallback when `RUNS_DIR` is overridden into the repository.
- #3 E2E evidence: link and API counts prove artifact shape only. An offline agent can hard-code those strings. Comments now describe E2E as a regression gate for thin, empty, or missing-placeholder output; live-retrieval validity requires a genuinely network-enabled research backend. The structure gate remains useful but does not prove retrieval occurred.
- #4 AwR judgment: removing the unavailable `PROGRAM.md` reference had also removed its unique strongest-counterexample and reproducible-query requirements. `roles/awr-judge.md` now embeds the full evidence gates: 5-8 linked neighbors, `Strongest Counterexample:`, a reproducible API query URL, minimal falsification experiment, and at least 2 matching crack-evidence items for axiom removal.
- #5 rubric: after 740-741 were corrected, lines 716 and 717-718 still contained score-only rejection language. It was reduced to diagnostic guidance. Verdicts follow fatal flaws, with CRITICAL -> Reject and at least 2 MAJOR -> cap at Accept-w-Rev, plus the policy clear-accept bar. All dimensions <=4 corroborate a value-based Reject but do not cause it. No dimension at 7+ caps SA and identifies the main axis without independently deciding the lower verdict. The autonomous hunt no longer contains an ask-the-user branch.
- Validation: all shell files passed `bash -n`. A stub confirmed archives under `$HOME/.ai-ideas-runs/<repo>`, no repository `tmp/runs`, preserved freeze semantics, and a published archive containing adjudication input, report-stage logs, and a 3-row delta.

## 2026-07-12 Code-review fixes: archive integrity, E2E structure, and calibration failure handling

Code review of P0 (#20) and the gold-set correction (#22) confirmed 10 defects: 5 P1 and 5 P2. All were fixed.

- P1: repository `tmp/runs` was inside agent write scope and outside the guard. `grok-worker.sh` gained `deny_tree tmp/runs`; `.claude/settings.json` denied `Write/Edit(tmp/runs/**)`. Bash remained the archive writer. Codex workspace-write could not deny a subtree, matching the existing `tmp/ledger.good` boundary and documented limitation.
- P1: `archive_round` reused a run ID after verdict and could replace frozen adjudication input with report-stage state. First archival now freezes ideas, prior work, and reviews. Later refreshes update only the manifest and add stages or logs.
- P1: E2E had counted links without validating retrieval shape. Each block now requires at least `E2E_MIN_LINKS=5` non-API neighbor links and at least 1 structured API query URL, matching `hunt.sh priorwork_ok`; insufficient output is `retrieval-thin`. This checks structure, not live network access.
- P1: `roles/awr-judge.md` referenced `PROGRAM.md`, which was absent from the AwR mirror. It now references only mirrored policy and rubric files.
- P1: lines 214-218 had already removed score-only SA decisions, but `rubric.md` lines 740-741 still treated the absence of an 8+ dimension as mechanically inconsistent with SA. Those lines now point to the policy clear-accept standard; scores are diagnostic.
- P2: the E2E mirror copied production settings containing `Write(//tmp/**)` and `Write(//private/tmp/**)`. It now writes restricted E2E settings that allow only `Edit/Write(tmp/**)` plus WebSearch and WebFetch.
- P2: `run_all` skipped an explicitly requested case with no expect file, counted empty or comment-only expect files as pass, and returned 0 when all cases skipped. Added `config-error`; success requires `fail=0 && panel-fail=0 && config-error=0 && pass+probe>=1`.
- P2: a panel failure could read stale `aggregate.tsv` ballots from the previous run when `run_panel` failed before cleanup. Panel-fail rows now record votes as `-`.
- P2: `publish.sh` failure exited before the terminal archive refresh, leaving `exit_reason=verdict`. The failure path now calls `archive_round publish-failed` with report-stage evidence.
- P2: `cp -R || true` silently ignored incomplete archive copies. Copy failures now produce a visible log warning. At this revision they did not halt the round; the later archive-failure entries above strengthened SA handling.
- Validation: all shell files passed `bash -n`, and settings JSON parsed. Empty, comment-only, and missing expect cases produced `config-error` and nonzero exit; a normal case returned 0; panel-fail used votes `-`. Retrieval checks admitted 7 links plus 1 API and rejected 1 link plus 0 API as `retrieval-thin`. Restricted E2E settings completed an Opus run of `neg-meanflow-mp1`. A full 3-seat Opus 4.8 gold-set regression reported pass=4, probe=1, fail=0, panel-fail=0.

## 2026-07-12 Gold-set evidence correction

The first full calibration in `calib/results-2026-07-12.md` gave both formal positives 0 SA because novelty was capped and their prior work omitted web or industrial occupants. To separate insufficient evidence from overly strict policy, the three positive cases received omniscient prior-work reconstructions based on web and API checks at each submission date. The policy remained unchanged.

- `pos-axiom-adam`: adding explicit no-hit records for web and industrial occupants changed 1/3 to 3/3 SA. Every ballot named the four required conditions. The earlier 1/3 was caused by incomplete evidence, and the axiom-removal decision path was correct.
- `pos-meanflow` became `neg-meanflow-mp1`: MP1, 2507.10543 from 2025-07 with code, occupied the headline 72 days before the ICLR submission. FlowPolicy, an AAAI 2025 oral, had already falsified the premise that one-step control must rely entirely on distillation. The published oral 2602.13810 survived through an IVC increment, an RL track mismatch, and omission of MP1, which does not make it an honest omniscient positive. `git mv` reclassified the case as a direct-hit negative, adding a 2025 occupant alongside the older 2022 `neg-replai` pattern. It returned 3/3 reject; E2E recalled 2507.10543 and recorded high overlap.
- `pos-robomme`: MIKASA-Robo, 2502.10550, preceded it by 11 months and already occupied the four-class memory taxonomy and isolated task families. Honest overlap is medium; the old material incorrectly claimed low. The rerun remained 3/3 AwR for novelty ceiling, single-investigator construction feasibility, and no 8+ dimension. The expectation became `min_vote>=accept-w-rev`: a real oral should not be rejected, but medium overlap and build cost keep it below this repository's single-investigator phase-1 SA ground truth.
- The two questioned verdict policies were behaving correctly. MeanFlow had reached AwR only because false low-overlap material elevated a negative. The diagnostic ceiling is conditional: it caps candidates with no 8+ dimension, while a low-overlap benchmark with high Broader can escape. `brainstorming_policy.md` and `roles/review.md` did not change.
- Recorded evidence gap: no clean low-overlap benchmark positive directly tested the diagnostic-ceiling escape, and the method-positive slot became empty after MeanFlow moved to the negative set.
- Fixed overlap parsing in `calib/run_e2e.sh` and `hunt.sh` by anchoring the English field as `^Overlap:` instead of using a first-substring match such as `grep -m1 'Overlap:'`. Other prose could mention that a query result was not an overlap decision; the old match captured that line and misread a real high value as low during the first `neg-meanflow-mp1` E2E run.
- Model record: the first Fable 5 panel exhausted quota. RoboMME and MeanFlow reruns used Opus 4.8. Axiom completed under Fable 5 before exhaustion, making evidence the only variable. Unchanged `neg-replai` and `neg-axiom-cosplay` retained their initial 3/3 reject results and were not rerun.

## 2026-07-12 P0: unified SA semantics, per-run archives, and machine-scored calibration

Implemented the five P0 items from the `DEVELOPMENT.md` success program.

- Unified SA semantics in policy. `rubric.md` Step 8 and Integrity gate #5 now point to the policy clear-accept definition rather than mechanically requiring two 8+ dimensions. `roles/review.md` removed the stricter oral-or-spotlight requirement because policy treated that outcome as preferable, not mandatory; this copy drift may have contributed to only 6 SA votes among 288 ballots. `roles/awr-judge.md`, README, and `trigger.md` now reference policy calibration plus `PROGRAM.md` evidence gates. Qinning manually synchronized the remote weekly routine on 2026-07-12.
- Added per-run archival to `hunt.sh`. Each round gets a run ID from start time, PID, and round; candidate IDs are `<run_id>/I<n>`. Terminal states `fail:<stage>`, `empty:<stage>`, `verdict`, `report-missing`, and `published` archive all `tmp/round` artifacts, a manifest with source, backend, `policy_sha`, `git_head`, exit reason, and vote vector, plus ledger delta under `tmp/runs/<run_id>/`. Later events for the same run ID replace the prior terminal reason. `run_stage` tees output to `tmp/round/logs/<stage>.log`; `stages.tsv` records start, end, and rc, including parallel review seats. `metrics.tsv` gained run ID through a one-time header migration while legacy rows remain 12 columns. The ledger baseline uses `grep -c ''`; appending `|| echo 0` would emit two lines for an empty file because grep already prints 0 before returning rc=1.
- Added a machine-scored calibration DSL. Each `cases/<case>/expect` uses `min_vote`, `sa_votes`, `reject_votes`, `all_votes`, and `probe`. `run_panel.sh` writes `aggregate.tsv`; `calib/run_all.sh` scores cases and appends `tmp/calib/summary.tsv`. Probes and panel infrastructure failures do not enter the denominator.
- Added `calib/run_e2e.sh` as a network-enabled retrieval-recall track separate from frozen judgment calibration. It mirrors `roles/research.md` and asserts known occupants through `e2e.expect`, beginning with `neg-replai` and 2209.13583. Published positive cases cannot use live retrieval because they would match themselves. A later correction clarified that artifact structure alone does not prove network retrieval.
- Moved fence-aware Markdown ID extraction into `lib/md_ids.sh`, shared by `run_panel` and `run_e2e`.
- Validation used a scratch clone with a local bare origin and fake agents. Empty research archived `empty:research`; unanimous SA produced a report, published a branch to the bare origin, and archived `published` with a 3-row SA delta plus stages, logs, and manifest. `research rc=1` with `MAX_FAILS=1` exited 1 and archived `fail:research`. Front-resume entered review first and archived a verdict with `sa_count=0`. Header migration preserved legacy 12-column metrics rows. Calibration stubs scored 5 cases as 2 pass, 2 fail, 1 probe with accuracy 2/4 and exit codes 1/0. E2E pass, assertion-fail, and agent-fail paths cleaned mirrors. Fence tests rejected phantom IDs and returned rc=3 for an unclosed fence. All shell files passed `bash -n`. No live Claude, Codex, or grok panel ran in this change; the routine command remained `./calib/run_all.sh`.

## 2026-07-11 Resolver, sandbox, and panel input review fixes

A high-depth code review confirmed and fixed 10 defects.

- `lib/resolve_cmd.sh`: replaced `read -r`, which consumed only the first line of commands containing newlines and could silently drop sandbox or approval flags. The resolver now splits the first word on arbitrary whitespace while preserving the remainder. Absolute executable paths containing whitespace are rejected with exit 2 because downstream IFS splitting cannot invoke them safely and had produced repeated exit 127 failures.
- `grok-worker.sh`: validates `GROK_SANDBOX` as `workspace` or `off`. grok 0.2.x only warned on unknown profiles and then ran without a sandbox. Custom profiles from `sandbox.toml` remain unsupported because reliable table detection requires a TOML parser and profile-name escaping; neither repository had such a file.
- `calib/run_panel.sh`: validates a positive integer `REVIEWERS` and resolves `PANEL_CMD` before clearing prior output. `REVIEWERS=0` or nonnumeric input had produced an empty `seq`, then an unbound array at `wait` under Bash 3.2 and `set -u`, after destroying prior ballots.
- Fence-aware ID extraction now uses `PIPESTATUS` to return visible `exit 3` for an unclosed CommonMark fence instead of silently hiding every later title.
- Copied `verdict.tsv` files now strip UTF-8 BOM as well as CR, including a leading `\xef\xbb\xbfI1`. `LC_ALL=C` makes the operation byte-based.
- The impossible missing-vote fallback after rc and `verdict_ok` checks now aborts as an internal inconsistency instead of pretending to convert a missing ballot to reject.
- `awr-side.sh` resolver errors now name the effective source variable: `SIDE_CMD`, `SIDE_RESEARCH_CMD`, or `SIDE_JUDGE_CMD`.
- Mirror isolation guidance moved to `lib/mirror_pre.sh`, eliminating drift between `run_judge` and `run_agent`; panel seats now receive the same home-directory write prohibitions. The distinct mirror copy and return functions remain separate because their inputs, throttling, and recovery semantics differ.
- Validation passed 22 regressions covering resolver whitespace, empty and `../` traversal inputs, bare-name PATH behavior, whitespace repository paths, invalid reviewer counts and panel commands without destroying old output, unclosed and closed fences, BOM/CR normalization, all `GROK_SANDBOX` states, and all three AwR source labels. All shell files passed `bash -n`.

## 2026-07-10 Resolver consolidation and panel snapshot fixes

A high-depth review confirmed 3 correctness defects, 1 phantom-ID hazard, and 1 snapshot-semantics issue.

- Consolidated `SIDE_CMD` and `PANEL_CMD` resolution in `lib/resolve_cmd.sh`. Traversal rejection now covers any `..` path segment, including `./tmp/../../x`. A bare name shadows PATH only when an executable of that name exists at repository root; a stray non-executable file no longer breaks `SIDE_CMD='claude -p ...'`. Error prefixes are parameterized. `grok-worker.sh` also denies writes to `lib`.
- `awr-side.sh`: agy throttle detection replaced `${cmd%% *}` with arbitrary-whitespace splitting, preventing tab-separated agy commands from bypassing the launch gate. Removed the obsolete `${nbad:-0}` fallback in favor of `$nbad` because the counter is unconditionally initialized; the old `ls|grep -c` pipeline no longer exists.
- `run_panel.sh`: snapshots the live `$CASE` once into `$OUT`; ID extraction and every seat mirror read that snapshot. The previous implementation could give seats different inputs if a case changed during startup. ID parsing now ignores CommonMark fenced code blocks, tracking fence character and length, allowing at most 3 leading spaces, and requiring a closing fence of the same character with at least the opening length. A backtick in a backtick-fence info string prevents it from being treated as a fence. BSD awk compatibility avoids brace intervals. Calibration files remain under `calib/`.
- Intentionally unchanged: `ideas.md` `## I<n>` headings remain the sole ID source, and the independent `[ -e ]` guarded-glob counters in `hunt.sh` and `awr-side.sh` remain separate.
- Validation passed 15 resolver cases across traversal positions, absolute paths, executable shadowing, tab splitting, empty input, and missing executables. A two-seat fake panel preserved a snapshot and ignored fenced phantom IDs. All shell files passed `bash -n`.

## 2026-07-10 First-class grok backend support

- Added `grok-worker.sh`, a headless adapter that accepts exactly one prompt and invokes `grok ... -p`. Direct `grok -p ...` could consume flags as values, while a bare positional prompt blocked without a TTY. Extra arguments fail visibly. Defaults include `--always-approve`, `--sandbox workspace`, and `--no-subagents`. File-tool writes are denied for `ledger.tsv`, `tmp/ledger.good`, fixed policy files, orchestration scripts, and `roles/`, `calib/`, and `.claude/`, using relative, absolute, and `**/` patterns. `GROK_REPO` selects the root. `GROK_DISABLE_WEB` is enum-validated and fails closed. `GROK_MODEL`, `GROK_MAX_TURNS`, `GROK_SANDBOX`, and `GROK_BIN` remain configurable. `--disallowed-tools Agent` was excluded because grok 0.2.x crashed during session construction.
- The write boundary is limited to file tools and statically recognizable shell writes. Indirect writes such as Python `open().write()` can bypass it. The workspace sandbox does not block network or processes, and inherited `~/.claude` or `~/.grok` hooks, plugins, and MCP state can have external effects. Full containment requires an OS sandbox outside this adapter.
- `hunt.sh`: `AGENT_CMD`, `FRONT_CMD`, `BACK_CMD`, and `REV_CMD_N` may point to `./grok-worker.sh`; grok is a trusted seat alongside Claude and Codex.
- `awr-side.sh`: research and judgment may use grok. Startup resolves custom `SIDE_CMD` once, pins repository-relative paths to absolute paths, checks absolute files for `-f` and `-x`, prefers an executable at repository root for bare names, then falls back to PATH. Invalid commands fail immediately instead of bypassing the no-file circuit breaker. `PANEL_CMD` uses the same resolver. Invocations receive `GROK_REPO=<mirror>`, and mirror guidance denies writes to `~/.gemini`, `~/.claude`, `~/.codex`, and `~/.grok`.
- `calib/run_panel.sh`: each seat executes in a disposable mirror; Bash copies back only `verdict.tsv` and `review.md`. The mirror isolates CWD, while backend sandboxing provides the write boundary. grok seats receive `GROK_DISABLE_WEB=1`; Claude mirrors use calibration settings that permit only `tmp/**` writes and deny WebSearch/WebFetch. OS networking remains available, so prompt policy and leak markers cover shell-side search. Parallel cases use injective encodings `_→_u` and `.→_d`, preventing cleanup collisions such as `foo.bar` versus `foo_bar`. A content-snapshot guard was discarded after a destructive failure mode in which an agent deleted the snapshot and the guard treated every tracked file as an untracked addition through `rm -rf` cleanup.
- Ballots are normalized by removing CR and trimming fields before validation and aggregation. With rc=0, every ID must appear exactly once, every verdict must be valid, and no unknown ID or duplicate may exist; headers and blank lines are tolerated. Invalid output fails the seat and panel. IDs come only from `ideas.md` `## I<n>` headings. Input snapshots remain in the result directory after mirrors are removed. Leak markers are aggregated once globally with `LC_ALL=C sort -u` instead of once per ID.
- Validation covered file-tool denials with the `DENIED` marker, `GROK_REPO` root pinning, relative, absolute, and bare resolver outcomes, seven fake-ballot forms, and a real grok no-search direct-hit panel that returned reject.

## 2026-07-07 Proposition-first generation and mirrored prior-work search

Three hunt days produced 0 SA. About 50 of 120 adjudications shared one ceiling: the headline was an axis transfer or composition, placing novelty in an enumerable mechanism-by-domain grid where deep retrieval found an occupant. Eight of 11 divergence lenses were replace-an-axis patterns; with 3 blank cards, `pick_lens` selected them with probability 8/14, about 57%. Proposition-first ideas put novelty in a falsifiable claim about the world and were less likely to be occupied by one paper.

- `brainstorming_policy.md`: collapsed 8 axis-swap lenses into one cautious axis-change lens, reducing selection from 57% to 11%. High-weight starts now include explaining an accepted phenomenon, removing a load-bearing assumption, naming a real unnamed problem, and changing the evaluation target. Form #4 gained a diagnostic-probe ceiling: a purely diagnostic result is borderline unless paired with a corrective arm or a surprising finding. Classic cross-domain CS transfer is incremental by default.
- `roles/generate.md`: every headline receives a pre-write test. If it reduces to mechanism-by-domain or A+B, it is expected to cap at AwR. Rephrasing counts only when it creates a distinct falsifiable signal in the minimal experiment. Added estimand alignment and a corrective-arm guard for diagnostic candidates.
- `roles/research.md` and `roles/prescreen.md`: added searches for competing explanations, named estimands or problems, and limitations or ablations acknowledged by the target work. Examples included LAPA's acknowledged camera-motion content in latent action codes and LDA's acknowledged Euclidean action-head bottleneck. Prescreen performs only a cheap target check; systematic search remains in research.
- `roles/review.md`: estimand mismatch and the pure-diagnostic ceiling became explicit review checks.

## 2026-07-07 Strict prescreen decision parsing

Code review found that `prescreen_dec` used `grep -oE 'kill|keep'`, so strings such as `not kill`, `kill? keep`, or `killed` could become kill. With one API record and one non-API link, that permanently wrote reject with `overlap=high`, violating the fail-open contract.

- The first decision line must match the complete `Decision: kill|keep` form after allowed whitespace and colon variants. Any extra word makes it invalid and therefore fail-open keep. A malformed first line does not fall through to a later valid line.
- `roles/prescreen.md` received the same exact-line contract; an appended token invalidates a kill.
- A second review found that the first regex had reduced the full-width colon class to ASCII `[::]`. Full-width input therefore failed open safely but unnecessarily. The class was corrected to `[:：]`; ASCII, full-width, and extra-token cases passed. Templates and observed outputs used ASCII colons, so the remaining exposure had been theoretical.

## 2026-07-07 Fail-open prescreen structure

The 07-07 review found that commit 9fa98c8 fixed backgrounded API work at the prompt layer, but structural failure in the orchestrator still discarded an entire round after generation and lens extraction. Prescreen is a cost optimization that may kill but cannot certify; failure should spend more on deep research rather than erase the round.

- Replaced `prescreen_ok` with `kill_evidence`. A kill requires at least 1 structured API search record and a non-API occupant link in the same block. Only validated kills enter `kills.tsv` and the ledger as reject with high overlap. Incomplete evidence downgrades to keep.
- Missing, empty, absent, or invalid prescreen decisions fail open into the prioritized shortlist, where research, review, and SA gates remain authoritative. A nonzero invocation rc still uses `fail_and_wait` because a backend failure would likely repeat in the next stage using the same `FRONT_CMD`.
- Each fail-open ID enters `hunt.log` and `metrics` with `outcome=failopen`. `roles/prescreen.md` now states that invalid kills are void and all such candidates remain keep.

## 2026-07-07 Prioritized prescreen shortlist and round metrics

- Replaced FIFO with `tmp/round/keeps.tsv` carrying rank, theme occupancy, and generation order. `select_shortlist` sorts rechecks or evolutions at rank 0, axiom-removal candidates at rank 1, and ordinary candidates at rank 2; ties use ascending ledger theme count and generation order. Overflow remains unrecorded. FIFO had discarded later scarce candidates 51 times in `hunt.log`, including an interrupted-round I6 recheck; the new order selected I6, I1, and I3, covering recheck, axiom removal, and a Human-Robot Interaction and Deployment theme with occupancy 2.
- Added append-only `tmp/hunt.metrics.tsv`. Failure, empty, and verdict events record round, stage, lens, generated, killed, kept, short, dropped, `pw_links`, `pw_api`, and per-candidate votes as `id=r1,r2,r3->verdict`, where 2=SA, 1=AwR, 0=reject, and -=missing. A `2,2,2->reject` line exposes an SA-gate downgrade. This distinguished why 84/107 Accept-w-Rev cases were novelty-capped or retrieval-thin without reconstructing logs.
- BSD awk, sort, and uniq use `strcoll`; under `en_US.UTF-8`, distinct CJK strings compared equal. Theme counts changed to byte-exact `grep -Fxc`. Existing `themes_ok` array keys were already byte-exact. CJK equality checks no longer use awk `==` or locale-default sort and uniq.

## 2026-07-06 One-shot prescreen execution and bounded rate-limit retry

Two empty prescreen rounds at 22:07/22:46 had the same cause: the agent backgrounded API retrieval and returned while waiting for a callback. `claude -p` exited with the response, so `prescreen.md` was never written, consuming 2 of 3 short retries.

- `roles/prescreen.md`: one-shot invocations cannot background work or wait for callbacks. On an API rate limit, they switch APIs or run `sleep 10`, at most 2 attempts per idea. Continued failure records the issued query URL, returns keep, and writes `prescreen.md` before the response ends.
- `.claude/settings.json`: allowed only `Bash(sleep 10)`, bounding the wait independently of `run_stage`.

## 2026-07-06 Divergence lenses, blank cards, and deployment theme

A review of 2025-26 award-winning work, including CoRL 2025 UniFP and Fabrica, RSS 2025 FEAST, NeurIPS 2025 best papers, and ICRA 2026 finalists, found that all 8 lenses were component substitutions and missed unification, closed-loop learning, extreme scale, and mechanism explanation.

- `brainstorming_policy.md`: expanded 8 lenses to 11 by adding output representation, unify or split, and closed-loop experience. The failure-assumption lens became explanation of accepted phenomena, including failure, success, scaling curves, and emergence. Compute became a bidirectional scale-axis lens. Time scale gained memory and context length; evaluation targets gained confounders. Lenses remain starting points, not mechanical quality gates.
- `hunt.sh` `pick_lens`: added 3 blank cards to `total+3`; a blank adds no prompt lens and is logged as free divergence.
- Added Human-Robot Interaction and Deployment to the theme vocabulary for FEAST-like work. Its initial occupancy was 0, so anti-collapse rules prioritized it. `themes_ok` parses vocabulary dynamically.

## 2026-07-06 AwR multi-backend sidecar

- Renamed `agy-side.sh` to `awr-side.sh`. Research and judgment seats accept Claude or Codex. `SIDE_CMD` sets both seats; `SIDE_RESEARCH_CMD` and `SIDE_JUDGE_CMD` override them independently, matching `hunt.sh` `AGENT_CMD`. Claude examples used `--strict-mcp-config`; Codex examples used `--skip-git-repo-check --ephemeral` for mirrors without `.git`. At this historical point, an unset command still selected built-in agy. Mirrors and mechanical checks applied to all backends; only agy used the launch throttle.
- Renamed `AGY_SIDE_*` variables to `SIDE_*`. `AGY_MODEL` and `AGY_PRINT_TIMEOUT` remained for built-in agy. Startup migrated `tmp/agy-side/` to `tmp/awr-side/` without losing queue state, and the lock became `tmp/awr-side.lock`.

## 2026-07-06 Automated Claude MCP isolation

- Changed historical defaults for `hunt.sh` `AGENT_CMD` and `calib/run_panel.sh` `PANEL_CMD` from `claude -p` to `claude -p --strict-mcp-config`. Child processes inherited no user MCP servers, reducing startup checks and keeping unrelated application credentials out of process arguments visible through `ps`.
- Outside the repository, the lark server was removed from Claude user scope. Its mode-600 configuration was stored at `~/.claude/mcp-lark.json` and could be mounted explicitly with `claude --mcp-config ~/.claude/mcp-lark.json`. Codex had no corresponding registration.

## 2026-07-06 Axiom-removal channel (merged, PR #13)

The ledger contained 51 AwR, 18 reject, and 0 SA. The 07-05 calibration showed that genuine oral-level material could not receive SA under the old policy: generation produced within-paradigm probes and review capped them. The new path targets a common SA structure: remove one load-bearing assumption, identify an external forcing constraint, and define a cheap decisive falsification experiment.

1. `brainstorming_policy.md`: added form 5, remove a load-bearing assumption, with the assumption, why it can now be removed, forcing constraint, at least 2 URL crack-evidence lines pending verification, and a minimal experiment that can kill the bet. At least 1 of 10 raw candidates must attempt the form; only quality determines whether it reaches the 4-6 selected candidates. An unsuccessful attempt records one candidate and blocker before the first idea block and does not enter the ledger.
2. `roles/research.md`: crack evidence is verified by full reading with outcomes supports, partial, contradicts, or unreachable. Draft self-report is not novelty evidence; prior work remains the judge's source.
3. `roles/review.md` and policy: an untested bet is not itself MAJOR when its experiment is cheap and decisive. SA becomes available only with low-overlap bounded zero hit, at least 2 supporting crack items, an explicit external forcing constraint, and a decisive experiment executable on `1 x H100`. Direct hits, CRITICAL findings, at least 2 MAJOR findings, thin retrieval, or a missing experiment still block SA.
4. `hunt.sh`: added `AXIOM_MIN_CRACKS`, default 2, plus `is_axiom_idea`, `axiom_ok`, and `cracks_ok` at generation, research, resume, and SA-gate boundaries. SA also requires at least 2 supporting verification outcomes. Marker lines before the first `##` are ignored by block parsers. Fixture tests passed 17/17. `trigger.md`, `PROGRAM.md`, and README were aligned; the remote weekly routine was manually synchronized on 2026-07-06.
5. Calibration in `calib/results-2026-07-06.md`: `neg-axiom-cosplay` used valid structure around false claims and real URLs, with all crack checks contradicting and Diffusion Policy occupying the headline; it returned 3/3 reject. `pos-axiom-adam`, a cross-domain pre-publication reconstruction of ICML 2026 oral 2602.07729, Do We Need Adam?, returned 3/3 SA, the first calibration minimum-vote SA. Every ballot named all four conditions and none rejected it for domain transfer. The earlier 07-05 v2 oral material had only 2/6 individual SA votes, consistent with evidence insufficiency. Selection of a formal embodied-domain positive remained open until RSS 2026, 7/13-17 in Sydney; the later `pos-axiom-torque` entry closed it.

## 2026-07-05 AwR revival sidecar (committed directly to main)

- Added `agy-side.sh`, `roles/awr.md`, and `roles/awr-judge.md`. Outside the main loop, multi-round agy research revised Accept-w-Rev ledger ideas, while a judge applied rubric and returned SA-possible or not-ready. Defects fed the next round; default finalization followed 3 feedback rounds. Artifacts stayed under `tmp/agy-side/awr/` and did not change verdicts, ledger rows, or idea reports.
- Each invocation received only a `tmp/agy-side/run.*` mirror, and Bash copied back the declared output because agy had not reliably honored prompt paths. Mechanical checks required `## Revised Idea`, at least 3 URL query records, a binary decision, and final `AGY-DONE`. Invalid output became `.badN`; 3 failures blacklisted the item. Targets were cleared before invocation to prevent stale `judge.md` reuse. The sidecar shared the `agy-worker.sh` launch timestamp, default 120s, to avoid login verification bursts.
- README documented sidecar use and interpretation.

## 2026-07-05 Daily SA target (merged, PR #8)

- Added `SA_TARGET` to `hunt.sh`, default 1; 0 means unlimited. The stop condition became a daily cumulative target rather than at least 1 unanimous Strong Accept. A published round below target continues. Multiple same-day reports retain the `-2` and `-3` suffix rules from `roles/report.md`; `publish.sh` appends idempotently to the same daily branch and PR.
- Re-entry checks the number of same-day hunt-source `strong-accept` rows in the `tmp/ledger.good` baseline. A report can already exist while the target remains unmet; startup republishes idempotently before continuing.
- Report completion now requires an increase in report-file count, preventing an older report from satisfying a later round.
- Aligned `hunt.md`, `PROGRAM.md` step 5, and README.

## 2026-07-05 Prescreen, deep prior work, evolution eligibility, and panel calibration (merged, PR #9)

The day produced 29 ideas and 0 SA: 21 AwR and 8 reject. All 8 rejects were F1 occupied-headline failures discovered only after expensive research or review. About seven in ten AwR reasons involved novelty ceilings or only 3 papers read, and all 3 evolution attempts selected novelty-capped parents. Retrieval depth and parent eligibility, not generation time, were the bottlenecks.

1. Added `roles/prescreen.md` between generation and deep research. It is cheap, may err, and can kill only a single-paper direct hit supported by an occupant link and at least 1 API query record. Valid kills enter the ledger immediately as reject with high overlap. Surviving candidates are cut to `SHORT_MAX`, default 3; overflow is not recorded, and an all-killed round uses the empty-output retry. Bash constructs shortlist and kill state. Theme validation reads the pre-prescreen `ideas.all.tsv`.
2. `roles/research.md`: increased full reading from 3-5 to 5-8 papers, searched for direct hits first, and required `Strongest Counterexample:`. Raised `PRIOR_MIN_LINKS` from 3 to 5 and `MIN_READ` from 3 to 5.
3. Expanded the ledger from 6 to 7 columns with overlap parsed as high, medium, or low from prior work. Prescreen kills record high.
4. `roles/generate.md`, `brainstorming_policy.md`, and `PROGRAM.md` invariant 6: evolution requires `accept-w-rev`, low overlap, and a design-class reason. Retrieval-thin AwR rows use one exact-story recheck; another ceiling permanently retires the story. Evolution and recheck share one slot per round. Minimal experiments name the strongest baseline, sample size, and expected effect.
5. `roles/meta.md`: deathlists gained fatal pattern, ceiling pattern, and evolution candidate sections. Trigger counts now include reject plus AwR. Reject rows remain in the ledger because deletion would starve distillation and duplicate prevention.

The calibration harness in `calib/run_panel.sh` runs N independent no-search reviewers and aggregates by minimum vote. Search is disabled because published controls would otherwise retrieve themselves; suspected counterparts are leak markers, not verdict changes. Positives were `pos-robomme`, ICML 2026 oral 2603.04639, and `pos-meanflow`, ICLR 2026 oral, each with 8-paper low-overlap prior work verified through arXiv API. `neg-replai` is directly occupied by RepLAI 2209.13583 with high overlap.

The run in `calib/results-2026-07-05.md` returned 3/3 reject for the negative and 3/3 Accept-w-Rev for both positives, with 0 SA among 6 positive ballots. Review evaluated full lifecycle instead of the minimal experiment and treated cross-domain mechanism transfer as capped. Changing aggregation to 2/3 would not help. The hand-built positive prior-work files had also missed MIKASA-Robo and MP1.

### Calibration policy correction A+B

- A: feasibility now evaluates the minimal falsification experiment plus a reasonable phase-1 first-paper scope. A larger vision exceeding single-investigator compute is not itself MAJOR. Updated `brainstorming_policy.md`, `roles/review.md`, and `rubric.md` Step 6.
- B: mechanism transfer may receive SA only when prior work shows a target-domain zero hit, adaptation is nontrivial, and the first signal is clear-accept quality. Missing any condition retains the cap.
- Aligned `PROGRAM.md` invariant 4 and `brainstorming_policy.md` at 5-8 papers. Backfilled 29 legacy ledger rows with unknown overlap in column 7; every later row has 7 fields.
- Qinning manually synchronized the remote `Weekly Embodied Idea Scout` prompt from `trigger.md` on 2026-07-05.

## 2026-07-05 Interruption recovery (merged, PR #6)

- Added the atomic directory lock `tmp/hunt.lock` with PID recording. A second live instance exits; a stale lock is reclaimed.
- Startup now runs idempotent `./publish.sh` before exiting when a same-day report exists, closing the report-written, publication-interrupted gap.
- `publish.sh` can resume when there is no new local diff but the daily branch already exists after a prior commit and before push or PR creation; it completes push and PR creation instead of returning no changes.
- With `RESUME_FRONT=1`, default, valid interrupted `tmp/round` front-stage artifacts can skip generation and research on the first round. Review ballots and blocks are always cleared and regenerated; verdicts are never resumed.

## 2026-07-05 Product research pipeline upgrades (merged, PR #5)

Implemented five findings from Google Co-Scientist, AI Scientist v2, and Si et al. 2409.04109 on LLM ideation collapse and weak feasibility.

1. Added `roles/meta.md`. Every `META_EVERY` rounds, default 6, once reject count reaches `META_MIN_REJECTS`, default 5, the front process distills ledger failure reasons into `tmp/deathlist.md`. This fallible stage does not block the hunt.
2. Added one evolution slot per round for a targeted revision of an Accept-w-Rev ledger row. It receives full new prior-work and review processing and inherits no ballot. Reject rows were not revivable at this stage.
3. Expanded `ledger.tsv` from 5 to 6 columns with `theme`; `tmp/round/ideas.tsv` gained theme in column 3. At least `THEME_MIN_LOW`, default 2, candidates must use one of the three least-populated themes. `hunt.sh` injects a randomly selected divergence lens.
4. Every idea gained a minimal falsification experiment with data, compute, and expected signal. Review feasibility depends on that experiment; missing or infeasible experiments count as MAJOR and cap at Accept-w-Rev. `hunt.sh sa_gate_ok`, `trigger.md`, `roles/generate.md`, `roles/review.md`, `rubric.md` Step 6, and `brainstorming_policy.md` were aligned.
5. Each `roles/research.md` block requires at least 1 reproducible arXiv or Semantic Scholar API query URL. `hunt.sh priorwork_ok` enforces `PRIOR_MIN_API`, default 1 and disabled by 0. APIs provide recall; full reading determines overlap.

README and `trigger.md` stages 1-4 were aligned with the five changes.

### Same-day review corrections

- `hunt.sh priorwork_ok` counts only neighbor bullets and excludes API URLs, preventing 2 neighbors plus 1 API query from satisfying `PRIOR_MIN_LINKS=3`.
- Added `themes_ok`: themes must belong to policy vocabulary and at least `THEME_MIN_LOW`, default 2 and disabled by 0, candidates must use themes at or below the third-lowest ledger count. Cold start admits all tied zero-count themes.
- `hunt.sh sa_gate_ok` now requires at least 30 bytes after the minimal-falsification label, blocking empty and placeholder fields while leaving semantic review to the panel.
- Reduced `hunt.md` to entry-specific behavior and linked protocol semantics to `PROGRAM.md`, eliminating stale statements about three processes and 3-link prior work.
- Qinning manually synchronized the remote `Weekly Embodied Idea Scout` prompt from `trigger.md` on 2026-07-05.
