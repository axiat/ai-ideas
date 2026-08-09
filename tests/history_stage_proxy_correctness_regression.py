#!/usr/bin/env python3
import json
import pathlib
import socket
import sys
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_stage_adapter
from lib import history_stage_proxy
from tests.history_stage_proxy_smoke import (
    canonical,
    encode_sse_records,
    model_output,
    parse_sse_records,
    valid_sse,
)


class HistoryStageProxyCorrectnessRegression(unittest.TestCase):
    def setUp(self):
        self.prompt = '{"schema_version":1,"stage":"meta"}\n'
        self.schema = history_stage_adapter.stage_response_schema("meta")
        self.canonical_request = history_stage_proxy.canonical_request(
            prompt=self.prompt,
            schema=self.schema,
            model="fixture-model",
            reasoning_effort="high",
            max_output_tokens=2048,
        )

    def exchange(self):
        return history_stage_proxy.CanonicalExchange(
            prompt=self.prompt,
            canonical_request=self.canonical_request,
            upstream_port=1,
            output_validator=lambda raw: history_stage_adapter.parse_model_output(
                "meta", raw
            ),
            max_output_tokens=2048,
        )

    def test_declared_output_budget_includes_reasoning_and_text(self):
        transcript = valid_sse(
            json.loads(self.canonical_request),
            output_tokens=2049,
            reasoning_tokens=2000,
        )
        with self.assertRaisesRegex(
            history_stage_proxy.ProxyError,
            r"output_tokens=2049 > 2048",
        ):
            self.exchange()._validate_response(transcript)

    def test_multiple_completed_assistant_outputs_are_rejected(self):
        records = parse_sse_records(
            valid_sse(json.loads(self.canonical_request))
        )
        _, first_done = next(
            (name, value)
            for name, value in reversed(records)
            if name == "response.output_item.done"
            and value["item"].get("type") == "message"
        )
        second_done = json.loads(canonical(first_done))
        second_done["item"]["id"] = "msg_local_2"
        second_done["output_index"] += 1
        records.insert(-1, ("response.output_item.done", second_done))
        records[-1][1]["response"]["output"] = []
        for sequence_number, (_, value) in enumerate(records):
            value["sequence_number"] = sequence_number

        with self.assertRaisesRegex(
            history_stage_proxy.ProxyError,
            "multiple assistant message outputs are ambiguous",
        ):
            self.exchange()._validate_response(encode_sse_records(records))

    def test_json_depth_bound_precedes_interpreter_parser(self):
        depth = history_stage_proxy.JSON_MAX_NESTING_DEPTH + 1
        overflow = {
            "arrays": b"[" * depth + b"0" + b"]" * depth,
            "objects": b'{"item":' * depth + b"0" + b"}" * depth,
        }
        for label, raw in overflow.items():
            with self.subTest(parser="proxy", shape=label):
                loads = mock.Mock(return_value={})
                with mock.patch.object(
                    history_stage_proxy.json,
                    "loads",
                    loads,
                ):
                    with self.assertRaises(
                        history_stage_proxy.ProxyError
                    ) as caught:
                        history_stage_proxy._load_json(raw, "fixture")
                loads.assert_not_called()
                self.assertEqual(
                    caught.exception.failure_code,
                    "canonicalizer_rejected",
                )

            with self.subTest(parser="adapter", shape=label):
                loads = mock.Mock(return_value={})
                with mock.patch.object(
                    history_stage_adapter.json,
                    "loads",
                    loads,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "not UTF-8 JSON",
                    ):
                        history_stage_adapter.parse_model_output("meta", raw)
                loads.assert_not_called()

    def test_json_depth_bound_accepts_arrays_and_objects_at_limit(self):
        depth = history_stage_proxy.JSON_MAX_NESTING_DEPTH
        fixtures = (
            (b"[" * depth + b"0" + b"]" * depth, list, 0),
            (b'{"item":' * depth + b"0" + b"}" * depth, dict, 0),
        )
        for raw, container_type, terminal in fixtures:
            value = history_stage_proxy._load_json(raw, "fixture")
            for _ in range(depth):
                self.assertIsInstance(value, container_type)
                value = value[0] if container_type is list else value["item"]
            self.assertEqual(value, terminal)

    def test_json_nesting_ignores_brackets_and_escapes_in_strings(self):
        content = (
            '[{"close": "]}", "quote": "\\\"", '
            '"slash": "\\\\"}]' * 200
        )
        raw = self._adapter_output(content)
        value = history_stage_proxy._load_json(raw, "fixture")
        self.assertEqual(value["artifacts"][0]["content"], content)
        rendered = history_stage_adapter.parse_model_output("meta", raw)
        self.assertEqual(
            rendered["output/failure-distillation.json"],
            content.encode("utf-8"),
        )

    def test_malformed_json_nesting_keeps_public_error_mapping(self):
        malformed = (b"[}", b'{"item":[0}', b'"unterminated\\')
        for raw in malformed:
            with self.subTest(raw=raw):
                with self.assertRaises(
                    history_stage_proxy.ProxyError
                ) as caught:
                    history_stage_proxy._load_json(raw, "fixture")
                self.assertEqual(
                    caught.exception.failure_code,
                    "canonicalizer_rejected",
                )
                with self.assertRaisesRegex(ValueError, "not UTF-8 JSON"):
                    history_stage_adapter.parse_model_output("meta", raw)

    def test_deep_client_json_keeps_public_http_error_mapping(self):
        deep_json = b"[" * 2000 + b"]" * 2000
        with history_stage_proxy.CanonicalProxyServer(
            prompt=self.prompt,
            canonical_request=self.canonical_request,
            upstream_port=1,
            output_validator=lambda raw: raw,
            max_output_tokens=2048,
        ) as proxy:
            response = self._raw_request(proxy.port, deep_json)
            self.assertIn(b"HTTP/1.1 400 Bad Request", response)
            self.assertIn(b'"type":"canonicalizer_rejected"', response)
            self.assertEqual(proxy.failure_code, "canonicalizer_rejected")

    def test_cyclic_schema_keeps_public_error_mapping(self):
        cyclic_schema = {}
        cyclic_schema["self"] = cyclic_schema
        with self.assertRaises(history_stage_proxy.ProxyError):
            history_stage_proxy.canonical_request(
                prompt=self.prompt,
                schema=cyclic_schema,
                model="fixture-model",
                reasoning_effort="high",
                max_output_tokens=2048,
            )

    def test_partial_client_body_has_deadline_and_short_eof(self):
        for short_eof in (False, True):
            with self.subTest(short_eof=short_eof):
                with mock.patch.object(
                    history_stage_proxy,
                    "CLIENT_BODY_READ_TIMEOUT_SECONDS",
                    0.1,
                ):
                    with history_stage_proxy.CanonicalProxyServer(
                        prompt=self.prompt,
                        canonical_request=self.canonical_request,
                        upstream_port=1,
                        output_validator=lambda raw: raw,
                        max_output_tokens=2048,
                    ) as proxy:
                        started = time.monotonic()
                        response = self._partial_request(
                            proxy.port,
                            short_eof=short_eof,
                        )
                        elapsed = time.monotonic() - started
                        self.assertLess(elapsed, 0.8)
                        self.assertIn(b"HTTP/1.1 400 Bad Request", response)
                        self.assertIn(
                            b'"type":"canonicalizer_rejected"', response
                        )
                        self.assertEqual(proxy.request_count, 0)

    def test_adapter_schema_matches_enforceable_content_semantics(self):
        descriptor = self.schema["properties"]["artifacts"]["items"][
            "properties"
        ]["content"]
        maximum = history_stage_adapter.model_artifacts("meta")[0][1]
        self.assertEqual(descriptor["minLength"], 1)
        self.assertEqual(descriptor["maxLength"], maximum)

        exact = "é" * (maximum // len("é".encode("utf-8")))
        rendered = history_stage_adapter.parse_model_output(
            "meta", self._adapter_output(exact)
        )
        self.assertEqual(len(rendered["output/failure-distillation.json"]), maximum)
        with self.assertRaisesRegex(ValueError, "content is invalid"):
            history_stage_adapter.parse_model_output(
                "meta", self._adapter_output(exact + "é")
            )
        with self.assertRaisesRegex(ValueError, "content is invalid"):
            history_stage_adapter.parse_model_output(
                "meta", self._adapter_output("")
            )

    def test_adapter_rejects_boolean_schema_version(self):
        value = model_output("meta")
        value["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "envelope is invalid"):
            history_stage_adapter.parse_model_output("meta", canonical(value))

    @staticmethod
    def _adapter_output(content):
        return canonical(
            {
                "schema_version": 1,
                "stage": "meta",
                "artifacts": [
                    {
                        "artifact_kind": "failure-distillation-json",
                        "content": content,
                    }
                ],
            }
        )

    @staticmethod
    def _raw_request(port, body):
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(
                b"POST /v1/responses HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + body
            )
            return HistoryStageProxyCorrectnessRegression._receive_all(client)

    @staticmethod
    def _partial_request(port, *, short_eof):
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(
                b"POST /v1/responses HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 100\r\n"
                b"Connection: close\r\n\r\n"
                b"{"
            )
            if short_eof:
                client.shutdown(socket.SHUT_WR)
            return HistoryStageProxyCorrectnessRegression._receive_all(client)

    @staticmethod
    def _receive_all(client):
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


if __name__ == "__main__":
    unittest.main()
