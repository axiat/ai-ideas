#!/usr/bin/env python3
"""One-request loopback canonicalizer for bounded Codex stage calls."""

import hashlib
import http.client
import http.server
import json
import re
import socket
import socketserver
import threading
import time


CLIENT_REQUEST_MAX_BYTES = 1024 * 1024
# Maximum silence between body chunks. Progress resets this deadline; the
# exchange contract supplies a separate overall deadline.
CLIENT_BODY_READ_TIMEOUT_SECONDS = 1
# xhigh reasoning streams include large encrypted_content blobs; 256KiB
# is routinely exceeded on generate.
SSE_MAX_BYTES = 4 * 1024 * 1024
MODEL_OUTPUT_MAX_BYTES = 256 * 1024
# Independent of interpreter-specific json decoder recursion behavior.
JSON_MAX_NESTING_DEPTH = 128
# Upstream SSE with xhigh reasoning + large generate prompts needs minutes.
UPSTREAM_EXCHANGE_TIMEOUT_SECONDS = 180
PROXY_SHUTDOWN_TIMEOUT_SECONDS = 1
# v2: ChatGPT Codex `/backend-api/codex/responses` rejects request-body
# `max_output_tokens` and `truncation`; response shells gained extra fields.
CANONICAL_REQUEST_VERSION = "history-canonical-request-v2"


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
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProxyError("JSON canonicalization failed") from exc


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
    """Return the exact provider body counted and sent by the gateway.

    ``max_output_tokens`` remains a local budget parameter (usage ceiling) but
    is not sent upstream: current ChatGPT Codex responses API rejects it, and
    also rejects request-body ``truncation``.
    """
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
    del max_output_tokens  # budget-only; not part of the wire body
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
        }
    )


def _validate_json_nesting(raw):
    stack = bytearray()
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            elif byte < 0x20:
                raise ValueError("invalid control character in JSON string")
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            if len(stack) >= JSON_MAX_NESTING_DEPTH:
                raise ValueError("JSON nesting depth exceeds its bound")
            stack.append(byte)
        elif byte == 0x5D:
            if not stack or stack.pop() != 0x5B:
                raise ValueError("JSON nesting is unbalanced")
        elif byte == 0x7D:
            if not stack or stack.pop() != 0x7B:
                raise ValueError("JSON nesting is unbalanced")
    if in_string or stack:
        raise ValueError("JSON nesting is unbalanced")


def _load_json(raw, label):
    try:
        _validate_json_nesting(raw)
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
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
    """Require the listed keys; allow additional provider fields."""
    if not isinstance(value, dict) or not set(expected).issubset(value):
        raise ProxyError(f"{label} is invalid")


def _expect_index_fields(value, *, output_index, item_id=None):
    if value.get("output_index") != output_index:
        raise ProxyError("output index is invalid")
    if item_id is not None and value.get("item_id") != item_id:
        raise ProxyError("output item ID is invalid")


def _validate_message(value):
    required = {"content", "id", "role", "status", "type"}
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or value.get("role") != "assistant"
        or value.get("status") != "completed"
        or value.get("type") != "message"
        or not isinstance(value.get("content"), list)
        or len(value["content"]) != 1
    ):
        raise ProxyError("completed message is invalid")
    _bounded_text(value.get("id"), "message ID", 128)
    content = value["content"][0]
    content_required = {"annotations", "logprobs", "text", "type"}
    if (
        not isinstance(content, dict)
        or not content_required.issubset(content)
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
    """Validate a response shell against the canonical request contract.

    Current ChatGPT Codex streams extra top-level fields and may leave
    ``output`` empty on ``response.completed`` (content already streamed).
    Critical identity fields are still enforced; non-contract fields are
    ignored.
    """
    required = {
        "created_at",
        "error",
        "id",
        "incomplete_details",
        "model",
        "object",
        "output",
        "status",
        "usage",
    }
    if (
        not isinstance(response, dict)
        or not required.issubset(response)
        or type(response.get("created_at")) is not int
        or response.get("error") is not None
        or response.get("incomplete_details") is not None
        or response.get("model") != canonical["model"]
        or response.get("object") != "response"
        or response.get("status") != status
        or response.get("usage") != usage
    ):
        raise ProxyError("response does not match the canonical request")
    instructions = response.get("instructions")
    if instructions not in (None, "", canonical.get("instructions", "")):
        raise ProxyError("response does not match the canonical request")
    mot = response.get("max_output_tokens")
    if mot is not None and mot != max_output_tokens:
        raise ProxyError("response does not match the canonical request")
    if response.get("parallel_tool_calls") not in (False, None):
        raise ProxyError("response does not match the canonical request")
    if response.get("previous_response_id") is not None:
        raise ProxyError("response does not match the canonical request")
    if response.get("store") not in (False, None):
        raise ProxyError("response does not match the canonical request")
    if response.get("tool_choice") not in ("none", None):
        raise ProxyError("response does not match the canonical request")
    truncation = response.get("truncation")
    if truncation not in (None, "disabled"):
        raise ProxyError("response does not match the canonical request")
    tools = response.get("tools")
    if tools not in ([], None):
        raise ProxyError("response does not match the canonical request")
    reasoning = response.get("reasoning")
    if (
        not isinstance(reasoning, dict)
        or reasoning.get("effort")
        != canonical["reasoning"]["effort"]
    ):
        raise ProxyError("response does not match the canonical request")
    text = response.get("text")
    if not isinstance(text, dict):
        raise ProxyError("response does not match the canonical request")
    fmt = text.get("format")
    expected_fmt = canonical["text"]["format"]
    if (
        not isinstance(fmt, dict)
        or fmt.get("type") != expected_fmt.get("type")
        or fmt.get("name") != expected_fmt.get("name")
        or fmt.get("strict") != expected_fmt.get("strict")
        or fmt.get("schema") != expected_fmt.get("schema")
    ):
        raise ProxyError("response does not match the canonical request")
    observed_output = response.get("output")
    if status == "in_progress":
        if observed_output != []:
            raise ProxyError("response does not match the canonical request")
    elif observed_output not in ([], output):
        # Completed shells may omit streamed items; accept empty or match.
        raise ProxyError("response does not match the canonical request")
    return _bounded_text(response.get("id"), "response ID", 128)


def _validate_usage(usage, max_output_tokens):
    """Validate Responses accounting against the declared output budget.

    Responses ``output_tokens`` includes both reasoning and visible text, so
    the declared local ceiling applies directly to that aggregate.
    """
    required = {
        "input_tokens",
        "input_tokens_details",
        "output_tokens",
        "output_tokens_details",
        "total_tokens",
    }
    if not isinstance(usage, dict) or not required.issubset(usage):
        raise ProxyError("response usage is invalid")
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        if type(usage.get(name)) is not int or usage[name] < 0:
            raise ProxyError("response usage is invalid")
    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    if not isinstance(input_details, dict) or not isinstance(
        output_details, dict
    ):
        raise ProxyError("response usage is invalid")
    cached = input_details.get("cached_tokens")
    reasoning = output_details.get("reasoning_tokens")
    if type(cached) is not int or cached < 0:
        raise ProxyError("response usage is invalid")
    if cached > usage["input_tokens"]:
        raise ProxyError("response usage is invalid")
    if type(reasoning) is not int or reasoning < 0:
        raise ProxyError("response usage is invalid")
    if reasoning > usage["output_tokens"]:
        raise ProxyError("response usage is invalid")
    if usage["output_tokens"] > max_output_tokens:
        raise ProxyError(
            "response usage is invalid: "
            f"output_tokens={usage['output_tokens']} > {max_output_tokens}"
        )
    # Prefer exact sum; accept totals that still dominate both sides when the
    # provider adds auxiliary accounting fields.
    exact = usage["input_tokens"] + usage["output_tokens"]
    if (
        usage["total_tokens"] != exact
        and usage["total_tokens"]
        < max(usage["input_tokens"], usage["output_tokens"])
    ):
        raise ProxyError(
            "response usage is invalid: "
            f"total_tokens={usage['total_tokens']} "
            f"input={usage['input_tokens']} output={usage['output_tokens']}"
        )
    return usage


def _validate_reasoning_item(item, *, summary, status):
    if (
        not isinstance(item, dict)
        or not {"id", "summary", "type"}.issubset(item)
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
    required = {"content", "id", "role", "status", "type"}
    if (
        not isinstance(item, dict)
        or not required.issubset(item)
        or item.get("content") != []
        or item.get("role") != "assistant"
        or item.get("status") != "in_progress"
        or item.get("type") != "message"
    ):
        raise ProxyError("assistant message start is invalid")
    return _bounded_text(item.get("id"), "message ID", 128)


def _validate_output_part(part, *, text):
    required = {"annotations", "logprobs", "text", "type"}
    if (
        not isinstance(part, dict)
        or not required.issubset(part)
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


_CLIENT_MESSAGE_ID = re.compile(r"^msg_[A-Za-z0-9-]+$")


def _normalize_client_message(message):
    """Strip volatile CLI-assigned fields from a client message item.

    Codex 0.146 tags each wire message with a server-style ``id``
    (``msg_...``). The field carries no prompt semantics, so it is
    removed before the exact preflight comparison; any other drift
    still fails the comparison.
    """
    if not isinstance(message, dict):
        return message
    normalized = dict(message)
    message_id = normalized.get("id")
    if isinstance(message_id, str) and _CLIENT_MESSAGE_ID.match(message_id):
        del normalized["id"]
    return normalized


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
        exchange_timeout_seconds=UPSTREAM_EXCHANGE_TIMEOUT_SECONDS,
    ):
        if (
            not isinstance(prompt, str)
            or not prompt
            or not isinstance(canonical_request, bytes)
            or not callable(output_validator)
            or type(max_output_tokens) is not int
            or max_output_tokens < 1
            or not isinstance(exchange_timeout_seconds, (int, float))
            or isinstance(exchange_timeout_seconds, bool)
            or not 0 < exchange_timeout_seconds <= 900
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
            or "max_output_tokens" in self.canonical
            or "truncation" in self.canonical
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
        # Local budget ceiling only; not present on the wire body.
        self.max_output_tokens = max_output_tokens
        self.exchange_timeout_seconds = float(
            exchange_timeout_seconds
        )
        self.request_count = 0
        self.receipt = None
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._cancelled = threading.Event()
        self._active_connection = None

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
        if _normalize_client_message(messages[-1]) != {
            "content": [
                {"text": self.prompt, "type": "input_text"}
            ],
            "role": "user",
            "type": "message",
        }:
            raise ProxyError("Codex prompt does not match preflight")
        return value

    def _set_active_connection(self, connection):
        with self._state_lock:
            if self._cancelled.is_set():
                raise ProxyError("canonical exchange was cancelled")
            self._active_connection = connection

    def _clear_active_connection(self, connection):
        with self._state_lock:
            if self._active_connection is connection:
                self._active_connection = None

    def cancel(self):
        self._cancelled.set()
        with self._state_lock:
            connection = self._active_connection
        if connection is None:
            return
        candidate = getattr(connection, "sock", None)
        if candidate is not None:
            try:
                candidate.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            connection.close()
        except OSError:
            pass

    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or self._cancelled.is_set():
            raise ProxyError("upstream exchange deadline exceeded")
        return remaining

    def _read_upstream_blocking(self, deadline):
        endpoint = self.upstream_endpoint
        connection_type = (
            http.client.HTTPSConnection
            if endpoint["scheme"] == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(
            endpoint["host"],
            endpoint["port"],
            timeout=self._remaining(deadline),
        )
        self._set_active_connection(connection)
        response = None
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
            if connection.sock is not None:
                connection.sock.settimeout(self._remaining(deadline))
            response = connection.getresponse()
            if response.status == 401:
                raise AuthRefreshRequired(
                    "upstream authorization requires refresh"
                )
            chunks = []
            total = 0
            read = getattr(response, "read1", response.read)
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(
                        self._remaining(deadline)
                    )
                chunk = read(
                    min(65536, SSE_MAX_BYTES + 1 - total)
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > SSE_MAX_BYTES:
                    raise ProxyError("SSE transcript exceeds its bound")
            raw = b"".join(chunks)
            content_type = (
                response.getheader("Content-Type", "") or ""
            ).split(";", 1)[0].strip().lower()
            # Current chatgpt.com Codex stream often omits Content-Type.
            if response.status != 200:
                detail = raw[:300].decode("utf-8", errors="replace")
                raise ProxyError(
                    "loopback upstream response is invalid: "
                    f"status={response.status} body={detail}"
                )
            if content_type in ("", "text/event-stream"):
                return raw
            if content_type == "application/json":
                detail = raw[:300].decode("utf-8", errors="replace")
                raise ProxyError(
                    "loopback upstream response is invalid: "
                    f"body={detail}"
                )
            raise ProxyError(
                "loopback upstream response is invalid: "
                f"content-type={content_type or '<missing>'}"
            )
        except (OSError, http.client.HTTPException) as exc:
            raise ProxyError("loopback upstream is unavailable") from exc
        finally:
            self._clear_active_connection(connection)
            if response is not None:
                response.close()
            connection.close()

    def _read_upstream(self):
        deadline = (
            time.monotonic() + self.exchange_timeout_seconds
        )
        result = {}

        def worker():
            try:
                result["raw"] = self._read_upstream_blocking(
                    deadline
                )
            except BaseException as exc:
                result["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=self.exchange_timeout_seconds)
        if thread.is_alive():
            self.cancel()
            thread.join(timeout=0.2)
            raise ProxyError("upstream exchange deadline exceeded")
        error = result.get("error")
        if error is not None:
            if isinstance(error, ProxyError):
                raise error
            raise ProxyError("loopback upstream is unavailable") from error
        return result["raw"]

    def _validate_response(self, raw):
        """Validate a Responses SSE transcript with version-tolerant scanning.

        Providers add intermediate event types (reasoning summary streams,
        obfuscation fields, extra output items). Extract the completed
        assistant message and terminal shell rather than requiring one fixed
        event order beyond: created/in_progress early, then a completed
        assistant message with text, then response.completed.
        """
        events = _parse_sse(raw)
        if not events:
            raise ProxyError("SSE transcript is empty")

        names = [name for name, _ in events]
        if "response.created" not in names or "response.in_progress" not in names:
            raise ProxyError("SSE event order is invalid")
        if names.index("response.created") > names.index(
            "response.in_progress"
        ):
            raise ProxyError("SSE event order is invalid")
        if "response.completed" not in names:
            raise ProxyError("upstream response did not complete")
        # Terminal event must close the stream (providers may insert
        # trailing keepalives later; require completed is the last
        # meaningful response event).
        if names[-1] != "response.completed":
            raise ProxyError("SSE event order is invalid")

        # Reject tool/side-effect output items: containment is text-only.
        for name, value in events:
            if name not in (
                "response.output_item.added",
                "response.output_item.done",
            ):
                continue
            item = value.get("item") if isinstance(value, dict) else None
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type not in (None, "message", "reasoning"):
                raise ProxyError(
                    f"non-message output item is invalid: {item_type}"
                )
        forbidden_event_prefixes = (
            "response.function_call",
            "response.file_search_call",
            "response.web_search_call",
            "response.mcp_call",
            "response.computer_call",
        )
        for name in names:
            if any(name.startswith(prefix) for prefix in forbidden_event_prefixes):
                raise ProxyError(f"forbidden SSE event: {name}")

        response_id = None
        for name, value in events:
            if name not in ("response.created", "response.in_progress"):
                continue
            _expect_keys(
                value,
                {"response", "sequence_number", "type"},
                name,
            )
            current_id = _response_shell(
                value.get("response"),
                canonical=self.canonical,
                max_output_tokens=self.max_output_tokens,
                status="in_progress",
                output=[],
                usage=None,
            )
            if response_id is not None and current_id != response_id:
                raise ProxyError("response ID changed during streaming")
            response_id = current_id

        completed_messages = []
        for name, value in events:
            if name != "response.output_item.done":
                continue
            item = value.get("item") if isinstance(value, dict) else None
            if (
                isinstance(item, dict)
                and item.get("type") == "message"
                and item.get("status") == "completed"
                and item.get("role") == "assistant"
            ):
                completed_messages.append(item)
        if not completed_messages:
            raise ProxyError("assistant message has no output text")
        if len(completed_messages) != 1:
            raise ProxyError("multiple assistant message outputs are ambiguous")
        message = completed_messages[0]

        output_raw = _validate_message(message)
        if not output_raw:
            raise ProxyError("assistant message has no output text")

        # Prefer streamed text.done when present and consistent.
        text_done_values = [
            value
            for name, value in events
            if name == "response.output_text.done"
        ]
        if text_done_values:
            last_done = text_done_values[-1]
            done_text = last_done.get("text") if isinstance(last_done, dict) else None
            if isinstance(done_text, str) and done_text.encode("utf-8") != output_raw:
                # Prefer the completed message item (authoritative final).
                pass

        completed_event = None
        for name, value in events:
            if name == "response.completed":
                completed_event = value
        _expect_keys(
            completed_event,
            {"response", "sequence_number", "type"},
            "response completion",
        )
        response = completed_event.get("response")
        if not isinstance(response, dict):
            raise ProxyError("completed response is invalid")
        usage = _validate_usage(
            response.get("usage"),
            self.max_output_tokens,
        )
        # Optional reasoning item for shell output matching.
        reasoning_item = None
        for name, value in events:
            if name != "response.output_item.done":
                continue
            item = value.get("item") if isinstance(value, dict) else None
            if isinstance(item, dict) and item.get("type") == "reasoning":
                try:
                    reasoning_item = _validate_reasoning_item(
                        item,
                        summary=item.get("summary") or [],
                        status="completed",
                    )
                except ProxyError:
                    reasoning_item = None
        expected_output = (
            [reasoning_item, message]
            if reasoning_item is not None
            else [message]
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

    def __init__(self, *args, **kwargs):
        self._client_lock = threading.Lock()
        self._active_client = None
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        with self._client_lock:
            self._active_client = request
        return request, address

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            with self._client_lock:
                if self._active_client is request:
                    self._active_client = None

    def cancel_active_request(self):
        with self._client_lock:
            request = self._active_client
        if request is None:
            return
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            request.close()
        except OSError:
            pass


def _read_client_body(
    connection,
    rfile,
    length,
    overall_timeout_seconds,
):
    started = time.monotonic()
    overall_deadline = started + overall_timeout_seconds
    inactivity_deadline = started + CLIENT_BODY_READ_TIMEOUT_SECONDS
    previous_timeout = connection.gettimeout()
    chunks = []
    remaining = length
    read = getattr(rfile, "read1", rfile.read)
    try:
        while remaining:
            now = time.monotonic()
            timeout = min(overall_deadline, inactivity_deadline) - now
            if timeout <= 0:
                raise ProxyError(
                    "canonicalizer request body deadline exceeded"
                )
            connection.settimeout(timeout)
            try:
                chunk = read(min(65536, remaining))
            except (OSError, ValueError) as exc:
                raise ProxyError(
                    "canonicalizer request body deadline exceeded"
                ) from exc
            if not chunk:
                raise ProxyError("canonicalizer request is truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
            inactivity_deadline = (
                time.monotonic() + CLIENT_BODY_READ_TIMEOUT_SECONDS
            )
    finally:
        try:
            connection.settimeout(previous_timeout)
        except OSError:
            pass
    return b"".join(chunks)


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
            raw = _read_client_body(
                self.connection,
                self.rfile,
                length,
                self.server.exchange.exchange_timeout_seconds,
            )
            response_raw = self.server.exchange.exchange(raw)
        except ProxyError as exc:
            payload = _canonical_bytes(
                {"error": {"type": exc.failure_code}}
            )
            self.server.last_error = str(exc)
            self.server.failure_code = exc.failure_code
            try:
                self.send_response(
                    401
                    if isinstance(exc, AuthRefreshRequired)
                    else 400
                )
                self.send_header(
                    "Content-Type",
                    "application/json",
                )
                self.send_header(
                    "Content-Length",
                    str(len(payload)),
                )
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                pass
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
        shutdown_timeout = exchange_arguments.pop(
            "shutdown_timeout_seconds",
            PROXY_SHUTDOWN_TIMEOUT_SECONDS,
        )
        if (
            not isinstance(shutdown_timeout, (int, float))
            or isinstance(shutdown_timeout, bool)
            or not 0 < shutdown_timeout <= 5
        ):
            raise ProxyError("proxy shutdown timeout is invalid")
        self.shutdown_timeout_seconds = float(shutdown_timeout)
        self.exchange = CanonicalExchange(**exchange_arguments)
        self.server = _LoopbackServer(("127.0.0.1", 0), _ProxyHandler)
        self.server.exchange = self.exchange
        self.server.last_error = None
        self.server.failure_code = None
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self._closed = False

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
        if self._closed:
            return
        self._closed = True
        deadline = (
            time.monotonic() + self.shutdown_timeout_seconds
        )
        self.exchange.cancel()
        self.server.cancel_active_request()
        shutdown = threading.Thread(
            target=self.server.shutdown,
            daemon=True,
        )
        shutdown.start()
        shutdown.join(
            timeout=max(0, deadline - time.monotonic())
        )
        self.server.server_close()
        self.thread.join(
            timeout=max(0, deadline - time.monotonic())
        )
