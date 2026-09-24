#!/usr/bin/env python3
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


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

    def test_observation_archives_preserve_original_language(self):
        self.assertEqual(
            contract.text_han_failures(
                "calib/observations/2026-09-23_review/report.md", han_block(81)
            ),
            [],
        )

    def test_archive_language_exception_is_scoped_to_observations(self):
        for path in (
            "calib/README.md",
            "calib/cases/example/ideas.md",
            "calib/observations-other/report.md",
            "roles/review.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(contract.text_han_failures(path, han_block(25)))

    def test_ledger_language_exception_requires_exact_snapshot_and_path(self):
        original = (han_block(25) + "\n").encode()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            ledger = root / "ledger.tsv"
            with mock.patch.object(contract, "ROOT", root), mock.patch.dict(
                contract.EXPECTED,
                {"ledger_snapshot": contract.hashlib.sha256(original).hexdigest()},
            ):
                ledger.write_bytes(original)
                contract.assert_text_contract([ledger])
                for changed in (
                    original.replace(b"token", b"Token", 1),
                    original.replace(b"\n", b"\r\n"),
                ):
                    ledger.write_bytes(changed)
                    with self.assertRaises(AssertionError):
                        contract.assert_text_contract([ledger])
                other = root / "other.tsv"
                other.write_bytes(original)
                with self.assertRaises(AssertionError):
                    contract.assert_text_contract([other])

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
