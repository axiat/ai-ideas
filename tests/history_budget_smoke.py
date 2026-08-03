#!/usr/bin/env python3
import hashlib
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_budget as budget
from lib import history_projection


class RecordingTokenizer:
    def __init__(self, fixed_count):
        self.fixed_count = fixed_count
        self.argument_sha256 = None
        self.identity = "history-stage-tokenizer-v1"
        self.revision = "1"

    def count(self, serialized):
        self.argument_sha256 = hashlib.sha256(serialized).hexdigest()
        return self.fixed_count


class HistoryBudgetSmoke(unittest.TestCase):
    def setUp(self):
        self.policy = {
            "adapter_version": "history-stage-v1",
            "adapter_wrapper_allowance": 256,
            "tested_adapter_allowances": {"history-stage-v1": 256},
            "model_context_limit": 32768,
            "max_output_tokens": 2048,
            "safety_margin": 1024,
            "tokenizer_identity": "history-stage-tokenizer-v1",
            "tokenizer_revision": "1",
        }
        self.minimal_invocation = {
            "stage": "generate",
            "adapter_version": "history-stage-v1",
            "fixed_instructions": "Generate bounded candidates.",
            "mounted_inputs": {
                "generation_brief.json": b'{"schema_version":1}\n',
                "generation_policy.md": b"# Bounded generation policy\n",
            },
            "candidate": None,
            "retrieval_payload": None,
            "receipts": [],
            "tool_schemas": [],
            "messages": [{"role": "user", "content": "Generate candidates."}],
        }

    def test_serialization_is_canonical_utf8_and_binds_mounted_bytes(self):
        one = budget.serialize_stage_invocation(**self.minimal_invocation)
        two = budget.serialize_stage_invocation(**dict(
            self.minimal_invocation,
            mounted_inputs={
                "generation_brief.json": b'{"schema_version":1}\n',
                "generation_policy.md": b"# Bounded generation policy\n",
            },
        ))
        self.assertIsInstance(one, bytes)
        self.assertEqual(one, two)
        self.assertIn(hashlib.sha256(b'{"schema_version":1}\n').hexdigest().encode(), one)
        self.assertIn(b"Generate bounded candidates.", one)

    def test_exact_tokenizer_boundary_does_not_add_byte_allowance(self):
        tokenizer = RecordingTokenizer(fixed_count=100)
        invocation = budget.serialize_stage_invocation(**dict(
            self.minimal_invocation,
            messages=[{"role": "user", "content": "Compare naïve café policy to baseline."}],
        ))
        at_limit = dict(self.policy, model_context_limit=100 + 2048 + 1024)
        receipt = budget.preflight_stage_invocation(
            invocation, at_limit, tokenizer=tokenizer,
            expected_mounted_inputs=self.minimal_invocation["mounted_inputs"],
        )
        self.assertEqual(receipt["count_method"], "exact_tokenizer")
        self.assertEqual(receipt["input_upper_bound"], 100)
        self.assertEqual(tokenizer.argument_sha256, hashlib.sha256(invocation).hexdigest())
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                invocation,
                dict(at_limit, model_context_limit=at_limit["model_context_limit"] - 1),
                tokenizer=tokenizer,
                expected_mounted_inputs=self.minimal_invocation["mounted_inputs"],
            )

    def test_fallback_boundary_and_one_byte_over(self):
        serialized = budget.serialize_stage_invocation(**self.minimal_invocation)
        exact_limit = len(serialized) + 256 + 2048 + 1024
        receipt = budget.preflight_stage_invocation(
            serialized, dict(self.policy, model_context_limit=exact_limit),
            expected_mounted_inputs=self.minimal_invocation["mounted_inputs"],
        )
        self.assertEqual(receipt["count_method"], "utf8_byte_upper_bound")
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                serialized + b"x", dict(self.policy, model_context_limit=exact_limit),
                expected_mounted_inputs=self.minimal_invocation["mounted_inputs"],
            )

    def test_unknown_adapter_or_unverified_allowance_fails_closed(self):
        invocation = budget.serialize_stage_invocation(**self.minimal_invocation)
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                invocation, dict(self.policy, adapter_version="unknown-adapter"),
                expected_mounted_inputs=self.minimal_invocation["mounted_inputs"],
            )
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                invocation, dict(self.policy, adapter_wrapper_allowance=255),
                expected_mounted_inputs=self.minimal_invocation["mounted_inputs"],
            )

    def test_preflight_rejects_missing_required_mount_and_unbound_tokenizer(self):
        missing = budget.serialize_stage_invocation(**dict(self.minimal_invocation, mounted_inputs={}))
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(missing, self.policy, expected_mounted_inputs={})
        invocation = budget.serialize_stage_invocation(**self.minimal_invocation)
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                invocation, self.policy, tokenizer=lambda _: 0,
                expected_mounted_inputs={"generation_brief.json": b'{"schema_version":1}\n'},
            )

    def test_preflight_rejects_closed_schema_drift_and_mount_mismatch(self):
        invocation = budget.serialize_stage_invocation(**self.minimal_invocation)
        malformed = invocation.replace(b'"schema_version":1', b'"schema_version":2')
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                malformed, self.policy,
                expected_mounted_inputs=self.minimal_invocation["mounted_inputs"],
            )

    def test_tracked_policy_binds_positive_tokenizer_identity(self):
        policy = history_projection.load_policy(
            ROOT / "history" / "retrieval-policy-v1.json"
        )
        invocation = budget.serialize_stage_invocation(**self.minimal_invocation)
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                invocation, policy, tokenizer=lambda _: 0,
                expected_mounted_inputs=self.minimal_invocation["mounted_inputs"],
            )
        receipt = budget.preflight_stage_invocation(
            invocation, policy, tokenizer=RecordingTokenizer(1),
            expected_mounted_inputs=self.minimal_invocation["mounted_inputs"],
        )
        self.assertEqual(receipt["count_method"], "exact_tokenizer")
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                invocation, self.policy,
                expected_mounted_inputs={
                    "generation_brief.json": b'{"schema_version":2}\n',
                    "generation_policy.md": b"# Bounded generation policy\n",
                },
            )

    def _assert_stage_mounts(
        self,
        stage,
        mounted_inputs,
        *,
        candidate=None,
        retrieval_payload=None,
    ):
        serialized = budget.serialize_stage_invocation(
            stage=stage,
            adapter_version="history-stage-v1",
            fixed_instructions="Bounded stage role.",
            mounted_inputs=mounted_inputs,
            candidate=candidate,
            retrieval_payload=retrieval_payload,
            receipts=[],
            tool_schemas=[],
            messages=[{"role": "user", "content": "Run the bounded stage."}],
        )
        return budget.preflight_stage_invocation(
            serialized,
            self.policy,
            expected_mounted_inputs=mounted_inputs,
        )

    def test_generate_mount_profile_is_exact_and_bounded(self):
        self.assertEqual(
            budget._STAGE_REQUIREMENTS["generate"]["optional_mounts"],
            {"research_context.md", "direction_constraint.json"},
        )
        required = {
            "generation_brief.json": b'{"schema_version":1}\n',
            "generation_policy.md": b"# Generation policy v1\n",
        }
        self.assertTrue(self._assert_stage_mounts("generate", required)["fits"])
        with_context = dict(
            required,
            **{"research_context.md": b"# Bounded research context\n"},
        )
        self.assertTrue(
            self._assert_stage_mounts("generate", with_context)["fits"]
        )
        for invalid in (
            {"generation_brief.json": required["generation_brief.json"]},
            dict(required, **{"ledger.tsv": b"history\n"}),
        ):
            with self.assertRaises(budget.PreflightError):
                self._assert_stage_mounts("generate", invalid)

    def test_review_mount_profile_uses_compact_contract_not_raw_history(self):
        required = {
            "candidate.json": b'{"candidate_id":"I1"}\n',
            "prior_work.md": b"# Prior work\n",
            "review_contract.md": b"# Review contract v1\n",
        }
        self.assertTrue(
            self._assert_stage_mounts(
                "review", required, candidate={"candidate_id": "I1"}
            )["fits"]
        )
        with_summary = dict(
            required,
            **{"history_summary.json": b'{"schema_version":1}\n'},
        )
        self.assertTrue(
            self._assert_stage_mounts(
                "review", with_summary, candidate={"candidate_id": "I1"}
            )["fits"]
        )
        with self.assertRaises(budget.PreflightError):
            self._assert_stage_mounts(
                "review",
                dict(required, **{"retrieval_pack.json": b"{}\n"}),
                candidate={"candidate_id": "I1"},
            )

    def test_meta_requires_exact_bounded_failure_batch(self):
        batch = {"failure_batch.json": b'{"schema_version":1,"items":[]}\n'}
        self.assertTrue(self._assert_stage_mounts("meta", batch)["fits"])
        for invalid in ({}, dict(batch, **{"tmp/deathlist.md": b"history\n"})):
            with self.assertRaises(budget.PreflightError):
                self._assert_stage_mounts("meta", invalid)


if __name__ == "__main__":
    unittest.main()
