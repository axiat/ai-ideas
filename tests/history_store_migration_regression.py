#!/usr/bin/env python3
import pathlib
import sqlite3
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_store


LEGACY_RECEIPT_SQL = """
CREATE TABLE history_receipts(
  receipt_id TEXT PRIMARY KEY,
  status TEXT NOT NULL
);
CREATE TRIGGER history_receipt_update_guard
BEFORE UPDATE ON history_receipts
BEGIN
  SELECT RAISE(ABORT, 'history receipt is immutable');
END;
CREATE TRIGGER history_receipt_delete_guard
BEFORE DELETE ON history_receipts
BEGIN
  SELECT RAISE(ABORT, 'history receipt is immutable');
END;
"""


def legacy_connection():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(LEGACY_RECEIPT_SQL)
    return conn


class HistoryStoreMigrationRegression(unittest.TestCase):
    def test_receipt_only_replacement_rebinds_guards_and_avoids_name_collisions(self):
        conn = legacy_connection()
        digest = history_store._sha(b"history_receipts")[:8]
        occupied = (
            "history_receipts_legacy_unverified",
            f"history_receipts_legacy_unverified_{digest}",
            f"history_receipts_legacy_unverified_{digest}_2",
        )
        for name in occupied:
            conn.execute(f'CREATE TABLE "{name}"(marker TEXT)')

        history_store.init_schema(conn)

        legacy_name = f"history_receipts_legacy_unverified_{digest}_3"
        self.assertIsNotNone(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (legacy_name,),
            ).fetchone()
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(history_receipts)")
        }
        self.assertIn("pack_publication_id", columns)
        guards = dict(
            conn.execute(
                "SELECT name, tbl_name FROM sqlite_master "
                "WHERE type='trigger' AND name IN "
                "('history_receipt_update_guard', "
                "'history_receipt_delete_guard')"
            )
        )
        self.assertEqual(
            guards,
            {
                "history_receipt_update_guard": "history_receipts",
                "history_receipt_delete_guard": "history_receipts",
            },
        )
        conn.close()

    def test_failed_receipt_migration_rolls_back_rename_and_trigger_drops(self):
        conn = legacy_connection()
        altered_in_transaction = []
        deny_create = False

        def authorize(action, _arg1, _arg2, _database, _source):
            nonlocal deny_create
            if action == sqlite3.SQLITE_ALTER_TABLE:
                altered_in_transaction.append(conn.in_transaction)
                deny_create = True
            elif deny_create and action == sqlite3.SQLITE_CREATE_TABLE:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        with self.assertRaises(sqlite3.DatabaseError):
            history_store.init_schema(conn)
        conn.set_authorizer(lambda *_args: sqlite3.SQLITE_OK)

        self.assertEqual(altered_in_transaction, [True])
        self.assertFalse(conn.in_transaction)
        self.assertEqual(
            [row[1] for row in conn.execute("PRAGMA table_info(history_receipts)")],
            ["receipt_id", "status"],
        )
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name LIKE "
                "'history_receipts_legacy_unverified%'"
            ).fetchone()
        )
        guards = dict(
            conn.execute(
                "SELECT name, tbl_name FROM sqlite_master "
                "WHERE type='trigger' AND name IN "
                "('history_receipt_update_guard', "
                "'history_receipt_delete_guard')"
            )
        )
        self.assertEqual(
            guards,
            {
                "history_receipt_update_guard": "history_receipts",
                "history_receipt_delete_guard": "history_receipts",
            },
        )
        conn.close()

    def test_schema_upgrade_uses_savepoint_inside_caller_transaction(self):
        conn = legacy_connection()
        conn.execute("BEGIN")

        history_store.init_schema(conn)

        self.assertTrue(conn.in_transaction)
        self.assertIn(
            "pack_publication_id",
            {row[1] for row in conn.execute("PRAGMA table_info(history_receipts)")},
        )
        conn.execute("ROLLBACK")
        self.assertEqual(
            [row[1] for row in conn.execute("PRAGMA table_info(history_receipts)")],
            ["receipt_id", "status"],
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
