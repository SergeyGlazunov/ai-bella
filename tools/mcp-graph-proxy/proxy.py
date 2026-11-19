"""Прокси перед MCP графа.

Клиентский токен не проверяется. С наружи пускается только офисная подсеть
и сама машина (loopback). В граф уходит мастер-секрет стенда.

Граф, чат и compose стенда не меняются.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}
FORWARD_REQUEST_HEADERS = (
    "content-type",
    "accept",
    "mcp-protocol-version",
    "mcp-session-id",
    "last-event-id",
)
DEFAULT_ALLOW = "10.0.28.0/24"


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    upstream: str
    upstream_secret: str
    allow: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    timeout: float


def parse_allow(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        raise ValueError("список подсетей пуст")
    return tuple(ipaddress.ip_network(part, strict=False) for part in parts)


def client_allowed(host: str, allow: tuple) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return True
    return any(ip in network for network in allow)


def config_from_env() -> Config:
    secret = os.environ.get("GRAPH_PROXY_UPSTREAM_SECRET", "").strip()
    if not secret:
        raise SystemExit(
            "GRAPH_PROXY_UPSTREAM_SECRET пуст. Прокси не стартует.\n"
            "Возьмите MCP_SHARED_SECRET графа УБД и положите его в .env "
            "этого каталога. В git секрет не кладётся."
        )
    upstream = os.environ.get(
        "GRAPH_PROXY_UPSTREAM", "http://127.0.0.1:8101"
    ).strip().rstrip("/")
    try:
        allow = parse_allow(os.environ.get("GRAPH_PROXY_ALLOW", DEFAULT_ALLOW))
    except ValueError as exc:
        raise SystemExit(f"GRAPH_PROXY_ALLOW: {exc}") from exc
    return Config(
        host=os.environ.get("GRAPH_PROXY_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=int(os.environ.get("GRAPH_PROXY_PORT", "8110")),
        upstream=upstream,
        upstream_secret=secret,
        allow=allow,
        timeout=float(os.environ.get("GRAPH_PROXY_TIMEOUT", "180")),
    )


class ProxyServer(ThreadingHTTPServer):
    def __init__(self, config: Config) -> None:
        self.config = config
        super().__init__((config.host, config.port), ProxyHandler)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ProxyServer

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")

    def _route(self, method: str) -> None:
        client = self.client_address[0]
        if not client_allowed(client, self.server.config.allow):
            sys.stderr.write(f"[graph-proxy] 403 {client} {method} {self.path}\n")
            self._send_json(403, {"error": "forbidden"})
            return
        path = self.path.split("?", 1)[0]
        if method == "GET" and path == "/healthz":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path != "/mcp":
            self._send_json(404, {"error": "not_found"})
            return
        self._forward(method, client)

    def _forward(self, method: str, client: str) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        request = urllib.request.Request(
            self.server.config.upstream + "/mcp",
            data=body if method != "GET" else None,
            method=method,
        )
        request.add_header(
            "Authorization", "Bearer " + self.server.config.upstream_secret
        )
        for name in FORWARD_REQUEST_HEADERS:
            value = self.headers.get(name)
            if value:
                request.add_header(name, value)
        try:
            upstream = urllib.request.urlopen(
                request, timeout=self.server.config.timeout
            )
        except urllib.error.HTTPError as exc:
            upstream = exc
        except Exception as exc:
            sys.stderr.write(
                f"[graph-proxy] 502 {client} {type(exc).__name__}\n"
            )
            self._send_json(502, {"error": "upstream_unavailable"})
            return
        try:
            status = upstream.status
            headers = []
            for key, value in upstream.headers.items():
                if key.lower() in HOP_BY_HOP:
                    continue
                headers.append((key, value))
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = upstream.read(8192)
                if not chunk:
                    break
                self._write_chunk(chunk)
            self._write_chunk(b"")
            sys.stderr.write(f"[graph-proxy] {status} {client} {method} /mcp\n")
        finally:
            upstream.close()

    def _write_chunk(self, chunk: bytes) -> None:
        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
        self.wfile.write(chunk)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


def serve(config: Config) -> ProxyServer:
    return ProxyServer(config)


def main() -> None:
    config = config_from_env()
    server = serve(config)
    nets = ", ".join(str(network) for network in config.allow)
    sys.stderr.write(
        f"[graph-proxy] {config.host}:{server.server_address[1]} -> "
        f"{config.upstream}/mcp, подсети: {nets}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
