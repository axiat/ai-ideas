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
        receipt = budget.preflight_stage_invocation(invocation, at_limit, tokenizer=tokenizer)
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
        exact_limit = len(serialized) + 256 + 2048 + 1024
        receipt = budget.preflight_stage_invocation(
            serialized, dict(self.policy, model_context_limit=exact_limit)
        )
        self.assertEqual(receipt["count_method"], "utf8_byte_upper_bound")
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                serialized + b"x", dict(self.policy, model_context_limit=exact_limit)
            )

    def test_unknown_adapter_or_unverified_allowance_fails_closed(self):
        invocation = budget.serialize_stage_invocation(**self.minimal_invocation)
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                invocation, dict(self.policy, adapter_version="unknown-adapter")
            )
        with self.assertRaises(budget.PreflightError):
            budget.preflight_stage_invocation(
                invocation, dict(self.policy, adapter_wrapper_allowance=255)
            )


if __name__ == "__main__":
    unittest.main()
