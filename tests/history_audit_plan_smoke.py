#!/usr/bin/env python3
import copy
import hashlib
import inspect
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_plan as planner
from lib import provider_adapters


def sha(label):
    return hashlib.sha256(label.encode()).hexdigest()


def capability(provider):
    return {
        "provider": provider,
        "profile_hash": sha("capability-" + provider),
        "hard_complete_eligible": True,
        "model_identity": "fake-model-v1",
        "reasoning_identity": "high",
        "cli_revision": "fake-cli-v1",
        "serializer_revision": "history-audit-request-v1",
        "immutable_capacity_identity": "fake-capacity-v1",
    }


def profile(capabilities=None):
    capabilities = capabilities or {
        "codex": capability("codex"),
        "grok": capability("grok"),
    }
    bindings = {}
    for provider, item in capabilities.items():
        bindings[provider] = {
            "state": "hard-complete",
            "capability_profile_hash": item["profile_hash"],
            "model_identity": item["model_identity"],
            "reasoning_identity": item["reasoning_identity"],
            "cli_revision": item["cli_revision"],
            "capability_serializer_revision": item["serializer_revision"],
            "request_serializer_revision": "history-audit-request-v1",
            "immutable_capacity_identity": item["immutable_capacity_identity"],
            "prompt_sha256": sha("prompt-v1"),
            "schema_sha256": sha("schema-v1"),
            "evidence_limit_tokens": 12288,
        }
    return {
        "profile_id": "fake-safe-24k-v1",
        "base_profile_id": "safe-24k-v1",
        "status": "hard-complete-test-only",
        "counter": {"kind": "exact", "revision": "fake-utf8-byte-counter-v1"},
        "context_tokens": 24576,
        "evidence_limit_tokens": 12288,
        "max_output_tokens": 3072,
        "item_cap": 12,
        "utilization_ppm": 1000000,
        "prompt": {
            "id": "map-prompt-v1", "sha256": sha("prompt-v1"), "text": "prompt-v1"
        },
        "schema": {
            "id": "map-output-v1", "sha256": sha("schema-v1"), "text": "schema-v1"
        },
        "serializer_revision": "history-audit-request-v1",
        "usage_source": "fake-usage-v1",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "provider_bindings": bindings,
    }


def budget_policy(**overrides):
    value = {
        "schema_version": "l2-budget-v1",
        "settlement_policy_sha": sha("settlement-v1"),
        "risk_policy_sha": sha("risk-v1"),
        "intents": {
            "duplicate_search": {
                "round": {
                    "candidates": 8,
                    "started_attempts": 1000,
                    "input_tokens": 100000000,
                    "output_tokens": 10000000,
                    "provider_usage_units": 110000000,
                },
                "candidate": {
                    "started_attempts": 1000,
                    "input_tokens": 10000000,
                    "output_tokens": 1000000,
                    "provider_usage_units": 11000000,
                },
            }
        },
    }
    for path, replacement in overrides.items():
        if path in {"settlement_policy_sha", "risk_policy_sha"}:
            value[path] = replacement
        else:
            value["intents"]["duplicate_search"][path] = replacement
    return value


def records(count, *, payload_size=8):
    return [
        {
            "item_id": f"asset-{index:04d}",
            "title": f"title-{index:04d}",
            "summary": (chr(97 + index % 26) * payload_size),
        }
        for index in range(count)
    ]


class HistoryAuditPlanSmoke(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "snapshot_id": "snapshot-1",
            "snapshot_hash": sha("snapshot-1"),
            "history_as_of_watermark": 550,
        }
        self.candidate = {
            "candidate_id": "staging-1",
            "candidate_hash": sha("candidate-1"),
        }
        self.pools = {
            "comparator": ["codex", "grok"],
            "map": ["codex", "grok"],
            "detail": ["codex"],
            "reduce": ["codex"],
        }
        self.capabilities = {
            "codex": capability("codex"),
            "grok": capability("grok"),
        }
        self.profile = profile(self.capabilities)
        self.policy = budget_policy()

    def _api(self, name):
        value = getattr(planner, name, None)
        self.assertTrue(callable(value), f"missing behavior: history_audit_plan.{name}")
        return value

    def _build(self, items, **changes):
        values = {
            "snapshot": self.snapshot,
            "candidate": self.candidate,
            "provider_pools": self.pools,
            "capabilities": self.capabilities,
            "capacity_profile": self.profile,
            "budget_policy": self.policy,
            "intent": "duplicate_search",
            "records": items,
            "budget_events": [],
        }
        values.update(changes)
        return self._api("build_plan")(**values)

    def _error(self):
        value = getattr(planner, "AuditPlanError", None)
        self.assertTrue(
            isinstance(value, type) and issubclass(value, Exception),
            "missing behavior: history_audit_plan.AuditPlanError",
        )
        return value

    def _build_with_events(self, events, items, **changes):
        build = self._api("build_plan")
        values = {
            "snapshot": self.snapshot,
            "candidate": self.candidate,
            "provider_pools": self.pools,
            "capabilities": self.capabilities,
            "capacity_profile": self.profile,
            "budget_policy": self.policy,
            "intent": "duplicate_search",
            "records": items,
        }
        values.update(changes)
        if "budget_events" in inspect.signature(build).parameters:
            values["budget_events"] = events
        return build(**values)

    def test_plan_uses_final_serialized_request_and_rejects_unbudgetable_provider(self):
        constrained = copy.deepcopy(self.profile)
        constrained["provider_bindings"]["codex"]["evidence_limit_tokens"] = 100
        constrained["provider_bindings"]["grok"]["evidence_limit_tokens"] = 100
        with self.assertRaises(self._error()) as caught:
            self._build(records(1, payload_size=1), capacity_profile=constrained)
        self.assertEqual(caught.exception.code, "single_item_overflow")

        unbudgetable = copy.deepcopy(self.profile)
        unbudgetable["provider_bindings"]["grok"] = {"state": "unbudgetable"}
        with self.assertRaises(self._error()) as caught:
            self._build(records(1), capacity_profile=unbudgetable)
        self.assertEqual(caught.exception.code, "unbudgetable_provider")

    def test_safe_profile_550_records_has_at_least_46_shards(self):
        plan = self._build(records(550, payload_size=2))
        self.assertEqual(plan["capacity_profile_id"], "fake-safe-24k-v1")
        self.assertEqual(plan["base_capacity_profile_id"], "safe-24k-v1")
        self.assertGreaterEqual(len(plan["shards"]), 46)

    def test_item_and_token_limits_both_apply(self):
        constrained = copy.deepcopy(self.profile)
        constrained["provider_bindings"]["codex"]["evidence_limit_tokens"] = 1050
        constrained["provider_bindings"]["grok"]["evidence_limit_tokens"] = 1050
        plan = self._build(records(30, payload_size=220), capacity_profile=constrained)
        self.assertGreater(len(plan["shards"]), 3)
        for shard in plan["shards"]:
            self.assertLessEqual(len(shard["item_ids"]), 12)
            self.assertLessEqual(shard["final_request_tokens"], 1050)
            self.assertEqual(shard["final_request_tokens"], len(shard["serialized_request"].encode("utf-8")))
            self.assertIn('"output_schema":{"id":"map-output-v1"', shard["serialized_request"])
            self.assertIn('"text":"schema-v1"', shard["serialized_request"])
            self.assertIn('"record":{"item_id":', shard["serialized_request"])

    def test_secondary_pool_capacity_constrains_primary_shard_before_launch(self):
        constrained = copy.deepcopy(self.profile)
        constrained["provider_bindings"]["codex"]["evidence_limit_tokens"] = 4000
        constrained["provider_bindings"]["grok"]["evidence_limit_tokens"] = 1000
        plan = self._build(records(20, payload_size=120), capacity_profile=constrained)
        self.assertEqual(plan["pool_bounds"]["map"], 1000)
        self.assertEqual(plan["b_pool"], 1000)
        self.assertTrue(all(shard["final_request_tokens"] <= 1000 for shard in plan["shards"]))

    def test_record_order_does_not_change_shard_membership_or_plan_sha(self):
        items = records(31, payload_size=100)
        forward = self._build(items)
        reverse = self._build(list(reversed(items)))
        reordered_keys = {
            key: copy.deepcopy(self.pools[key])
            for key in reversed(tuple(self.pools))
        }
        reordered_mapping = self._build(items, provider_pools=reordered_keys)
        self.assertEqual(
            [shard["item_ids"] for shard in forward["shards"]],
            [shard["item_ids"] for shard in reverse["shards"]],
        )
        self.assertEqual(forward["plan_sha"], reverse["plan_sha"])
        self.assertEqual(forward["plan_sha"], reordered_mapping["plan_sha"])

    def test_pool_order_capability_capacity_and_settlement_change_plan_sha(self):
        base = self._build(records(13))
        reordered = copy.deepcopy(self.pools)
        reordered["map"].reverse()
        changed_pool = self._build(records(13), provider_pools=reordered)

        changed_caps = copy.deepcopy(self.capabilities)
        changed_caps["codex"]["profile_hash"] = sha("capability-codex-v2")
        changed_profile = copy.deepcopy(self.profile)
        changed_profile["provider_bindings"]["codex"]["capability_profile_hash"] = changed_caps["codex"]["profile_hash"]
        changed_capability = self._build(
            records(13), capabilities=changed_caps, capacity_profile=changed_profile
        )

        changed_capacity_profile = copy.deepcopy(self.profile)
        changed_capacity_profile["provider_bindings"]["codex"]["evidence_limit_tokens"] = 11000
        changed_capacity = self._build(records(13), capacity_profile=changed_capacity_profile)
        changed_settlement = self._build(
            records(13),
            budget_policy=budget_policy(settlement_policy_sha=sha("settlement-v2")),
        )
        changed_risk = self._build(
            records(13), budget_policy=budget_policy(risk_policy_sha=sha("risk-v2"))
        )
        hashes = {
            base["plan_sha"], changed_pool["plan_sha"], changed_capability["plan_sha"],
            changed_capacity["plan_sha"], changed_settlement["plan_sha"],
            changed_risk["plan_sha"],
        }
        self.assertEqual(len(hashes), 6)

    def test_attempt_provider_does_not_change_logical_task_key(self):
        plan = self._build(records(1))
        make_attempt = self._api("attempt_manifest")
        codex = make_attempt(plan, 0, 0, self.capabilities["codex"])
        grok = make_attempt(plan, 0, 0, self.capabilities["grok"])
        self.assertEqual(codex["logical_task_key"], grok["logical_task_key"])
        self.assertNotEqual(codex["attempt_id"], grok["attempt_id"])

    def test_candidate_gate_rejects_whole_set_before_fake_launch(self):
        reserve = self._api("reserve_candidate_set")
        events = []
        policy = budget_policy(round={
            "candidates": 2, "started_attempts": 10, "input_tokens": 1000,
            "output_tokens": 1000, "provider_usage_units": 1000,
        })
        with self.assertRaises(self._error()) as caught:
            reserve(policy, "duplicate_search", ["c1", "c2", "c3"], events)
        self.assertEqual(caught.exception.code, "candidate_budget_exceeded")
        self.assertEqual([event["event_type"] for event in events], ["reservation_rejected"])
        self.assertEqual(events[0]["candidate_ids"], ["c1", "c2", "c3"])

    def test_retry_split_detail_reduce_share_one_intent_budget(self):
        reserve = self._api("reserve_attempt")
        events = []
        policy = budget_policy(candidate={
            "started_attempts": 4, "input_tokens": 1000,
            "output_tokens": 400, "provider_usage_units": 40,
        })
        estimate = {"input_tokens": 100, "output_tokens": 40, "provider_usage_units": 4}
        for kind in ("retry", "failover", "split", "detail"):
            reserve(policy, "duplicate_search", "c1", "task-1", kind, estimate, events)
        with self.assertRaises(self._error()) as caught:
            reserve(policy, "duplicate_search", "c1", "task-2", "reduce", estimate, events)
        self.assertEqual(caught.exception.code, "attempt_budget_exceeded")
        self.assertEqual(
            [event["attempt_kind"] for event in events if event["event_type"] == "attempt_reserved"],
            ["retry", "failover", "split", "detail"],
        )

    def test_unknown_price_is_unknown_not_zero(self):
        reserve = self._api("reserve_attempt")
        events = []
        event = reserve(
            self.policy, "duplicate_search", "c1", "task-1", "initial",
            {"input_tokens": 100, "output_tokens": 40, "provider_usage_units": 4},
            events,
        )
        self.assertNotIn("currency_micros", event["reserved"])
        totals = self._api("budget_totals")(events, "duplicate_search", "c1")
        self.assertNotIn("currency_micros", totals)

    def test_usage_unverified_retains_worst_case_reservation(self):
        reserve = self._api("reserve_attempt")
        settle = self._api("settle_attempt")
        totals = self._api("budget_totals")
        events = []
        reservation = reserve(
            self.policy, "duplicate_search", "c1", "task-1", "initial",
            {"input_tokens": 100, "output_tokens": 40, "provider_usage_units": 4},
            events,
        )
        settle(reservation["event_id"], None, False, events)
        self.assertEqual([event["event_type"] for event in events], ["attempt_reserved", "attempt_settled"])
        self.assertEqual(
            totals(events, "duplicate_search", "c1"),
            {"started_attempts": 1, "input_tokens": 100, "output_tokens": 40, "provider_usage_units": 4},
        )

    def test_prompt_schema_cli_and_effective_identity_drift_stale_capacity(self):
        mutations = (
            ("cli_revision", "fake-cli-v2"),
            ("model_identity", "fake-model-v2"),
            ("reasoning_identity", "low"),
            ("serializer_revision", "history-audit-request-v2"),
            ("immutable_capacity_identity", "fake-capacity-v2"),
        )
        for field, changed in mutations:
            with self.subTest(field=field):
                capabilities = copy.deepcopy(self.capabilities)
                capabilities["codex"][field] = changed
                with self.assertRaises(self._error()) as caught:
                    self._build(records(1), capabilities=capabilities)
                self.assertEqual(caught.exception.code, "stale_capacity")
        tampered = copy.deepcopy(self.profile)
        tampered["schema"]["text"] = "schema-content-drift"
        with self.assertRaises(self._error()) as caught:
            self._build(records(1), capacity_profile=tampered)
        self.assertEqual(caught.exception.code, "stale_capacity")

    def test_resolved_frozen_capability_shape_is_plannable(self):
        values = capability("codex")
        values["serializer_revision"] = "portable-agent-command-v1"
        capabilities = {"codex": values, "grok": capability("grok")}
        capacity = profile(capabilities)
        capabilities["codex"] = provider_adapters.ProviderCapability(
            provider="codex",
            surface="hunt",
            executable="codex",
            executable_path=str(ROOT / "tests/fake_portable_agent.py"),
            model_override="fake-model-v1",
            reasoning_override="high",
            model_identity=values["model_identity"],
            reasoning_identity=values["reasoning_identity"],
            cli_revision=values["cli_revision"],
            serializer_revision=values["serializer_revision"],
            immutable_capacity_identity=values["immutable_capacity_identity"],
            evidence_sha256=sha("capability-evidence"),
            profile_hash=values["profile_hash"],
            hard_complete_eligible=True,
            authority="hard-complete",
        )
        plan = self._build(
            records(1), capabilities=capabilities, capacity_profile=capacity
        )
        self.assertEqual(plan["provider_capability_profile_hashes"]["codex"], values["profile_hash"])
        attempt = self._api("attempt_manifest")(plan, 0, 0, capabilities["codex"])
        self.assertEqual(attempt["provenance"]["provider"], "codex")

    def test_grammar_only_command_intent_cannot_enter_hard_complete_plan(self):
        resolve_intent = getattr(
            provider_adapters, "_resolve_command_intent_for_test", None
        )
        self.assertTrue(
            callable(resolve_intent),
            "missing behavior: provider_adapters._resolve_command_intent_for_test",
        )
        registry = provider_adapters.load_registry(
            ROOT / "history/provider-adapters-v1.json"
        )
        intent = resolve_intent(
            registry,
            "hunt",
            "codex",
            model="requested-model",
            reasoning="high",
            executable_lookup=lambda _: str(
                ROOT / "tests/fake_portable_stage_provider.py"
            ),
        )
        self.assertEqual(intent.requested_model, "requested-model")
        self.assertEqual(
            intent.requested_reasoning,
            "high",
        )
        self.assertIsNone(intent.effective_model)
        self.assertIsNone(intent.effective_reasoning)
        self.assertIsNone(intent.model_override_applied)
        self.assertIsNone(intent.reasoning_override_applied)
        self.assertEqual(intent.provider_validation, "unverified")
        self.assertFalse(intent.hard_complete_eligible)
        argv, environment = provider_adapters.render_command(
            intent, "/portable-mirror", "PROMPT"
        )
        self.assertIn("requested-model", argv)
        self.assertIn(
            "model_reasoning_effort=high", argv
        )
        self.assertEqual(environment, {})

        capabilities = dict(self.capabilities)
        capabilities["codex"] = intent
        with self.assertRaises(self._error()) as caught:
            self._build(records(1), capabilities=capabilities)
        self.assertEqual(caught.exception.code, "unbudgetable_provider")

    def test_build_plan_reserves_every_worst_case_pool_attempt(self):
        events = []
        plan = self._build_with_events(events, records(13))
        reservations = [
            event for event in events
            if event["event_type"] == "attempt_reserved"
        ]
        expected_attempts = len(plan["shards"]) * len(self.pools["map"])
        self.assertEqual(len(reservations), expected_attempts)
        expected_input = sum(
            shard["final_request_tokens"] for shard in plan["shards"]
        ) * len(self.pools["map"])
        expected_output = (
            len(plan["shards"])
            * len(self.pools["map"])
            * self.profile["max_output_tokens"]
        )
        totals = self._api("budget_totals")(
            events, "duplicate_search", self.candidate["candidate_id"]
        )
        self.assertEqual(totals["started_attempts"], expected_attempts)
        self.assertEqual(totals["input_tokens"], expected_input)
        self.assertEqual(totals["output_tokens"], expected_output)
        self.assertEqual(
            totals["provider_usage_units"], expected_input + expected_output
        )
        self.assertNotIn("currency_micros", totals)

    def test_multiple_plans_share_budget_and_reject_without_partial_events(self):
        events = []
        pools = {stage: ["codex"] for stage in self.pools}
        policy = budget_policy(candidate={
            "started_attempts": 1,
            "input_tokens": 20000,
            "output_tokens": 10000,
            "provider_usage_units": 30000,
        })
        first = self._build_with_events(
            events, records(1), provider_pools=pools, budget_policy=policy
        )
        self.assertEqual(len(first["shards"]), 1)
        before = copy.deepcopy(events)
        with self.assertRaises(self._error()) as caught:
            self._build_with_events(
                events, records(1), provider_pools=pools, budget_policy=policy
            )
        self.assertEqual(caught.exception.code, "attempt_budget_exceeded")
        self.assertEqual(events, before)
        self.assertEqual(
            sum(event["event_type"] == "candidate_reserved" for event in events),
            1,
        )


if __name__ == "__main__":
    unittest.main()
