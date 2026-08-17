#!/usr/bin/env python3
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import verify_product_contract as contract


def han_block(count):
    return "\n".join(f"token 复活成品 {i}" for i in range(count))


class ProductContractSparseHan(unittest.TestCase):
    def test_runtime_allows_sparse_historical_tokens(self):
        self.assertEqual(
            contract.text_han_failures("awr-side.sh", han_block(14)),
            [],
        )

    def test_runtime_rejects_dense_han(self):
        failures = contract.text_han_failures("awr-side.sh", han_block(25))
        self.assertTrue(failures)
        self.assertTrue(failures[0].startswith("awr-side.sh:"))

    def test_tests_allow_legacy_fixture_payloads(self):
        self.assertEqual(
            contract.text_han_failures(
                "tests/runtime_abi_smoke.sh",
                han_block(44),
            ),
            [],
        )

    def test_tests_still_reject_a_chinese_document(self):
        failures = contract.text_han_failures(
            "tests/runtime_abi_smoke.sh",
            han_block(81),
        )
        self.assertTrue(failures)


if __name__ == "__main__":
    unittest.main()
