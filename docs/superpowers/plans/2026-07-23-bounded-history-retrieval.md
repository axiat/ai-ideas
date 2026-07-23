# Bounded Historical-Idea Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unbounded ledger reads with a SQLite-backed, evidence-addressed, token-bounded historical-idea retrieval path while preserving the existing research and publication protocol.

**Architecture:** SQLite becomes the local canonical structured store and exports the stable TSV projection through a replayable ledger-projection outbox. A separate transactional search outbox feeds exact lookup, FTS5, deterministic per-facet vectors, bounded generation briefs, retrieval packs, and replayable receipts. `generate`, the history comparator, and every reviewer seat run in distinct allowlisted temporary mirrors under OS containment; `hunt.sh`, the roles, and `PROGRAM.md` switch together after parity and recovery checks pass.

**Tech Stack:** Bash 3.2, Python 3.9 standard library, SQLite 3 with FTS5, JSON, TSV, Markdown, macOS `sandbox-exec`, optional Linux `bwrap`, and Git worktrees.

## Global Constraints

- Work only in `/Users/qinningxu/code/ai-ideas/.worktrees/bounded-history-retrieval` on `feat/bounded-history-retrieval`.
- Never invoke Claude directly or indirectly. Offline tests use deterministic fake backends only.
- Preserve the committed ledger baseline byte-for-byte through import/export: 531 data rows, including 216 seven-field and 315 eight-field rows.
- Keep `ledger.tsv` as the stable tracked import/export and audit projection. The local primary store is `.ai-ideas/history.sqlite3`; add `.ai-ideas/` to `.gitignore`.
- Create and track one immutable single-line `ledger.instance-id`. Derive `origin_stable_id` exactly as `origin-row-v2` from that repository identity, the 1-based data-row ordinal, and the exact row-byte SHA without its line terminator.
- Use schema version `1`, canonicalization version `canonical-story-v1`, Unicode NFC, trimmed leading/trailing whitespace, collapsed internal whitespace, normalized line endings, and unchanged punctuation and quotation marks.
- Derive import candidate identity from the physical row and raw bytes with domain-tagged, length-prefixed SHA-256. Appending rows must not change prior candidate IDs.
- Keep canonical `story_aliases`, candidates, verdicts, typed lineage, artifacts, invocations, and provenance separate from rebuildable search projections.
- Add `lineage_edges`, `search_projection_outbox`, and `ledger_projection_outbox`; do not reuse or reinterpret AWR's `materialization_outbox`.
- Do not create partial AWR `reentry_grants`, `reentry_requests`, `round_slots`, or `materialization_outbox` tables. The automatic bridge remains deferred under `DEVELOPMENT.md` and `AWR-REBUILD-DRAFT.md`; its reserved names and semantics remain untouched. Preserve the current near-SA generation priority through canonical `near_sa_observations`, not an ignored queue as sole truth.
- Use SQLite `BEGIN IMMEDIATE`, `PRAGMA foreign_keys=ON`, WAL mode, schema-hash validation, and `foreign_key_check` on every canonical write path.
- Use the bundled deterministic embedding provider `hash-ngram-v1`: word unigrams, word bigrams, and character trigrams; signed SHA-256 feature hashing; 256 float32 dimensions; L2 normalization; cosine similarity; preprocessing version `search-text-v1`.
- Use exhaustive vector scan. Do not add ANN, a vector database, GraphRAG, model-managed history mutation, or automatic semantic lineage merges.
- Use FTS5 `unicode61`; use reciprocal-rank fusion with `rrf_k=60`.
- Theme may adjust a fused score but can never exclude a candidate from retrieval.
- Store retrieval policy in `history/retrieval-policy-v1.json`: mode `shadow`, per-channel depth `50`, final lineage count `10`, comparator cutoff `10`, `max_matches=10`, `max_retrieval_tokens=4096`, `max_expansion_rounds=1`, `model_context_limit=32768`, `max_output_tokens=2048`, `safety_margin=1024`, adapter version `history-stage-v1`, and its tested `adapter_wrapper_allowance=256`.
- Shadow mode changes generation context and storage plumbing, then archives retrieval evidence. For one frozen generated-candidate batch, retrieval observations cannot gate or reorder candidates, mutate lineage, alter selector-through-review prompts, or change a ledger decision. Enforcement mode activates receipt gates only when a sealed calibration artifact satisfies the benchmark contract.
- Synthetic fixtures prove contracts only and must not be described as retrieval-quality evidence.
- Every generated candidate receives deterministic duplicate and failure-pattern retrieval. Evolution retrieval is additionally required when the candidate declares an evolution or recheck parent.
- Only a `complete` pack enters the comparator. In enforcement mode, `partial`, `backend_failed`, `budget_exceeded`, `uncertain`, and `conflicting_evidence` cannot create a permanent ledger conclusion. In shadow mode, those statuses are observations and cannot affect the existing pipeline.
- Similarity and comparator relations never write lineage or verdict state automatically. Stable exact identity may resolve an alias; semantic relations remain evidence for the normal research and review protocol.
- `complete_no_match` means no match inside the recorded corpus watermark, policy, channel depths, and comparator cutoff. It never means academic novelty.
- Generation, comparator, and reviewer mirrors contain no `ledger.tsv`, SQLite database, search index, `.git`, run archive, unrestricted repository root, or writable path outside the mirror.
- Update `PROGRAM.md`, `roles/generate.md`, `roles/meta.md`, the new comparator role, `hunt.sh`, fake-agent fixtures, runtime tests, and operator docs as one compatibility cutover.
- Preserve current prescreen semantics: a single external occupying work may still create an immediate `reject/high`; internal history retrieval cannot replace that rule.
- Preserve existing `SHORT_MAX`, theme inventory, evolution/recheck eligibility, near-SA priority, review aggregation, archive, resume, and publication semantics unless the spec explicitly changes their data source.
- All tracked prose remains English, minimal, bounded, and dense.

---

### Task 1: Canonical SQLite Store and Stable TSV Projection

**Files:**
- Create: `lib/history_store.py`
- Create: `lib/history_cli.py`
- Create: `ledger.instance-id`
- Create: `tests/history_store_smoke.py`
- Create: `tests/fixtures/near-sa-queue.tsv`
- Modify: `.gitignore`
- Modify: `CONTRIBUTING.md`

**Interfaces:**
- Consumes: `ledger.tsv`, an optional versioned lineage-mapping manifest with evidence artifacts, an optional near-SA bootstrap snapshot, optional run metadata, and an explicit database path.
- Produces: `connect(path)`, `init_schema(conn)`, `canonical_story_v1(text)`, `origin_stable_id(ledger_instance_id, row_number, raw_row)`, `build_import_plan(inputs, state_root)`, `commit_import_plan(conn, plan)`, `import_tsv_epoch(conn, path)`, `import_near_sa_observations(conn, path)`, `append_rows(conn, rows, provenance)`, `get_candidate(conn, candidate_id)`, `render_tsv(conn)`, `materialize_ledger_projection(conn, targets, state_root)`, `reconcile_ledger_projection(conn, targets, state_root)`, and `validate_store(conn)`.
- CLI: `python3 lib/history_cli.py --db PATH init|sync-ledger|append-tsv|import-near-sa|materialize-ledger|reconcile-ledger|export-tsv|validate`.

- [ ] **Step 1: Write the failing canonical-store tests**

```python
class HistoryStoreSmoke(unittest.TestCase):
    def test_import_export_preserves_legacy_and_current_rows(self):
        db = self.root / "history.sqlite3"
        store = history_store.connect(db)
        history_store.init_schema(store)
        receipt = history_store.import_tsv_epoch(store, self.ledger)
        exported = self.root / "export.tsv"
        history_store.export_tsv(store, exported)
        self.assertEqual(exported.read_bytes(), self.ledger.read_bytes())
        self.assertEqual(receipt["data_rows"], 3)

    def test_append_keeps_existing_candidate_ids_stable(self):
        before = self._candidate_ids()
        history_store.append_rows(self.conn, [self.new_row], {"run_id": "r1"})
        self.assertEqual(self._candidate_ids()[:len(before)], before)

    def test_origin_row_v2_fixes_instance_ordinal_and_row_bytes(self):
        one = history_store.origin_stable_id("instance-a", 1, b"a\tb")
        self.assertEqual(one, history_store.origin_stable_id(
            "instance-a", 1, b"a\tb\r\n"
        ))
        self.assertNotEqual(one, history_store.origin_stable_id(
            "instance-b", 1, b"a\tb"
        ))
        self.assertNotEqual(one, history_store.origin_stable_id(
            "instance-a", 2, b"a\tb"
        ))

    def test_exact_duplicate_rows_are_distinct_candidates_in_one_lineage(self):
        receipt = history_store.import_tsv_epoch(self.conn, self.duplicate_ledger)
        candidates = self._candidates_for_story("same proposition")
        self.assertEqual(len(candidates), 2)
        self.assertNotEqual(candidates[0]["candidate_id"], candidates[1]["candidate_id"])
        self.assertEqual(candidates[0]["lineage_id"], candidates[1]["lineage_id"])
        self.assertEqual(receipt["root_candidate_id"], candidates[0]["candidate_id"])

    def test_conflicting_lineage_mapping_rolls_back_whole_epoch(self):
        plan = history_store.build_import_plan(
            self.conflicting_inputs, self.state_root
        )
        before = self._canonical_counts()
        with self.assertRaises(history_store.ImportConflict):
            history_store.commit_import_plan(self.conn, plan)
        self.assertEqual(self._canonical_counts(), before)

    def test_similarity_cannot_write_lineage_edge(self):
        with self.assertRaises(ValueError):
            history_store.add_lineage_edge(
                self.conn, self.parent, self.child, "evolved_from",
                evidence_artifact_id=None, authority="similarity",
            )

    def test_explicit_lineage_edge_cannot_create_cycle(self):
        history_store.add_lineage_edge(
            self.conn, self.a, self.b, "evolved_from",
            evidence_artifact_id=self.ab_evidence, authority="explicit",
        )
        history_store.add_lineage_edge(
            self.conn, self.b, self.c, "evolved_from",
            evidence_artifact_id=self.bc_evidence, authority="explicit",
        )
        with self.assertRaises(history_store.LineageCycle):
            history_store.add_lineage_edge(
                self.conn, self.c, self.a, "evolved_from",
                evidence_artifact_id=self.ca_evidence, authority="explicit",
            )

    def test_near_sa_priority_survives_queue_removal(self):
        history_store.import_near_sa_observations(self.conn, self.near_sa_fixture)
        self.near_sa_fixture.unlink()
        parent = history_store.select_generation_parent(self.conn)
        self.assertEqual(parent["candidate_id"], self.expected_near_sa_candidate)

    def test_later_ledger_winner_invalidates_stale_near_sa_observation(self):
        history_store.import_near_sa_observations(self.conn, self.near_sa_fixture)
        history_store.append_rows(
            self.conn, [self.later_terminal_row], {"run_id": "r2"}
        )
        self.assertIsNone(history_store.select_generation_parent(self.conn))

    def test_reconcile_after_each_ledger_projection_crash(self):
        fault_points = (
            "db_commit",
            "snapshot_temp_fsync", "snapshot_rename", "snapshot_parent_fsync",
            "pointer_temp_fsync", "pointer_rename", "pointer_parent_fsync",
            "ledger_temp_fsync", "ledger_rename", "ledger_parent_fsync",
            "ledger_receipt_fsync",
            "ledger_good_temp_fsync", "ledger_good_rename",
            "ledger_good_parent_fsync", "ledger_good_receipt_fsync",
            "db_mark",
        )
        for crash_after in fault_points:
            with self.subTest(crash_after=crash_after):
                fixture = self._fresh_projection_fixture()
                with self.assertRaises(history_store.InjectedCrash):
                    history_store.materialize_ledger_projection(
                        fixture.conn, fixture.targets, fixture.state_root,
                        fault_after=crash_after,
                    )
                history_store.reconcile_ledger_projection(
                    fixture.conn, fixture.targets, fixture.state_root
                )
                expected = history_store.render_tsv(fixture.conn)
                self.assertEqual(fixture.ledger.read_bytes(), expected)
                self.assertEqual(fixture.ledger_good.read_bytes(), expected)
                self.assertEqual(
                    history_store.pending_ledger_projection_count(fixture.conn), 0
                )

    def test_newer_full_projection_satisfies_older_pending_rows(self):
        fixture = self._fixture_with_two_unmaterialized_appends()
        history_store.reconcile_ledger_projection(
            fixture.conn, fixture.targets, fixture.state_root
        )
        pointer = fixture.read_pointer()
        self.assertEqual(pointer["sequence"], fixture.current_db_sequence)
        self.assertEqual(self._done_sequences(fixture.conn), [1, 2])
        self.assertEqual(self._satisfied_by(fixture.conn, 1), 2)

    def test_pointer_cannot_regress_or_change_hash_at_equal_sequence(self):
        fixture = self._fresh_projection_fixture()
        fixture.write_pointer(sequence=99, sha256="higher")
        with self.assertRaises(history_store.ProjectionConflict):
            history_store.reconcile_ledger_projection(
                fixture.conn, fixture.targets, fixture.state_root
            )

    def test_two_consumers_fence_stale_projection_token(self):
        fixture = self._fresh_projection_fixture()
        claim_a = history_store.claim_ledger_projection(fixture.conn, now=10)
        self.assertIsNone(
            history_store.claim_ledger_projection(fixture.conn, now=11)
        )
        claim_b = history_store.claim_ledger_projection(fixture.conn, now=100)
        with self.assertRaises(history_store.StaleClaim):
            history_store.publish_claimed_ledger_projection(
                fixture.conn, claim_a, fixture.targets, fixture.state_root
            )
        history_store.publish_claimed_ledger_projection(
            fixture.conn, claim_b, fixture.targets, fixture.state_root
        )
        self.assertEqual(history_store.pending_ledger_projection_count(
            fixture.conn
        ), 0)
        fixture.write_pointer(
            sequence=fixture.current_db_sequence, sha256="wrong"
        )
        with self.assertRaises(history_store.ProjectionConflict):
            history_store.reconcile_ledger_projection(
                fixture.conn, fixture.targets, fixture.state_root
            )
```

- [ ] **Step 2: Run the store test and verify RED**

Run: `python3 tests/history_store_smoke.py`

Expected: FAIL because `lib/history_store.py` and its schema do not exist.

- [ ] **Step 3: Implement schema, identity, import, append, export, and validation**

The schema must include:

```sql
CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE import_epochs(
  epoch_id TEXT PRIMARY KEY,
  input_manifest_sha256 TEXT NOT NULL UNIQUE,
  plan_sha256 TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK(state = 'done'),
  row_count INTEGER NOT NULL CHECK(row_count >= 0),
  result_sha256 TEXT NOT NULL,
  committed_at TEXT NOT NULL
);
CREATE TABLE lineages(
  lineage_id TEXT PRIMARY KEY,
  root_candidate_id TEXT NOT NULL UNIQUE,
  UNIQUE(lineage_id, root_candidate_id),
  FOREIGN KEY(root_candidate_id, lineage_id)
    REFERENCES candidates(candidate_id, lineage_id)
    DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE candidates(
  candidate_id TEXT PRIMARY KEY,
  origin_stable_id TEXT UNIQUE,
  lineage_id TEXT NOT NULL REFERENCES lineages(lineage_id),
  row_number INTEGER,
  raw_sha256 TEXT NOT NULL,
  field_count INTEGER NOT NULL CHECK(field_count IN (7, 8)),
  date TEXT NOT NULL,
  source TEXT NOT NULL,
  theme TEXT NOT NULL,
  story TEXT NOT NULL,
  verdict TEXT NOT NULL,
  reason TEXT NOT NULL,
  overlap TEXT NOT NULL,
  category TEXT NOT NULL,
  source_sequence INTEGER NOT NULL UNIQUE,
  UNIQUE(candidate_id, lineage_id)
);
CREATE TABLE story_aliases(
  canonical_version TEXT NOT NULL,
  canonical_hash TEXT NOT NULL,
  canonical_story TEXT NOT NULL,
  lineage_id TEXT NOT NULL REFERENCES lineages(lineage_id),
  PRIMARY KEY(canonical_version, canonical_hash),
  UNIQUE(canonical_version, canonical_story)
);
CREATE TABLE candidate_facets(
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  facet TEXT NOT NULL,
  text TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  source_artifact_id TEXT REFERENCES artifacts(artifact_id),
  PRIMARY KEY(candidate_id, facet)
);
CREATE TABLE lineage_edges(
  parent_candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  child_candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  relation_type TEXT NOT NULL CHECK(relation_type IN ('evolved_from','recheck_of','supersedes')),
  evidence_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  PRIMARY KEY(parent_candidate_id, child_candidate_id, relation_type),
  CHECK(parent_candidate_id <> child_candidate_id)
);
CREATE TABLE artifacts(
  artifact_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('prepared','installed','archived')),
  sha256 TEXT NOT NULL,
  byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
  source_path TEXT NOT NULL,
  source_sequence INTEGER NOT NULL,
  producer_invocation_id TEXT REFERENCES invocations(invocation_id),
  provenance_json TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE invocations(
  invocation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  role TEXT NOT NULL,
  backend TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('prepared','started','completed','installed','failed')),
  process_instance_id TEXT NOT NULL,
  context_id TEXT NOT NULL,
  session_lineage_id TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  input_manifest_sha256 TEXT NOT NULL,
  output_artifact_id TEXT REFERENCES artifacts(artifact_id),
  idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE reviews(
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  seat INTEGER NOT NULL CHECK(seat >= 1),
  vote INTEGER NOT NULL CHECK(vote BETWEEN 0 AND 2),
  reason TEXT NOT NULL,
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  producer_invocation_id TEXT NOT NULL REFERENCES invocations(invocation_id),
  policy_version TEXT NOT NULL,
  PRIMARY KEY(candidate_id, seat),
  UNIQUE(candidate_id, producer_invocation_id)
);
CREATE TABLE near_sa_observations(
  observation_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  source_sequence INTEGER NOT NULL,
  sa_votes INTEGER NOT NULL CHECK(sa_votes >= 0),
  vote_vector TEXT NOT NULL,
  overlap TEXT NOT NULL,
  category TEXT NOT NULL,
  reason TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  UNIQUE(candidate_id, source_sequence)
);
CREATE TABLE history_receipts(
  receipt_id TEXT PRIMARY KEY,
  query_candidate_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  pack_sha256 TEXT NOT NULL,
  retrieval_policy_version TEXT NOT NULL,
  source_watermark INTEGER NOT NULL,
  index_generation INTEGER NOT NULL,
  comparator_version TEXT NOT NULL,
  status TEXT NOT NULL,
  receipt_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE search_projection_outbox(
  record_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  projection_kind TEXT NOT NULL,
  content_version TEXT NOT NULL,
  source_sequence INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','processing','done')),
  generation INTEGER NOT NULL CHECK(generation >= 0),
  claim_token TEXT,
  lease_until TEXT,
  CHECK(
    (state = 'processing' AND claim_token IS NOT NULL AND lease_until IS NOT NULL)
    OR
    (state IN ('pending','done') AND claim_token IS NULL AND lease_until IS NULL)
  ),
  PRIMARY KEY(record_id, projection_kind, content_version)
);
CREATE TABLE ledger_projection_outbox(
  projection_sequence INTEGER PRIMARY KEY,
  snapshot_sha256 TEXT NOT NULL,
  row_count INTEGER NOT NULL CHECK(row_count >= 0),
  state TEXT NOT NULL CHECK(state IN ('pending','processing','done')),
  generation INTEGER NOT NULL CHECK(generation >= 0),
  claim_token TEXT,
  lease_until TEXT,
  satisfied_by_sequence INTEGER,
  satisfied_by_sha256 TEXT,
  completed_at TEXT,
  CHECK(
    (state = 'processing' AND claim_token IS NOT NULL AND lease_until IS NOT NULL)
    OR
    (state IN ('pending','done') AND claim_token IS NULL AND lease_until IS NULL)
  ),
  CHECK(
    (state = 'done' AND satisfied_by_sequence IS NOT NULL
      AND satisfied_by_sha256 IS NOT NULL AND completed_at IS NOT NULL)
    OR
    (state IN ('pending','processing') AND satisfied_by_sequence IS NULL
      AND satisfied_by_sha256 IS NULL AND completed_at IS NULL)
  ),
  CHECK(satisfied_by_sequence IS NULL
        OR satisfied_by_sequence >= projection_sequence),
  UNIQUE(projection_sequence, snapshot_sha256),
  FOREIGN KEY(satisfied_by_sequence, satisfied_by_sha256)
    REFERENCES ledger_projection_outbox(projection_sequence, snapshot_sha256)
);
CREATE TABLE ledger_projection_receipts(
  projection_sequence INTEGER NOT NULL
    REFERENCES ledger_projection_outbox(projection_sequence),
  target TEXT NOT NULL CHECK(target IN ('ledger.tsv','tmp/ledger.good')),
  published_sequence INTEGER NOT NULL,
  snapshot_sha256 TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL,
  installed_at TEXT NOT NULL,
  PRIMARY KEY(projection_sequence, target),
  FOREIGN KEY(published_sequence, snapshot_sha256)
    REFERENCES ledger_projection_outbox(projection_sequence, snapshot_sha256)
);
```

The automatic AWR bridge remains deferred. This schema deliberately does not create `reentry_grants`, `reentry_requests`, `round_slots`, or `materialization_outbox`; a future AWR migration must add all of them atomically under the complete §5 contract. Retrieval uses only the separately named `ledger_projection_outbox` and `search_projection_outbox`.

`near_sa_observations` stores immutable vote/category facts, not a durable eligibility bit. `select_generation_parent()` recomputes eligibility from the current latest lineage winner, source, verdict, overlap, category, canonical story count, and latest observation inside the same read transaction, then applies the existing bounded newest-first priority. `tmp/near-sa-queue.tsv` is only a migration input and disposable compatibility projection.

Historical import follows the AWR §5 identity boundary. Before any DB write, `build_import_plan()` copies the ledger, explicit parent evidence, promotion receipts, and the versioned manual-mapping manifest into immutable content-addressed objects under `.ai-ideas/import-cas/`, fsyncs every object and parent, and writes a canonical input manifest and complete union plan. Exact canonical aliases, evidence-verified parent pointers, promotion attestations, and explicit mappings are the only union edges; similarity is excluded. The plan validates components, explicit roots for distinct parentless stories, alias ownership, the parent DAG, existing lineage anchors, row locations, and raw hashes.

`commit_import_plan()` inserts or verifies the epoch, lineages, aliases, candidates, typed edges, search-outbox entries, and the ledger-projection row in one transaction. Exact duplicate physical rows retain distinct origin-stable candidate IDs and share a deterministic root lineage. A cycle, multiple existing anchors, hash collision, ambiguous parent, changed row at an existing append-only location, or conflicting mapping rolls back the entire epoch. Retry after a crash uses the immutable plan and is byte-identical; later changes to live mappings cannot change a committed or resumable epoch.

Every later `add_lineage_edge()` call also runs under `BEGIN IMMEDIATE`, requires a durable explicit evidence artifact and an allowed non-similarity authority, and uses a recursive reachability query before insertion. Adding parent→child fails if child already reaches parent; import-time DAG validation is not the only cycle gate.

`ledger.instance-id` is read as one normalized nonempty line and never generated by the runtime. `origin_stable_id` is exactly:

```text
raw_row_sha = sha256(exact row bytes after removing one terminal LF or CRLF)
sha256("tsv-row-v2\0" + ledger_instance_id + "\0"
       + decimal(data_row_ordinal) + "\0" + raw_row_sha)
```

Snapshot SHA never enters candidate identity. Import retry validates ledger instance, row ordinal, and raw SHA against the sealed plan.

Import candidate IDs are the domain-separated SHA-256 of `candidate-import-v1` and `origin_stable_id`. A new lineage ID is `sha256("tsv-v1\0" + UTF8(root canonical story))`; an evidence-backed mapping with multiple distinct parentless stories must name its root explicitly. A legacy near-SA row resolves to exactly one imported candidate and stored source sequence or fails as ambiguous.

Every business transaction that changes the TSV projection must increment the monotonic projection sequence and insert one `ledger_projection_outbox` row in the same `BEGIN IMMEDIATE` transaction as candidates and search-outbox entries. Claim, renewal, and completion transactions never increment that sequence.

`materialize_ledger_projection()` must:

1. Acquire a kernel advisory export lock, then claim the newest required projection with a fenced token; retain the lock through publication and DB marking.
2. Render the current complete DB projection, then revalidate claim token, current projection sequence, SHA-256, and row count before any publication.
3. Write and fsync immutable `.ai-ideas/ledger-snapshots/<sequence>-<sha256>.tsv`, atomically rename it, and fsync its parent.
4. Publish and fsync `.ai-ideas/ledger-current.json` as the durable `(sequence, hash, row_count, immutable_object)` pointer. A lower pointer advances; an exact pointer is idempotent; equal sequence with another hash or a pointer ahead of the DB fails closed.
5. Atomically replace `ledger.tsv`, reread and hash it, then emit and fsync its canonical target receipt under `.ai-ideas/ledger-target-receipts/`.
6. Atomically replace `tmp/ledger.good`, reread and hash it, then emit and fsync its canonical target receipt.
7. Revalidate both targets, both receipts, the durable pointer, current DB sequence, and claim token. Mark the current row and older pending rows done with `satisfied_by_sequence` only when the same DB proves that the monotonic full projection contains those earlier commits.

Startup reconciliation treats the newest complete DB projection as authority. It replays any missing durability step, repairs a stale or missing target, handles a crash after an effect but before its DB receipt, refuses equal-sequence/different-hash or pointer-ahead state, and never re-appends a business row. A stale consumer cannot publish after a newer one because claim, pointer publication, and completion share the export lock and are revalidated against DB sequence and token. `export_tsv()` remains an explicit audit export to an operator-chosen path; runtime publication uses the outbox protocol.

Completion rows and target receipts bind `(published_sequence, snapshot_sha256)` through composite foreign keys. Two-consumer tests cover live-lease exclusion, expired-lease fencing, stale-token publication and marking failures, one durable publication by the replacement claim, and verified satisfaction of older pending rows by the newer full projection.

- [ ] **Step 4: Run store tests and baseline contract**

Run:

```bash
python3 tests/history_store_smoke.py
python3 tests/verify_product_contract.py all
```

Expected: both commands PASS; exported fixture bytes match exactly.

- [ ] **Step 5: Update offline contributor commands and commit**

Add `python3 tests/history_store_smoke.py` and the ledger reconciliation command to `CONTRIBUTING.md`.

Commit:

```bash
git add .gitignore CONTRIBUTING.md ledger.instance-id lib/history_store.py lib/history_cli.py tests/fixtures/near-sa-queue.tsv tests/history_store_smoke.py
git commit -m "feat: add canonical idea history store"
```

---

### Task 2: Search Projections, Failure Codes, and Generation Brief

**Files:**
- Create: `lib/history_budget.py`
- Create: `lib/history_projection.py`
- Create: `history/retrieval-policy-v1.json`
- Create: `tests/history_budget_smoke.py`
- Create: `tests/history_projection_smoke.py`
- Modify: `lib/history_cli.py`
- Modify: `lib/history_store.py`
- Modify: `CONTRIBUTING.md`

**Interfaces:**
- Consumes: pending `search_projection_outbox` rows and canonical facets.
- Produces: exact lookup, FTS5 rows, 256-dimensional vectors, published index generations, structured failure codes, `generation_brief.json`, exact invocation serialization, and fail-closed stage-budget receipts.
- CLI: `rebuild-projections`, `recover-projections`, and `build-brief`.

- [ ] **Step 1: Write failing projection and brief tests**

```python
def test_noop_rebuild_embeds_zero_facets(self):
    first = projection.rebuild(self.conn, self.policy)
    second = projection.rebuild(self.conn, self.policy)
    self.assertGreater(first["embedded_facets"], 0)
    self.assertEqual(second["embedded_facets"], 0)

def test_incremental_matches_clean_rebuild(self):
    projection.rebuild(self.conn, self.policy)
    incremental = self._rank("confidence gated world model")
    clean = self._clean_rebuild_and_rank("confidence gated world model")
    self.assertEqual(incremental, clean)

def test_change_and_delete_update_only_affected_facets(self):
    changed = projection.update_candidate_facets(
        self.conn, self.candidate_id, {"mechanism": "new mechanism"}
    )
    self.assertEqual(changed["queued_facets"], 1)
    projection.rebuild(self.conn, self.policy)
    projection.remove_candidate_from_search(self.conn, self.candidate_id)
    self.assertNotIn(self.candidate_id, self._all_searchable_ids())
    self.assertIsNotNone(history_store.get_candidate(self.conn, self.candidate_id))

def test_generation_brief_is_bounded_and_has_one_parent(self):
    brief = projection.build_generation_brief(self.conn, self.policy)
    self.assertLessEqual(len(brief.get("parents", [])), 1)
    self.assertNotIn("ledger_rows", brief)
    self.assertLessEqual(brief["estimated_tokens"], self.policy["max_retrieval_tokens"])

def test_exact_generation_invocation_fits_before_backend_call(self):
    invocation = budget.serialize_stage_invocation(
        stage="generate",
        adapter_version="history-stage-v1",
        fixed_instructions=self.generate_role.read_text(),
        mounted_inputs={"generation_brief.json": self.brief_bytes},
        candidate=None,
        retrieval_payload=None,
        receipts=[],
        tool_schemas=[],
        messages=[{"role": "user", "content": "Generate candidates."}],
    )
    receipt = budget.preflight_stage_invocation(
        invocation, self.policy, tokenizer=None
    )
    self.assertTrue(receipt["fits"])

def test_unknown_adapter_or_unverified_allowance_fails_closed(self):
    with self.assertRaises(budget.PreflightError):
        budget.preflight_stage_invocation(
            self.invocation,
            dict(self.policy, adapter_version="unknown-adapter"),
            tokenizer=None,
        )

def test_exact_tokenizer_boundary_does_not_add_byte_allowance(self):
    tokenizer = RecordingTokenizer(fixed_count=100)
    invocation = budget.serialize_stage_invocation(
        **dict(self.minimal_invocation, messages=[
            {"role": "user", "content": "Compare naïve café policy to baseline."}
        ])
    )
    at_limit = dict(
        self.policy,
        model_context_limit=100
            + self.policy["max_output_tokens"]
            + self.policy["safety_margin"],
    )
    receipt = budget.preflight_stage_invocation(
        invocation, at_limit, tokenizer=tokenizer
    )
    self.assertEqual(receipt["count_method"], "exact_tokenizer")
    self.assertEqual(receipt["input_upper_bound"], 100)
    self.assertEqual(tokenizer.argument_sha256, hashlib.sha256(invocation).hexdigest())
    with self.assertRaises(budget.PreflightError):
        budget.preflight_stage_invocation(
            invocation,
            dict(at_limit, model_context_limit=at_limit["model_context_limit"] - 1),
            tokenizer=tokenizer,
        )

def test_fallback_boundary_and_one_byte_over(self):
    serialized = budget.serialize_stage_invocation(**self.minimal_invocation)
    exact_limit = (
        len(serialized)
        + self.policy["adapter_wrapper_allowance"]
        + self.policy["max_output_tokens"]
        + self.policy["safety_margin"]
    )
    receipt = budget.preflight_stage_invocation(
        serialized, dict(self.policy, model_context_limit=exact_limit)
    )
    self.assertEqual(receipt["count_method"], "utf8_byte_upper_bound")
    with self.assertRaises(budget.PreflightError):
        budget.preflight_stage_invocation(
            serialized + b"x",
            dict(self.policy, model_context_limit=exact_limit),
        )
```

- [ ] **Step 2: Run projection tests and verify RED**

Run:

```bash
python3 tests/history_projection_smoke.py
python3 tests/history_budget_smoke.py
```

Expected: FAIL because projection, serialization, preflight, and the policy file do not exist.

- [ ] **Step 3: Implement deterministic facet extraction and projections**

Use these facet names exactly:

```python
FACETS = (
    "problem_estimand",
    "claimed_delta",
    "mechanism",
    "evaluation_expected_signal",
    "setting_task",
    "entities_datasets_methods",
)
```

Historical TSV import uses the story for `problem_estimand` and `claimed_delta`, theme plus story for `setting_task`, and deterministic token extraction for entities. New round artifacts additionally map `Summary:` to `mechanism` and `Minimal Falsification Experiment:` to `evaluation_expected_signal`.

Implement `hash-ngram-v1` exactly as fixed in Global Constraints. Store float32 vectors as BLOB plus model, revision, preprocessing version, dimensions, metric, content hash, and L2 norm.

Assign deterministic failure codes from verdict/category/reason:

```python
FAILURE_CODES = (
    "direct-hit", "strong-baseline", "statistical-power", "estimand",
    "attribution-control", "weak-prior-work", "novelty-cap",
    "feasibility", "evidence-incomplete", "other",
)
```

- [ ] **Step 4: Implement generation brief and published-generation recovery**

`build_generation_brief()` emits:

```json
{
  "schema_version": 1,
  "retrieval_policy_version": "retrieval-policy-v1",
  "source_watermark": 531,
  "index_generation": 1,
  "theme_counts": {},
  "failure_code_counts": {},
  "parent": null,
  "research_context": null,
  "estimated_tokens": 0
}
```

It must select at most one eligible evolution/recheck parent using the existing category, overlap, story-count, and near-SA priority rules. It must never emit raw ledger rows or an unbounded story list.

- [ ] **Step 5: Implement exact stage serialization and generation preflight**

`serialize_stage_invocation()` returns canonical UTF-8 `bytes` and must length-bind every byte the model can receive:

```text
adapter version and fixed wrapper
stage role instructions
all allowlisted mounted text inputs with relative paths and SHA-256
candidate content
retrieval payload
tool receipts and tool schemas
ordered message wrappers
output schema instructions
```

The returned bytes are the exact final prompt argument passed to the backend adapter without a second serialization step. The tokenizer interface receives those exact bytes, decodes UTF-8 internally as required by the target tokenizer, and returns an exact count; preflight does not add the byte-fallback allowance on that path. Otherwise it uses `len(serialized_bytes)` and adds the allowance only when the adapter version and allowance match the tested policy entry. It then adds `max_output_tokens` and `safety_margin`. Equality with the context limit is allowed; one token or fallback byte over is rejected. A non-ASCII recording-tokenizer test proves the tokenizer argument hash equals the serialized-input hash. A missing input, hidden unmounted prompt fragment, unknown adapter, unverified allowance, or over-budget invocation returns a structured failure before process launch. Generation preflight includes the fully serialized `generation_brief.json`, not its `estimated_tokens` field alone.

- [ ] **Step 6: Run projection, budget, store, and contract tests**

Run:

```bash
python3 tests/history_budget_smoke.py
python3 tests/history_projection_smoke.py
python3 tests/history_store_smoke.py
python3 tests/verify_product_contract.py all
```

Expected: all PASS; no-op rebuild reports zero new embeddings and generation preflight accounts for every mounted byte.

- [ ] **Step 7: Commit**

```bash
git add CONTRIBUTING.md history/retrieval-policy-v1.json lib/history_budget.py lib/history_cli.py lib/history_projection.py lib/history_store.py tests/history_budget_smoke.py tests/history_projection_smoke.py
git commit -m "feat: build bounded history projections"
```

---

### Task 3: Hybrid Retrieval, Token Preflight, and Replayable Receipts

**Files:**
- Create: `lib/history_retrieval.py`
- Create: `tests/history_retrieval_smoke.py`
- Modify: `lib/history_budget.py`
- Modify: `lib/history_cli.py`
- Modify: `CONTRIBUTING.md`

**Interfaces:**
- Consumes: candidate facets, intent, published index generation, and retrieval policy.
- Produces: deterministic retrieval traces, `retrieval_pack.json`, validated comparison outcomes, and `history_receipt.json`.
- CLI: `retrieve`, `finalize-comparison`, and `replay-receipt`.

- [ ] **Step 1: Write failing retrieval-contract tests**

```python
def test_every_missing_required_channel_is_partial_for_each_intent(self):
    intents = (
        "duplicate_search", "evolution_search", "failure_pattern_search"
    )
    required = ("exact", "fts", "dense", "lineage")
    for intent in intents:
        for channel in required:
            with self.subTest(intent=intent, channel=channel):
                pack = retrieval.build_pack(
                    self.conn, self.query, intent, self.policy,
                    disabled_channels={channel},
                )
                self.assertEqual(pack["retrieval_status"], "partial")

def test_not_applicable_and_requested_expansion_contract(self):
    initial = retrieval.build_pack(
        self.conn, self.query, "duplicate_search", self.policy
    )
    self.assertEqual(initial["channels"]["expansion"]["status"], "not_applicable")
    expanded = retrieval.build_pack(
        self.conn, self.query, "duplicate_search", self.policy,
        expansion_request={"lineage_ids": [self.lineage_id]},
        disabled_channels={"expansion"},
    )
    self.assertEqual(expanded["retrieval_status"], "partial")

def test_cutoff_lineage_cannot_be_dropped_to_fit_budget(self):
    tiny = dict(self.policy, max_retrieval_tokens=1)
    pack = retrieval.build_pack(self.conn, self.query, "duplicate_search", tiny)
    self.assertEqual(pack["retrieval_status"], "budget_exceeded")

def test_comparator_preflight_serializes_pack_and_receipts(self):
    invocation = budget.serialize_stage_invocation(
        stage="history-compare",
        adapter_version=self.policy["adapter_version"],
        fixed_instructions=self.compare_role,
        mounted_inputs={},
        candidate=self.query,
        retrieval_payload=self.complete_pack,
        receipts=[self.tool_receipt],
        tool_schemas=[self.output_schema],
        messages=[{"role": "user", "content": "Compare the candidate."}],
    )
    receipt = budget.preflight_stage_invocation(invocation, self.policy)
    self.assertIn(self.complete_pack["pack_sha256"], receipt["input_sha256s"])

def test_noncomplete_receipt_forbids_permanent_conclusion(self):
    for status in ("partial", "backend_failed", "budget_exceeded",
                   "uncertain", "conflicting_evidence"):
        self.assertFalse(retrieval.permits_permanent_conclusion({"status": status}))

def test_comparison_cannot_reference_evidence_outside_pack(self):
    for field, value in (
        ("candidate_id", "outside-candidate"),
        ("lineage_id", "outside-lineage"),
        ("facet", "outside-facet"),
        ("evidence_id", "outside-evidence"),
    ):
        response = dict(self.valid_response)
        response["relations"] = [dict(response["relations"][0], **{field: value})]
        with self.subTest(field=field):
            with self.assertRaises(retrieval.ComparisonValidationError):
                retrieval.finalize_comparison(
                    self.conn, self.complete_pack, response, self.policy
                )
```

- [ ] **Step 2: Run retrieval tests and verify RED**

Run: `python3 tests/history_retrieval_smoke.py`

Expected: FAIL because retrieval-pack and receipt code does not exist.

- [ ] **Step 3: Implement exact, FTS, exhaustive dense, lineage, and RRF retrieval**

Each channel returns stable candidate IDs, lineage IDs, facet, raw score, rank, source location, and evidence span. Union candidates across channels, fuse ranks with `rrf_k=60`, then group by lineage.

Mandatory channels follow the full design matrix. Duplicate and evolution exact lookup use story hash plus aliases; failure-pattern exact lookup uses structured failure-code equality. `not_applicable` is accepted only for a conditional channel that the comparator has not requested. A conditional expansion becomes mandatory after a comparator requests it.

- [ ] **Step 4: Implement pack reduction and reuse exact stage preflight**

Use `history_budget.serialize_stage_invocation()` and `preflight_stage_invocation()` for comparator invocations. With the verified fallback:

```python
input_upper_bound = len(serialized_invocation_bytes) + wrapper_allowance
fits = input_upper_bound + max_output_tokens + safety_margin <= model_context_limit
```

Reduction order is relevant facets, extractive spans, then lineages below the calibrated cutoff. If any lineage inside the cutoff must be dropped, emit `budget_exceeded` and do not invoke a comparator.

- [ ] **Step 5: Implement comparison validation and receipt replay**

Accept only:

```python
RELATIONS = {
    "same_core_idea", "same_lineage_revision", "related_component",
    "same_failure_mechanism", "related_failure_pattern", "distinct", "uncertain",
}
FINAL_STATUSES = {
    "complete_match", "complete_no_match", "uncertain", "partial",
    "backend_failed", "budget_exceeded", "conflicting_evidence",
}
```

Every referenced candidate, lineage, facet, and evidence ID must exist in the pack. The host constructs the receipt from the pack SHA-256 and validated comparison; the agent never writes the final receipt. Replay verifies policy, source watermark, index generation, pack hash, comparator version, and evidence references.

- [ ] **Step 6: Run retrieval and lower-layer tests**

Run:

```bash
python3 tests/history_retrieval_smoke.py
python3 tests/history_projection_smoke.py
python3 tests/history_store_smoke.py
python3 tests/verify_product_contract.py all
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add CONTRIBUTING.md lib/history_budget.py lib/history_cli.py lib/history_retrieval.py tests/history_retrieval_smoke.py
git commit -m "feat: add bounded hybrid history retrieval"
```

---

### Task 4: Contained Generation and Comparator Stages

**Files:**
- Create: `lib/history_stage.py`
- Create: `roles/history-compare.md`
- Create: `tests/history_mirror_smoke.sh`
- Create: `tests/malicious_history_agent.sh`
- Modify: `roles/generate.md`
- Modify: `roles/meta.md`
- Modify: `roles/review.md`
- Modify: `tests/fake_agent.sh`
- Modify: `CONTRIBUTING.md`

**Interfaces:**
- Consumes: an allowlist manifest, a resolved backend command, adapter version, stage policy, and output allowlist.
- Produces: a preflight receipt plus copied-back stage artifacts only after containment and output validation.
- CLI: `python3 lib/history_stage.py run --stage generate|history-compare|review|meta --manifest PATH --command JSON`.

- [ ] **Step 1: Write the malicious-backend mirror smoke test**

The fake backend must attempt to read absolute sentinels in the real repository:

```bash
if [ -r "$REAL_ROOT/ledger.tsv" ]; then exit 70; fi
if [ -r "$REAL_ROOT/.ai-ideas/history.sqlite3" ]; then exit 71; fi
if [ -r "$REAL_ROOT/.git/HEAD" ]; then exit 72; fi
printf '%s\n' '{"ok":true}' > tmp/round/stage-output.json
```

It must also compute the SHA-256 of its exact final prompt argument and place it in the allowed stage output. Run this attack separately as `generate`, `history-compare`, `meta`, and two independent `review` seats. Each review attack also attempts to read the other seat's output. The test must assert that each stage succeeds, the adapter-observed prompt SHA equals the preflight serialized-input SHA, all attempted reads fail, each mirror contains only its stage allowlist, no reviewer can read a sibling output, and no write outside the mirror appears.

Add negative cases that mutate a mounted input after manifest hashing, add an unregistered wrapper fragment, change adapter version or allowance, replace an input with a symlink, and exceed the budget by one token. Every case must fail before the fake adapter records a call.

- [ ] **Step 2: Run mirror smoke and verify RED**

Run: `bash tests/history_mirror_smoke.sh`

Expected: FAIL because the contained stage runner does not exist.

- [ ] **Step 3: Implement fail-closed containment**

On Darwin, use `sandbox-exec` with a generated default-deny profile. Allow reads only from the temporary mirror, fixed system runtime paths, the resolved backend executable and its pinned runtime dependencies, and the minimum registered backend configuration paths. Explicitly deny the real repository, `.ai-ideas-runs`, repository Git metadata, unregistered home/config paths, and sibling reviewer mirrors. Allow writes only inside the current mirror. Combine this with the backend's workspace-write sandbox where applicable.

On Linux, use `bwrap` with only fixed system/runtime paths and registered backend dependencies mounted read-only; do not mount the repository root, state root, run archive, home, or sibling mirrors. Mount only the current mirror writable. If neither mechanism is present, exit before invoking the backend.

Create mirrors outside the real repository with `mktemp`. Reject manifest symlinks, hardlinks, special files, absolute paths, `..`, duplicate normalized paths, and any resolved path outside the declared input roots. Copy only regular allowlisted entries, verify each copied SHA against the manifest, make inputs and their parent directories read-only, omit `.git`, and expose a separate writable output directory. Set the mirror as CWD, validate outputs with `lstat` plus no-follow reads, copy allowed artifacts back atomically, and remove the mirror.

Before process launch, serialize the fixed wrapper, role, every mounted input, prompt/message, tool schema, candidate, retrieval pack, and receipt through `history_budget.py`. Rehash read-only mirror inputs immediately before launch. Pass the already-preflighted serialized bytes directly as the backend's final prompt argument; do not reconstruct the prompt. Write the preflight receipt into the run archive. Any input drift, hidden byte, unknown adapter, unverified wrapper allowance, or budget failure exits without executing the backend. The same path is mandatory for generation, comparator, meta, and every reviewer seat.

- [ ] **Step 4: Update roles and fake backend**

`roles/generate.md` reads `generation_brief.json`, policy, and optional bounded research context. Remove all ledger, deathlist, queue, and direct-scan instructions.

`roles/meta.md` becomes an isolated optional distillation role that reads only a bounded batch artifact; routine failure counts come from SQLite.

`roles/history-compare.md` reads only the candidate and one validated pack, emits JSON with relation, referenced IDs/facets/evidence, material differences, confidence, and optional bounded expansion request, and performs no repository search.

`roles/review.md` receives only the frozen candidate, external prior-work artifact, rubric and policy, and an optional receipt-derived bounded history summary. It must not search repository history or read any other reviewer output. Each reviewer invocation receives a fresh mirror and independent process context.

Extend `tests/fake_agent.sh` to produce a valid comparator response.

- [ ] **Step 5: Run mirror and role contract tests**

Run:

```bash
bash tests/history_mirror_smoke.sh
python3 tests/history_budget_smoke.py
python3 tests/verify_product_contract.py runtime
python3 tests/runtime_policy_smoke.py
bash -n tests/history_mirror_smoke.sh tests/malicious_history_agent.sh tests/fake_agent.sh
```

Expected: all PASS; malicious absolute reads fail under real OS containment.

- [ ] **Step 6: Commit**

```bash
git add CONTRIBUTING.md lib/history_stage.py roles/generate.md roles/history-compare.md roles/meta.md roles/review.md tests/fake_agent.sh tests/history_mirror_smoke.sh tests/malicious_history_agent.sh
git commit -m "feat: contain history-aware agent stages"
```

---

### Task 5: Atomic `hunt.sh` and Protocol Cutover

**Files:**
- Create: `tests/history_runtime_smoke.sh`
- Modify: `hunt.sh`
- Modify: `PROGRAM.md`
- Modify: `roles/research.md`
- Modify: `roles/review.md`
- Modify: `tests/runtime_abi_smoke.sh`
- Modify: `tests/fake_agent.sh`
- Modify: `tests/verify_product_contract.py`
- Modify: `CONTRIBUTING.md`

**Interfaces:**
- Consumes: SQLite history state, published projections, generated candidates, comparisons, external prior work, and votes.
- Produces: bounded briefs, per-candidate traces/packs/receipts, archived receipts, transactional canonical rows, and stable TSV export.
- Shell helpers: `history_sync`, `history_reconcile_ledger`, `history_policy_mode`, `history_build_brief`, `run_contained_stage`, `history_observe_round`, `history_compare_shortlist`, `history_receipts_ok`, `history_append_rows`, and `history_materialize_ledger`.

- [ ] **Step 1: Write failing end-to-end runtime cases**

`tests/history_runtime_smoke.sh` must freeze one generated `ideas.all.md`/`ideas.all.tsv` batch, run the downstream reference path with history observation disabled, then replay that identical batch with observation enabled:

```text
shadow + every status -> for the frozen candidate batch, same selector, prescreen,
                         research/review inputs, verdict, ledger delta, and
                         near-SA decision as the downstream reference
shadow + complete pack -> comparator artifact archived but never enters downstream prompts
shadow + noncomplete pack -> comparator not called; infrastructure status archived
enforcement without matching sealed calibration -> startup fails before agent invocation
enforcement + complete_no_match -> normal external research/review and one exported row
enforcement + complete_match -> evidence reaches research/review but creates no automatic verdict
enforcement + partial/backend_failed/budget_exceeded/uncertain/conflicting_evidence
            -> affected candidate creates no ledger row
enforcement + budget_exceeded -> comparator not called
resume -> only matching policy/watermark/pack receipts are reused
archive -> pack, comparison, and receipt are preserved
projection crash after DB commit/ledger.tsv/tmp/ledger.good -> startup reconciliation
            converges both targets to the DB snapshot without a duplicate row
review seats -> independent contained mirrors cannot read repository history or sibling output
```

Each case runs with `tests/fake_agent.sh`; no external backend is invoked. The enforcement cases construct an ephemeral test-scope capability and signed pre-held-out receipt using a test-only trust root passed directly to the validator. The production runtime never loads that trust root. These fixtures prove gate wiring only and are never installed as repository policy or reported as retrieval quality.

- [ ] **Step 2: Run runtime smoke and verify RED**

Run: `bash tests/history_runtime_smoke.sh`

Expected: FAIL because `hunt.sh` does not create or gate history artifacts.

- [ ] **Step 3: Switch startup and write ownership to SQLite**

At startup:

1. If the database does not exist, import an explicitly validated operator ledger baseline and any legacy near-SA queue snapshot in one migration epoch.
2. If the database exists, treat it as authority and reconcile pending `ledger_projection_outbox` work before consulting either TSV target.
3. Verify `ledger.tsv` and `tmp/ledger.good` against the current immutable DB snapshot and durable target receipts.
4. Recover pending search projections.
5. Publish a clean index generation.
6. Validate policy mode and its calibration capability.
7. Build and preflight the generation brief.

Prescreen kills and aggregated rows are written to a temporary TSV delta and committed with `history_cli.py append-tsv`. The same DB transaction creates search and ledger projection outbox work. A separate idempotent materializer publishes one immutable snapshot to `ledger.tsv` and `tmp/ledger.good`, records both target receipts, and marks completion. It never performs two untracked `cp` operations. Startup reconciliation covers a crash after the DB commit, either target rename, either receipt, and before the final outbox mark.

New round vote vectors and category facts append canonical `near_sa_observations` in the same business transaction. Parent selection recomputes current eligibility from the latest lineage winner and immutable observations. `tmp/near-sa-queue.tsv` may be refreshed as a compatibility projection, but deleting it cannot change priority.

- [ ] **Step 4: Replace routine meta and unbounded generation**

Remove the model `meta` call from the normal round. Build `generation_brief.json` deterministically.

Run `generate` through `history_stage.py` with only:

```text
roles/generate.md
brainstorming_policy.md
generation_brief.json
optional bounded research_context.md
tmp/round output directory
```

Do not expose the database, ledger, indexes, `.git`, or run archives.

Run the exact serialized generation invocation through `history_budget.py` before backend launch. A brief that fits its internal field budget but makes the complete role-plus-wrapper invocation exceed the stage limit returns `budget_exceeded` without calling the backend.

- [ ] **Step 5: Add deterministic retrieval and bounded comparison**

After contained generation, create duplicate and failure-pattern traces for every candidate; create evolution traces when the candidate declares a parent. This observation path runs before selection so its artifacts bind the original candidate bytes, but mode determines whether it may affect the existing protocol.

In `shadow` mode, for the same frozen generated-candidate batch:

- run the existing selector, prescreen, research, review, aggregation, near-SA handling, and ledger commit with the same inputs and decisions as the reference pipeline;
- run the comparator only for complete packs and archive all retrieval outcomes;
- do not place relation evidence in research or review prompts;
- do not suppress, reorder, retry, or reclassify a candidate because of any history status.

In `enforcement` mode, available only with a matching sealed calibration artifact:

- run selector and prescreen without ledger mutation;
- build complete packs and run the contained comparator for every validated prescreen kill and every candidate that would enter the `SHORT_MAX` shortlist;
- require a validated complete receipt before recording a prescreen kill or starting deep research;
- forward only evidence-addressed complete-match relations as a bounded history summary;
- stop the affected candidate without ledger mutation on `partial`, `backend_failed`, `budget_exceeded`, `uncertain`, or `conflicting_evidence`;
- preserve the normal external occupying-work rule and require normal review for every permanent verdict.

If a comparator requests named-record or named-lineage expansion, rebuild the pack with only those allowed identifiers and rerun the comparator. Stop after `max_expansion_rounds`; an unresolved result remains `uncertain`.

Research and review may read a bounded history summary derived from receipts only in enforcement mode. They must treat `complete_no_match` as an internal scoped result, not novelty. Every reviewer still runs through a fresh contained `review` stage with only candidate, external prior work, rubric, policy, and the optional bounded summary.

- [ ] **Step 6: Extend resume, archive, guard, and receipt gates**

Resume accepts history artifacts only when policy mode, policy version, source watermark, index generation, pack SHA, comparator version, candidate content hash, adapter version, and preflight input hash match.

Archive `generation_brief.json`, all traces, packs, comparisons, receipts, policy, calibration capability reference, preflight receipts, target receipts, and projection receipt. Enforcement requires complete receipts for every affected candidate before any prescreen or review ledger append. Shadow archives the same evidence but never uses receipt completeness as a decision gate.

- [ ] **Step 7: Update the runtime protocol atomically**

`PROGRAM.md` must describe:

```text
brief -> contained generation -> model-free history retrieval -> selection
-> contained comparator observation/gate -> external prescreen/research
-> contained independent reviews -> DB commit -> replayable TSV projection
```

Define both policy modes explicitly. Shadow is the shipped default: generation still uses the new bounded brief, while history retrieval has observational equivalence only from a fixed generated batch through the downstream decision path. Enforcement requires a matching sealed production calibration contract. Update `roles/research.md` and `roles/review.md` to consume bounded history evidence only when enforcement supplies it and never treat it as academic novelty.

- [ ] **Step 8: Run all runtime and compatibility tests**

Run:

```bash
bash tests/history_runtime_smoke.sh
bash tests/runtime_abi_smoke.sh
bash tests/calibration_abi_smoke.sh
python3 tests/runtime_policy_smoke.py
python3 tests/verify_product_contract.py runtime
python3 tests/verify_product_contract.py fixtures
python3 tests/history_store_smoke.py
bash -n hunt.sh
```

Expected: all PASS; shadow is retrieval-decision-equivalent for a frozen candidate batch, enforcement fails closed without calibration, enforcement failure statuses do not change the ledger, and every projection crash converges.

- [ ] **Step 9: Commit**

```bash
git add CONTRIBUTING.md PROGRAM.md hunt.sh roles/research.md roles/review.md tests/fake_agent.sh tests/history_runtime_smoke.sh tests/runtime_abi_smoke.sh tests/verify_product_contract.py
git commit -m "feat: cut over hunt to bounded history retrieval"
```

---

### Task 6: Benchmark Contract and Evaluation Harness

**Files:**
- Create: `calib/history-retrieval/README.md`
- Create: `calib/history-retrieval/synthetic/queries.jsonl`
- Create: `calib/history-retrieval/synthetic/qrels.jsonl`
- Create: `calib/history-retrieval/synthetic/adjudications.jsonl`
- Create: `calib/history-retrieval/synthetic/folds.json`
- Create: `calib/history-retrieval/synthetic/corpus.jsonl`
- Create: `calib/history-retrieval/synthetic/oracle-packs.jsonl`
- Create: `calib/history-retrieval/synthetic/policy-commitment.json`
- Create: `calib/history-retrieval/synthetic/pre-heldout-receipt.json`
- Create: `calib/history-retrieval/synthetic/test-witness-key.json`
- Create: `calib/history-retrieval/synthetic/outputs/retrieval-only.jsonl`
- Create: `calib/history-retrieval/synthetic/outputs/comparator-only.jsonl`
- Create: `calib/history-retrieval/synthetic/outputs/end-to-end.jsonl`
- Create: `calib/history-retrieval/synthetic/outputs/closed-book.jsonl`
- Create: `calib/history-retrieval/synthetic/expected-metrics.json`
- Create: `calib/history-retrieval/calibration-policy-commitment.schema.json`
- Create: `calib/history-retrieval/calibration-pre-heldout-receipt.schema.json`
- Create: `calib/history-retrieval/calibration-capability.schema.json`
- Create: `lib/history_eval.py`
- Create: `tests/verify_history_retrieval_benchmark.py`
- Modify: `lib/history_cli.py`
- Modify: `CONTRIBUTING.md`

**Interfaces:**
- Consumes: versioned policy, pre-held-out policy commitment and trusted-runner receipt, temporal corpus, queries, qrels, adjudications, folds, oracle packs, and four-arm system outputs.
- Produces: validated benchmark metadata and metrics for retrieval-only, oracle-comparator, end-to-end, and closed-book arms, plus validation of a sealed enforcement capability.
- CLI: `history_cli.py evaluate --benchmark PATH --output PATH`.

- [ ] **Step 1: Write failing benchmark-verifier tests**

Tests must reject:

```text
future record visible to an as-of query
one lineage split across train/calibration/test
missing no-hit queries
future verdict/reason/citation leaked into query text
missing second adjudication or unresolved disagreement
oracle pack without the gold IDs
unjudged pair treated as negative
policy thresholds sealed after held-out results
synthetic_contract_only artifact used to enable enforcement
calibration artifact whose policy, benchmark snapshot, qrels, adjudications,
or selected thresholds do not match its seal
policy commitment containing held-out labels, adjudications, outputs, or metrics
held-out output missing or changing its policy-commitment SHA
mutated commitment thresholds, split SHA, or calibration-query set
missing or invalid trusted-runner signature on the pre-held-out receipt
commitment resealed after the held-out run started
```

- [ ] **Step 2: Run verifier and verify RED**

Run: `python3 tests/verify_history_retrieval_benchmark.py calib/history-retrieval/synthetic`

Expected: FAIL because the verifier and synthetic contract fixtures do not exist.

- [ ] **Step 3: Implement benchmark schemas and four-arm metrics**

Use gains:

```text
duplicate: blocking=2, substantive=1, unrelated=0
lineage: direct-parent=3, ancestor-or-descendant=2, sibling=1, unrelated=0
failure: same-mechanism=2, related-defect=1, unrelated=0
```

Compute Hit@K, MRR@10, nDCG@10, Recall@K, relation precision/recall, false-duplicate, false-internal-no-match, abstention accuracy, evidence precision/recall, unsupported-claim rate, p50/p95 latency, and tokens per query. Paired bootstrap uses a fixed seed recorded in output.

Every arm uses a versioned JSONL output schema with:

```text
query_id, corpus_watermark, ranked_record_ids, retrieval_pack_id,
relation, abstained, evidence_ids, status, latency_ms, input_tokens,
comparator_pairs, policy_commitment_sha256, preheldout_receipt_sha256,
heldout_run_nonce
```

Fields not used by an arm remain explicit empty values. The evaluator validates query coverage, temporal watermark, stable tie ordering, referenced packs/evidence, and policy-commitment hash before metrics. Its output reports each arm separately and records every input SHA. The synthetic outputs cover an unjudged pair, explicit no-hit false positive, unsupported evidence, abstention, tied ranks at a cutoff, and fixed-seed paired bootstrap. `expected-metrics.json` contains hand-checked exact values and tolerances; a mismatch fails the verifier.

- [ ] **Step 4: Document the non-fabrication boundary**

`calib/history-retrieval/README.md` must state that synthetic fixtures validate schemas and metric code only. Production thresholds require two independent human judgments, a third adjudication of disagreements, temporal snapshots, and at least 30 held-out positives plus 30 hard negatives per automated relation.

`calibration-policy-commitment.schema.json` seals thresholds and error budgets selected from training/calibration, policy SHA, fold/split SHA, calibration-query ID hash, held-out-query ID hash, and benchmark input hashes. It excludes held-out qrels, adjudications, outputs, and metrics.

`calibration-pre-heldout-receipt.schema.json` binds the commitment SHA, split SHA, trusted-runner release SHA, monotonically allocated run nonce, witness time, trust-root ID, and signature. The trusted runner writes and signs this receipt under its journal lock before it opens any held-out labels or output path. Only then may the held-out runner proceed; every output row binds both receipt SHA and nonce. Production validation requires a configured external or trusted-runner witness verifier. A bare local hash or timestamp is insufficient. Synthetic tests use an explicit test-only trust root that production rejects.

`calibration-capability.schema.json` binds the policy commitment and pre-held-out receipt SHAs, policy version and SHA, temporal benchmark snapshot SHA, qrels and adjudication SHAs, relation-level held-out counts, held-out evaluation output SHA, and a canonical seal SHA. `validate_calibration_capability()` must reject synthetic scope, advisory relations, insufficient counts, unresolved adjudication, a missing or invalid witness signature, a receipt that does not precede held-out start, any commitment/output/nonce mismatch, held-out data in the commitment, or any hash mutation. The repository ships no production capability or trust root; therefore the committed policy remains `shadow`.

- [ ] **Step 5: Run evaluation tests**

Run:

```bash
python3 tests/verify_history_retrieval_benchmark.py calib/history-retrieval/synthetic
python3 lib/history_cli.py evaluate --benchmark calib/history-retrieval/synthetic --output tmp/history-eval.json
```

Expected: PASS; output labels itself `synthetic_contract_only`, contains all four arms, records all input hashes, and matches `expected-metrics.json`.

- [ ] **Step 6: Commit**

```bash
git add CONTRIBUTING.md calib/history-retrieval history/retrieval-policy-v1.json lib/history_cli.py lib/history_eval.py tests/verify_history_retrieval_benchmark.py
git commit -m "test: define history retrieval evaluation contract"
```

---

### Task 7: Product Integration, Migration Replay, and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/trust-boundaries.md`
- Modify: `docs/getting-started.md`
- Modify: `docs/backends.md`
- Modify: `DEVELOPMENT.md`
- Modify: `docs/superpowers/specs/2026-07-23-bounded-history-retrieval-design.md`
- Modify: `tests/verify_product_contract.py`

**Interfaces:**
- Consumes: the complete implementation and committed ledger baseline.
- Produces: operator documentation, migration/recovery commands, updated roadmap status, and full verification evidence.

- [ ] **Step 1: Extend product-contract coverage**

Require tracked runtime surfaces:

```python
HISTORY_RUNTIME_FILES = {
    "ledger.instance-id",
    "lib/history_store.py", "lib/history_budget.py", "lib/history_projection.py",
    "lib/history_retrieval.py", "lib/history_stage.py",
    "lib/history_cli.py", "lib/history_eval.py",
    "history/retrieval-policy-v1.json", "roles/history-compare.md",
}
```

Add checks for policy constants, forbidden full-ledger reads in `roles/generate.md`, `roles/meta.md`, `roles/review.md`, and `roles/history-compare.md`, distinct search and ledger outboxes, absence of the deferred AWR bridge tables, canonical near-SA observations, no automatic semantic lineage mutation, exact stage preflight, contained generation/comparator/reviewer use in `hunt.sh`, explicit shadow/enforcement branches, and the replayable two-target ledger materializer.

- [ ] **Step 2: Update operator documentation**

Document:

- `.ai-ideas/history.sqlite3` ownership and backup;
- import, validate, export, clean rebuild, receipt replay, and recovery commands;
- shadow-mode observational equivalence, enforcement calibration requirements, and the absence of automatic retrieval verdicts;
- immutable ledger snapshots, target receipts, and startup recovery after each projection crash point;
- canonical near-SA observations and the disposable status of the compatibility queue;
- Darwin/Linux containment requirements and fail-closed behavior;
- scoped meaning of `complete_no_match`;
- the benchmark non-fabrication boundary and pre-held-out policy commitment.

Resolve the approved design's mode ambiguity explicitly: in shipped shadow mode the normal external research/review protocol remains the sole authority for ledger verdicts and history receipts are observational for a fixed candidate batch; the complete-receipt gate becomes authoritative only in calibrated enforcement mode. This clarification must appear in the design, `PROGRAM.md`, architecture, trust-boundary, and operator docs with the same wording.

Mark the design status as implemented only after every verification command below passes.

- [ ] **Step 3: Run migration parity and replay against the committed ledger**

Run:

```bash
history_db=$(mktemp -d)/history.sqlite3
python3 lib/history_cli.py --db "$history_db" sync-ledger --ledger ledger.tsv
python3 lib/history_cli.py --db "$history_db" rebuild-projections
python3 lib/history_cli.py --db "$history_db" validate
python3 lib/history_cli.py --db "$history_db" export-tsv --output tmp/ledger.export.tsv
cmp ledger.tsv tmp/ledger.export.tsv
```

Expected: validation PASS and `cmp` exit 0.

- [ ] **Step 4: Run the complete offline suite**

Run:

```bash
python3 tests/history_store_smoke.py
python3 tests/history_budget_smoke.py
python3 tests/history_projection_smoke.py
python3 tests/history_retrieval_smoke.py
bash tests/history_mirror_smoke.sh
bash tests/history_runtime_smoke.sh
python3 tests/verify_history_retrieval_benchmark.py calib/history-retrieval/synthetic
python3 lib/history_cli.py evaluate --benchmark calib/history-retrieval/synthetic --output tmp/history-eval.json
python3 tests/runtime_policy_smoke.py
bash tests/runtime_abi_smoke.sh
bash tests/calibration_abi_smoke.sh
python3 tests/verify_product_contract.py all
bash -n hunt.sh tests/history_mirror_smoke.sh tests/history_runtime_smoke.sh
git diff --check
```

Expected: every command exits 0; `tmp/history-eval.json` is `synthetic_contract_only`, contains all four arms and matching input hashes, and matches the hand-checked metrics within declared tolerances.

- [ ] **Step 5: Commit documentation and contract integration**

```bash
git add README.md DEVELOPMENT.md docs history tests/verify_product_contract.py
git commit -m "docs: integrate bounded history retrieval"
```

- [ ] **Step 6: Run independent whole-branch review until clean**

Review the complete branch against:

- every Global Constraint;
- every design invariant and deliberate exclusion;
- schema/import/export/outbox crash boundaries;
- physical generation, comparator, meta, and per-reviewer isolation;
- token and omission bounds;
- exact-invocation preflight plus status, shadow-equivalence, and permanent-conclusion gates;
- protocol parity, resume, archive, and publication behavior;
- benchmark honesty and documentation accuracy.

Fix all Critical and Important findings, rerun covering tests, and repeat independent review until every reviewer returns `CLEAN`.
