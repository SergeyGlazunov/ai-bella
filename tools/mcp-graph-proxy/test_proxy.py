"""Прокси без живого графа: локальный upstream принимает запрос и стримит ответ."""
from __future__ import annotations

import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from proxy import Config, client_allowed, parse_allow, serve


class UpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        self.server.seen.append(  # type: ignore[attr-defined]
            {
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
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        return


class ProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream.seen = []  # type: ignore[attr-defined]
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        upstream_port = self.upstream.server_address[1]
        self.proxy = serve(
            Config(
                host="127.0.0.1",
                port=0,
                upstream=f"http://127.0.0.1:{upstream_port}",
                upstream_secret="master-secret",
                allow=parse_allow("10.0.28.0/24"),
                timeout=5,
            )
        )
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.port = self.proxy.server_address[1]

    def tearDown(self) -> None:
        self.proxy.shutdown()
        self.upstream.shutdown()

    def _open(self, path: str, body: bytes | None = None, token: str | None = None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "application/json, text/event-stream"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            headers=headers,
            method="POST" if body is not None else "GET",
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        return response

    def test_healthz_without_token(self) -> None:
        response = self._open("/healthz")
        try:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok")
        finally:
            response.close()

    def test_mcp_without_client_token_uses_upstream_secret(self) -> None:
        response = self._open("/mcp", body=b'{"id":1}')
        try:
            self.assertEqual(response.status, 200)
            self.assertIn(b"event: message", response.read())
        finally:
            response.close()
        seen = self.upstream.seen[0]  # type: ignore[attr-defined]
        self.assertEqual(seen["authorization"], "Bearer master-secret")
        self.assertEqual(seen["body"], b'{"id":1}')
        self.assertIn("text/event-stream", seen["accept"])

    def test_client_authorization_is_not_forwarded(self) -> None:
        response = self._open("/mcp", body=b"{}", token="person-token")
        try:
            self.assertEqual(response.status, 200)
        finally:
            response.close()
        seen = self.upstream.seen[0]  # type: ignore[attr-defined]
        self.assertEqual(seen["authorization"], "Bearer master-secret")

    def test_unknown_path_is_not_forwarded(self) -> None:
        response = self._open("/admin")
        try:
            self.assertEqual(response.status, 404)
        finally:
            response.close()
        self.assertEqual(self.upstream.seen, [])  # type: ignore[attr-defined]


class AllowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.allow = parse_allow("10.0.28.0/24")

    def test_office_subnet_and_loopback_pass(self) -> None:
        self.assertTrue(client_allowed("10.0.28.149", self.allow))
        self.assertTrue(client_allowed("10.0.28.5", self.allow))
        self.assertTrue(client_allowed("127.0.0.1", self.allow))
        self.assertTrue(client_allowed("::1", self.allow))
        self.assertTrue(client_allowed("::ffff:10.0.28.10", self.allow))

    def test_other_networks_are_rejected(self) -> None:
        self.assertFalse(client_allowed("10.0.29.5", self.allow))
        self.assertFalse(client_allowed("8.8.8.8", self.allow))
        self.assertFalse(client_allowed("not-an-ip", self.allow))


if __name__ == "__main__":
    unittest.main()
