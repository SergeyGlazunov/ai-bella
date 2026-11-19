"""Прокси без живого графа: локальный upstream принимает запрос и стримит ответ."""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from proxy import Config, TokenFile, serve


class UpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        self.server.seen.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "accept": self.headers.get("Accept"),
                "body": body,
            }
        )
        payload = b'event: message\ndata: {"ok":true}\n\n'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload[:10])
        self.wfile.flush()
        self.wfile.write(payload[10:])

    def log_message(self, fmt: str, *args) -> None:
        return


class ProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.tokens_path = Path(self.tmp.name) / "tokens.json"
        self.tokens_path.write_text(
            json.dumps([{"name": "ivanov", "token": "personal-token"}]),
            encoding="utf-8",
        )
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream.seen = []  # type: ignore[attr-defined]
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()
        upstream_port = self.upstream.server_address[1]
        self.proxy = serve(
            Config(
                host="127.0.0.1",
                port=0,
                upstream=f"http://127.0.0.1:{upstream_port}",
                upstream_secret="master-secret",
                tokens_path=self.tokens_path,
                timeout=5,
            )
        )
        self.thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.proxy.server_address[1]

    def tearDown(self) -> None:
        self.proxy.shutdown()
        self.upstream.shutdown()
        self.tmp.cleanup()

    def _open(self, path: str, token: str | None = None, body: bytes | None = None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "application/json, text/event-stream"
            headers["MCP-Protocol-Version"] = "2025-06-18"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            headers=headers,
            method="POST" if body is not None else "GET",
        )
        try:
            return urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            return exc

    def test_healthz_without_token(self) -> None:
        response = self._open("/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"ok")

    def test_mcp_rejects_missing_and_unknown_token(self) -> None:
        missing = self._open("/mcp", body=b"{}")
        unknown = self._open("/mcp", token="nope", body=b"{}")
        self.assertEqual(missing.status, 401)
        self.assertEqual(unknown.status, 401)
        self.assertEqual(self.upstream.seen, [])  # type: ignore[attr-defined]

    def test_mcp_replaces_token_and_streams_body(self) -> None:
        response = self._open("/mcp", token="personal-token", body=b'{"id":1}')
        self.assertEqual(response.status, 200)
        self.assertIn(b"event: message", response.read())
        seen = self.upstream.seen[0]  # type: ignore[attr-defined]
        self.assertEqual(seen["authorization"], "Bearer master-secret")
        self.assertEqual(seen["body"], b'{"id":1}')
        self.assertIn("text/event-stream", seen["accept"])

    def test_unknown_path_is_not_forwarded(self) -> None:
        response = self._open("/admin", token="personal-token")
        self.assertEqual(response.status, 404)
        self.assertEqual(self.upstream.seen, [])  # type: ignore[attr-defined]

    def test_new_token_is_picked_up_without_restart(self) -> None:
        denied = self._open("/mcp", token="second-token", body=b"{}")
        self.assertEqual(denied.status, 401)
        self.tokens_path.write_text(
            json.dumps(
                [
                    {"name": "ivanov", "token": "personal-token"},
                    {"name": "petrov", "token": "second-token"},
                ]
            ),
            encoding="utf-8",
        )
        allowed = self._open("/mcp", token="second-token", body=b"{}")
        self.assertEqual(allowed.status, 200)

    def test_broken_token_file_fails_closed(self) -> None:
        self.tokens_path.write_text("{", encoding="utf-8")
        response = self._open("/mcp", token="personal-token", body=b"{}")
        self.assertEqual(response.status, 503)
        self.assertEqual(self.upstream.seen, [])  # type: ignore[attr-defined]


class TokenFileTests(unittest.TestCase):
    def test_rejects_duplicate_token(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokens.json"
            path.write_text(
                json.dumps(
                    [
                        {"name": "a", "token": "same"},
                        {"name": "b", "token": "same"},
                    ]
                ),
                encoding="utf-8",
            )
            tokens = TokenFile(path)
            self.assertIsNotNone(tokens.error)
            self.assertIsNone(tokens.lookup("same"))


if __name__ == "__main__":
    unittest.main()
