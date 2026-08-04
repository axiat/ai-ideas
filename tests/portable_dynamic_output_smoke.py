#!/usr/bin/env python3
"""Dynamic portable envelopes are hashed only after provider completion."""

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import portable_agent
from lib import provider_adapters


REGISTRY = ROOT / "history/provider-adapters-v1.json"
FAKE = ROOT / "tests/fake_portable_agent.py"


def _probe(provider, executable_path, model, reasoning):
    return {
        "cli_revision": "fake-cli-v1",
        "serializer_revision": "portable-agent-command-v1",
        "effective_model": model,
        "effective_reasoning": reasoning,
        "model_override_applied": True,
        "reasoning_override_applied": True,
        "immutable_capacity_identity": "fake-capacity-v1",
    }


class PortableDynamicOutputSmoke(unittest.TestCase):
    def test_output_sha_is_computed_after_a_dynamic_envelope_is_written(self):
        registry = provider_adapters.load_registry(REGISTRY)
        capability = provider_adapters._resolve_provider_for_test(
            registry,
            "hunt",
            "codex",
            model="MODEL",
            reasoning="high",
            executable_lookup=lambda _: str(FAKE),
            version_probe=_probe,
        )
        output_contract = {
            "path": "output/result.json",
            "max_bytes": 256,
            "sha256": None,
            "allowed_fields": ["request_id", "status"],
            "required_fields": ["request_id", "status"],
            "field_types": {"request_id": "string", "status": "string"},
            "forbid_extra_files": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            try:
                result = portable_agent.run_portable_attempt(
                    capability,
                    inputs=[],
                    output_contract=output_contract,
                    prompt=json.dumps(
                        {"mode": "success", "request_id": "request-1"}
                    ),
                    state_root=pathlib.Path(directory) / "state",
                    timeout_seconds=2,
                )
            except portable_agent.PortableAgentError as exc:
                self.fail(
                    "portable output contracts rejected a host-computed SHA: "
                    f"{exc.code}"
                )
            self.assertEqual(
                result["output_sha256"],
                "c41117ed3254d09435111108f40b5900ec6537b69b417828bc6bb90c44359dea",
            )
            self.assertEqual(
                pathlib.Path(result["output_path"]).read_bytes(),
                b'{"request_id":"request-1","status":"ok"}\n',
            )
            self.assertEqual(
                result["value"], {"request_id": "request-1", "status": "ok"}
            )


if __name__ == "__main__":
    unittest.main()
