#!/usr/bin/env python3
"""Offline product-entry tests for host-owned router authority."""

import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "lib/history_audit_cli.py"
sys.path.insert(0, str(ROOT))

from lib import history_audit_plan
from lib import history_audit_store
from lib import history_contract_v2


def _load_router_fixture():
    path = ROOT / "tests/history_router_source_authority_smoke.py"
    spec = importlib.util.spec_from_file_location(
        "_history_audit_host_cli_router_fixture", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROUTER_FIXTURE = _load_router_fixture()


PREPARE_INPUT_SCHEMA = "history-router-host-cli-prepare-input-v1"
PREPARE_RECEIPT_SCHEMA = "history-router-host-cli-prepare-receipt-v1"
FINAL_RECEIPT_SCHEMA = "history-router-host-cli-final-receipt-v1"


def canonical_bytes(value):
    return history_contract_v2.canonical_bytes(value)


def canonical_sha(domain, value):
    return history_contract_v2.framed_sha256(
        domain, history_contract_v2.canonical_bytes(value)
    )


def seal(domain, material, field):
    return {**copy.deepcopy(material), field: canonical_sha(domain, material)}


class HistoryAuditHostCliSmoke(unittest.TestCase):
    def setUp(self):
        self.router = ROUTER_FIXTURE.HistoryRouterSourceAuthoritySmoke(
            "runTest"
        )
        self.router.setUp()
        self.runtime = self.router.runtime
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.provider_log = self.root / "provider-launched"
        trap = (
            "#!/bin/sh\n"
            "printf '%s\\n' \"$0\" >> \"$REAL_PROVIDER_LAUNCH_LOG\"\n"
            "exit 97\n"
        )
        for provider in ("codex", "kimi", "grok", "opencode", "agy"):
            executable = self.bin / provider
            executable.write_text(trap, encoding="utf-8")
            executable.chmod(0o700)

        self.plan = self.router._cohort_plan()
        self.additional = [self.router._additional_candidate()]
        self.candidates = self.router._candidate_cohort(
            self.plan, self.additional
        )
        material = self.router._round_material(
            self.plan, self.additional
        )
        host_authority = history_audit_plan._host_runtime_authority()
        material["semantic_policy_profile_id"] = host_authority[
            "semantic_policy_profile_id"
        ]
        material["risk_policy_sha"] = host_authority[
            "risk_policy_sha"
        ]
        material["budget_policy_sha"] = (
            history_audit_plan.runtime_budget_policy_sha(
                host_authority["budget_policy"]
            )
        )
        self.route_round_sha256 = history_audit_store._router_round_sha(
            material
        )
        self.router._install_host_shadow_calibration(
            self.runtime, self.route_round_sha256
        )
        self.prepare_input_path = self.root / "prepare-input.json"
        self.prepare_receipt_path = self.root / "prepare-receipt.json"
        self.final_receipt_path = self.root / "final-receipt.json"
        self.prepare_input = self._prepare_input()
        self._write_prepare_input(self.prepare_input)

    def tearDown(self):
        self.router.tearDown()
        self.temporary.cleanup()

    def _environment(self):
        environment = os.environ.copy()
        environment["PATH"] = (
            str(self.bin) + os.pathsep + environment.get("PATH", "")
        )
        environment["REAL_PROVIDER_LAUNCH_LOG"] = str(self.provider_log)
        return environment

    def _run(
        self,
        *arguments,
        bomb_test_maps=False,
        finalize_fault_after=None,
        fault_receipt_path=None,
    ):
        if not bomb_test_maps and finalize_fault_after is None:
            command = [sys.executable, str(CLI), *map(str, arguments)]
        else:
            script = textwrap.dedent(
                f"""
                import os
                import pathlib
                import sys
                sys.path.insert(0, {str(ROOT)!r})
                from lib import history_audit_plan
                from lib import history_audit_store

                class Bomb(dict):
                    def _explode(self, *args, **kwargs):
                        raise AssertionError("test authority map was accessed")
                    __getitem__ = _explode
                    get = _explode
                    __contains__ = _explode
                    __iter__ = _explode
                    __len__ = _explode
                    keys = _explode
                    items = _explode
                    values = _explode

                history_audit_plan._TEST_RUNTIME_AUTHORITIES = Bomb()
                history_audit_store._TEST_ROUTER_ROUND_AUTHORITIES = Bomb()
                from lib import history_audit_cli

                fault_after = {finalize_fault_after!r}
                if fault_after == "l1_batch":
                    original = history_audit_store.record_host_router_l1_observations
                    def crash_after_l1_batch(*args, **kwargs):
                        original(*args, **kwargs)
                        os._exit(91)
                    history_audit_store.record_host_router_l1_observations = (
                        crash_after_l1_batch
                    )
                elif fault_after == "final_sources":
                    original = history_audit_store.issue_host_router_domain_sources
                    def crash_after_final_sources(*args, **kwargs):
                        original(*args, **kwargs)
                        os._exit(92)
                    history_audit_store.issue_host_router_domain_sources = (
                        crash_after_final_sources
                    )
                elif fault_after == "derive":
                    original = history_audit_store.derive_candidate_route_facts
                    def crash_after_derive(*args, **kwargs):
                        original(*args, **kwargs)
                        os._exit(93)
                    history_audit_store.derive_candidate_route_facts = (
                        crash_after_derive
                    )
                elif fault_after == "output":
                    def fail_output(_path, raw):
                        pathlib.Path({str(fault_receipt_path)!r}).write_bytes(raw)
                        raise history_audit_cli.AuditCliError(
                            "injected output publication failure"
                        )
                    history_audit_cli._atomic_write = fail_output
                elif fault_after is not None:
                    raise AssertionError("unknown finalize fault point")
                raise SystemExit(history_audit_cli.main(sys.argv[1:]))
                """
            )
            command = [
                sys.executable, "-c", script, *map(str, arguments)
            ]
        return subprocess.run(
            command,
            cwd=ROOT,
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

    def _canonical_stdout(self, completed):
        try:
            value = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.fail(f"CLI stdout is not canonical JSON: {exc}")
        self.assertEqual(completed.stdout, canonical_bytes(value))
        return value

    def _prepare_input(self):
        material = {
            "schema_version": PREPARE_INPUT_SCHEMA,
            "authority_scope": "host_production",
            "preplan": {
                "run_id": self.plan["run_id"],
                "batch_id": self.plan["batch_id"],
                "intent": self.plan["intent"],
                "history_as_of_watermark": self.plan["snapshot"][
                    "history_as_of_watermark"
                ],
                "exclusion_policy_sha": self.plan["snapshot"][
                    "exclusion_policy_sha"
                ],
                "records": copy.deepcopy(self.plan["snapshot"]["records"]),
                "candidates": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "raw_artifact_sha": candidate["raw_artifact_sha"],
                        "source_order": candidate["source_order"],
                    }
                    for candidate in self.candidates
                ],
            },
            "observations": self.router._host_observations(
                self.plan, self.additional
            ),
        }
        return seal(
            "history-router-host-cli-prepare-input-v1",
            material,
            "input_sha256",
        )

    def _write_prepare_input(self, value):
        self.prepare_input_path.write_bytes(canonical_bytes(value))

    def _prepare(self, *, bomb_test_maps=False):
        return self._run(
            "host-route-prepare",
            "--db", self.runtime.db_path,
            "--input", self.prepare_input_path,
            "--output", self.prepare_receipt_path,
            bomb_test_maps=bomb_test_maps,
        )

    def _raw_l1_files(self, receipt, *, mutate_second=False):
        paths = []
        for index, candidate in enumerate(receipt["candidates"]):
            if not candidate["call_l1_model"]:
                continue
            raw_value = {
                "schema_version": "history-router-host-l1-observation-v2",
                "route_round_sha256": receipt["route_round_sha256"],
                "host_round_authority_sha256": receipt[
                    "host_round_authority_sha256"
                ],
                "run_id": receipt["run_id"],
                "batch_id": receipt["batch_id"],
                "intent": receipt["intent"],
                "snapshot_id": receipt["snapshot_id"],
                "snapshot_hash": receipt["snapshot_hash"],
                "candidate_id": candidate["candidate_id"],
                "candidate_hash": candidate["candidate_hash"],
                "candidate_raw_artifact_sha256": candidate[
                    "raw_artifact_sha"
                ],
                "source_order": candidate["source_order"],
                "pre_phase_fact_sha256": candidate[
                    "pre_phase_fact_sha256"
                ],
                "comparator_outcome": "no_match",
                "coverage_state": "complete",
            }
            if mutate_second and index == 1:
                raw_value["candidate_hash"] = "0" * 64
            path = self.root / f"l1-{index}.json"
            path.write_bytes(canonical_bytes(raw_value))
            paths.append(path)
        return paths

    def _finalize(
        self,
        l1_paths,
        *,
        bomb_test_maps=False,
        fault_after=None,
        fault_receipt_path=None,
    ):
        arguments = [
            "host-route-finalize",
            "--db", self.runtime.db_path,
            "--prepare-receipt", self.prepare_receipt_path,
            "--output", self.final_receipt_path,
        ]
        for path in l1_paths:
            arguments.extend(("--l1-observation", path))
        return self._run(
            *arguments,
            bomb_test_maps=bomb_test_maps,
            finalize_fault_after=fault_after,
            fault_receipt_path=fault_receipt_path,
        )

    def _host_counts(self):
        tables = (
            "audit_router_host_preplan_batches_v2",
            "audit_router_rounds_v2",
            "audit_router_host_observation_sets_v2",
            "audit_router_host_round_authorities_v2",
            "audit_router_host_l1_comparator_facts_v2",
            "audit_router_domain_sources_v2",
            "audit_router_host_source_authorities_v2",
            "audit_router_source_sets_v2",
            "audit_router_phase_facts_v2",
        )
        with sqlite3.connect(self.runtime.db_path) as connection:
            return {
                table: connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                for table in tables
            }

    def _assert_no_l1_or_final_writes(self):
        with sqlite3.connect(self.runtime.db_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM "
                    "audit_router_host_l1_comparator_facts_v2"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM audit_router_domain_sources_v2 "
                    "WHERE source_kind='l1_observation'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM audit_router_source_sets_v2 "
                    "WHERE phase='final'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM audit_router_phase_facts_v2 "
                    "WHERE phase='final'"
                ).fetchone()[0],
                0,
            )

    def _final_phase_rows(self):
        with sqlite3.connect(self.runtime.db_path) as connection:
            source = connection.execute(
                "SELECT count(*) FROM audit_router_domain_sources_v2 "
                "WHERE source_kind='l1_observation'"
            ).fetchone()[0]
            source_set = connection.execute(
                "SELECT count(*) FROM audit_router_source_sets_v2 "
                "WHERE phase='final'"
            ).fetchone()[0]
            phase_rows = connection.execute(
                "SELECT release_authorized FROM "
                "audit_router_phase_facts_v2 WHERE phase='final' "
                "ORDER BY candidate_id"
            ).fetchall()
            comparator_count = connection.execute(
                "SELECT count(*) FROM "
                "audit_router_host_l1_comparator_facts_v2"
            ).fetchone()[0]
        return {
            "l1_source": source,
            "source_set": source_set,
            "release_authorized": [row[0] for row in phase_rows],
            "comparator_count": comparator_count,
        }

    def _assert_finalize_recovery(self, fault_after):
        prepared = self._prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr.decode())
        prepare_receipt = self._canonical_stdout(prepared)
        l1_paths = self._raw_l1_files(prepare_receipt)
        expected_comparator_count = sum(
            candidate["call_l1_model"]
            for candidate in prepare_receipt["candidates"]
        )
        captured_receipt = self.root / f"{fault_after}-receipt.json"

        interrupted = self._finalize(
            l1_paths,
            fault_after=fault_after,
            fault_receipt_path=captured_receipt,
        )
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertFalse(self.final_receipt_path.exists())
        interrupted_phase = self._final_phase_rows()
        self.assertEqual(
            interrupted_phase["comparator_count"],
            expected_comparator_count,
        )
        if fault_after == "l1_batch":
            self.assertEqual(interrupted_phase["l1_source"], 0)
            self.assertEqual(interrupted_phase["source_set"], 0)
            self.assertEqual(interrupted_phase["release_authorized"], [])
        elif fault_after == "final_sources":
            self.assertEqual(interrupted_phase["l1_source"], 1)
            self.assertEqual(interrupted_phase["source_set"], 0)
            self.assertEqual(interrupted_phase["release_authorized"], [])
        else:
            self.assertEqual(interrupted_phase["l1_source"], 1)
            self.assertEqual(interrupted_phase["source_set"], 1)
            self.assertEqual(
                interrupted_phase["release_authorized"],
                [0] * len(prepare_receipt["candidates"]),
            )

        recovered = self._finalize(l1_paths, bomb_test_maps=True)
        self.assertEqual(recovered.returncode, 0, recovered.stderr.decode())
        self._canonical_stdout(recovered)
        self.assertEqual(self.final_receipt_path.read_bytes(), recovered.stdout)
        if fault_after == "output":
            self.assertEqual(captured_receipt.read_bytes(), recovered.stdout)
        recovered_counts = self._host_counts()
        self.assertEqual(
            recovered_counts,
            {
                "audit_router_host_preplan_batches_v2": 1,
                "audit_router_rounds_v2": 1,
                "audit_router_host_observation_sets_v2": 1,
                "audit_router_host_round_authorities_v2": 1,
                "audit_router_host_l1_comparator_facts_v2": (
                    expected_comparator_count
                ),
                "audit_router_domain_sources_v2": 7,
                "audit_router_host_source_authorities_v2": 7,
                "audit_router_source_sets_v2": 2,
                "audit_router_phase_facts_v2": (
                    2 * len(prepare_receipt["candidates"])
                ),
            },
        )

        replayed = self._finalize(l1_paths, bomb_test_maps=True)
        self.assertEqual(replayed.returncode, 0, replayed.stderr.decode())
        self.assertEqual(replayed.stdout, recovered.stdout)
        self.assertEqual(self.final_receipt_path.read_bytes(), recovered.stdout)
        self.assertEqual(self._host_counts(), recovered_counts)
        self.assertEqual(
            self._final_phase_rows()["release_authorized"],
            [0] * len(prepare_receipt["candidates"]),
        )
        self.assertFalse(self.provider_log.exists())

    def test_prepare_finalize_emit_host_receipts_without_provider_launch_and_replay(
        self,
    ):
        prepared = self._prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr.decode())
        prepare_receipt = self._canonical_stdout(prepared)
        self.assertEqual(
            prepare_receipt["schema_version"], PREPARE_RECEIPT_SCHEMA
        )
        self.assertEqual(
            prepare_receipt["authority_scope"], "host_production"
        )
        self.assertEqual(
            prepare_receipt["route_round_sha256"],
            self.route_round_sha256,
        )
        self.assertTrue(
            all(item["call_l1_model"] for item in prepare_receipt["candidates"])
        )
        self.assertEqual(self.prepare_receipt_path.read_bytes(), prepared.stdout)
        self.assertFalse(self.provider_log.exists())

        l1_paths = self._raw_l1_files(prepare_receipt)
        finalized = self._finalize(l1_paths)
        self.assertEqual(finalized.returncode, 0, finalized.stderr.decode())
        final_receipt = self._canonical_stdout(finalized)
        self.assertEqual(final_receipt["schema_version"], FINAL_RECEIPT_SCHEMA)
        self.assertEqual(final_receipt["authority_scope"], "host_production")
        self.assertEqual(
            final_receipt["prepare_receipt_sha256"],
            prepare_receipt["receipt_sha256"],
        )
        self.assertEqual(
            [item["candidate_id"] for item in final_receipt["candidate_routes"]],
            [item["candidate_id"] for item in prepare_receipt["candidates"]],
        )
        self.assertTrue(
            all(
                route["release_authorized"] is False
                for route in final_receipt["candidate_routes"]
            )
        )
        self.assertEqual(self.final_receipt_path.read_bytes(), finalized.stdout)
        self.assertFalse(self.provider_log.exists())
        counts = self._host_counts()

        replayed_prepare = self._prepare(bomb_test_maps=True)
        self.assertEqual(
            replayed_prepare.returncode, 0, replayed_prepare.stderr.decode()
        )
        self.assertEqual(replayed_prepare.stdout, prepared.stdout)
        replayed_final = self._finalize(l1_paths, bomb_test_maps=True)
        self.assertEqual(
            replayed_final.returncode, 0, replayed_final.stderr.decode()
        )
        self.assertEqual(replayed_final.stdout, finalized.stdout)
        self.assertEqual(self._host_counts(), counts)
        self.assertFalse(self.provider_log.exists())

    def test_finalize_recovers_after_l1_batch_commit(self):
        self._assert_finalize_recovery("l1_batch")

    def test_finalize_recovers_after_final_source_commit(self):
        self._assert_finalize_recovery("final_sources")

    def test_finalize_recovers_after_final_derivation_commit(self):
        self._assert_finalize_recovery("derive")

    def test_finalize_recovers_after_output_publication_failure(self):
        self._assert_finalize_recovery("output")

    def test_forbidden_observation_field_fails_before_any_host_write(self):
        value = copy.deepcopy(self.prepare_input)
        value.pop("input_sha256")
        value["observations"]["release_authorized"] = True
        value = seal(
            "history-router-host-cli-prepare-input-v1",
            value,
            "input_sha256",
        )
        self._write_prepare_input(value)

        completed = self._prepare()
        self.assertEqual(completed.returncode, 2, completed.stderr.decode())
        self.assertIn(
            b"invalid host router prepare input", completed.stderr
        )
        self.assertFalse(self.prepare_receipt_path.exists())
        counts = self._host_counts()
        for table in (
            "audit_router_host_preplan_batches_v2",
            "audit_router_rounds_v2",
            "audit_router_host_observation_sets_v2",
            "audit_router_host_round_authorities_v2",
            "audit_router_domain_sources_v2",
            "audit_router_host_source_authorities_v2",
            "audit_router_source_sets_v2",
            "audit_router_phase_facts_v2",
        ):
            self.assertEqual(counts[table], 0, table)
        self.assertFalse(self.provider_log.exists())

    def test_l1_batch_tamper_rolls_back_entire_comparator_cohort(self):
        prepared = self._prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr.decode())
        receipt = self._canonical_stdout(prepared)
        l1_paths = self._raw_l1_files(receipt, mutate_second=True)

        completed = self._finalize(l1_paths)
        self.assertEqual(completed.returncode, 2, completed.stderr.decode())
        self.assertFalse(self.final_receipt_path.exists())
        self._assert_no_l1_or_final_writes()
        self.assertFalse(self.provider_log.exists())

    def test_finalize_without_l1_observations_succeeds_for_full_cohort_pre_l1_skip(
        self,
    ):
        value = copy.deepcopy(self.prepare_input)
        value.pop("input_sha256")
        for member in value["observations"]["members"]:
            member["channel_states"][0]["state"] = "failed"
        value = seal(
            "history-router-host-cli-prepare-input-v1",
            value,
            "input_sha256",
        )
        self._write_prepare_input(value)

        prepared = self._prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr.decode())
        receipt = self._canonical_stdout(prepared)
        self.assertTrue(
            all(
                candidate["call_l1_model"] is False
                for candidate in receipt["candidates"]
            )
        )

        finalized = self._finalize([])
        self.assertEqual(finalized.returncode, 0, finalized.stderr.decode())
        final_receipt = self._canonical_stdout(finalized)
        self.assertEqual(
            final_receipt["l1_comparator_fact_sha256_by_candidate"],
            {},
        )
        self.assertEqual(
            [item["candidate_id"] for item in final_receipt["candidate_routes"]],
            [item["candidate_id"] for item in receipt["candidates"]],
        )
        self.assertFalse(self.provider_log.exists())

    def test_rehashed_tampered_prepare_receipt_fails_before_l1_or_final_writes(
        self,
    ):
        prepared = self._prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr.decode())
        receipt = self._canonical_stdout(prepared)
        tampered = copy.deepcopy(receipt)
        tampered.pop("receipt_sha256")
        tampered["pre_l1_source_set_sha256"] = "0" * 64
        tampered = seal(
            "history-router-host-cli-prepare-receipt-v1",
            tampered,
            "receipt_sha256",
        )
        self.prepare_receipt_path.write_bytes(canonical_bytes(tampered))
        l1_paths = self._raw_l1_files(tampered)

        completed = self._finalize(l1_paths)
        self.assertEqual(completed.returncode, 2, completed.stderr.decode())
        self.assertFalse(self.final_receipt_path.exists())
        self._assert_no_l1_or_final_writes()
        self.assertFalse(self.provider_log.exists())

    def test_rehashed_input_provenance_tamper_is_rejected_before_writes(self):
        prepared = self._prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr.decode())
        receipt = self._canonical_stdout(prepared)
        tampered = copy.deepcopy(receipt)
        tampered.pop("receipt_sha256")
        tampered["input_sha256"] = "0" * 64
        tampered = seal(
            "history-router-host-cli-prepare-receipt-v1",
            tampered,
            "receipt_sha256",
        )
        self.prepare_receipt_path.write_bytes(canonical_bytes(tampered))
        l1_paths = self._raw_l1_files(tampered)

        completed = self._finalize(l1_paths)
        self.assertEqual(completed.returncode, 2, completed.stderr.decode())
        self.assertFalse(self.final_receipt_path.exists())
        self._assert_no_l1_or_final_writes()
        self.assertFalse(self.provider_log.exists())


if __name__ == "__main__":
    unittest.main()
