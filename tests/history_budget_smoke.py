#!/usr/bin/env python3
import hashlib
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_budget as budget


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
            "mounted_inputs": {"generation_brief.json": b'{"schema_version":1}\n'},
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
            mounted_inputs={"generation_brief.json": b'{"schema_version":1}\n'},
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
            expected_mounted_inputs={"generation_brief.json": b'{"schema_version":1}\n'},
        )
        self.assertEqual(receipt["count_method"], "exact_tokenizer")
        self.assertEqual(receipt["input_upper_bound"], 100)
        self.assertEqual(tokenizer.argument_sha256, hashlib.sha256(invocation).hexdigest())
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                invocation,
                dict(at_limit, model_context_limit=at_limit["model_context_limit"] - 1),
                tokenizer=tokenizer,
                expected_mounted_inputs={"generation_brief.json": b'{"schema_version":1}\n'},
            )

    def test_fallback_boundary_and_one_byte_over(self):
        serialized = budget.serialize_stage_invocation(**self.minimal_invocation)
        exact_limit = len(serialized) + 256 + 2048 + 1024
        receipt = budget.preflight_stage_invocation(
            serialized, dict(self.policy, model_context_limit=exact_limit),
            expected_mounted_inputs={"generation_brief.json": b'{"schema_version":1}\n'},
        )
        self.assertEqual(receipt["count_method"], "utf8_byte_upper_bound")
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                serialized + b"x", dict(self.policy, model_context_limit=exact_limit),
                expected_mounted_inputs={"generation_brief.json": b'{"schema_version":1}\n'},
            )

    def test_unknown_adapter_or_unverified_allowance_fails_closed(self):
        invocation = budget.serialize_stage_invocation(**self.minimal_invocation)
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                invocation, dict(self.policy, adapter_version="unknown-adapter"),
                expected_mounted_inputs={"generation_brief.json": b'{"schema_version":1}\n'},
            )
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                invocation, dict(self.policy, adapter_wrapper_allowance=255),
                expected_mounted_inputs={"generation_brief.json": b'{"schema_version":1}\n'},
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
                expected_mounted_inputs={"generation_brief.json": b'{"schema_version":1}\n'},
            )
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                invocation, self.policy,
                expected_mounted_inputs={"generation_brief.json": b'{"schema_version":2}\n'},
            )


if __name__ == "__main__":
    unittest.main()
