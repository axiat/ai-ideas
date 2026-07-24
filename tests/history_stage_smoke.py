#!/usr/bin/env python3
import concurrent.futures
import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_budget
from lib import history_projection
from lib import history_retrieval
from lib import history_stage
from lib import history_runtime
from lib import history_store


POLICY_PATH = ROOT / "history" / "retrieval-policy-v1.json"
ADAPTER_PATH = ROOT / "lib" / "history_stage_adapter.py"
CANONICALIZER_PATH = ROOT / "lib" / "history_stage_proxy.py"
BACKEND_PATH = ROOT / "tests" / "malicious_history_agent.py"
FAKE_AGENT_PATH = ROOT / "tests" / "fake_agent.sh"
ROLE_PATHS = {
    "generate": "roles/generate.md",
    "history-compare": "roles/history-compare.md",
    "review": "roles/review.md",
    "meta": "roles/meta.md",
}
INPUT_CAPS = {
    "generation_brief.json": 65536,
    "generation_policy.md": 16384,
    "research_context.md": 65536,
    "retrieval_pack.json": 65536,
    "candidate.json": 16384,
    "prior_work.md": 16384,
    "review_contract.md": 16384,
    "history_summary.json": 16384,
    "failure_batch.json": 65536,
}
OUTPUTS = {
    "generate": [
        ("output/ideas.tsv", "ideas.tsv", "generation-ideas-tsv", 65536),
        ("output/ideas.md", "ideas.md", "generation-ideas-markdown", 65536),
        (
            "output/prompt-attestation.json",
            "prompt-attestation.json",
            "prompt-attestation-json",
            4096,
        ),
    ],
    "history-compare": [
        (
            "output/history-comparison.json",
            "history-comparison.json",
            "history-comparison-json",
            65536,
        ),
        (
            "output/prompt-attestation.json",
            "prompt-attestation.json",
            "prompt-attestation-json",
            4096,
        ),
    ],
    "review": [
        ("output/review.md", "review.md", "review-markdown", 65536),
        ("output/verdict.tsv", "verdict.tsv", "review-verdict-tsv", 16384),
        (
            "output/prompt-attestation.json",
            "prompt-attestation.json",
            "prompt-attestation-json",
            4096,
        ),
    ],
    "meta": [
        (
            "output/failure-distillation.json",
            "failure-distillation.json",
            "failure-distillation-json",
            65536,
        ),
        (
            "output/prompt-attestation.json",
            "prompt-attestation.json",
            "prompt-attestation-json",
            4096,
        ),
    ],
}
MESSAGES = {
    "generate": [{"role": "user", "content": "Generate bounded candidates."}],
    "history-compare": [{"role": "user", "content": "Compare the candidate."}],
    "review": [{"role": "user", "content": "Review the bounded candidate."}],
    "meta": [{"role": "user", "content": "Distill the bounded failure batch."}],
}


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


class StageFixture:
    def __init__(
        self,
        testcase,
        stage,
        seat_id=None,
        attack_mode="none",
        retrieval_complete=True,
        review_verdict="accept-w-rev",
    ):
        self.testcase = testcase
        self.stage = stage
        self.seat_id = seat_id or f"{stage}-seat"
        self.temp = pathlib.Path(
            tempfile.mkdtemp(prefix=f"history-stage-{stage}-")
        )
        testcase.addCleanup(shutil.rmtree, self.temp, True)
        self.inputs = self.temp / "inputs"
        self.destinations = self.temp / "destinations"
        self.inputs.mkdir()
        self.destinations.mkdir()
        self.backend_entry_log = self.temp / "host-backend-entry.log"
        self.sibling = self.temp / "sibling-review.md"
        self.sibling.write_text("other reviewer output\n", encoding="utf-8")
        self.outside_write = self.temp / "outside-write.txt"
        self.attack_mode = attack_mode
        self.retrieval_complete = retrieval_complete
        self.review_verdict = review_verdict
        self.history_store_reference = None
        self.history_database = None
        self.policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        self.role_relative = ROLE_PATHS[stage]
        self.role_bytes = (ROOT / self.role_relative).read_bytes()
        self.input_bytes = self._stage_inputs()
        for name, raw in self.input_bytes.items():
            (self.inputs / name).write_bytes(raw)
        self.manifest = self._manifest()
        self.manifest_path = self.temp / "manifest.json"
        self.write_manifest()

    def _ensure_history_authority(self):
        if self.history_database is not None:
            return self.history_database
        authority = self.temp / "history-authority"
        authority.mkdir()
        (authority / "ledger.instance-id").write_text(
            "stage-fixture\n",
            encoding="utf-8",
        )
        ledger = authority / "ledger.tsv"
        ledger.write_text(
            "date\tsource\ttheme\tidea\tverdict\treason\t"
            "overlap\tcategory\n"
            "2026-07-24\thunt\tWorld Models\tBounded candidate.\t"
            "accept-w-rev\tmissing strong baseline\tlow\t"
            "design-fixable\n",
            encoding="utf-8",
        )
        database = authority / "history.sqlite3"
        connection = history_store.connect(database)
        try:
            history_store.init_schema(connection)
            history_store.import_tsv_epoch(connection, ledger)
            history_projection.rebuild(connection, self.policy)
        finally:
            connection.close()
        self.history_store_reference = {
            "root": str(authority),
            "source": "history.sqlite3",
        }
        self.history_database = database
        return database

    def _stage_inputs(self):
        if self.stage == "generate":
            database = self._ensure_history_authority()
            connection = history_store.connect(database)
            try:
                brief = history_projection.build_generation_brief(
                    connection,
                    self.policy,
                )
            finally:
                connection.close()
            return {
                "generation_brief.json": canonical(brief),
                "generation_policy.md": (
                    ROOT / "brainstorming_policy.md"
                ).read_bytes(),
            }
        if self.stage == "history-compare":
            database = self._ensure_history_authority()
            connection = history_store.connect(database)
            try:
                candidate = connection.execute(
                    "SELECT * FROM candidates "
                    "ORDER BY source_sequence LIMIT 1"
                ).fetchone()
                query = {
                    "candidate_id": "I1",
                    "story": candidate["story"],
                    "theme": candidate["theme"],
                    "verdict": candidate["verdict"],
                    "reason": candidate["reason"],
                    "category": candidate["category"],
                }
                pack = history_retrieval.build_pack(
                    connection,
                    query,
                    "duplicate_search",
                    self.policy,
                    disabled_channels=(
                        None
                        if self.retrieval_complete
                        else {"dense"}
                    ),
                    comparator_role_bytes=self.role_bytes,
                    comparator_role_identity=self.role_relative,
                )
            finally:
                connection.close()
            return {"retrieval_pack.json": canonical(pack)}
        if self.stage == "review":
            return {
                "candidate.json": canonical(
                    {
                        "candidate_id": "I1",
                        "story": "Bounded candidate.",
                    }
                ),
                "prior_work.md": b"# Prior work\n\nNo occupying work in the fixture.\n",
                "review_contract.md": (
                    ROOT / "history" / "review-contract-v1.md"
                ).read_bytes(),
            }
        if self.stage == "meta":
            return {
                "failure_batch.json": canonical(
                    {
                        "schema_version": 1,
                        "failure_codes": ["unmapped"],
                        "themes": ["unmapped"],
                        "items": [],
                    }
                )
            }
        raise AssertionError(self.stage)

    def _invocation(self):
        mounted = dict(self.input_bytes)
        candidate = None
        retrieval_payload = None
        receipts = []
        tool_schemas = []
        if self.stage == "history-compare":
            retrieval_payload = json.loads(
                mounted["retrieval_pack.json"].decode("utf-8")
            )
            candidate = retrieval_payload["query"]
            receipts = [
                {
                    "pack_publication_id": retrieval_payload[
                        "pack_publication_id"
                    ],
                    "role_identity": self.role_relative,
                    "role_sha256": sha256(self.role_bytes),
                }
            ]
            tool_schemas = [
                history_retrieval.comparator_output_schema(
                    retrieval_payload, self.policy
                )
            ]
        elif self.stage == "review":
            candidate = json.loads(
                mounted["candidate.json"].decode("utf-8")
            )
        invocation = {
            "candidate": candidate,
            "retrieval_payload": retrieval_payload,
            "receipts": receipts,
            "tool_schemas": tool_schemas,
            "messages": MESSAGES[self.stage],
            "output_schema_instructions": None,
        }
        serialized = history_budget.serialize_stage_invocation(
            stage=self.stage,
            adapter_version="history-stage-v1",
            fixed_instructions=self.role_bytes.decode("utf-8"),
            mounted_inputs=mounted,
            **invocation,
        )
        invocation["expected_serialized_sha256"] = sha256(serialized)
        return invocation

    def _manifest(self):
        policy_bytes = POLICY_PATH.read_bytes()
        first_input = sorted(self.input_bytes)[0]
        environment = {
            "HISTORY_STAGE_ATTACK_MODE": self.attack_mode,
            "HISTORY_STAGE_COMPARATOR_STATUS":
                "complete_no_match",
            "HISTORY_STAGE_REVIEW_VERDICT": self.review_verdict,
            "HISTORY_STAGE_INPUT_PATH": f"input/{first_input}",
            "HISTORY_STAGE_OUTSIDE_WRITE": str(self.outside_write),
            "HISTORY_STAGE_SEAT_ID": self.seat_id,
            "HISTORY_STAGE_SENTINELS_JSON": json.dumps(
                [str(path) for path in self.testcase.sentinels]
            ),
            "HISTORY_STAGE_SIBLING": str(self.sibling),
        }
        return {
            "schema_version": 1,
            "stage": self.stage,
            "seat_id": self.seat_id,
            "adapter": {
                "version": "history-stage-v1",
                "fixed_wrapper": "history-stage-prompt-v1",
                "wrapper_allowance": 256,
                "executable_source": "lib/history_stage_adapter.py",
                "executable_sha256": sha256(ADAPTER_PATH.read_bytes()),
                "canonicalizer_source": "lib/history_stage_proxy.py",
                "canonicalizer_sha256": sha256(
                    CANONICALIZER_PATH.read_bytes()
                ),
            },
            "policy": {
                "source": "history/retrieval-policy-v1.json",
                "sha256": sha256(policy_bytes),
            },
            "role": {
                "source": self.role_relative,
                "sha256": sha256(self.role_bytes),
            },
            "input_roots": [str(self.inputs)],
            "inputs": [
                {
                    "source": name,
                    "mirror_path": name,
                    "sha256": sha256(raw),
                    "max_bytes": INPUT_CAPS[name],
                }
                for name, raw in sorted(self.input_bytes.items())
            ],
            "invocation": self._invocation(),
            "output_roots": [str(self.destinations)],
            "outputs": [
                {
                    "mirror_path": mirror_path,
                    "destination": destination,
                    "artifact_kind": kind,
                    "max_bytes": max_bytes,
                    "required": True,
                }
                for mirror_path, destination, kind, max_bytes in OUTPUTS[
                    self.stage
                ]
            ],
            "preflight_receipt_destination": "preflight.json",
            "completion_receipt_destination": "completion.json",
            "registered_runtime_reads": [],
            "registered_environment": environment,
            "history_store": self.history_store_reference,
        }

    def write_manifest(self):
        self.manifest_path.write_bytes(canonical(self.manifest))

    def refresh_invocation(self):
        self.input_bytes = {
            item["mirror_path"]: (self.inputs / item["source"]).read_bytes()
            for item in self.manifest["inputs"]
        }
        self.manifest["invocation"] = self._invocation()
        self.write_manifest()

    def replace_input(self, name, raw):
        (self.inputs / name).write_bytes(raw)
        self.input_bytes[name] = raw
        for item in self.manifest["inputs"]:
            if item["mirror_path"] == name:
                item["sha256"] = sha256(raw)
                break
        else:
            raise AssertionError(name)
        self.manifest["invocation"] = self._invocation()
        self.write_manifest()

    def run(self, backend_entry_fd=None):
        owned_descriptor = None
        if backend_entry_fd is None:
            owned_descriptor = os.open(
                self.backend_entry_log,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            backend_entry_fd = owned_descriptor
        try:
            return history_stage.run_stage(
                self.stage,
                self.manifest_path,
                [str(BACKEND_PATH)],
                backend_entry_fd=backend_entry_fd,
            )
        finally:
            if owned_descriptor is not None:
                os.close(owned_descriptor)


class HistoryStageSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state_parent = ROOT / ".ai-ideas"
        cls.state_parent.mkdir(exist_ok=True)
        cls.state_dir = pathlib.Path(
            tempfile.mkdtemp(
                prefix="history-stage-sentinels-",
                dir=cls.state_parent,
            )
        )
        for name in (
            "history.sqlite3",
            "history.sqlite3-wal",
            "history.sqlite3-shm",
        ):
            (cls.state_dir / name).write_text("sentinel\n", encoding="utf-8")
        cls.archive_dir = pathlib.Path(
            tempfile.mkdtemp(prefix="ai-ideas-runs-history-stage-")
        )
        cls.archive_sentinel = cls.archive_dir / "run.log"
        cls.archive_sentinel.write_text("sentinel\n", encoding="utf-8")
        git_pointer = ROOT / ".git"
        git_dir = pathlib.Path(
            subprocess.check_output(
                ["git", "rev-parse", "--git-dir"],
                cwd=ROOT,
                text=True,
            ).strip()
        )
        if not git_dir.is_absolute():
            git_dir = ROOT / git_dir
        cls.sentinels = [
            ROOT / "ledger.tsv",
            git_pointer,
            git_dir,
            cls.state_dir / "history.sqlite3",
            cls.state_dir / "history.sqlite3-wal",
            cls.state_dir / "history.sqlite3-shm",
            cls.archive_sentinel,
            pathlib.Path.home() / ".codex" / "RTK.md",
            ROOT,
        ]
        for target in cls.sentinels:
            if target.is_dir():
                os.listdir(target)
            else:
                with target.open("rb") as handle:
                    handle.read(1)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.state_dir, ignore_errors=True)
        shutil.rmtree(cls.archive_dir, ignore_errors=True)

    def test_command_json_is_a_closed_nonempty_string_array(self):
        self.assertEqual(
            history_stage.parse_command_json('["/bin/echo","fixed"]'),
            ["/bin/echo", "fixed"],
        )
        for invalid in (
            "",
            "{}",
            "[]",
            '["/bin/echo",1]',
            '["/bin/echo",""]',
            '["/bin/echo","--search"]',
            '["/bin/echo","x\\u0000y"]',
        ):
            with self.assertRaises(history_stage.StageError):
                history_stage.parse_command_json(invalid)

    def test_bounded_roles_are_canonical_after_hunt_cutover(self):
        hunt = (ROOT / "hunt.sh").read_text(encoding="utf-8")
        self.assertIn("run_contained_stage", hunt)
        self.assertIn("history_runtime_authorized run-stage", hunt)
        self.assertIn("history_runtime_authorized compare-targets", hunt)
        self.assertIn("history_runtime_authorized run-review-matrix", hunt)
        self.assertNotIn("Read roles/generate.md and follow it", hunt)
        self.assertNotIn("Read roles/meta.md and follow it", hunt)
        self.assertNotIn("Read roles/review.md and follow it", hunt)
        self.assertEqual(ROLE_PATHS["generate"], "roles/generate.md")
        self.assertEqual(ROLE_PATHS["meta"], "roles/meta.md")
        self.assertEqual(ROLE_PATHS["review"], "roles/review.md")
        for role in ROLE_PATHS.values():
            self.assertTrue((ROOT / role).is_file())
        generate = (ROOT / "roles/generate.md").read_text(
            encoding="utf-8"
        )
        meta = (ROOT / "roles/meta.md").read_text(encoding="utf-8")
        review = (ROOT / "roles/review.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("generation_brief.json", generate)
        self.assertIn("failure_batch.json", meta)
        self.assertIn("history_summary.json", review)
        self.assertIn("final JSON object", generate)
        self.assertIn("final JSON object", meta)
        self.assertIn("final JSON object", review)

    def _backend_entry_log(self, fixture, action):
        path = fixture.temp / "host-backend-entry.log"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            action(descriptor)
        finally:
            os.close(descriptor)
        return path.read_bytes()

    def test_generation_requires_canonical_policy_and_current_db_brief(self):
        valid = StageFixture(self, "generate")
        valid.run()

        cases = []
        changed_policy = StageFixture(self, "generate")
        changed_policy.replace_input(
            "generation_policy.md",
            changed_policy.input_bytes["generation_policy.md"]
            + b"\nUnregistered instruction.\n",
        )
        cases.append(("mutated-policy", changed_policy))

        free_form = StageFixture(self, "generate")
        free_form.replace_input(
            "generation_brief.json",
            canonical({"schema_version": 1, "free_form": "scan everything"}),
        )
        cases.append(("free-form-brief", free_form))

        smuggled = StageFixture(self, "generate")
        brief = json.loads(smuggled.input_bytes["generation_brief.json"])
        brief["ledger_rows"] = [
            {
                "idea": "raw historical idea",
                "reason": "unbounded historical evidence",
            }
        ]
        smuggled.replace_input("generation_brief.json", canonical(brief))
        cases.append(("raw-ledger-smuggling", smuggled))

        stale = StageFixture(self, "generate")
        brief = json.loads(stale.input_bytes["generation_brief.json"])
        brief["theme_counts"]["World Models"] += 1
        stale.replace_input("generation_brief.json", canonical(brief))
        cases.append(("mutated-count", stale))

        unbounded = StageFixture(self, "generate")
        brief = json.loads(unbounded.input_bytes["generation_brief.json"])
        brief["padding"] = "x" * 32000
        unbounded.replace_input("generation_brief.json", canonical(brief))
        cases.append(("unbounded-brief", unbounded))

        for label, fixture in cases:
            with self.subTest(label=label):
                def reject(descriptor):
                    with self.assertRaises(history_stage.StageError):
                        fixture.run(backend_entry_fd=descriptor)

                self.assertEqual(
                    self._backend_entry_log(fixture, reject),
                    b"",
                )
                self.assertFalse(
                    (fixture.destinations / "preflight.json").exists()
                )

    def _review_fixture_with_durable_summary(self):
        review = StageFixture(self, "review")
        review.policy = copy.deepcopy(review.policy)
        review.policy["mode"] = "enforcement"
        database = review._ensure_history_authority()
        candidate = {
            "candidate_id": "I1",
            "story": "Bounded candidate.",
            "theme": "World Models",
            "candidate_markdown": (
                "## I1\n"
                "One-Sentence Story: Bounded candidate.\n"
                "Theme: World Models\n"
            ),
        }
        candidate["content_sha256"] = (
            history_runtime.candidate_content_sha256(candidate)
        )
        candidate_raw = canonical(candidate)
        bindings = []
        connection = history_store.connect(database)
        try:
            for intent in (
                "duplicate_search",
                "failure_pattern_search",
            ):
                pack = history_retrieval.build_pack(
                    connection,
                    history_runtime._retrieval_query(
                        candidate, intent
                    ),
                    intent,
                    review.policy,
                    comparator_role_bytes=(
                        ROOT / "roles" / "history-compare.md"
                    ).read_bytes(),
                    comparator_role_identity=(
                        "roles/history-compare.md"
                    ),
                )
                relations = []
                for lineage in pack["lineages"]:
                    match = lineage["matches"][0]
                    relations.append(
                        {
                            "relation": "distinct",
                            "candidate_id": match["candidate_id"],
                            "lineage_id": match["lineage_id"],
                            "facet": match["facet"],
                            "evidence_id": match["evidence_id"],
                            "material_difference": (
                                "The bounded evidence differs."
                            ),
                            "confidence": 0.8,
                        }
                    )
                receipt = history_retrieval.finalize_comparison(
                    connection,
                    pack,
                    {
                        "status": "complete_no_match",
                        "comparator_version": (
                            history_retrieval.COMPARATOR_VERSION
                        ),
                        "relations": relations,
                        "expansion_request": None,
                    },
                    review.policy,
                )
                bindings.append((pack, receipt))
            summary = history_runtime.build_history_summary(
                connection,
                candidate,
                bindings,
                review.policy,
            )
        finally:
            connection.close()
        review.input_bytes = {
            "candidate.json": candidate_raw,
            "prior_work.md": review.input_bytes["prior_work.md"],
            "review_contract.md": review.input_bytes[
                "review_contract.md"
            ],
            "history_summary.json": canonical(summary),
        }
        for name, raw in review.input_bytes.items():
            (review.inputs / name).write_bytes(raw)
        review.history_store_reference = dict(
            review.history_store_reference
        )
        review.manifest = review._manifest()
        synthetic_policy = (
            review.temp / "synthetic-enforcement-policy.json"
        )
        synthetic_policy.write_bytes(canonical(review.policy))
        review.manifest["policy"] = {
            "source": "synthetic_contract_only",
            "host_path": str(synthetic_policy),
            "sha256": sha256(canonical(review.policy)),
            "authority_scope": "synthetic_contract_only",
        }
        review.write_manifest()
        return review, summary

    def test_review_summary_replays_exact_durable_receipt_before_launch(self):
        valid, _ = self._review_fixture_with_durable_summary()
        valid.run()
        mutations = {
            "forged-receipt": lambda value: value["receipts"][0].update(
                {"receipt_id": "f" * 64}
            ),
            "status-drift": lambda value: value["receipts"][0].update(
                {"status": "complete_match"}
            ),
            "candidate-drift": lambda value: value.update(
                {"candidate_id": "I2"}
            ),
            "candidate-hash-drift": lambda value: value.update(
                {"candidate_content_sha256": "e" * 64}
            ),
            "evidence-drift": lambda value: value["receipts"][0][
                "relations"
            ][0].update(
                {"evidence_id": "forged-evidence"}
            ),
            "provenance-drift": lambda value: value["receipts"][0][
                "provenance"
            ].update(
                {"pack_sha256": "d" * 64}
            ),
            "aggregate-drift": lambda value: value.update(
                {"aggregate_sha256": "c" * 64}
            ),
            "open-schema": lambda value: value.update(
                {"unregistered": True}
            ),
        }
        for label, mutate in mutations.items():
            fixture, summary = self._review_fixture_with_durable_summary()
            mutate(summary)
            fixture.replace_input(
                "history_summary.json",
                canonical(summary),
            )
            with self.subTest(label=label):
                def reject(descriptor):
                    with self.assertRaises(history_stage.StageError):
                        fixture.run(backend_entry_fd=descriptor)

                self.assertEqual(
                    self._backend_entry_log(fixture, reject),
                    b"",
                )
                self.assertFalse(
                    (fixture.destinations / "preflight.json").exists()
                )

        candidate_fixture, _ = (
            self._review_fixture_with_durable_summary()
        )
        candidate = json.loads(
            candidate_fixture.input_bytes["candidate.json"]
        )
        candidate["story"] = "Candidate bytes outside the receipt."
        candidate_fixture.replace_input(
            "candidate.json",
            canonical(candidate),
        )

        def reject_candidate(descriptor):
            with self.assertRaises(history_stage.StageError):
                candidate_fixture.run(backend_entry_fd=descriptor)

        self.assertEqual(
            self._backend_entry_log(
                candidate_fixture,
                reject_candidate,
            ),
            b"",
        )

    def test_registered_fake_review_verdicts_are_valid(self):
        cases = (
            ("strong-accept", "strong-accept", "0", "0"),
            (None, "accept-w-rev", "0", "1"),
            ("reject", "reject", "1", "1"),
        )
        for configured, verdict, critical, major in cases:
            with self.subTest(configured=configured):
                fixture_options = {}
                if configured is not None:
                    fixture_options["review_verdict"] = configured
                fixture = StageFixture(
                    self,
                    "review",
                    **fixture_options,
                )
                self.assertEqual(
                    fixture.manifest["registered_environment"][
                        "HISTORY_STAGE_REVIEW_VERDICT"
                    ],
                    verdict,
                )
                history_stage.run_stage(
                    "review",
                    fixture.manifest_path,
                    [str(ROOT / "tests" / "fake_stage_agent.py")],
                )
                fields = (
                    fixture.destinations / "verdict.tsv"
                ).read_text(encoding="utf-8").strip().split("\t")
                self.assertEqual(fields[1:3], [verdict, major])
                review = (
                    fixture.destinations / "review.md"
                ).read_text(encoding="utf-8")
                self.assertIn(f"Verdict: {verdict}\n", review)
                self.assertIn(f"CRITICAL: {critical}\n", review)
                self.assertIn(f"MAJOR: {major}\n", review)

    def test_codex_command_grammar_allows_reasoning_without_tool_widening(self):
        prefix = [
            "/absolute/codex",
            "-m",
            "gpt-5.3-codex-spark",
            "-c",
            "model_reasoning_effort=xhigh",
        ]
        identity = history_stage._parse_codex_prefix(prefix)
        mirror = pathlib.Path(
            tempfile.mkdtemp(prefix="codex-argv-fixture-")
        )
        self.addCleanup(shutil.rmtree, mirror, True)
        (mirror / "runtime").mkdir()
        (mirror / "tmp").mkdir()
        schema = mirror / "runtime" / "schema.json"
        schema.write_text("{}", encoding="utf-8")
        final = mirror / "tmp" / "final.json"
        command = history_stage.codex_loopback_argv(
            pathlib.Path(prefix[0]),
            model=identity["model"],
            reasoning_effort="xhigh",
            mirror=mirror,
            proxy_port=43123,
            output_schema_path=schema,
            output_last_message_path=final,
        )
        for required in (
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--strict-config",
            "sandbox_workspace_write.network_access=false",
            "tools.web_search=false",
            'shell_environment_policy.inherit="none"',
            "project_doc_max_bytes=0",
            "--output-schema",
            "--output-last-message",
        ):
            self.assertEqual(command.count(required), 1)
        self.assertLess(command.index("-a"), command.index("exec"))
        self.assertLess(command.index("-s"), command.index("exec"))
        self.assertIn("requires_openai_auth=false", "\0".join(command))
        self.assertIn("request_max_retries=0", "\0".join(command))
        for unsafe_prefix in (
            ["/absolute/codex", "exec"],
            [
                "/absolute/codex",
                "-m",
                "model",
                "-c",
                "sandbox_workspace_write.network_access=true",
            ],
            ["/absolute/codex", "-m", "model", "-m", "other"],
        ):
            with self.assertRaises(history_stage.StageError):
                history_stage._parse_codex_prefix(unsafe_prefix)
        with self.assertRaises(history_stage.StageError):
            history_stage.codex_loopback_argv(
                pathlib.Path(prefix[0]),
                model=identity["model"],
                reasoning_effort="xhigh",
                mirror=mirror,
                proxy_port=0,
                output_schema_path=schema,
                output_last_message_path=final,
            )

    def test_unprovisioned_codex_capability_fails_before_launch(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="codex-bootstrap-"))
        self.addCleanup(shutil.rmtree, root, True)
        executable = root / "codex"
        executable.write_bytes(b"pinned fake executable\n")
        executable.chmod(0o755)
        fixture = StageFixture(self, "generate")
        fixture.manifest["registered_environment"] = {}
        fixture.manifest["registered_runtime_reads"] = []
        fixture.write_manifest()
        command = [
            str(executable),
            "-m",
            "gpt-5.3-codex-spark",
            "-c",
            "model_reasoning_effort=xhigh",
        ]
        launched = False

        def observe_launch(*_args, **_kwargs):
            nonlocal launched
            launched = True

        with mock.patch.object(
            history_stage,
            "_run_contained",
            side_effect=observe_launch,
        ):
            with self.assertRaises(history_stage.StageError):
                history_stage.run_stage(
                    "generate",
                    fixture.manifest_path,
                    command,
                )
        self.assertFalse(launched)
        self.assertFalse(
            (fixture.destinations / "preflight.json").exists()
        )

    def test_linux_codex_fails_before_capability_auth_or_launch(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="linux-codex-"))
        self.addCleanup(shutil.rmtree, root, True)
        executable = root / "codex"
        executable.write_bytes(b"pinned fake executable\n")
        executable.chmod(0o755)
        fixture = StageFixture(self, "generate")
        fixture.manifest["registered_environment"] = {}
        fixture.write_manifest()
        touched = []
        with mock.patch.object(
            history_stage.platform,
            "system",
            return_value="Linux",
        ):
            with mock.patch.object(
                history_stage,
                "_validated_codex_capability",
                side_effect=lambda *_args: touched.append("capability"),
            ):
                with mock.patch.object(
                    history_stage,
                    "_capture_codex_auth",
                    side_effect=lambda *_args: touched.append("auth"),
                ):
                    with mock.patch.object(
                        history_stage,
                        "_run_contained",
                        side_effect=lambda *_args: touched.append("launch"),
                    ):
                        with self.assertRaisesRegex(
                            history_stage.StageError,
                            "Codex containment is unavailable",
                        ):
                            history_stage.run_stage(
                                "generate",
                                fixture.manifest_path,
                                [
                                    str(executable),
                                    "-m",
                                    "gpt-5.3-codex-spark",
                                    "-c",
                                    "model_reasoning_effort=xhigh",
                                ],
                            )
        self.assertEqual(touched, [])
        self.assertFalse(
            (fixture.destinations / "preflight.json").exists()
        )

    def test_codex_auth_is_owner_only_read_only_and_revalidated(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="codex-auth-fixture-"))
        self.addCleanup(shutil.rmtree, root, True)
        auth_path = root / "auth.json"
        value = {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": "fixture-id-token",
                "access_token": "fixture-access-token",
                "refresh_token": "fixture-refresh-token",
                "account_id": "fixture-account",
            },
            "last_refresh": "2026-07-24T00:00:00Z",
        }
        auth_path.write_bytes(canonical(value))
        auth_path.chmod(0o600)
        before = auth_path.read_bytes()
        captured = history_stage._capture_codex_auth(auth_path)
        self.assertEqual(
            set(captured),
            {"access_token", "account_id", "source", "_path"},
        )
        self.assertEqual(
            captured["source"]["path_kind"],
            "canonical-codex-auth-v1",
        )
        self.assertNotIn("sha256", captured["source"])
        self.assertEqual(auth_path.read_bytes(), before)
        history_stage._revalidate_codex_auth(captured)
        auth_path.write_bytes(before + b" ")
        with self.assertRaisesRegex(
            history_stage.StageError,
            "auth_refresh_required",
        ):
            history_stage._revalidate_codex_auth(captured)

        loose = root / "loose-auth.json"
        loose.write_bytes(before)
        loose.chmod(0o644)
        with self.assertRaisesRegex(
            history_stage.StageError,
            "auth_refresh_required",
        ):
            history_stage._capture_codex_auth(loose)

    def test_manifest_rejects_unknown_fields_and_stage_drift(self):
        fixture = StageFixture(self, "generate")
        fixture.manifest["unknown"] = True
        fixture.write_manifest()
        with self.assertRaises(history_stage.StageError):
            fixture.run()
        fixture = StageFixture(self, "generate")
        fixture.manifest["stage"] = "meta"
        fixture.write_manifest()
        with self.assertRaises(history_stage.StageError):
            fixture.run()

    def test_manifest_rejects_input_hash_and_path_attacks_before_launch(self):
        mutations = []

        def stale_hash(fixture):
            fixture.manifest["inputs"][0]["sha256"] = "0" * 64

        mutations.append(stale_hash)

        def extra_mount(fixture):
            source = fixture.inputs / "ledger.tsv"
            source.write_text("history\n", encoding="utf-8")
            fixture.manifest["inputs"].append(
                {
                    "source": "ledger.tsv",
                    "mirror_path": "ledger.tsv",
                    "sha256": sha256(source.read_bytes()),
                    "max_bytes": 1024,
                }
            )

        mutations.append(extra_mount)

        def traversal(fixture):
            fixture.manifest["inputs"][0]["source"] = "../manifest.json"

        mutations.append(traversal)

        def normalized_collision(fixture):
            duplicate = dict(fixture.manifest["inputs"][0])
            duplicate["mirror_path"] = (
                "a/../" + fixture.manifest["inputs"][0]["mirror_path"]
            )
            fixture.manifest["inputs"].append(duplicate)

        mutations.append(normalized_collision)

        for mutate in mutations:
            fixture = StageFixture(self, "generate")
            mutate(fixture)
            fixture.write_manifest()
            with self.subTest(mutation=mutate.__name__):
                with self.assertRaises(history_stage.StageError):
                    fixture.run()
                self.assertEqual(
                    fixture.backend_entry_log.read_bytes(),
                    b"",
                )
                self.assertFalse(
                    (fixture.destinations / "preflight.json").exists()
                )

    def test_manifest_rejects_symlink_hardlink_and_fifo_inputs(self):
        for kind in ("symlink", "hardlink", "fifo"):
            fixture = StageFixture(self, "generate")
            item = fixture.manifest["inputs"][0]
            path = fixture.inputs / item["source"]
            original = path.read_bytes()
            path.unlink()
            if kind == "symlink":
                path.symlink_to(fixture.manifest_path)
            elif kind == "hardlink":
                backing = fixture.inputs / "backing"
                backing.write_bytes(original)
                os.link(backing, path)
            else:
                os.mkfifo(path)
            fixture.write_manifest()
            with self.subTest(kind=kind):
                with self.assertRaises(history_stage.StageError):
                    fixture.run()
                self.assertEqual(
                    fixture.backend_entry_log.read_bytes(),
                    b"",
                )
                self.assertFalse(
                    (fixture.destinations / "preflight.json").exists()
                )

    def test_policy_role_adapter_and_output_authority_are_closed(self):
        mutations = []
        mutations.append(
            lambda fixture: fixture.manifest["adapter"].update(
                {"version": "unknown"}
            )
        )
        mutations.append(
            lambda fixture: fixture.manifest["adapter"].update(
                {"wrapper_allowance": 255}
            )
        )
        mutations.append(
            lambda fixture: fixture.manifest["adapter"].update(
                {"hidden_wrapper": "surprise"}
            )
        )
        mutations.append(
            lambda fixture: fixture.manifest["adapter"].update(
                {"canonicalizer_sha256": "0" * 64}
            )
        )
        mutations.append(
            lambda fixture: fixture.manifest["role"].update(
                {"sha256": "0" * 64}
            )
        )
        mutations.append(
            lambda fixture: fixture.manifest["policy"].update(
                {"sha256": "0" * 64}
            )
        )
        mutations.append(
            lambda fixture: fixture.manifest["outputs"][0].update(
                {"destination": "../escape"}
            )
        )
        for index, mutate in enumerate(mutations):
            fixture = StageFixture(self, "generate")
            mutate(fixture)
            fixture.write_manifest()
            with self.subTest(index=index):
                with self.assertRaises(history_stage.StageError):
                    fixture.run()
                self.assertEqual(
                    fixture.backend_entry_log.read_bytes(),
                    b"",
                )
                self.assertFalse(
                    (fixture.destinations / "preflight.json").exists()
                )

    def test_source_mutation_after_manifest_hash_fails_before_receipt(self):
        fixture = StageFixture(self, "generate")
        source = fixture.inputs / "generation_brief.json"
        source.write_bytes(source.read_bytes() + b" ")
        with self.assertRaises(history_stage.StageError):
            fixture.run()
        self.assertEqual(fixture.backend_entry_log.read_bytes(), b"")
        self.assertFalse((fixture.destinations / "preflight.json").exists())
        self.assertFalse((fixture.destinations / "completion.json").exists())

    def test_comparator_requires_host_published_complete_pack_before_launch(self):
        def install_pack(fixture, pack):
            raw = canonical(pack)
            fixture.input_bytes["retrieval_pack.json"] = raw
            (
                fixture.inputs / "retrieval_pack.json"
            ).write_bytes(raw)
            fixture.manifest["inputs"][0]["sha256"] = sha256(raw)
            fixture.manifest["invocation"] = fixture._invocation()
            fixture.write_manifest()

        def reseal(pack, policy):
            pack["pack_sha256"] = history_retrieval.pack_sha256(pack)
            pack["receipt_id"] = sha256(
                b"retrieval-pack-v1\0"
                + pack["pack_sha256"].encode("ascii")
            )
            pack["pack_publication_id"] = sha256(
                b"history-pack-publication-v1\0"
                + pack["pack_sha256"].encode("ascii")
                + pack["policy_sha256"].encode("ascii")
                + pack["generation_manifest_sha256"].encode("ascii")
            )

        fixtures = []

        truncated = StageFixture(self, "history-compare")
        valid = json.loads(
            truncated.input_bytes["retrieval_pack.json"]
        )
        install_pack(
            truncated,
            {
                "query": valid["query"],
                "intent": valid["intent"],
                "lineages": valid["lineages"],
                "pack_publication_id": valid[
                    "pack_publication_id"
                ],
            },
        )
        fixtures.append(("truncated", truncated))

        self_hashed = StageFixture(self, "history-compare")
        pack = json.loads(
            self_hashed.input_bytes["retrieval_pack.json"]
        )
        pack["query"]["story"] = "Self-hashed forged candidate."
        reseal(pack, self_hashed.policy)
        install_pack(self_hashed, pack)
        fixtures.append(("self-hashed", self_hashed))

        fake_publication = StageFixture(self, "history-compare")
        pack = json.loads(
            fake_publication.input_bytes["retrieval_pack.json"]
        )
        pack["pack_publication_id"] = "f" * 64
        install_pack(fake_publication, pack)
        fixtures.append(("fake-publication", fake_publication))

        wrong_candidate = StageFixture(self, "history-compare")
        pack = json.loads(
            wrong_candidate.input_bytes["retrieval_pack.json"]
        )
        pack["query"]["candidate_id"] = "wrong-candidate"
        reseal(pack, wrong_candidate.policy)
        install_pack(wrong_candidate, pack)
        fixtures.append(("wrong-candidate", wrong_candidate))

        fixtures.append(
            (
                "noncomplete",
                StageFixture(
                    self,
                    "history-compare",
                    retrieval_complete=False,
                ),
            )
        )

        for label, fixture in fixtures:
            launched = False

            def observe_launch(*_args, **_kwargs):
                nonlocal launched
                launched = True
                raise AssertionError("invalid pack reached backend")

            with self.subTest(label=label):
                with mock.patch.object(
                    history_stage,
                    "_run_contained",
                    side_effect=observe_launch,
                ):
                    with self.assertRaises(history_stage.StageError):
                        fixture.run()
                self.assertFalse(launched)
                self.assertEqual(
                    fixture.backend_entry_log.read_bytes(),
                    b"",
                )
                self.assertFalse(
                    (fixture.destinations / "preflight.json").exists()
                )

    def test_input_drift_after_preflight_receipt_still_prevents_launch(self):
        fixture = StageFixture(self, "generate")
        original_publish = history_stage._atomic_publish
        launched = False

        def mutate_after_preflight(guard, raw, mode=0o644):
            original_publish(guard, raw, mode)
            if guard["relative"] == "preflight.json":
                source = fixture.inputs / "generation_brief.json"
                source.write_bytes(source.read_bytes() + b" ")

        def observe_launch(*_args, **_kwargs):
            nonlocal launched
            launched = True
            raise AssertionError("backend launched after input drift")

        with mock.patch.object(
            history_stage,
            "_atomic_publish",
            side_effect=mutate_after_preflight,
        ):
            with mock.patch.object(
                history_stage,
                "_run_contained",
                side_effect=observe_launch,
            ):
                with self.assertRaises(history_stage.StageError):
                    fixture.run()
        self.assertFalse(launched)
        self.assertEqual(fixture.backend_entry_log.read_bytes(), b"")
        self.assertTrue((fixture.destinations / "preflight.json").exists())
        self.assertFalse((fixture.destinations / "completion.json").exists())

    def test_preflight_publication_failure_never_launches_backend(self):
        fixture = StageFixture(self, "generate")
        launched = False

        def refuse_preflight(*_args, **_kwargs):
            raise history_stage.StageError("receipt publication failed")

        def observe_launch(*_args, **_kwargs):
            nonlocal launched
            launched = True

        with mock.patch.object(
            history_stage,
            "_atomic_publish",
            side_effect=refuse_preflight,
        ):
            with mock.patch.object(
                history_stage,
                "_run_contained",
                side_effect=observe_launch,
            ):
                with self.assertRaises(history_stage.StageError):
                    fixture.run()
        self.assertFalse(launched)
        self.assertEqual(fixture.backend_entry_log.read_bytes(), b"")
        self.assertFalse((fixture.destinations / "preflight.json").exists())
        self.assertFalse((fixture.destinations / "completion.json").exists())

    def test_all_four_stages_run_under_real_darwin_containment(self):
        if sys.platform != "darwin":
            self.skipTest("Darwin containment integration")
        for stage_name in (
            "generate",
            "history-compare",
            "review",
            "meta",
        ):
            fixture = StageFixture(self, stage_name)
            completion = fixture.run()
            preflight = json.loads(
                (fixture.destinations / "preflight.json").read_text(
                    encoding="utf-8"
                )
            )
            attestation = json.loads(
                (fixture.destinations / "prompt-attestation.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                attestation["prompt_sha256"],
                preflight["serialized_sha256"],
            )
            self.assertEqual(completion["stage"], stage_name)
            self.assertEqual(
                fixture.backend_entry_log.read_bytes(),
                b"backend-entry\n",
            )
            self.assertFalse(fixture.outside_write.exists())
            self.assertFalse(pathlib.Path(completion["mirror_path"]).exists())

    def test_two_review_seats_have_disjoint_containment(self):
        if sys.platform != "darwin":
            self.skipTest("Darwin containment integration")
        first = StageFixture(self, "review", seat_id="review-1")
        second = StageFixture(self, "review", seat_id="review-2")
        original_prepare = history_stage._prepare_mirror
        original_run = history_stage._run_contained
        prepared = {}
        prepared_lock = threading.Lock()
        prepare_barrier = threading.Barrier(2)
        launch_barrier = threading.Barrier(2)

        def coordinate_prepare(
            inputs,
            backend,
            adapter,
            stage,
            seat_id,
            **kwargs,
        ):
            mirror, launch, execution = original_prepare(
                inputs,
                backend,
                adapter,
                stage,
                seat_id,
                **kwargs,
            )
            with prepared_lock:
                prepared[seat_id] = (mirror, backend)
            prepare_barrier.wait()
            other_seat = (
                "review-2"
                if seat_id == "review-1"
                else "review-1"
            )
            other_mirror = prepared[other_seat][0]
            self.assertTrue((other_mirror / "output").is_dir())
            self.assertEqual(
                os.listdir(other_mirror / "output"),
                [],
            )
            backend["environment"]["HISTORY_STAGE_SIBLING"] = str(
                other_mirror / "output"
            )
            return mirror, launch, execution

        def coordinate_launch(*args, **kwargs):
            launch_barrier.wait()
            return original_run(*args, **kwargs)

        with mock.patch.object(
            history_stage,
            "_prepare_mirror",
            side_effect=coordinate_prepare,
        ):
            with mock.patch.object(
                history_stage,
                "_run_contained",
                side_effect=coordinate_launch,
            ):
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=2
                ) as pool:
                    results = list(
                        pool.map(
                            lambda fixture: fixture.run(),
                            (first, second),
                        )
                    )
        self.assertNotEqual(results[0]["mirror_path"], results[1]["mirror_path"])
        self.assertNotEqual(results[0]["home_path"], results[1]["home_path"])
        self.assertNotEqual(results[0]["tmp_path"], results[1]["tmp_path"])
        self.assertNotEqual(results[0]["seat_id"], results[1]["seat_id"])
        self.assertFalse(first.outside_write.exists())
        self.assertFalse(second.outside_write.exists())

    def test_delayed_child_cannot_write_after_parent_exit(self):
        if sys.platform != "darwin":
            self.skipTest("Darwin containment integration")
        fixture = StageFixture(
            self,
            "generate",
            attack_mode="delayed-child",
        )
        fixture.manifest["registered_environment"][
            "HISTORY_STAGE_ATTACK_MODE"
        ] = "delayed-child"
        fixture.write_manifest()
        with self.assertRaises(history_stage.StageError):
            fixture.run()
        self.assertFalse(
            (fixture.destinations / "completion.json").exists()
        )
        time.sleep(0.4)
        self.assertFalse(fixture.outside_write.exists())

    def test_fork_and_double_fork_are_denied_before_child_creation(self):
        if sys.platform != "darwin":
            self.skipTest("Darwin containment integration")
        for mode in ("fork-probe", "rapid-double-fork"):
            with self.subTest(mode=mode):
                fixture = StageFixture(
                    self,
                    "generate",
                    attack_mode=mode,
                )
                completion = None
                try:
                    completion = fixture.run()
                    markdown = (
                        fixture.destinations / "ideas.md"
                    ).read_text(encoding="utf-8")
                    self.assertIn(
                        f"{mode}: denied before child creation",
                        markdown,
                    )
                    self.assertFalse(fixture.outside_write.exists())
                finally:
                    if completion is not None:
                        mirror = completion["mirror_path"]
                        processes = subprocess.check_output(
                            ["/bin/ps", "-axo", "pid=,command="],
                            text=True,
                        )
                        for line in processes.splitlines():
                            if mirror not in line:
                                continue
                            pid_text, _ = line.strip().split(None, 1)
                            try:
                                os.kill(int(pid_text), 9)
                            except ProcessLookupError:
                                pass

    def test_detached_child_is_killed_and_cannot_write_after_exit(self):
        if sys.platform != "darwin":
            self.skipTest("Darwin containment integration")
        fixture = StageFixture(
            self,
            "generate",
            attack_mode="detached-child",
        )
        with self.assertRaises(history_stage.StageError):
            fixture.run()
        self.assertFalse(
            (fixture.destinations / "completion.json").exists()
        )
        self.assertFalse(fixture.outside_write.exists())

    def test_backend_timeout_publishes_no_artifact_or_completion(self):
        fixture = StageFixture(
            self,
            "generate",
            attack_mode="timeout",
        )
        with mock.patch.object(
            history_stage,
            "PROCESS_TIMEOUT_SECONDS",
            0.05,
        ):
            with self.assertRaises(history_stage.StageError):
                fixture.run()
        self.assertTrue((fixture.destinations / "preflight.json").exists())
        self.assertFalse((fixture.destinations / "completion.json").exists())
        for _, destination, _, _ in OUTPUTS["generate"]:
            self.assertFalse((fixture.destinations / destination).exists())

    def test_backend_logs_are_bounded_while_streaming(self):
        for mode in ("stdout-overflow", "stderr-overflow"):
            fixture = StageFixture(
                self,
                "generate",
                attack_mode=mode,
            )
            started = time.monotonic()
            with self.subTest(mode=mode):
                with mock.patch.object(
                    history_stage,
                    "PROCESS_TIMEOUT_SECONDS",
                    5,
                ):
                    with self.assertRaises(history_stage.StageError):
                        fixture.run()
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertTrue(
                    (fixture.destinations / "preflight.json").exists()
                )
                self.assertFalse(
                    (fixture.destinations / "completion.json").exists()
                )

    def test_output_failures_never_publish_artifacts_or_completion(self):
        modes = (
            "extra-output",
            "wrong-attestation",
            "missing-output",
            "invalid-utf8",
            "oversized-output",
            "symlink-output",
            "hardlink-output",
            "fifo-output",
            "nonzero",
        )
        for mode in modes:
            fixture = StageFixture(self, "generate", attack_mode=mode)
            with self.subTest(mode=mode):
                with self.assertRaises(history_stage.StageError):
                    fixture.run()
                self.assertTrue(
                    (fixture.destinations / "preflight.json").exists()
                )
                self.assertFalse(
                    (fixture.destinations / "completion.json").exists()
                )
                for _, destination, _, _ in OUTPUTS["generate"]:
                    self.assertFalse(
                        (fixture.destinations / destination).exists()
                    )

    def test_destination_parent_replacement_prevents_all_copyback(self):
        fixture = StageFixture(self, "generate")
        original_validator = history_stage.validate_stage_outputs
        displaced = fixture.temp / "displaced-destinations"

        def replace_parent(*args, **kwargs):
            result = original_validator(*args, **kwargs)
            fixture.destinations.rename(displaced)
            fixture.destinations.mkdir()
            return result

        with mock.patch.object(
            history_stage,
            "validate_stage_outputs",
            side_effect=replace_parent,
        ):
            with self.assertRaises(history_stage.StageError):
                fixture.run()
        for _, destination, _, _ in OUTPUTS["generate"]:
            self.assertFalse((fixture.destinations / destination).exists())
        self.assertFalse((fixture.destinations / "completion.json").exists())

    def test_copyback_never_follows_parent_swap_for_receipts_or_artifacts(self):
        for publication_index in (1, 2, 5):
            fixture = StageFixture(self, "generate")
            displaced = fixture.temp / "displaced-destinations"
            outside = fixture.temp / "outside-destinations"
            outside.mkdir()
            original_mkstemp = history_stage.tempfile.mkstemp
            original_open = history_stage.os.open
            publications = 0
            swapped = False

            def swap_parent():
                nonlocal publications, swapped
                publications += 1
                if publications != publication_index:
                    return
                fixture.destinations.rename(displaced)
                fixture.destinations.symlink_to(
                    outside,
                    target_is_directory=True,
                )
                swapped = True

            def racing_mkstemp(*args, **kwargs):
                if kwargs.get("prefix") == ".history-stage-":
                    swap_parent()
                return original_mkstemp(*args, **kwargs)

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                if (
                    dir_fd is not None
                    and isinstance(path, str)
                    and path.startswith(".history-stage-")
                    and flags & os.O_EXCL
                ):
                    swap_parent()
                if dir_fd is None:
                    return original_open(path, flags, mode)
                return original_open(
                    path,
                    flags,
                    mode,
                    dir_fd=dir_fd,
                )

            with self.subTest(publication_index=publication_index):
                with mock.patch.object(
                    history_stage.tempfile,
                    "mkstemp",
                    side_effect=racing_mkstemp,
                ):
                    with mock.patch.object(
                        history_stage.os,
                        "open",
                        side_effect=racing_open,
                    ):
                        try:
                            fixture.run()
                        except history_stage.StageError:
                            pass
                self.assertTrue(swapped)
                self.assertEqual(list(outside.iterdir()), [])

    def test_generation_output_requires_closed_ids_and_candidate_schema(self):
        StageFixture(self, "generate").run()
        for mode in (
            "generation-missing-field",
            "generation-id-mismatch",
            "generation-duplicate-id",
        ):
            fixture = StageFixture(
                self,
                "generate",
                attack_mode=mode,
            )
            with self.subTest(mode=mode):
                with self.assertRaises(history_stage.StageError):
                    fixture.run()
                self.assertFalse(
                    (fixture.destinations / "ideas.tsv").exists()
                )
                self.assertFalse(
                    (fixture.destinations / "completion.json").exists()
                )

    def test_review_output_requires_contract_fields_and_matching_vote(self):
        StageFixture(self, "review").run()
        for mode in (
            "review-missing-field",
            "review-vote-mismatch",
            "review-major-mismatch",
            "review-gate-violation",
        ):
            fixture = StageFixture(
                self,
                "review",
                attack_mode=mode,
            )
            with self.subTest(mode=mode):
                with self.assertRaises(history_stage.StageError):
                    fixture.run()
                self.assertFalse(
                    (fixture.destinations / "review.md").exists()
                )
                self.assertFalse(
                    (fixture.destinations / "completion.json").exists()
                )

    def test_malformed_comparator_output_is_rejected(self):
        fixture = StageFixture(
            self,
            "history-compare",
            attack_mode="malformed-comparator",
        )
        with self.assertRaises(history_stage.StageError):
            fixture.run()
        self.assertFalse(
            (fixture.destinations / "history-comparison.json").exists()
        )
        self.assertFalse((fixture.destinations / "completion.json").exists())

    def test_meta_output_is_bound_to_batch_order_and_vocabularies(self):
        batch = {
            "schema_version": 1,
            "failure_codes": ["baseline-gap", "unmapped"],
            "themes": ["World Models", "unmapped"],
            "items": [
                {
                    "source_id": "source-1",
                    "reason": "missing strongest baseline",
                },
                {
                    "source_id": "source-2",
                    "reason": "unclassified failure",
                },
            ],
        }

        def install_batch(fixture):
            raw = canonical(batch)
            fixture.input_bytes["failure_batch.json"] = raw
            (
                fixture.inputs / "failure_batch.json"
            ).write_bytes(raw)
            fixture.manifest["inputs"][0]["sha256"] = sha256(raw)
            fixture.manifest["invocation"] = fixture._invocation()
            fixture.write_manifest()

        valid = StageFixture(self, "meta")
        install_batch(valid)
        valid.run()
        result = json.loads(
            (
                valid.destinations
                / "failure-distillation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            [
                mapping["source_id"]
                for mapping in result["mappings"]
            ],
            ["source-1", "source-2"],
        )

        for mode in (
            "meta-missing",
            "meta-duplicate",
            "meta-extra",
            "meta-wrong-vocabulary",
            "meta-wrong-order",
        ):
            fixture = StageFixture(
                self,
                "meta",
                attack_mode=mode,
            )
            install_batch(fixture)
            with self.subTest(mode=mode):
                with self.assertRaises(history_stage.StageError):
                    fixture.run()
                self.assertFalse(
                    (
                        fixture.destinations
                        / "failure-distillation.json"
                    ).exists()
                )
                self.assertFalse(
                    (
                        fixture.destinations / "completion.json"
                    ).exists()
                )

    def test_exact_fallback_boundary_and_one_byte_over(self):
        fixture = StageFixture(self, "generate")
        fixture.input_bytes["research_context.md"] = b""
        (fixture.inputs / "research_context.md").write_bytes(b"")
        fixture.manifest["inputs"].append(
            {
                "source": "research_context.md",
                "mirror_path": "research_context.md",
                "sha256": sha256(b""),
                "max_bytes": INPUT_CAPS["research_context.md"],
            }
        )
        role_text = fixture.role_bytes.decode("utf-8")
        target = (
            fixture.policy["model_context_limit"]
            - fixture.policy["max_output_tokens"]
            - fixture.policy["safety_margin"]
            - fixture.policy["adapter_wrapper_allowance"]
        )

        def invocation_for(raw):
            mounted = dict(fixture.input_bytes, **{"research_context.md": raw})
            return history_budget.serialize_stage_invocation(
                stage="generate",
                adapter_version="history-stage-v1",
                fixed_instructions=role_text,
                mounted_inputs=mounted,
                candidate=None,
                retrieval_payload=None,
                receipts=[],
                tool_schemas=[],
                messages=MESSAGES["generate"],
            )

        base = invocation_for(b"")
        padding = b"x" * (target - len(base))
        self.assertEqual(len(invocation_for(padding)), target)
        fixture.input_bytes["research_context.md"] = padding
        (fixture.inputs / "research_context.md").write_bytes(padding)
        for item in fixture.manifest["inputs"]:
            if item["mirror_path"] == "research_context.md":
                item["sha256"] = sha256(padding)
        fixture.manifest["invocation"] = fixture._invocation()
        fixture.write_manifest()
        fixture.run()

        over = StageFixture(self, "generate")
        over.input_bytes["research_context.md"] = padding + b"x"
        (over.inputs / "research_context.md").write_bytes(padding + b"x")
        over.manifest["inputs"].append(
            {
                "source": "research_context.md",
                "mirror_path": "research_context.md",
                "sha256": sha256(padding + b"x"),
                "max_bytes": INPUT_CAPS["research_context.md"],
            }
        )
        over.manifest["invocation"] = over._invocation()
        over.write_manifest()
        with self.assertRaises(history_stage.StageError):
            over.run()
        self.assertFalse((over.destinations / "preflight.json").exists())

    def test_generation_rejects_duplicate_research_context(self):
        fixture = StageFixture(self, "generate")
        brief = json.loads(
            (fixture.inputs / "generation_brief.json").read_text(
                encoding="utf-8"
            )
        )
        brief["research_context"] = "already embedded"
        raw = canonical(brief)
        (fixture.inputs / "generation_brief.json").write_bytes(raw)
        fixture.input_bytes["generation_brief.json"] = raw
        fixture.input_bytes["research_context.md"] = b"duplicate\n"
        (fixture.inputs / "research_context.md").write_bytes(b"duplicate\n")
        for item in fixture.manifest["inputs"]:
            if item["mirror_path"] == "generation_brief.json":
                item["sha256"] = sha256(raw)
        fixture.manifest["inputs"].append(
            {
                "source": "research_context.md",
                "mirror_path": "research_context.md",
                "sha256": sha256(b"duplicate\n"),
                "max_bytes": INPUT_CAPS["research_context.md"],
            }
        )
        fixture.manifest["invocation"] = fixture._invocation()
        fixture.write_manifest()
        with self.assertRaises(history_stage.StageError):
            fixture.run()

    def test_linux_builder_has_closed_mounts_and_missing_bwrap_fails(self):
        mirror = pathlib.Path("/private/tmp/history-stage-test")
        argv = history_stage.build_linux_launch(
            pathlib.Path("/usr/bin/bwrap"),
            mirror,
            [str(mirror / "runtime" / "backend"), "prompt"],
        )
        rendered = "\0".join(argv)
        self.assertIn("--die-with-parent", argv)
        self.assertIn("--ro-bind", argv)
        self.assertIn("--unshare-pid", argv)
        self.assertIn(str(mirror / "input"), argv)
        self.assertIn(str(mirror / "output"), argv)
        self.assertEqual(argv.count(str(mirror / "input")), 2)
        self.assertEqual(argv.count(str(mirror / "runtime")), 2)
        self.assertNotIn("--share-net", argv)
        for forbidden in (
            str(ROOT),
            str(pathlib.Path.home()),
            ".ai-ideas-runs",
        ):
            self.assertNotIn(forbidden, rendered)
        networked = history_stage.build_linux_launch(
            pathlib.Path("/usr/bin/bwrap"),
            mirror,
            [str(mirror / "runtime" / "backend"), "prompt"],
            network=True,
            registered_reads=["/opt/registered/backend"],
        )
        self.assertIn("--share-net", networked)
        self.assertIn("/opt/registered/backend", networked)
        fixture = StageFixture(self, "generate")
        fake_bin = fixture.temp / "fake-bin"
        fake_bin.mkdir()
        fake_bwrap = fake_bin / "bwrap"
        fake_bwrap.write_text(
            "#!/bin/sh\nexec \"$@\"\n",
            encoding="utf-8",
        )
        fake_bwrap.chmod(0o755)
        launched = False

        def observe_launch(*_args, **_kwargs):
            nonlocal launched
            launched = True

        with mock.patch.object(
            history_stage.platform,
            "system",
            return_value="Linux",
        ):
            with mock.patch.object(
                history_stage,
                "LINUX_BWRAP",
                fixture.temp / "missing-approved-bwrap",
            ):
                with mock.patch.dict(
                    os.environ,
                    {"PATH": str(fake_bin)},
                    clear=False,
                ):
                    with mock.patch.object(
                        history_stage,
                        "_run_contained",
                        side_effect=observe_launch,
                    ):
                        with self.assertRaises(
                            history_stage.StageError
                        ):
                            fixture.run()
        self.assertFalse(launched)
        self.assertFalse(
            (fixture.destinations / "preflight.json").exists()
        )

    def test_darwin_builder_allows_only_registered_external_runtime(self):
        mirror = pathlib.Path("/private/tmp/history-stage-test")
        profile_path = mirror / "runtime/stage.sb"
        argv, profile = history_stage.build_darwin_launch(
            profile_path,
            mirror,
            ["/opt/registered/backend", "prompt"],
            [],
            False,
            runtime_executables=["/opt/registered/backend"],
        )
        self.assertEqual(argv[0], "/usr/bin/sandbox-exec")
        self.assertIn('(literal "/opt/registered/backend")', profile)
        self.assertNotIn(f'(subpath "{ROOT.parent}")', profile)
        self.assertNotIn("(allow network-outbound)", profile)
        self.assertNotIn("(allow mach-lookup)", profile)
        self.assertNotIn("(allow file-read-metadata)", profile)
        self.assertIn(
            f'(deny file-read* (subpath "{ROOT}"))',
            profile,
        )

    def test_roles_use_only_bounded_stage_inputs(self):
        forbidden = {
            "ledger.tsv",
            "deathlist",
            "near-sa",
            "repository search",
            "search the repository",
            "web search",
            "sibling review",
            "other reviewer",
        }
        for relative in ROLE_PATHS.values():
            text = (ROOT / relative).read_text(encoding="utf-8").lower()
            with self.subTest(role=relative):
                for token in forbidden:
                    self.assertNotIn(token, text)
        fixed_review_bytes = (
            (ROOT / ROLE_PATHS["review"]).stat().st_size
            + (ROOT / "history" / "review-contract-v1.md").stat().st_size
        )
        self.assertLess(fixed_review_bytes, 16384)

    def test_fake_agent_supports_canonical_and_legacy_abis(self):
        for stage_name in (
            "generate",
            "history-compare",
            "review",
            "meta",
        ):
            fixture = StageFixture(self, stage_name)
            invocation = dict(fixture.manifest["invocation"])
            invocation.pop("expected_serialized_sha256")
            serialized = history_budget.serialize_stage_invocation(
                stage=stage_name,
                adapter_version="history-stage-v1",
                fixed_instructions=fixture.role_bytes.decode("utf-8"),
                mounted_inputs=fixture.input_bytes,
                **invocation,
            )
            direct_root = pathlib.Path(
                tempfile.mkdtemp(prefix=f"fake-stage-{stage_name}-")
            )
            self.addCleanup(shutil.rmtree, direct_root, True)
            subprocess.run(
                [str(FAKE_AGENT_PATH), serialized.decode("utf-8")],
                cwd=direct_root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertTrue((direct_root / "output").is_dir())
            self.assertFalse(
                (
                    direct_root
                    / "output"
                    / "prompt-attestation.json"
                ).exists()
            )

        legacy_root = pathlib.Path(
            tempfile.mkdtemp(prefix="fake-stage-legacy-")
        )
        self.addCleanup(shutil.rmtree, legacy_root, True)
        subprocess.run(
            [str(FAKE_AGENT_PATH), "Read roles/generate.md and follow it"],
            cwd=legacy_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertTrue((legacy_root / "tmp/round/ideas.tsv").is_file())


if __name__ == "__main__":
    unittest.main()
