#!/usr/bin/env python3
"""Sealed v2 review protocol through portable stages, commit and replay."""
import copy
import inspect
import hashlib
import json
import os
import pathlib
import sys
import unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT / "tests"))
from lib import history_runtime, review_assessment, history_store
import history_runtime_smoke as runtime_fixture
from history_runtime_smoke import canonical

class ReviewProtocolRuntime(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_fixture.RoundCoordinatorContract("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def chain(self, name, *, mode="", version=2, overlap="low", min_read=5):
        self.assertNotIn("review_output_version", inspect.signature(history_runtime.seal_round_review_plan).parameters,
                         "current review rules must not be a selectable production mode")
        f=self.fixture
        state=f._compared_round(selected=("I1",))
        prior=f.root / (name + "-prior.md")
        prior.write_text("## I1\nPapers Read: " + str(min_read) + "\nOverlap: " + overlap
                         + "\nNearest Work: https://example.org/prior — Bounded fixture.\n")
        with mock.patch.dict(os.environ,{"FAKE_AGENT_MODE":mode}):
            chain=f._review_chain(state,stem=name,reviewer_count=2,legacy_review=version == 1,prior_work_path=prior)
        return state,chain

    def verify(self, state, chain):
        f=self.fixture
        return history_runtime.verify_round_aggregation(db_path=f.database,policy_path=f.policy_path,
            batch_path=state["batch"],review_plan_path=chain["plan_path"],review_index_path=chain["index_path"],
            aggregation_path=chain["aggregation_path"],authority=f.shadow_test_authority())

    def test_v2_reject_has_neutral_category_no_parent_and_replays(self):
        state,chain=self.chain("neutral",mode="review-v2-reject",overlap="high")
        self.assertEqual(chain["plan"]["schema_version"],3)
        self.assertEqual(chain["aggregation"]["schema_version"],2)
        self.assertEqual(chain["aggregation"]["ledger_rows"][0].split("\t")[4:],
            ["reject","Current design cannot identify the claimed effect.","high","review-unresolved"])
        self.assertEqual(chain["aggregation"]["near_sa_observations"],[])
        target=chain["aggregation"]["targets"][0]
        self.assertEqual(target["coverage"],"not-covered")
        self.assertEqual(len(target["assessments"]),2)
        self.assertEqual(self.verify(state,chain),chain["aggregation"])
        f=self.fixture
        result=history_runtime.commit_round(db_path=f.database,policy_path=f.policy_path,batch_path=state["batch"],
            selection_path=state["selection"],comparison_index_path=state["observation_root"] / "comparison-index.json",
            review_plan_path=chain["plan_path"],review_index_path=chain["index_path"],
            aggregation_path=chain["aggregation_path"],authority=f.shadow_test_authority())
        self.assertIsNotNone(result)
        conn=history_store.connect(f.database)
        try:
            self.assertIsNone(history_store.select_generation_parent(conn))
        finally:
            conn.close()

    def test_public_execution_rejects_legacy_plan_before_provider_launch(self):
        f=self.fixture
        state=f._compared_round(selected=("I1",))
        sealed=f._seal_review_plan(state,stem="legacy-execution",legacy_review=True,
                                   authority=f.shadow_test_authority())
        stage_root=f.root / "legacy-public-stages"
        output_path=f.root / "legacy-public-index.json"
        with mock.patch.object(history_runtime, "_run_portable_stage",
                               side_effect=AssertionError("legacy provider must not launch")) as provider:
            with self.assertRaisesRegex(history_runtime.RuntimeContractError, "current review plan"):
                history_runtime.run_review_matrix(
                    db_path=f.database,policy_path=f.policy_path,batch_path=state["batch"],
                    review_plan_path=sealed["plan_path"],reviewer_request_profiles=sealed["profiles"],
                    stage_root=stage_root,output_path=output_path,authority=f.shadow_test_authority(),
                )
            self.assertEqual(provider.call_count,0)
        self.assertFalse(stage_root.exists())
        self.assertFalse(output_path.exists())
        # Read-only validation of the same historical plan remains available.
        self.assertEqual(history_runtime.verify_round_review_plan(
            db_path=f.database,policy_path=f.policy_path,batch_path=state["batch"],
            review_plan_path=sealed["plan_path"],authority=f.shadow_test_authority(),
        )["schema_version"],2)

    def test_public_guard_uses_verified_plan_even_inside_test_context(self):
        f=self.fixture
        state=f._compared_round(selected=("I1",))
        authority=f.shadow_test_authority()
        sealed=f._seal_review_plan(state,stem="plan-swap",legacy_review=False,authority=authority)
        legacy=copy.deepcopy(sealed["plan"])
        legacy["schema_version"]=2
        del legacy["review_protocol"]
        legacy["review_plan_sha256"]=history_runtime._review_plan_hash(legacy)
        verify=history_runtime.verify_round_review_plan
        observed=[]
        def swap_then_verify(**values):
            sealed["plan_path"].chmod(0o600)
            sealed["plan_path"].write_bytes(canonical(legacy))
            result=verify(**values)
            observed.append(result["schema_version"])
            return result
        stage_root=f.root / "swapped-public-stages"
        output_path=f.root / "swapped-public-index.json"
        with history_runtime._runtime_for_test(f.policy,authority,f.root,state_paths=(f.database,)), \
             mock.patch.object(history_runtime,"verify_round_review_plan",side_effect=swap_then_verify), \
             mock.patch.object(history_runtime,"_run_portable_stage",
                               side_effect=AssertionError("verified legacy provider must not launch")) as provider:
            with self.assertRaisesRegex(history_runtime.RuntimeContractError,"current review plan"):
                history_runtime.run_review_matrix(
                    db_path=f.database,policy_path=f.policy_path,batch_path=state["batch"],
                    review_plan_path=sealed["plan_path"],reviewer_request_profiles=sealed["profiles"],
                    stage_root=stage_root,output_path=output_path,authority=authority,
                )
            self.assertEqual(observed,[2])
            self.assertEqual(provider.call_count,0)
        self.assertFalse(stage_root.exists())
        self.assertFalse(output_path.exists())

    def test_v1_low_reject_keeps_historical_novelty_dead_replay(self):
        # Legacy output remains twelve lines and the frozen old mapping is retained.
        state,chain=self.chain("legacy",version=1,mode="review-legacy-reject")
        self.assertEqual(chain["plan"]["schema_version"],2)
        self.assertEqual(chain["aggregation"]["schema_version"],1)
        self.assertEqual(chain["aggregation"]["ledger_rows"][0].split("\t")[-1],"novelty-dead")
        self.assertNotIn("review_protocol",chain["plan"])
        self.assertNotIn("assessments",chain["aggregation"]["targets"][0])
        material=dict(chain["aggregation"])
        digest=material.pop("aggregation_sha256")
        self.assertEqual(digest,hashlib.sha256(
            b"history-runtime-round-aggregation-v1\0"+canonical(material)
        ).hexdigest())
        self.assertEqual(self.verify(state,chain),chain["aggregation"])

    def test_v2_supported_coverage_and_hard_evidence_downgrade(self):
        _,chain=self.chain("covered",mode="review-v2-covered")
        self.assertEqual(chain["aggregation"]["ledger_rows"][0].split("\t")[-1],"novelty-dead")

    def test_v2_unanimous_sa_gate_still_creates_only_legacy_reentry(self):
        _,chain=self.chain("gate",min_read=0)
        self.assertEqual(chain["aggregation"]["ledger_rows"][0].split("\t")[-1],"evidence-incomplete")
        self.assertEqual(len(chain["aggregation"]["near_sa_observations"]),1)

    def test_invalid_v2_output_fails_without_canonical_row(self):
        with self.assertRaises(history_runtime.RuntimeContractError):
            self.chain("invalid",mode="review-v2-invalid-ref")
        conn=history_store.connect(self.fixture.database)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM candidates").fetchone()[0],1)
        finally:
            conn.close()

    def test_plan_schema_rejects_numeric_alias_after_rehash(self):
        state,chain=self.chain("numeric-schema")
        plan=copy.deepcopy(chain["plan"])
        plan["schema_version"]=3.0
        plan["review_plan_sha256"]=history_runtime._review_plan_hash(plan)
        chain["plan_path"].chmod(0o600)
        chain["plan_path"].write_bytes(canonical(plan))
        f=self.fixture
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime.verify_round_review_plan(db_path=f.database,policy_path=f.policy_path,
                batch_path=state["batch"],review_plan_path=chain["plan_path"],authority=f.shadow_test_authority())

    def test_quote_context_rechecks_source_hash_after_plan_verification(self):
        state,chain=self.chain("source-race")
        self.verify(state,chain)
        target=chain["plan"]["targets"][0]
        source=pathlib.Path(target["prior_work"]["path"])
        source.chmod(0o600)
        source.write_text(source.read_text()+"Injected evidence after verification.\n")
        with self.assertRaises(history_runtime.RuntimeContractError):
            history_runtime._review_output_context(chain["plan"],target)

    def test_protocol_or_aggregation_version_cannot_be_rebound(self):
        state,chain=self.chain("tamper")
        path=chain["aggregation_path"]
        bad=copy.deepcopy(chain["aggregation"]); bad["schema_version"]=1
        bad["aggregation_sha256"]=history_runtime._round_aggregation_hash(bad)
        path.chmod(0o600); path.write_bytes(canonical(bad))
        with self.assertRaises(history_runtime.RuntimeContractError):
            self.verify(state,chain)
        path.write_bytes(canonical(chain["aggregation"]))
        protocol=pathlib.Path(str(chain["plan_path"])+"-inputs") / "review_protocol.json"
        protocol.chmod(0o600); protocol.write_text('{"aggregation_version":1,"review_output_version":1}\n')
        with self.assertRaises(history_runtime.RuntimeContractError):
            self.verify(state,chain)

if __name__ == "__main__":
    unittest.main()
