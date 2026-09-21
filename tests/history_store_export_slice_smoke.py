#!/usr/bin/env python3
import base64
import hashlib
import os
import pathlib
import shutil
import tempfile
import unittest

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_store


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"


def row(story, verdict="accept-w-rev", overlap="low", category="design-fixable"):
    return (
        "2026-07-23\thunt\tEvaluation and Diagnostics\t"
        + story
        + "\t"
        + verdict
        + "\treason\t"
        + overlap
        + "\t"
        + category
    ).encode("utf-8")


class HistoryStoreExportSliceSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.state_root = self.root / ".ai-ideas"
        (self.root / "ledger.instance-id").write_text(
            "test-ledger-instance\n", encoding="utf-8"
        )
        self.rows = [
            row("slice alpha proposition"),
            row("slice beta proposition", verdict="accept"),
            row(
                "slice gamma proposition",
                verdict="reject",
                overlap="high",
                category="novelty-capped",
            ),
        ]
        self.terminators = [b"\r\n", b"\n", b"\r\n"]
        body = HEADER
        for raw, terminator in zip(self.rows, self.terminators):
            body += raw + terminator
        self.ledger = self.root / "ledger.tsv"
        self.ledger.write_bytes(body)
        self.db = self.root / "history.sqlite3"
        self.conn = history_store.connect(self.db)
        history_store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _import(self):
        return history_store.import_tsv_epoch(self.conn, self.ledger)

    def _rendered(self, header, rows, terminators):
        body = header
        for raw, terminator in zip(rows, terminators):
            body += raw + terminator
        return body

    def test_slice_is_byte_exact_with_crlf_rows(self):
        self._import()
        dest = self.root / "slice.tsv"
        result = history_store.export_slice(self.conn, 1, dest)
        self.assertEqual(
            dest.read_bytes(),
            self._rendered(HEADER, self.rows[1:], self.terminators[1:]),
        )
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["max_sequence"], 3)
        self.assertEqual(result["after_sequence"], 1)

    def test_slice_honors_custom_meta_header(self):
        self._import()
        custom = b"custom\tslice\theader\n"
        history_store._set_meta(
            self.conn,
            "ledger_header_b64",
            base64.b64encode(custom).decode("ascii"),
        )
        dest = self.root / "slice.tsv"
        history_store.export_slice(self.conn, 0, dest)
        self.assertEqual(
            dest.read_bytes(),
            self._rendered(custom, self.rows, self.terminators),
        )

    def test_slice_empty_is_header_only(self):
        self._import()
        dest = self.root / "slice.tsv"
        result = history_store.export_slice(self.conn, 3, dest)
        self.assertEqual(dest.read_bytes(), HEADER)
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(result["max_sequence"], 3)

    def test_slice_mark_above_max(self):
        self._import()
        dest = self.root / "slice.tsv"
        result = history_store.export_slice(self.conn, 10**9, dest)
        self.assertEqual(dest.read_bytes(), HEADER)
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(result["max_sequence"], 10**9)

    def test_slice_refuses_invalid_mark(self):
        self._import()
        dest = self.root / "slice.tsv"
        for bad in (-1, 1.5, "1", True, None):
            with self.assertRaises(ValueError):
                history_store.export_slice(self.conn, bad, dest)
        self.assertFalse(dest.exists())

    def test_slice_refuses_existing_destination(self):
        self._import()
        dest = self.root / "slice.tsv"
        dest.write_bytes(b"pre-existing")
        with self.assertRaises(ValueError):
            history_store.export_slice(self.conn, 0, dest)
        self.assertEqual(dest.read_bytes(), b"pre-existing")

    def test_slice_refuses_canonical_targets(self):
        self._import()
        scratch = pathlib.Path(tempfile.mkdtemp())
        previous = os.getcwd()
        try:
            os.chdir(scratch)
            try:
                for target in (
                    scratch / "ledger.tsv",
                    scratch / "tmp" / "ledger.good",
                ):
                    with self.assertRaises(ValueError):
                        history_store.export_slice(self.conn, 0, target)
            finally:
                os.chdir(previous)
            self.assertFalse((scratch / "ledger.tsv").exists())
            self.assertFalse((scratch / "tmp" / "ledger.good").exists())
        finally:
            shutil.rmtree(scratch, True)
        state_target = self.state_root / "x.tsv"
        with self.assertRaises(ValueError):
            history_store.export_slice(self.conn, 0, state_target)
        self.assertFalse(state_target.exists())

    def test_slice_refuses_symlinked_destination(self):
        self._import()
        target = self.root / "slice-target.tsv"
        link = self.root / "slice-link.tsv"
        os.symlink(str(target), link)
        with self.assertRaises(ValueError):
            history_store.export_slice(self.conn, 0, link)
        self.assertFalse(target.exists())
        self.assertTrue(link.is_symlink())

    def test_slice_allows_outside_checkout_and_creates_parent(self):
        self._import()
        dest = self.root / "external" / "harvest" / "slice.tsv"
        result = history_store.export_slice(self.conn, 0, dest)
        data = dest.read_bytes()
        self.assertEqual(
            data, self._rendered(HEADER, self.rows, self.terminators)
        )
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(result["byte_count"], len(data))
        self.assertEqual(result["path"], str(dest.resolve()))

    def test_max_source_sequence(self):
        self.assertEqual(history_store.max_source_sequence(self.conn), 0)
        self._import()
        self.assertEqual(history_store.max_source_sequence(self.conn), 3)


if __name__ == "__main__":
    unittest.main()
