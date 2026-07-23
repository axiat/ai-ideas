#!/usr/bin/env python3
"""One-request loopback canonicalizer for bounded Codex stage calls."""

import hashlib
import http.client
import http.server
import json
import socket
import socketserver
import threading


CLIENT_REQUEST_MAX_BYTES = 1024 * 1024
SSE_MAX_BYTES = 256 * 1024
MODEL_OUTPUT_MAX_BYTES = 128 * 1024
CANONICAL_REQUEST_VERSION = "history-canonical-request-v1"


class ProxyError(RuntimeError):
    failure_code = "canonicalizer_rejected"


class AuthRefreshRequired(ProxyError):
    failure_code = "auth_refresh_required"


_AUTH_REFRESH_CODES = {
    "auth_refresh_required",
    "authentication_error",
    "invalid_api_key",
    "token_expired",
    "unauthorized",
}

_RESPONSE_FIELDS = {
    "created_at",
    "error",
    "id",
    "incomplete_details",
    "instructions",
    "max_output_tokens",
    "metadata",
    "model",
    "object",
    "output",
    "parallel_tool_calls",
    "previous_response_id",
    "reasoning",
    "status",
    "store",
    "temperature",
    "text",
    "tool_choice",
    "tools",
    "top_p",
    "truncation",
    "usage",
    "user",
}


def _canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_request(
    *,
    prompt,
    schema,
    model,
    reasoning_effort,
    max_output_tokens,
):
    """Return the exact provider body counted and sent by the gateway."""
    if (
        not isinstance(prompt, str)
        or not prompt
        or not isinstance(schema, dict)
        or not isinstance(model, str)
        or not model
        or reasoning_effort not in {"low", "medium", "high", "xhigh"}
        or type(max_output_tokens) is not int
        or max_output_tokens < 1
    ):
        raise ProxyError("canonical request configuration is invalid")
    return _canonical_bytes(
        {
            "include": [],
            "input": [
                {
                    "content": [
                        {
                            "text": prompt,
                            "type": "input_text",
                        }
                    ],
                    "role": "user",
                    "type": "message",
                }
            ],
            "instructions": "",
            "max_output_tokens": max_output_tokens,
            "model": model,
            "parallel_tool_calls": False,
            "reasoning": {
                "effort": reasoning_effort,
                "summary": "auto",
            },
            "store": False,
            "stream": True,
            "text": {
                "format": {
                    "name": "bounded_stage_output_v1",
                    "schema": schema,
                    "strict": True,
                    "type": "json_schema",
                },
                "verbosity": "low",
            },
            "tool_choice": "none",
            "tools": [],
            "truncation": "disabled",
        }
    )


def _load_json(raw, label):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProxyError(f"{label} is invalid") from exc


def _bounded_text(value, label, maximum=4096):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
    ):
        raise ProxyError(f"{label} is invalid")
    return value


def _optional_header(value, label):
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 8192
        or any(character in value for character in "\r\n\x00")
    ):
        raise ProxyError(f"{label} is invalid")
    return value


def _normalize_upstream_endpoint(upstream_endpoint, upstream_port):
    if upstream_endpoint is None:
        if type(upstream_port) is not int or not 1 <= upstream_port <= 65535:
            raise ProxyError("upstream endpoint is invalid")
        return {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": upstream_port,
            "path": "/v1/responses",
        }
    if upstream_port is not None or not isinstance(upstream_endpoint, dict):
        raise ProxyError("upstream endpoint is invalid")
    if set(upstream_endpoint) != {"scheme", "host", "port", "path"}:
        raise ProxyError("upstream endpoint is invalid")
    scheme = upstream_endpoint.get("scheme")
    host = upstream_endpoint.get("host")
    port = upstream_endpoint.get("port")
    path = upstream_endpoint.get("path")
    if (
        scheme not in {"http", "https"}
        or not isinstance(host, str)
        or not host
        or len(host.encode("utf-8")) > 253
        or any(character in host for character in "/\r\n\x00")
        or type(port) is not int
        or not 1 <= port <= 65535
        or not isinstance(path, str)
        or not path.startswith("/")
        or len(path.encode("utf-8")) > 2048
        or any(character in path for character in "?#\r\n\x00")
    ):
        raise ProxyError("upstream endpoint is invalid")
    return {
        "scheme": scheme,
        "host": host,
        "port": port,
        "path": path,
    }


def _is_auth_refresh(value):
    if not isinstance(value, dict):
        return False
    candidates = [value.get("code")]
    error = value.get("error")
    if isinstance(error, dict):
        candidates.append(error.get("code"))
    response = value.get("response")
    if isinstance(response, dict) and isinstance(
        response.get("error"), dict
    ):
        candidates.append(response["error"].get("code"))
    return any(
        isinstance(candidate, str)
        and candidate.casefold() in _AUTH_REFRESH_CODES
        for candidate in candidates
    )


def _expect_keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ProxyError(f"{label} is invalid")


def _expect_index_fields(value, *, output_index, item_id=None):
    if value.get("output_index") != output_index:
        raise ProxyError("output index is invalid")
    if item_id is not None and value.get("item_id") != item_id:
        raise ProxyError("output item ID is invalid")


def _validate_message(value):
    if (
        not isinstance(value, dict)
        or set(value)
        != {"content", "id", "role", "status", "type"}
        or value.get("role") != "assistant"
        or value.get("status") != "completed"
        or value.get("type") != "message"
        or not isinstance(value.get("content"), list)
        or len(value["content"]) != 1
    ):
        raise ProxyError("completed message is invalid")
    _bounded_text(value.get("id"), "message ID", 128)
    content = value["content"][0]
    if (
        not isinstance(content, dict)
        or set(content)
        != {"annotations", "logprobs", "text", "type"}
        or content.get("type") != "output_text"
        or content.get("annotations") != []
        or content.get("logprobs") != []
        or not isinstance(content.get("text"), str)
        or len(content["text"].encode("utf-8"))
        > MODEL_OUTPUT_MAX_BYTES
    ):
        raise ProxyError("completed output text is invalid")
    return content["text"].encode("utf-8")


def _parse_sse(raw):
    if not raw or len(raw) > SSE_MAX_BYTES or b"\r" in raw:
        raise ProxyError("SSE transcript is invalid")
    records = raw.split(b"\n\n")
    if records[-1] != b"":
        raise ProxyError("SSE transcript is unterminated")
    records.pop()
    parsed = []
    for sequence_number, record in enumerate(records):
        lines = record.split(b"\n")
        if (
            len(lines) != 2
            or not lines[0].startswith(b"event: ")
            or not lines[1].startswith(b"data: ")
        ):
            raise ProxyError("SSE event framing is invalid")
        try:
            event = lines[0][len(b"event: "):].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProxyError("SSE event name is invalid") from exc
        value = _load_json(lines[1][len(b"data: "):], "SSE data")
        if not isinstance(value, dict) or value.get("type") != event:
            raise ProxyError("SSE event type is invalid")
        if event in {
            "error",
            "response.failed",
            "response.incomplete",
        }:
            if _is_auth_refresh(value):
                raise AuthRefreshRequired(
                    "upstream authorization requires refresh"
                )
            raise ProxyError("upstream response did not complete")
        if (
            type(value.get("sequence_number")) is not int
            or value["sequence_number"] != sequence_number
        ):
            raise ProxyError("SSE sequence is not contiguous")
        parsed.append((event, value))
    if not parsed:
        raise ProxyError("SSE transcript is empty")
    return parsed


class _EventCursor:
    def __init__(self, events):
        self.events = events
        self.index = 0

    def peek(self):
        if self.index >= len(self.events):
            return None
        return self.events[self.index][0]

    def take(self, event):
        if self.peek() != event:
            raise ProxyError("SSE event order is invalid")
        value = self.events[self.index][1]
        self.index += 1
        return value

    def finish(self):
        if self.index != len(self.events):
            raise ProxyError("SSE contains trailing events")


def _response_shell(
    response,
    *,
    canonical,
    max_output_tokens,
    status,
    output,
    usage,
):
    if (
        not isinstance(response, dict)
        or set(response) != _RESPONSE_FIELDS
        or type(response.get("created_at")) is not int
        or response.get("error") is not None
        or response.get("incomplete_details") is not None
        or response.get("instructions") != canonical["instructions"]
        or response.get("max_output_tokens") != max_output_tokens
        or response.get("metadata") != {}
        or response.get("model") != canonical["model"]
        or response.get("object") != "response"
        or response.get("output") != output
        or response.get("parallel_tool_calls") is not False
        or response.get("previous_response_id") is not None
        or response.get("reasoning") != canonical["reasoning"]
        or response.get("status") != status
        or response.get("store") is not False
        or response.get("temperature") is not None
        or response.get("text") != canonical["text"]
        or response.get("tool_choice") != "none"
        or response.get("tools") != []
        or response.get("top_p") is not None
        or response.get("truncation") != canonical["truncation"]
        or response.get("usage") != usage
        or response.get("user") is not None
    ):
        raise ProxyError("response does not match the canonical request")
    return _bounded_text(response.get("id"), "response ID", 128)


def _validate_usage(usage, max_output_tokens):
    if (
        not isinstance(usage, dict)
        or set(usage)
        != {
            "input_tokens",
            "input_tokens_details",
            "output_tokens",
            "output_tokens_details",
            "total_tokens",
        }
    ):
        raise ProxyError("response usage is invalid")
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        if type(usage.get(name)) is not int or usage[name] < 0:
            raise ProxyError("response usage is invalid")
    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    if (
        not isinstance(input_details, dict)
        or set(input_details) != {"cached_tokens"}
        or type(input_details.get("cached_tokens")) is not int
        or input_details["cached_tokens"] < 0
        or input_details["cached_tokens"] > usage["input_tokens"]
        or not isinstance(output_details, dict)
        or set(output_details) != {"reasoning_tokens"}
        or type(output_details.get("reasoning_tokens")) is not int
        or output_details["reasoning_tokens"] < 0
        or output_details["reasoning_tokens"] > usage["output_tokens"]
        or usage["output_tokens"] > max_output_tokens
        or usage["total_tokens"]
        != usage["input_tokens"] + usage["output_tokens"]
    ):
        raise ProxyError("response usage is invalid")
    return usage


def _validate_reasoning_item(item, *, summary, status):
    if (
        not isinstance(item, dict)
        or set(item) not in (
            {"id", "summary", "type"},
            {"id", "status", "summary", "type"},
        )
        or item.get("type") != "reasoning"
        or item.get("summary") != summary
        or (
            "status" in item
            and item.get("status") != status
        )
    ):
        raise ProxyError("reasoning output item is invalid")
    _bounded_text(item.get("id"), "reasoning item ID", 128)
    return item


def _validate_message_added(item):
    if (
        not isinstance(item, dict)
        or set(item)
        != {"content", "id", "role", "status", "type"}
        or item.get("content") != []
        or item.get("role") != "assistant"
        or item.get("status") != "in_progress"
        or item.get("type") != "message"
    ):
        raise ProxyError("assistant message start is invalid")
    return _bounded_text(item.get("id"), "message ID", 128)


def _validate_output_part(part, *, text):
    if (
        not isinstance(part, dict)
        or set(part)
        != {"annotations", "logprobs", "text", "type"}
        or part.get("annotations") != []
        or part.get("logprobs") != []
        or part.get("text") != text
        or part.get("type") != "output_text"
    ):
        raise ProxyError("output text part is invalid")
    return part


def _event_bytes(name, value):
    return (
        f"event: {name}\n".encode("ascii")
        + b"data: "
        + _canonical_bytes(value)
        + b"\n\n"
    )


def _synthesize_response(message, completed_response):
    item_done = {
        "item": message,
        "output_index": 0,
        "sequence_number": 0,
        "type": "response.output_item.done",
    }
    response = dict(completed_response)
    response["output"] = [message]
    completed = {
        "response": response,
        "sequence_number": 1,
        "type": "response.completed",
    }
    return (
        _event_bytes("response.output_item.done", item_done)
        + _event_bytes("response.completed", completed)
    )


class CanonicalExchange:
    """Validate one Codex request and one bounded loopback response."""

    def __init__(
        self,
        *,
        prompt,
        canonical_request,
        upstream_endpoint=None,
        upstream_port=None,
        authorization=None,
        account_id=None,
        output_validator,
        max_output_tokens,
    ):
        if (
            not isinstance(prompt, str)
            or not prompt
            or not isinstance(canonical_request, bytes)
            or not callable(output_validator)
            or type(max_output_tokens) is not int
            or max_output_tokens < 1
        ):
            raise ProxyError("canonical exchange configuration is invalid")
        self.prompt = prompt
        self.prompt_sha256 = _sha256(prompt.encode("utf-8"))
        self.canonical_request = canonical_request
        self.canonical = _load_json(
            canonical_request,
            "canonical request",
        )
        if (
            not isinstance(self.canonical, dict)
            or self.canonical.get("tools") != []
            or self.canonical.get("tool_choice") != "none"
            or self.canonical.get("stream") is not True
            or self.canonical.get("truncation") != "disabled"
            or self.canonical.get("max_output_tokens")
            != max_output_tokens
            or self.canonical.get("input")
            != [
                {
                    "content": [
                        {"text": prompt, "type": "input_text"}
                    ],
                    "role": "user",
                    "type": "message",
                }
            ]
        ):
            raise ProxyError("canonical request contract is invalid")
        self.upstream_endpoint = _normalize_upstream_endpoint(
            upstream_endpoint,
            upstream_port,
        )
        self.authorization = _optional_header(
            authorization,
            "upstream authorization",
        )
        self.account_id = _optional_header(
            account_id,
            "upstream account ID",
        )
        self.output_validator = output_validator
        self.max_output_tokens = max_output_tokens
        self.request_count = 0
        self.receipt = None
        self._lock = threading.Lock()

    def validate_client_request(self, raw):
        if (
            not isinstance(raw, bytes)
            or not raw
            or len(raw) > CLIENT_REQUEST_MAX_BYTES
        ):
            raise ProxyError("Codex request size is invalid")
        value = _load_json(raw, "Codex request")
        if not isinstance(value, dict):
            raise ProxyError("Codex request must be an object")
        messages = value.get("input")
        if not isinstance(messages, list) or not messages:
            raise ProxyError("Codex request input is invalid")
        if messages[-1] != {
            "content": [
                {"text": self.prompt, "type": "input_text"}
            ],
            "role": "user",
            "type": "message",
        }:
            raise ProxyError("Codex prompt does not match preflight")
        return value

    def _read_upstream(self):
        endpoint = self.upstream_endpoint
        connection_type = (
            http.client.HTTPSConnection
            if endpoint["scheme"] == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(
            endpoint["host"],
            endpoint["port"],
            timeout=5,
        )
        try:
            headers = {"Content-Type": "application/json"}
            if self.authorization is not None:
                headers["Authorization"] = (
                    f"Bearer {self.authorization}"
                )
            if self.account_id is not None:
                headers["ChatGPT-Account-ID"] = self.account_id
            connection.request(
                "POST",
                endpoint["path"],
                body=self.canonical_request,
                headers=headers,
            )
            response = connection.getresponse()
            if response.status == 401:
                raise AuthRefreshRequired(
                    "upstream authorization requires refresh"
                )
            if (
                response.status != 200
                or response.getheader("Content-Type", "").split(";", 1)[0]
                != "text/event-stream"
            ):
                raise ProxyError("loopback upstream response is invalid")
            chunks = []
            total = 0
            while True:
                chunk = response.read(
                    min(65536, SSE_MAX_BYTES + 1 - total)
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > SSE_MAX_BYTES:
                    raise ProxyError("SSE transcript exceeds its bound")
            return b"".join(chunks)
        except (OSError, http.client.HTTPException) as exc:
            raise ProxyError("loopback upstream is unavailable") from exc
        finally:
            connection.close()

    def _validate_response(self, raw):
        events = _parse_sse(raw)
        cursor = _EventCursor(events)
        response_id = None
        for event_name in ("response.created", "response.in_progress"):
            event = cursor.take(event_name)
            _expect_keys(
                event,
                {"response", "sequence_number", "type"},
                event_name,
            )
            current_id = _response_shell(
                event.get("response"),
                canonical=self.canonical,
                max_output_tokens=self.max_output_tokens,
                status="in_progress",
                output=[],
                usage=None,
            )
            if response_id is not None and current_id != response_id:
                raise ProxyError("response ID changed during streaming")
            response_id = current_id

        output_index = 0
        reasoning_item = None
        if cursor.peek() == "response.output_item.added":
            candidate = cursor.events[cursor.index][1].get("item")
            if isinstance(candidate, dict) and candidate.get("type") == (
                "reasoning"
            ):
                added = cursor.take("response.output_item.added")
                _expect_keys(
                    added,
                    {
                        "item",
                        "output_index",
                        "sequence_number",
                        "type",
                    },
                    "reasoning item start",
                )
                _expect_index_fields(
                    added,
                    output_index=output_index,
                )
                added_item = _validate_reasoning_item(
                    added.get("item"),
                    summary=[],
                    status="in_progress",
                )
                reasoning_id = added_item["id"]
                summary = []
                if cursor.peek() == (
                    "response.reasoning_summary_part.added"
                ):
                    part_added = cursor.take(
                        "response.reasoning_summary_part.added"
                    )
                    _expect_keys(
                        part_added,
                        {
                            "item_id",
                            "output_index",
                            "part",
                            "sequence_number",
                            "summary_index",
                            "type",
                        },
                        "reasoning summary part start",
                    )
                    _expect_index_fields(
                        part_added,
                        output_index=output_index,
                        item_id=reasoning_id,
                    )
                    if (
                        part_added.get("summary_index") != 0
                        or part_added.get("part")
                        != {"text": "", "type": "summary_text"}
                    ):
                        raise ProxyError(
                            "reasoning summary part is invalid"
                        )
                    summary_fragments = []
                    summary_bytes = 0
                    while cursor.peek() == (
                        "response.reasoning_summary_text.delta"
                    ):
                        delta_event = cursor.take(
                            "response.reasoning_summary_text.delta"
                        )
                        _expect_keys(
                            delta_event,
                            {
                                "delta",
                                "item_id",
                                "output_index",
                                "sequence_number",
                                "summary_index",
                                "type",
                            },
                            "reasoning summary delta",
                        )
                        _expect_index_fields(
                            delta_event,
                            output_index=output_index,
                            item_id=reasoning_id,
                        )
                        if delta_event.get("summary_index") != 0:
                            raise ProxyError(
                                "reasoning summary index is invalid"
                            )
                        delta = _bounded_text(
                            delta_event.get("delta"),
                            "reasoning summary delta",
                            MODEL_OUTPUT_MAX_BYTES,
                        )
                        summary_bytes += len(delta.encode("utf-8"))
                        if summary_bytes > MODEL_OUTPUT_MAX_BYTES:
                            raise ProxyError(
                                "reasoning summary exceeds its bound"
                            )
                        summary_fragments.append(delta)
                    if not summary_fragments:
                        raise ProxyError(
                            "reasoning summary has no text delta"
                        )
                    summary_text = "".join(summary_fragments)
                    text_done = cursor.take(
                        "response.reasoning_summary_text.done"
                    )
                    _expect_keys(
                        text_done,
                        {
                            "item_id",
                            "output_index",
                            "sequence_number",
                            "summary_index",
                            "text",
                            "type",
                        },
                        "reasoning summary text completion",
                    )
                    _expect_index_fields(
                        text_done,
                        output_index=output_index,
                        item_id=reasoning_id,
                    )
                    if (
                        text_done.get("summary_index") != 0
                        or text_done.get("text") != summary_text
                    ):
                        raise ProxyError(
                            "reasoning summary text does not match"
                        )
                    expected_summary_part = {
                        "text": summary_text,
                        "type": "summary_text",
                    }
                    part_done = cursor.take(
                        "response.reasoning_summary_part.done"
                    )
                    _expect_keys(
                        part_done,
                        {
                            "item_id",
                            "output_index",
                            "part",
                            "sequence_number",
                            "summary_index",
                            "type",
                        },
                        "reasoning summary part completion",
                    )
                    _expect_index_fields(
                        part_done,
                        output_index=output_index,
                        item_id=reasoning_id,
                    )
                    if (
                        part_done.get("summary_index") != 0
                        or part_done.get("part")
                        != expected_summary_part
                    ):
                        raise ProxyError(
                            "reasoning summary part does not match"
                        )
                    summary = [expected_summary_part]
                reasoning_done = cursor.take(
                    "response.output_item.done"
                )
                _expect_keys(
                    reasoning_done,
                    {
                        "item",
                        "output_index",
                        "sequence_number",
                        "type",
                    },
                    "reasoning item completion",
                )
                _expect_index_fields(
                    reasoning_done,
                    output_index=output_index,
                )
                reasoning_item = _validate_reasoning_item(
                    reasoning_done.get("item"),
                    summary=summary,
                    status="completed",
                )
                if reasoning_item["id"] != reasoning_id:
                    raise ProxyError(
                        "reasoning item ID changed during streaming"
                    )
                output_index += 1

        message_added = cursor.take("response.output_item.added")
        _expect_keys(
            message_added,
            {
                "item",
                "output_index",
                "sequence_number",
                "type",
            },
            "message item start",
        )
        _expect_index_fields(
            message_added,
            output_index=output_index,
        )
        message_id = _validate_message_added(
            message_added.get("item")
        )

        part_added = cursor.take("response.content_part.added")
        _expect_keys(
            part_added,
            {
                "content_index",
                "item_id",
                "output_index",
                "part",
                "sequence_number",
                "type",
            },
            "output text part start",
        )
        _expect_index_fields(
            part_added,
            output_index=output_index,
            item_id=message_id,
        )
        if part_added.get("content_index") != 0:
            raise ProxyError("content index is invalid")
        _validate_output_part(part_added.get("part"), text="")

        output_fragments = []
        output_bytes = 0
        while cursor.peek() == "response.output_text.delta":
            delta_event = cursor.take("response.output_text.delta")
            allowed_keys = {
                "content_index",
                "delta",
                "item_id",
                "output_index",
                "sequence_number",
                "type",
            }
            if "logprobs" in delta_event:
                allowed_keys.add("logprobs")
            _expect_keys(
                delta_event,
                allowed_keys,
                "output text delta",
            )
            _expect_index_fields(
                delta_event,
                output_index=output_index,
                item_id=message_id,
            )
            if (
                delta_event.get("content_index") != 0
                or (
                    "logprobs" in delta_event
                    and delta_event.get("logprobs") != []
                )
            ):
                raise ProxyError("output text delta is invalid")
            delta = _bounded_text(
                delta_event.get("delta"),
                "output text delta",
                MODEL_OUTPUT_MAX_BYTES,
            )
            output_bytes += len(delta.encode("utf-8"))
            if output_bytes > MODEL_OUTPUT_MAX_BYTES:
                raise ProxyError("model output exceeds its bound")
            output_fragments.append(delta)
        if not output_fragments:
            raise ProxyError("assistant message has no output text")
        output_text = "".join(output_fragments)
        output_raw = output_text.encode("utf-8")

        text_done = cursor.take("response.output_text.done")
        allowed_keys = {
            "content_index",
            "item_id",
            "output_index",
            "sequence_number",
            "text",
            "type",
        }
        if "logprobs" in text_done:
            allowed_keys.add("logprobs")
        _expect_keys(text_done, allowed_keys, "output text completion")
        _expect_index_fields(
            text_done,
            output_index=output_index,
            item_id=message_id,
        )
        if (
            text_done.get("content_index") != 0
            or text_done.get("text") != output_text
            or (
                "logprobs" in text_done
                and text_done.get("logprobs") != []
            )
        ):
            raise ProxyError("completed output text does not match")

        part_done = cursor.take("response.content_part.done")
        _expect_keys(
            part_done,
            {
                "content_index",
                "item_id",
                "output_index",
                "part",
                "sequence_number",
                "type",
            },
            "output text part completion",
        )
        _expect_index_fields(
            part_done,
            output_index=output_index,
            item_id=message_id,
        )
        if part_done.get("content_index") != 0:
            raise ProxyError("content index is invalid")
        _validate_output_part(
            part_done.get("part"),
            text=output_text,
        )

        message_done = cursor.take("response.output_item.done")
        _expect_keys(
            message_done,
            {
                "item",
                "output_index",
                "sequence_number",
                "type",
            },
            "message item completion",
        )
        _expect_index_fields(
            message_done,
            output_index=output_index,
        )
        message = message_done.get("item")
        completed_output_raw = _validate_message(message)
        if (
            message.get("id") != message_id
            or completed_output_raw != output_raw
        ):
            raise ProxyError(
                "completed message does not match streamed text"
            )

        completed = cursor.take("response.completed")
        _expect_keys(
            completed,
            {"response", "sequence_number", "type"},
            "response completion",
        )
        cursor.finish()
        response = completed.get("response")
        expected_output = (
            [reasoning_item, message]
            if reasoning_item is not None
            else [message]
        )
        if not isinstance(response, dict):
            raise ProxyError("completed response is invalid")
        usage = _validate_usage(
            response.get("usage"),
            self.max_output_tokens,
        )
        completed_response_id = _response_shell(
            response,
            canonical=self.canonical,
            max_output_tokens=self.max_output_tokens,
            status="completed",
            output=expected_output,
            usage=usage,
        )
        if (
            response_id is not None
            and completed_response_id != response_id
        ):
            raise ProxyError("response ID changed during streaming")
        try:
            self.output_validator(output_raw)
        except Exception as exc:
            raise ProxyError("model output schema is invalid") from exc
        synthesized = _synthesize_response(message, response)
        if len(synthesized) > SSE_MAX_BYTES:
            raise ProxyError("synthesized SSE exceeds its bound")
        return output_raw, usage, synthesized

    def exchange(self, client_raw):
        with self._lock:
            self.request_count += 1
            if self.request_count != 1:
                raise ProxyError("canonicalizer accepts one request")
        self.validate_client_request(client_raw)
        upstream_raw = self._read_upstream()
        output_raw, usage, synthesized_raw = self._validate_response(
            upstream_raw
        )
        schema_raw = _canonical_bytes(
            self.canonical["text"]["format"]["schema"]
        )
        self.receipt = {
            "schema_version": 1,
            "request_count": 1,
            "prompt_sha256": self.prompt_sha256,
            "canonical_request_sha256": _sha256(
                self.canonical_request
            ),
            "canonical_request_bytes": len(self.canonical_request),
            "response_schema_sha256": _sha256(schema_raw),
            "upstream_sse_sha256": _sha256(upstream_raw),
            "upstream_sse_bytes": len(upstream_raw),
            "synthesized_sse_sha256": _sha256(synthesized_raw),
            "synthesized_sse_bytes": len(synthesized_raw),
            "model_output_sha256": _sha256(output_raw),
            "model_output_bytes": len(output_raw),
            "output_tokens": usage["output_tokens"],
        }
        return synthesized_raw


class _LoopbackServer(socketserver.TCPServer):
    allow_reuse_address = True


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def do_POST(self):
        try:
            if self.path != "/v1/responses":
                raise ProxyError("canonicalizer path is invalid")
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0] != "application/json":
                raise ProxyError("canonicalizer content type is invalid")
            length_text = self.headers.get("Content-Length")
            if length_text is None or not length_text.isdigit():
                raise ProxyError("canonicalizer content length is invalid")
            length = int(length_text)
            if not 1 <= length <= CLIENT_REQUEST_MAX_BYTES:
                raise ProxyError("canonicalizer content length is invalid")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ProxyError("canonicalizer request is truncated")
            response_raw = self.server.exchange.exchange(raw)
        except ProxyError as exc:
            payload = _canonical_bytes(
                {"error": {"type": exc.failure_code}}
            )
            self.send_response(
                401
                if isinstance(exc, AuthRefreshRequired)
                else 400
            )
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.server.last_error = str(exc)
            self.server.failure_code = exc.failure_code
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(response_raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response_raw)
        self.wfile.flush()


class CanonicalProxyServer:
    """Context-managed loopback server for one canonical exchange."""

    def __init__(self, **exchange_arguments):
        self.exchange = CanonicalExchange(**exchange_arguments)
        self.server = _LoopbackServer(("127.0.0.1", 0), _ProxyHandler)
        self.server.exchange = self.exchange
        self.server.last_error = None
        self.server.failure_code = None
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def request_count(self):
        return self.exchange.request_count

    @property
    def failure_code(self):
        return self.server.failure_code

    @property
    def last_error(self):
        return self.server.last_error

    def __enter__(self):
        self.thread.start()
        try:
            with socket.create_connection(
                ("127.0.0.1", self.port),
                timeout=1,
            ):
                pass
        except OSError:
            self.__exit__()
            raise ProxyError("canonicalizer did not start")
        return self

    def receipt(self):
        if self.exchange.receipt is None:
            raise ProxyError("canonical exchange is incomplete")
        return dict(self.exchange.receipt)

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
