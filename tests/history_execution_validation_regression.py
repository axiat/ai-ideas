#!/usr/bin/env python3
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib import history_execution


class ExecutionValidationRegression(unittest.TestCase):
    def test_map_mixed_item_ids_raise_contract_error(self):
        task = {
            "assigned_item_ids": ["a"],
            "frozen_records": [],
            "snapshot_id": "s",
            "snapshot_hash": "h",
        }
        output = {
            "schema_version": history_execution.MAP_SCHEMA,
            "snapshot_id": "s",
            "snapshot_hash": "h",
            "items": [{"item_id": None}],
        }
        with self.assertRaises(history_execution.MapValidationError):
            history_execution.validate_map_output(
                task, json.dumps(output).encode(), {"snapshot_id": "s", "snapshot_hash": "h"}
            )

    def test_detail_unhashable_anchor_id_raises_contract_error(self):
        task = {
            "generation_id": "g",
            "snapshot_id": "s",
            "snapshot_hash": "h",
            "task_hash": "t",
            "assigned_item_ids": ["a"],
            "frozen_records": [{"item_id": "a", "artifact_sha": "x", "content": "q"}],
        }
        output = {
            "schema_version": history_execution.DETAIL_OUTPUT_SCHEMA,
            "generation_id": "g",
            "snapshot_id": "s",
            "snapshot_hash": "h",
            "task_hash": "t",
            "card": {
                "lineage_id": "l",
                "semantic_relation": "duplicate",
                "item_ids": ["a"],
                "evidence": [{"asset_id": [], "artifact_sha": "x", "start": 0, "end": 1, "quote": "q"}],
            },
        }
        with self.assertRaises(history_execution.MapValidationError):
            history_execution.validate_detail_output(task, json.dumps(output).encode())


if __name__ == "__main__":
    unittest.main()
