"""Токен-прокси перед MCP графа.

Клиент приходит со своим Bearer. Прокси подставляет мастер-секрет графа
и отдаёт ответ потоком. Сам граф, чат и compose стенда не меняются.

Секрет графа и файл токенов живут только на машине стенда, не в git.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    upstream: str
    upstream_secret: str
    tokens_path: Path
    timeout: float


class TokenFile:
    """Список личных токенов. Перечитывается, когда файл меняется."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime_ns: int | None = None
        self._by_digest: dict[bytes, str] = {}
        self.error: str | None = None
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError as exc:
            self._mtime_ns = None
            self._by_digest = {}
            self.error = f"файл токенов недоступен: {exc}"
            return
        if not force and mtime_ns == self._mtime_ns:
            return
        self._mtime_ns = mtime_ns
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._by_digest = _parse_tokens(raw)
            self.error = None
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self._by_digest = {}
            self.error = f"файл токенов повреждён: {exc}"

    def lookup(self, presented: str) -> str | None:
        self.reload()
        if self.error or not presented:
            return None
        digest = hashlib.sha256(presented.encode("utf-8")).digest()
        for known, name in self._by_digest.items():
            if hmac.compare_digest(digest, known):
                return name
        return None

    @property
    def count(self) -> int:
        self.reload()
        return len(self._by_digest)


def _parse_tokens(raw: object) -> dict[bytes, str]:
    if not isinstance(raw, list):
        raise ValueError("корень файла должен быть массивом")
    found: dict[bytes, str] = {}
    seen_names: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("элемент токена должен быть объектом")
        name = str(item.get("name", "")).strip()
        token = str(item.get("token", "")).strip()
        if not name or not token:
            raise ValueError("у токена нужны непустые name и token")
        if name in seen_names:
            raise ValueError(f"имя повторяется: {name}")
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        if digest in found:
            raise ValueError("два человека с одним и тем же токеном")
        seen_names.add(name)
        found[digest] = name
    return found


def bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    prefix = "bearer "
    if not header.lower().startswith(prefix):
        return None
    token = header[len(prefix):].strip()
    return token or None


def config_from_env() -> Config:
    secret = os.environ.get("GRAPH_PROXY_UPSTREAM_SECRET", "").strip()
    if not secret:
        raise SystemExit(
            "GRAPH_PROXY_UPSTREAM_SECRET пуст. Прокси не стартует.\n"
            "Возьмите MCP_SHARED_SECRET графа УБД и положите его в .env "
            "этого каталога. В git секрет не кладётся."
        )
    tokens_path = Path(
        os.environ.get("GRAPH_PROXY_TOKENS", "tokens.json")
    )
    if not tokens_path.is_file():
        raise SystemExit(
            f"Файл токенов не найден: {tokens_path}\n"
            "Скопируйте tokens.example.json в tokens.json и впишите людей."
        )
    upstream = os.environ.get(
        "GRAPH_PROXY_UPSTREAM", "http://127.0.0.1:8101"
    ).strip().rstrip("/")
    return Config(
        host=os.environ.get("GRAPH_PROXY_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=int(os.environ.get("GRAPH_PROXY_PORT", "8110")),
        upstream=upstream,
        upstream_secret=secret,
        tokens_path=tokens_path,
        timeout=float(os.environ.get("GRAPH_PROXY_TIMEOUT", "180")),
    )


class ProxyServer(ThreadingHTTPServer):
    def __init__(self, config: Config, tokens: TokenFile) -> None:
        self.config = config
        self.tokens = tokens
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
        principal = self.server.tokens.lookup(
            bearer_token(self.headers.get("Authorization")) or ""
        )
        if principal is None:
            if self.server.tokens.error:
                sys.stderr.write(
                    f"[graph-proxy] 503 tokens {self.server.tokens.error}\n"
                )
                self._send_json(503, {"error": "tokens_unavailable"})
                return
            sys.stderr.write(f"[graph-proxy] 401 {method} /mcp\n")
            self._send_json(401, {"error": "unauthorized"})
            return
        self._forward(method, principal)

    def _forward(self, method: str, principal: str) -> None:
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
        except Exception as exc:  # сеть, таймаут, обрыв
            sys.stderr.write(
                f"[graph-proxy] 502 {principal} {type(exc).__name__}\n"
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
            sys.stderr.write(
                f"[graph-proxy] {status} {principal} {method} /mcp\n"
            )
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
        self.send_header("WWW-Authenticate", 'Bearer realm="mcp-graph-proxy"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


def serve(config: Config) -> ProxyServer:
    tokens = TokenFile(config.tokens_path)
    if tokens.error:
        raise SystemExit(tokens.error)
    return ProxyServer(config, tokens)


def main() -> None:
    config = config_from_env()
    server = serve(config)
    sys.stderr.write(
        f"[graph-proxy] {config.host}:{server.server_address[1]} -> "
        f"{config.upstream}/mcp, токенов: {server.tokens.count}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
