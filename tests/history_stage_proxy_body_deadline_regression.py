#!/usr/bin/env python3
import contextlib
import pathlib
import socket
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_stage_proxy


class _RecordingExchange:
    def __init__(self, overall_timeout_seconds):
        self.exchange_timeout_seconds = overall_timeout_seconds
        self.requests = []

    def exchange(self, raw):
        self.requests.append(raw)
        return b"ok"


class HistoryStageProxyBodyDeadlineRegression(unittest.TestCase):
    @contextlib.contextmanager
    def proxy(self, overall_timeout_seconds=3):
        exchange = _RecordingExchange(overall_timeout_seconds)
        server = history_stage_proxy._LoopbackServer(
            ("127.0.0.1", 0),
            history_stage_proxy._ProxyHandler,
        )
        server.exchange = exchange
        server.last_error = None
        server.failure_code = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1], exchange, server
        finally:
            server.cancel_active_request()
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_progress_resets_inactivity_deadline(self):
        body_chunks = [b"{", b'"a"', b":1", b"}"]
        body = b"".join(body_chunks)
        with self.proxy() as (port, exchange, _server):
            with socket.create_connection(
                ("127.0.0.1", port), timeout=2
            ) as client:
                client.sendall(self._headers(len(body)))
                started = time.monotonic()
                for index, chunk in enumerate(body_chunks):
                    if index:
                        time.sleep(0.4)
                    client.sendall(chunk)
                response = self._receive_all(client)
                elapsed = time.monotonic() - started

        self.assertGreater(elapsed, 1.0)
        self.assertIn(b"HTTP/1.1 200 OK", response)
        self.assertEqual(exchange.requests, [body])

    def test_stalled_body_hits_inactivity_deadline(self):
        with mock.patch.object(
            history_stage_proxy,
            "CLIENT_BODY_READ_TIMEOUT_SECONDS",
            0.1,
        ):
            with self.proxy() as (port, exchange, server):
                with socket.create_connection(
                    ("127.0.0.1", port), timeout=2
                ) as client:
                    client.sendall(self._headers(2) + b"{")
                    started = time.monotonic()
                    response = self._receive_all(client)
                    elapsed = time.monotonic() - started

                self.assertLess(elapsed, 0.8)
                self.assertIn(b"HTTP/1.1 400 Bad Request", response)
                self.assertIn(
                    b'"type":"canonicalizer_rejected"', response
                )
                self.assertEqual(exchange.requests, [])
                self.assertEqual(
                    server.last_error,
                    "canonicalizer request body deadline exceeded",
                )

    @staticmethod
    def _headers(length):
        return (
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {length}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
        )

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
