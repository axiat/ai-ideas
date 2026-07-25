#!/usr/bin/env python3
import hashlib
import http.client
import http.server
import json
import os
import pathlib
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_budget
from lib import history_stage
from lib import history_stage_adapter
from lib import history_stage_proxy
from tests.history_stage_smoke import StageFixture


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def model_output(stage="meta"):
    contents = {
        "generate": [
            (
                "generation-ideas-markdown",
                "Assumption-Removal Attempt: incomplete — fixture; "
                "blocked by: evidence\n\n"
                "## I1\n"
                "One-Sentence Story: Bounded Test Idea\n"
                "Theme: World Models - Architecture\n"
                "Form: new mechanism or new problem\n"
                "Summary: Exercise the bounded stage contract.\n"
                "Minimal Falsification Experiment: Compare against the "
                "strongest fixture baseline on 128 episodes using one H100; "
                "kill the idea if the expected bounded signal is absent.\n"
                "Why It May Be Novel: Downstream research must test "
                "occupation.\n",
            ),
        ],
        "history-compare": [
            (
                "history-comparison-json",
                '{"comparator_version":"history-comparator-v1",'
                '"expansion_request":null,"relations":[],'
                '"status":"complete_no_match"}\n',
            )
        ],
        "review": [
            (
                "review-markdown",
                "# I1\n"
                "Verdict: accept-w-rev\n"
                "CRITICAL: 0\n"
                "MAJOR: 1\n"
                "Headline: The bounded candidate remains plausible.\n"
                "Occupation: One supplied gap remains.\n"
                "Experiment: The falsification is bounded.\n"
                "Estimand: The estimand is aligned.\n"
                "Payoff: One attributable payoff remains.\n"
                "Feasibility: One researcher and one H100 suffice.\n"
                "History: unavailable\n"
                "Reason: One bounded revision remains.\n",
            ),
        ],
        "meta": [
            (
                "failure-distillation-json",
                '{"mappings":[],"schema_version":1}\n',
            )
        ],
    }
    return {
        "schema_version": 1,
        "stage": stage,
        "artifacts": [
            {"artifact_kind": kind, "content": content}
            for kind, content in contents[stage]
        ],
    }


def _response(
    request,
    *,
    output,
    status,
    usage,
    max_output_tokens=2048,
):
    return {
        "created_at": 1,
        "error": None,
        "id": "resp_local_1",
        "incomplete_details": None,
        "instructions": "",
        "max_output_tokens": max_output_tokens,
        "metadata": {},
        "model": request["model"],
        "object": "response",
        "output": output,
        "parallel_tool_calls": False,
        "previous_response_id": None,
        "reasoning": request["reasoning"],
        "status": status,
        "store": False,
        "temperature": None,
        "text": request["text"],
        "tool_choice": "none",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": usage,
        "user": None,
    }


def _event(name, sequence_number, **fields):
    value = {
        "type": name,
        "sequence_number": sequence_number,
        **fields,
    }
    return (
        f"event: {name}\n"
        f"data: {canonical(value).decode('utf-8')}\n\n"
    ).encode("utf-8")


def valid_sse(
    request,
    output_text=None,
    output_tokens=11,
    *,
    input_tokens=10,
    cached_tokens=3,
    reasoning_tokens=2,
    include_reasoning=True,
    deltas=None,
):
    output_text = output_text or canonical(model_output()).decode("utf-8")
    message = {
        "content": [
            {
                "annotations": [],
                "logprobs": [],
                "text": output_text,
                "type": "output_text",
            }
        ],
        "id": "msg_local_1",
        "role": "assistant",
        "status": "completed",
        "type": "message",
    }
    reasoning = {
        "id": "rs_local_1",
        "summary": [
            {
                "text": "Checked bounded constraints.",
                "type": "summary_text",
            }
        ],
        "type": "reasoning",
    }
    usage = {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached_tokens},
        "output_tokens": output_tokens,
        "output_tokens_details": {
            "reasoning_tokens": reasoning_tokens,
        },
        "total_tokens": input_tokens + output_tokens,
    }
    records = []

    def emit(name, **fields):
        records.append(_event(name, len(records), **fields))

    emit(
        "response.created",
        response=_response(
            request,
            output=[],
            status="in_progress",
            usage=None,
        ),
    )
    emit(
        "response.in_progress",
        response=_response(
            request,
            output=[],
            status="in_progress",
            usage=None,
        ),
    )
    output_index = 0
    completed_output = []
    if include_reasoning:
        emit(
            "response.output_item.added",
            item={
                "id": reasoning["id"],
                "summary": [],
                "type": "reasoning",
            },
            output_index=output_index,
        )
        emit(
            "response.reasoning_summary_part.added",
            item_id=reasoning["id"],
            output_index=output_index,
            summary_index=0,
            part={"text": "", "type": "summary_text"},
        )
        emit(
            "response.reasoning_summary_text.delta",
            delta="Checked bounded ",
            item_id=reasoning["id"],
            output_index=output_index,
            summary_index=0,
        )
        emit(
            "response.reasoning_summary_text.delta",
            delta="constraints.",
            item_id=reasoning["id"],
            output_index=output_index,
            summary_index=0,
        )
        emit(
            "response.reasoning_summary_text.done",
            item_id=reasoning["id"],
            output_index=output_index,
            summary_index=0,
            text="Checked bounded constraints.",
        )
        emit(
            "response.reasoning_summary_part.done",
            item_id=reasoning["id"],
            output_index=output_index,
            summary_index=0,
            part=reasoning["summary"][0],
        )
        emit(
            "response.output_item.done",
            item=reasoning,
            output_index=output_index,
        )
        completed_output.append(reasoning)
        output_index += 1
    emit(
        "response.output_item.added",
        item={
            "content": [],
            "id": message["id"],
            "role": "assistant",
            "status": "in_progress",
            "type": "message",
        },
        output_index=output_index,
    )
    empty_part = {
        "annotations": [],
        "logprobs": [],
        "text": "",
        "type": "output_text",
    }
    emit(
        "response.content_part.added",
        content_index=0,
        item_id=message["id"],
        output_index=output_index,
        part=empty_part,
    )
    if deltas is None:
        one_third = max(1, len(output_text) // 3)
        two_thirds = max(one_third + 1, 2 * len(output_text) // 3)
        deltas = (
            output_text[:one_third],
            output_text[one_third:two_thirds],
            output_text[two_thirds:],
        )
    for delta in deltas:
        emit(
            "response.output_text.delta",
            content_index=0,
            delta=delta,
            item_id=message["id"],
            logprobs=[],
            output_index=output_index,
        )
    emit(
        "response.output_text.done",
        content_index=0,
        item_id=message["id"],
        logprobs=[],
        output_index=output_index,
        text=output_text,
    )
    emit(
        "response.content_part.done",
        content_index=0,
        item_id=message["id"],
        output_index=output_index,
        part=message["content"][0],
    )
    emit(
        "response.output_item.done",
        item=message,
        output_index=output_index,
    )
    completed_output.append(message)
    emit(
        "response.completed",
        response=_response(
            request,
            output=completed_output,
            status="completed",
            usage=usage,
        ),
    )
    return b"".join(records)


def parse_sse_records(raw):
    records = []
    for record in raw.removesuffix(b"\n\n").split(b"\n\n"):
        event_line, data_line = record.split(b"\n")
        records.append(
            (
                event_line.removeprefix(b"event: ").decode("ascii"),
                json.loads(data_line.removeprefix(b"data: ")),
            )
        )
    return records


def encode_sse_records(records):
    encoded = []
    for name, value in records:
        encoded.append(
            (
                f"event: {name}\n"
                f"data: {canonical(value).decode('utf-8')}\n\n"
            ).encode("utf-8")
        )
    return b"".join(encoded)


def mutate_sse(raw, name, mutate, *, occurrence=0):
    records = parse_sse_records(raw)
    found = 0
    for event_index, (event_name, value) in enumerate(records):
        if event_name != name:
            continue
        if found == occurrence:
            cloned = json.loads(canonical(value))
            mutate(cloned)
            records[event_index] = (cloned["type"], cloned)
            return encode_sse_records(records)
        found += 1
    raise AssertionError(f"missing fixture event: {name}[{occurrence}]")


class LoopbackServer(socketserver.TCPServer):
    allow_reuse_address = True


class UpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        self.server.requests.append(
            {
                "account_id": self.headers.get("ChatGPT-Account-ID"),
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "path": self.path,
                "raw": raw,
            }
        )
        request = json.loads(raw) if raw else {}
        payload = self.server.response_factory(request)
        self.send_response(self.server.status)
        self.send_header(
            "Content-Type",
            (
                "text/event-stream"
                if self.server.status == 200
                else "application/json"
            ),
        )
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()


class FakeUpstream:
    def __init__(self, response_factory=valid_sse, *, status=200):
        self.server = LoopbackServer(("127.0.0.1", 0), UpstreamHandler)
        self.server.requests = []
        self.server.response_factory = response_factory
        self.server.status = status
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def requests(self):
        return self.server.requests

    def __enter__(self):
        self.thread.start()
        with socket.create_connection(("127.0.0.1", self.port), timeout=1):
            pass
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class DeadlineUpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.server.started.set()
        if self.server.mode == "stall":
            self.server.release.wait(timeout=5)
            return
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                self.wfile.write(b"x")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(0.03)


class DeadlineUpstream:
    def __init__(self, mode):
        self.server = LoopbackServer(
            ("127.0.0.1", 0),
            DeadlineUpstreamHandler,
        )
        self.server.mode = mode
        self.server.started = threading.Event()
        self.server.release = threading.Event()
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )

    @property
    def port(self):
        return self.server.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class HistoryStageProxySmoke(unittest.TestCase):
    def setUp(self):
        self.prompt = '{"schema_version":1,"stage":"meta"}\n'
        self.schema = history_stage_adapter.stage_response_schema("meta")
        self.canonical_request = history_stage_proxy.canonical_request(
            prompt=self.prompt,
            schema=self.schema,
            model="gpt-5.3-codex-spark",
            reasoning_effort="xhigh",
            max_output_tokens=2048,
        )

    def client_request(self):
        return canonical(
            {
                "input": [
                    {
                        "content": [
                            {
                                "text": self.prompt,
                                "type": "input_text",
                            }
                        ],
                        "role": "user",
                        "type": "message",
                    }
                ]
            }
        )

    def exchange_for_validation(self):
        return history_stage_proxy.CanonicalExchange(
            prompt=self.prompt,
            canonical_request=self.canonical_request,
            upstream_port=1,
            output_validator=lambda raw: (
                history_stage_adapter.parse_model_output("meta", raw)
            ),
            max_output_tokens=2048,
        )

    def test_canonical_request_is_exact_and_budgeted_as_sent(self):
        request = json.loads(self.canonical_request)
        self.assertEqual(
            set(request),
            {
                "include",
                "input",
                "instructions",
                "model",
                "parallel_tool_calls",
                "reasoning",
                "store",
                "stream",
                "text",
                "tool_choice",
                "tools",
            },
        )
        self.assertEqual(request["tools"], [])
        self.assertEqual(request["tool_choice"], "none")
        self.assertNotIn("max_output_tokens", request)
        self.assertNotIn("truncation", request)
        self.assertEqual(
            history_stage_proxy.CANONICAL_REQUEST_VERSION,
            "history-canonical-request-v2",
        )
        self.assertEqual(request["input"][0]["content"][0]["text"], self.prompt)
        self.assertEqual(
            request["text"]["format"]["schema"],
            self.schema,
        )
        policy = {
            "model_context_limit": len(self.canonical_request) + 2048 + 1024,
            "max_output_tokens": 2048,
            "safety_margin": 1024,
        }
        receipt = history_budget.preflight_canonical_request(
            self.prompt.encode("utf-8"),
            self.canonical_request,
            policy,
        )
        self.assertEqual(
            receipt["input_upper_bound"],
            len(self.canonical_request),
        )
        self.assertEqual(
            receipt["canonical_request_sha256"],
            hashlib.sha256(self.canonical_request).hexdigest(),
        )
        with self.assertRaises(history_budget.PreflightError):
            history_budget.preflight_canonical_request(
                self.prompt.encode("utf-8"),
                self.canonical_request + b"x",
                dict(
                    policy,
                    model_context_limit=policy["model_context_limit"],
                ),
            )

    def test_proxy_discards_client_harness_and_forwards_one_canonical_body(self):
        validator = lambda raw: history_stage_adapter.parse_model_output(
            "meta", raw
        )
        with FakeUpstream() as upstream:
            with history_stage_proxy.CanonicalProxyServer(
                prompt=self.prompt,
                canonical_request=self.canonical_request,
                upstream_port=upstream.port,
                output_validator=validator,
                max_output_tokens=2048,
            ) as proxy:
                original = {
                    "instructions": "discarded",
                    "input": [
                        {
                            "content": [
                                {"text": "discarded", "type": "input_text"}
                            ],
                            "role": "developer",
                            "type": "message",
                        },
                        {
                            "content": [
                                {"text": self.prompt, "type": "input_text"}
                            ],
                            "role": "user",
                            "type": "message",
                        },
                    ],
                    "model": "capture-model",
                    "tools": [{"type": "web_search"}] * 7,
                }
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    proxy.port,
                    timeout=2,
                )
                connection.request(
                    "POST",
                    "/v1/responses",
                    body=canonical(original),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response_raw = response.read()
                connection.close()
                self.assertEqual(response.status, 200)
                receipt = proxy.receipt()
        self.assertEqual(len(upstream.requests), 1)
        self.assertEqual(upstream.requests[0]["path"], "/v1/responses")
        self.assertIsNone(upstream.requests[0]["authorization"])
        self.assertEqual(
            upstream.requests[0]["raw"],
            self.canonical_request,
        )
        downstream = parse_sse_records(response_raw)
        self.assertEqual(
            [name for name, _ in downstream],
            [
                "response.output_item.done",
                "response.completed",
            ],
        )
        downstream_message = downstream[0][1]["item"]
        self.assertEqual(
            downstream_message["content"][0]["text"],
            canonical(model_output()).decode("utf-8"),
        )
        self.assertEqual(
            downstream[1][1]["response"]["output"],
            [downstream_message],
        )
        self.assertEqual(receipt["request_count"], 1)
        self.assertEqual(
            receipt["prompt_sha256"],
            hashlib.sha256(self.prompt.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            receipt["canonical_request_sha256"],
            hashlib.sha256(self.canonical_request).hexdigest(),
        )
        upstream_raw = valid_sse(json.loads(self.canonical_request))
        self.assertEqual(
            receipt["upstream_sse_sha256"],
            hashlib.sha256(upstream_raw).hexdigest(),
        )
        self.assertEqual(
            receipt["synthesized_sse_sha256"],
            hashlib.sha256(response_raw).hexdigest(),
        )
        self.assertEqual(
            receipt["model_output_sha256"],
            hashlib.sha256(
                canonical(model_output())
            ).hexdigest(),
        )

    def test_upstream_exchange_has_one_absolute_deadline(self):
        for mode in ("stall", "trickle"):
            with self.subTest(mode=mode):
                with DeadlineUpstream(mode) as upstream:
                    exchange = history_stage_proxy.CanonicalExchange(
                        prompt=self.prompt,
                        canonical_request=self.canonical_request,
                        upstream_port=upstream.port,
                        output_validator=lambda raw: (
                            history_stage_adapter.parse_model_output(
                                "meta",
                                raw,
                            )
                        ),
                        max_output_tokens=2048,
                        exchange_timeout_seconds=0.15,
                    )
                    started = time.monotonic()
                    with self.assertRaises(
                        history_stage_proxy.ProxyError
                    ):
                        exchange.exchange(self.client_request())
                    self.assertLess(time.monotonic() - started, 0.8)

    def test_proxy_exit_cancels_an_active_upstream_exchange(self):
        with DeadlineUpstream("stall") as upstream:
            proxy = history_stage_proxy.CanonicalProxyServer(
                prompt=self.prompt,
                canonical_request=self.canonical_request,
                upstream_port=upstream.port,
                output_validator=lambda raw: (
                    history_stage_adapter.parse_model_output("meta", raw)
                ),
                max_output_tokens=2048,
                exchange_timeout_seconds=10,
                shutdown_timeout_seconds=0.5,
            )
            proxy.__enter__()
            finished = threading.Event()

            def invoke():
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    proxy.port,
                    timeout=2,
                )
                try:
                    connection.request(
                        "POST",
                        "/v1/responses",
                        body=self.client_request(),
                        headers={"Content-Type": "application/json"},
                    )
                    response = connection.getresponse()
                    response.read()
                except (OSError, http.client.HTTPException):
                    pass
                finally:
                    connection.close()
                    finished.set()

            client = threading.Thread(target=invoke, daemon=True)
            client.start()
            self.assertTrue(upstream.server.started.wait(timeout=1))
            started = time.monotonic()
            proxy.__exit__()
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(finished.wait(timeout=1))

    def test_adapter_installs_closed_resource_limits_before_exec(self):
        script = (
            "import json,resource;"
            "from lib import history_stage_adapter as a;"
            "a._install_resource_limits();"
            "print(json.dumps({name:list(resource.getrlimit("
            "getattr(resource,name))) for name in a.RESOURCE_LIMITS "
            "if hasattr(resource,name)},sort_keys=True))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        observed = json.loads(result.stdout)
        for name, configured in (
            history_stage_adapter.RESOURCE_LIMITS.items()
        ):
            if name not in observed:
                continue
            soft, hard = observed[name]
            self.assertGreaterEqual(soft, 0)
            self.assertGreaterEqual(hard, 0)
            self.assertLessEqual(soft, configured)
            self.assertLessEqual(hard, configured)
        self.assertEqual(observed["RLIMIT_CORE"], [0, 0])
        for required in (
            "RLIMIT_CPU",
            "RLIMIT_AS",
            "RLIMIT_FSIZE",
            "RLIMIT_NOFILE",
            "RLIMIT_NPROC",
        ):
            if hasattr(__import__("resource"), required):
                self.assertIn(required, observed)

    def test_lifecycle_rejects_sequence_order_and_framing_drift(self):
        request = json.loads(self.canonical_request)
        baseline = valid_sse(request)
        records = parse_sse_records(baseline)

        duplicate = parse_sse_records(baseline)
        duplicate[4][1]["sequence_number"] = 3

        gap = parse_sse_records(baseline)
        gap[4][1]["sequence_number"] = 5

        # Completed must be terminal under the version-tolerant scanner.
        completed_not_last = parse_sse_records(baseline)
        completed_not_last = (
            completed_not_last[:-1][-1:]
            + completed_not_last[:-1][:-1]
            + completed_not_last[-1:]
        )
        # Simpler: move completed one slot earlier by swapping with previous.
        completed_not_last = parse_sse_records(baseline)
        completed_not_last[-1], completed_not_last[-2] = (
            completed_not_last[-2],
            completed_not_last[-1],
        )
        for sequence_number, (_, value) in enumerate(completed_not_last):
            value["sequence_number"] = sequence_number

        missing_created = parse_sse_records(baseline)[1:]
        for sequence_number, (_, value) in enumerate(missing_created):
            value["sequence_number"] = sequence_number
        missing_in_progress = (
            parse_sse_records(baseline)[:1]
            + parse_sse_records(baseline)[2:]
        )
        for sequence_number, (_, value) in enumerate(
            missing_in_progress
        ):
            value["sequence_number"] = sequence_number

        cases = {
            "duplicate-sequence": encode_sse_records(duplicate),
            "gapped-sequence": encode_sse_records(gap),
            "missing-created": encode_sse_records(missing_created),
            "missing-in-progress": encode_sse_records(
                missing_in_progress
            ),
            "out-of-order-events": encode_sse_records(completed_not_last),
            "truncated-transcript": baseline[:-1],
            "oversize-transcript": (
                b"x" * (history_stage_proxy.SSE_MAX_BYTES + 1)
            ),
            "duplicate-completion": encode_sse_records(
                records + [records[-1]]
            ),
        }
        exchange = self.exchange_for_validation()
        for label, transcript in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(history_stage_proxy.ProxyError):
                    exchange._validate_response(transcript)

    def test_lifecycle_rejects_text_mismatches(self):
        request = json.loads(self.canonical_request)
        baseline = valid_sse(request)

        # Version-tolerant scanner trusts the completed assistant message
        # item; intermediate delta/part mismatches are ignored. Mutating
        # the final message content must still fail schema validation.
        cases = {
            "message-item-done": mutate_sse(
                baseline,
                "response.output_item.done",
                lambda value: value["item"]["content"][0].__setitem__(
                    "text", "wrong"
                ),
                occurrence=1,
            ),
        }
        exchange = self.exchange_for_validation()
        for label, transcript in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(history_stage_proxy.ProxyError):
                    exchange._validate_response(transcript)

    def test_lifecycle_rejects_non_message_items_and_events(self):
        request = json.loads(self.canonical_request)
        baseline = valid_sse(request, include_reasoning=False)

        item_cases = {}
        for item_type in (
            "function_call",
            "file_search_call",
            "web_search_call",
            "mcp_call",
            "computer_call",
        ):
            item_cases[f"item-{item_type}"] = mutate_sse(
                baseline,
                "response.output_item.added",
                lambda value, item_type=item_type: value[
                    "item"
                ].__setitem__("type", item_type),
            )

        event_cases = {}
        for event_type in (
            "response.function_call_arguments.delta",
            "response.file_search_call.in_progress",
            "response.web_search_call.in_progress",
            "response.mcp_call.in_progress",
            "response.computer_call.in_progress",
        ):
            event_cases[f"event-{event_type}"] = mutate_sse(
                baseline,
                "response.output_text.delta",
                lambda value, event_type=event_type: value.__setitem__(
                    "type", event_type
                ),
            )

        exchange = self.exchange_for_validation()
        for label, transcript in {**item_cases, **event_cases}.items():
            with self.subTest(label=label):
                with self.assertRaises(history_stage_proxy.ProxyError):
                    exchange._validate_response(transcript)

    def test_lifecycle_rejects_failure_incomplete_and_truncation(self):
        request = json.loads(self.canonical_request)
        baseline = valid_sse(request)

        def replace_completion(event_type, status):
            records = parse_sse_records(baseline)
            records[-1][1]["type"] = event_type
            records[-1][1]["response"]["status"] = status
            records[-1] = (event_type, records[-1][1])
            return encode_sse_records(records)

        generic_error = _event(
            "error",
            0,
            code="server_error",
            message="upstream failed",
        )
        cases = {
            "response-failed": replace_completion(
                "response.failed", "failed"
            ),
            "response-incomplete-event": replace_completion(
                "response.incomplete", "incomplete"
            ),
            "error-event": generic_error,
            "incomplete-status": mutate_sse(
                baseline,
                "response.completed",
                lambda value: (
                    value["response"].__setitem__(
                        "status", "incomplete"
                    ),
                    value["response"].__setitem__(
                        "incomplete_details",
                        {"reason": "max_output_tokens"},
                    ),
                ),
            ),
            "truncation-enabled": mutate_sse(
                baseline,
                "response.completed",
                lambda value: value["response"].__setitem__(
                    "truncation", "auto"
                ),
            ),
        }
        exchange = self.exchange_for_validation()
        for label, transcript in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(history_stage_proxy.ProxyError):
                    exchange._validate_response(transcript)

    def test_lifecycle_validates_usage_detail_bounds(self):
        request = json.loads(self.canonical_request)
        cases = {
            "cached-exceeds-input": valid_sse(
                request,
                input_tokens=10,
                cached_tokens=11,
            ),
            "reasoning-exceeds-output": valid_sse(
                request,
                output_tokens=10,
                reasoning_tokens=11,
            ),
            "negative-cached": valid_sse(
                request,
                cached_tokens=-1,
            ),
            "negative-reasoning": valid_sse(
                request,
                reasoning_tokens=-1,
            ),
        }
        exchange = self.exchange_for_validation()
        for label, transcript in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(history_stage_proxy.ProxyError):
                    exchange._validate_response(transcript)

    def test_lifecycle_accepts_no_reasoning_and_caps_accumulated_text(self):
        request = json.loads(self.canonical_request)
        baseline = valid_sse(request, include_reasoning=False)
        output_raw, usage, synthesized = (
            self.exchange_for_validation()._validate_response(baseline)
        )
        self.assertEqual(output_raw, canonical(model_output()))
        self.assertEqual(
            usage["output_tokens_details"]["reasoning_tokens"],
            2,
        )
        self.assertEqual(
            [name for name, _ in parse_sse_records(synthesized)],
            [
                "response.output_item.done",
                "response.completed",
            ],
        )
        with mock.patch.object(
            history_stage_proxy,
            "MODEL_OUTPUT_MAX_BYTES",
            16,
        ):
            with self.assertRaises(history_stage_proxy.ProxyError):
                self.exchange_for_validation()._validate_response(
                    baseline
                )

    def test_endpoint_injects_credentials_without_recording_them(self):
        token = "local-secret-token"
        account_id = "acct_local_1"
        with FakeUpstream() as upstream:
            exchange = history_stage_proxy.CanonicalExchange(
                prompt=self.prompt,
                canonical_request=self.canonical_request,
                upstream_endpoint={
                    "scheme": "http",
                    "host": "127.0.0.1",
                    "port": upstream.port,
                    "path": "/custom/responses",
                },
                authorization=token,
                account_id=account_id,
                output_validator=lambda raw: (
                    history_stage_adapter.parse_model_output("meta", raw)
                ),
                max_output_tokens=2048,
            )
            exchange.exchange(self.client_request())
            receipt_text = json.dumps(exchange.receipt, sort_keys=True)
        self.assertEqual(
            upstream.requests[0]["authorization"],
            f"Bearer {token}",
        )
        self.assertEqual(
            upstream.requests[0]["account_id"],
            account_id,
        )
        self.assertEqual(
            upstream.requests[0]["path"],
            "/custom/responses",
        )
        self.assertNotIn(token, receipt_text)
        self.assertNotIn(account_id, receipt_text)

    def test_auth_refresh_failures_are_distinct_and_sanitized(self):
        token = "never-report-this-token"
        with FakeUpstream(
            lambda _request: b'{"error":{"code":"token_expired"}}',
            status=401,
        ) as upstream:
            with history_stage_proxy.CanonicalProxyServer(
                prompt=self.prompt,
                canonical_request=self.canonical_request,
                upstream_endpoint={
                    "scheme": "http",
                    "host": "127.0.0.1",
                    "port": upstream.port,
                    "path": "/v1/responses",
                },
                authorization=token,
                output_validator=lambda raw: (
                    history_stage_adapter.parse_model_output("meta", raw)
                ),
                max_output_tokens=2048,
            ) as proxy:
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    proxy.port,
                    timeout=2,
                )
                connection.request(
                    "POST",
                    "/v1/responses",
                    body=self.client_request(),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response.read()
                connection.close()
                self.assertEqual(response.status, 401)
                self.assertEqual(
                    proxy.failure_code,
                    "auth_refresh_required",
                )
                self.assertNotIn(token, proxy.last_error or "")

        explicit_auth_error = (
            b"event: error\n"
            b'data: {"code":"token_expired","message":"expired",'
            b'"type":"error"}\n\n'
        )
        with self.assertRaises(
            history_stage_proxy.AuthRefreshRequired
        ):
            self.exchange_for_validation()._validate_response(
                explicit_auth_error
            )

    def test_proxy_rejects_prompt_response_and_usage_drift(self):
        validator = lambda raw: history_stage_adapter.parse_model_output(
            "meta", raw
        )
        bad_responses = (
            lambda request: valid_sse(
                request,
                output_text='{"schema_version":1}',
            ),
            lambda request: valid_sse(
                request,
                # Soft ceiling is large (reasoning models); exceed it.
                output_tokens=70000,
            ),
            lambda request: valid_sse(request) + b"event: extra\ndata: {}\n\n",
        )
        for response_factory in bad_responses:
            with self.subTest(factory=response_factory):
                with FakeUpstream(response_factory) as upstream:
                    exchange = history_stage_proxy.CanonicalExchange(
                        prompt=self.prompt,
                        canonical_request=self.canonical_request,
                        upstream_port=upstream.port,
                        output_validator=validator,
                        max_output_tokens=2048,
                    )
                    original = {
                        "input": [
                            {
                                "content": [
                                    {
                                        "text": self.prompt,
                                        "type": "input_text",
                                    }
                                ],
                                "role": "user",
                                "type": "message",
                            }
                        ]
                    }
                    with self.assertRaises(
                        history_stage_proxy.ProxyError
                    ):
                        exchange.exchange(canonical(original))
                    self.assertIsNone(exchange.receipt)
        wrong = history_stage_proxy.CanonicalExchange(
            prompt=self.prompt,
            canonical_request=self.canonical_request,
            upstream_port=1,
            output_validator=validator,
            max_output_tokens=2048,
        )
        with self.assertRaises(history_stage_proxy.ProxyError):
            wrong.validate_client_request(
                canonical(
                    {
                        "input": [
                            {
                                "content": [
                                    {"text": "wrong", "type": "input_text"}
                                ],
                                "role": "user",
                                "type": "message",
                            }
                        ]
                    }
                )
            )

    def test_adapter_renders_exact_declared_artifacts_for_every_stage(self):
        for stage in ("generate", "history-compare", "review", "meta"):
            with self.subTest(stage=stage):
                root = pathlib.Path(
                    tempfile.mkdtemp(prefix=f"history-adapter-{stage}-")
                )
                self.addCleanup(shutil.rmtree, root, True)
                (root / "output").mkdir()
                raw = canonical(model_output(stage))
                rendered = history_stage_adapter.materialize_model_output(
                    root,
                    stage,
                    f"{stage}-seat",
                    "1" * 64,
                    raw,
                )
                expected = {
                    path
                    for path, _ in history_stage_adapter.model_artifacts(stage)
                }
                self.assertEqual(set(rendered), expected)
                self.assertEqual(
                    {
                        str(path.relative_to(root / "output"))
                        for path in (root / "output").iterdir()
                    },
                    {
                        pathlib.PurePosixPath(path).name
                        for path in expected
                    }
                    | {"prompt-attestation.json"},
                )

    def test_default_deny_profile_reaches_only_declared_loopback_port(self):
        if sys.platform != "darwin":
            self.skipTest("Darwin loopback Seatbelt integration")
        root = pathlib.Path(
            tempfile.mkdtemp(
                prefix="history-loopback-profile-",
                dir="/private/tmp",
            )
        )
        self.addCleanup(shutil.rmtree, root, True)
        for name in (
            "output",
            "runtime",
            "tmp",
            "home",
            "xdg-config",
            "xdg-cache",
            "xdg-data",
        ):
            (root / name).mkdir()
        interpreter, runtime_executables = (
            history_stage._capture_python_runtime()
        )
        with FakeUpstream() as upstream, FakeUpstream() as undeclared:
            command = [
                interpreter["path"],
                "-c",
                (
                    "import socket;"
                    "value=socket.create_connection("
                    f"('127.0.0.1',{upstream.port}),2);"
                    "value.close();"
                    "denied=False\n"
                    "try:\n"
                    " socket.create_connection("
                    f"('127.0.0.1',{undeclared.port}),2)\n"
                    "except PermissionError:\n"
                    " denied=True\n"
                    "raise SystemExit(0 if denied else 73)"
                ),
            ]
            profile_path = root / "runtime" / "stage.sb"
            launch, profile = history_stage.build_darwin_launch(
                profile_path,
                root,
                command,
                [],
                upstream.port,
                runtime_executables=[
                    value["path"]
                    for value in runtime_executables.values()
                ],
            )
            self.assertIn(
                "(allow socket-option-get "
                "(socket-option-name SO_ERROR))",
                profile,
            )
            self.assertIn(
                "(allow socket-option-set "
                "(socket-option-name SO_NOSIGPIPE))",
                profile,
            )
            self.assertNotIn("(allow mach-lookup", profile)
            self.assertNotIn(
                f'localhost:{undeclared.port}',
                profile,
            )
            profile_path.write_text(profile, encoding="utf-8")
            history_stage._run_contained(
                launch,
                root,
                {
                    "HOME": str(root / "home"),
                    "TMPDIR": str(root / "tmp"),
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                },
            )

    def test_installed_codex_0145_loopback_contract(self):
        if sys.platform != "darwin":
            self.skipTest("Codex loopback Seatbelt integration")
        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("Codex 0.145.0 is unavailable")
        version = subprocess.run(
            [codex, "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if "0.145" not in version and "codex" not in version.lower():
            self.skipTest(f"Codex unavailable or unexpected version: {version}")
        root = pathlib.Path(
            tempfile.mkdtemp(
                prefix="history-codex-loopback-",
                dir="/private/tmp",
            )
        )
        self.addCleanup(shutil.rmtree, root, True)
        for name in (
            "output",
            "runtime",
            "tmp",
            "home",
            "xdg-config",
            "xdg-cache",
            "xdg-data",
        ):
            (root / name).mkdir()
        schema_path = root / "runtime" / "output-schema.json"
        schema_path.write_bytes(canonical(self.schema))
        adapter_path = root / "runtime" / "history_stage_adapter.py"
        adapter_path.write_bytes(
            (ROOT / "lib" / "history_stage_adapter.py").read_bytes()
        )
        final_path = root / "tmp" / "model-final.json"
        prompt_sha256 = hashlib.sha256(
            self.prompt.encode("utf-8")
        ).hexdigest()
        before = subprocess.check_output(
            ["git", "status", "--porcelain=v1"],
            cwd=ROOT,
        )
        with FakeUpstream() as upstream:
            with history_stage_proxy.CanonicalProxyServer(
                prompt=self.prompt,
                canonical_request=self.canonical_request,
                upstream_port=upstream.port,
                output_validator=lambda raw: (
                    history_stage_adapter.parse_model_output("meta", raw)
                ),
                max_output_tokens=2048,
            ) as proxy:
                codex_command = history_stage.codex_loopback_argv(
                    pathlib.Path(codex).resolve(),
                    model="capture-model",
                    reasoning_effort="xhigh",
                    mirror=root,
                    proxy_port=proxy.port,
                    output_schema_path=schema_path,
                    output_last_message_path=final_path,
                )
                wrong = list(codex_command)
                wrong.remove("-a")
                wrong.remove("never")
                wrong_exec = wrong.index("exec")
                wrong[wrong_exec + 1:wrong_exec + 1] = ["-a", "never"]
                wrong_run = subprocess.run(
                    wrong,
                    cwd=root,
                    env={
                        "HOME": str(root / "home"),
                        "CODEX_HOME": str(root / "home"),
                        "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
                        "LANG": "C",
                        "LC_ALL": "C",
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5,
                )
                self.assertNotEqual(wrong_run.returncode, 0)
                self.assertEqual(proxy.request_count, 0)
                interpreter, runtime_executables = (
                    history_stage._capture_python_runtime()
                )
                adapter_command = [
                    interpreter["path"],
                    str(adapter_path),
                    "meta",
                    "meta-seat",
                    "output/prompt-attestation.json",
                    json.dumps(codex_command, separators=(",", ":")),
                    self.prompt,
                ]
                profile_path = root / "runtime" / "stage.sb"
                launch, profile = history_stage.build_darwin_launch(
                    profile_path,
                    root,
                    adapter_command,
                    [],
                    proxy.port,
                    runtime_executables=[
                        pathlib.Path(codex).resolve(),
                        *[
                            value["path"]
                            for value in runtime_executables.values()
                        ],
                    ],
                )
                self.assertIn("(deny default)", profile)
                self.assertIn("(deny process-fork)", profile)
                self.assertIn(
                    f'(remote ip "localhost:{proxy.port}")',
                    profile,
                )
                self.assertNotIn("(allow network-outbound)", profile)
                profile_path.write_text(profile, encoding="utf-8")
                environment = {
                    "HOME": str(root / "home"),
                    "CODEX_HOME": str(root / "home"),
                    "TMPDIR": str(root / "tmp"),
                    "XDG_CONFIG_HOME": str(root / "xdg-config"),
                    "XDG_CACHE_HOME": str(root / "xdg-cache"),
                    "XDG_DATA_HOME": str(root / "xdg-data"),
                    "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                }
                try:
                    stdout, stderr = history_stage._run_contained(
                        launch,
                        root,
                        environment,
                    )
                except history_stage.StageError as exc:
                    self.fail(
                        f"{exc}; proxy_error={proxy.server.last_error!r}; "
                        f"request_count={proxy.request_count}"
                    )
                self.assertLess(len(stdout), 1024 * 1024)
                self.assertLess(len(stderr), 1024 * 1024)
                receipt = proxy.receipt()
        self.assertEqual(len(upstream.requests), 1)
        self.assertEqual(
            upstream.requests[0]["raw"],
            self.canonical_request,
        )
        self.assertTrue(final_path.is_file())
        history_stage_adapter.materialize_model_output(
            root,
            "meta",
            "meta-seat",
            prompt_sha256,
            final_path.read_bytes(),
        )
        attestation = json.loads(
            (root / "output" / "prompt-attestation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(attestation["prompt_sha256"], prompt_sha256)
        self.assertEqual(receipt["request_count"], 1)
        self.assertEqual(
            subprocess.check_output(
                ["git", "status", "--porcelain=v1"],
                cwd=ROOT,
            ),
            before,
        )

    def test_run_stage_uses_installed_codex_through_canonical_proxy(self):
        if sys.platform != "darwin":
            self.skipTest("Codex run-stage Seatbelt integration")
        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("Codex 0.145.0 is unavailable")
        version = subprocess.run(
            [codex, "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if "0.145" not in version and "codex" not in version.lower():
            self.skipTest(f"Codex unavailable or unexpected version: {version}")
        self.sentinels = [ROOT / "ledger.tsv", ROOT / ".git"]
        fixture = StageFixture(self, "meta")
        fixture.manifest["registered_environment"] = {}
        fixture.manifest["registered_runtime_reads"] = []
        fixture.write_manifest()
        auth_path = fixture.temp / "auth.json"
        auth_value = {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": "fixture-id-token",
                "access_token": "fixture-access-token",
                "refresh_token": "fixture-refresh-token",
                "account_id": "fixture-account",
            },
            "last_refresh": "2026-07-24T00:00:00Z",
        }
        auth_path.write_bytes(canonical(auth_value))
        auth_path.chmod(0o600)
        auth_before = auth_path.read_bytes()
        repository_before = subprocess.check_output(
            ["git", "status", "--porcelain=v1"],
            cwd=ROOT,
        )
        with FakeUpstream() as upstream:
            endpoint = {
                "scheme": "http",
                "host": "127.0.0.1",
                "port": upstream.port,
                "path": "/backend-api/codex/responses",
            }
            with mock.patch.object(
                history_stage,
                "CODEX_AUTH_PATH",
                auth_path,
            ):
                with mock.patch.object(
                    history_stage,
                    "_codex_upstream_endpoint",
                    return_value=endpoint,
                ):
                    completion = history_stage.run_stage(
                        "meta",
                        fixture.manifest_path,
                        [
                            codex,
                            "-m",
                            "gpt-5.3-codex-spark",
                            "-c",
                            "model_reasoning_effort=xhigh",
                        ],
                    )
        self.assertEqual(len(upstream.requests), 1)
        self.assertEqual(
            upstream.requests[0]["authorization"],
            "Bearer fixture-access-token",
        )
        self.assertEqual(
            upstream.requests[0]["account_id"],
            "fixture-account",
        )
        self.assertEqual(
            upstream.requests[0]["path"],
            endpoint["path"],
        )
        request_raw = upstream.requests[0]["raw"]
        preflight = json.loads(
            (fixture.destinations / "preflight.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            preflight["canonical_request_sha256"],
            hashlib.sha256(request_raw).hexdigest(),
        )
        self.assertEqual(
            preflight["canonical_request_bytes"],
            len(request_raw),
        )
        self.assertEqual(
            completion["canonical_exchange"][
                "canonical_request_sha256"
            ],
            preflight["canonical_request_sha256"],
        )
        self.assertEqual(
            completion["canonical_exchange"]["request_count"],
            1,
        )
        self.assertTrue(
            (
                fixture.destinations
                / "failure-distillation.json"
            ).is_file()
        )
        self.assertTrue(
            (fixture.destinations / "completion.json").is_file()
        )
        self.assertEqual(auth_path.read_bytes(), auth_before)
        self.assertEqual(
            subprocess.check_output(
                ["git", "status", "--porcelain=v1"],
                cwd=ROOT,
            ),
            repository_before,
        )

    def test_run_stage_auth_refresh_publishes_no_stage_artifact(self):
        if sys.platform != "darwin":
            self.skipTest("Codex run-stage Seatbelt integration")
        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("Codex 0.145.0 is unavailable")
        version = subprocess.run(
            [codex, "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if "0.145" not in version and "codex" not in version.lower():
            self.skipTest(f"Codex unavailable or unexpected version: {version}")
        self.sentinels = [ROOT / "ledger.tsv", ROOT / ".git"]
        fixture = StageFixture(self, "meta")
        fixture.manifest["registered_environment"] = {}
        fixture.write_manifest()
        auth_path = fixture.temp / "auth.json"
        auth_path.write_bytes(
            canonical(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {
                        "id_token": "fixture-id-token",
                        "access_token": "fixture-access-token",
                        "refresh_token": "fixture-refresh-token",
                        "account_id": "fixture-account",
                    },
                    "last_refresh": "2026-07-24T00:00:00Z",
                }
            )
        )
        auth_path.chmod(0o600)
        auth_before = auth_path.read_bytes()
        with FakeUpstream(status=401) as upstream:
            endpoint = {
                "scheme": "http",
                "host": "127.0.0.1",
                "port": upstream.port,
                "path": "/backend-api/codex/responses",
            }
            with mock.patch.object(
                history_stage,
                "CODEX_AUTH_PATH",
                auth_path,
            ):
                with mock.patch.object(
                    history_stage,
                    "_codex_upstream_endpoint",
                    return_value=endpoint,
                ):
                    with self.assertRaisesRegex(
                        history_stage.StageError,
                        "^auth_refresh_required$",
                    ):
                        history_stage.run_stage(
                            "meta",
                            fixture.manifest_path,
                            [
                                codex,
                                "-m",
                                "gpt-5.3-codex-spark",
                                "-c",
                                "model_reasoning_effort=xhigh",
                            ],
                        )
        self.assertEqual(len(upstream.requests), 1)
        self.assertTrue(
            (fixture.destinations / "preflight.json").is_file()
        )
        self.assertFalse(
            (fixture.destinations / "completion.json").exists()
        )
        for name in (
            "failure-distillation.json",
            "prompt-attestation.json",
        ):
            self.assertFalse((fixture.destinations / name).exists())
        self.assertEqual(auth_path.read_bytes(), auth_before)


if __name__ == "__main__":
    unittest.main()
