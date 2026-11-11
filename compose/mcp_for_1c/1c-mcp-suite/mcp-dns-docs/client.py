"""HTTP-клиент к неофициальному API портала docs.dns-shop.ru (guest, read-only)."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "https://docs.dns-shop.ru"
DEFAULT_TIMEOUT_SEC = 20
GLOSSARY_TTL_SEC = 3600

_VIEW_ARTICLE = re.compile(
    r"(?:https?://[^/]+)?/ViewArticle/(\d+)\b",
    re.IGNORECASE,
)


class DocsClientError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_article_id(*, article_id: int | None = None, url: str | None = None) -> int:
    has_id = article_id is not None
    has_url = bool(url and str(url).strip())
    if has_id and has_url:
        m = _VIEW_ARTICLE.search(str(url))
        if not m:
            raise DocsClientError("bad_request", "url не содержит /ViewArticle/{id}")
        url_id = int(m.group(1))
        if int(article_id) != url_id:
            raise DocsClientError(
                "bad_request",
                f"article_id={article_id} и url id={url_id} не совпадают",
            )
        return url_id
    if has_id:
        return int(article_id)
    if has_url:
        m = _VIEW_ARTICLE.search(str(url))
        if not m:
            raise DocsClientError("bad_request", "url не содержит /ViewArticle/{id}")
        return int(m.group(1))
    raise DocsClientError(
        "bad_request",
        "Нужен article_id или url вида .../ViewArticle/{id}",
    )


def article_url(base_url: str, article_id: int) -> str:
    return f"{base_url.rstrip('/')}/ViewArticle/{article_id}"


class DnsDocsClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout_sec: int | None = None,
    ):
        self.base_url = (base_url or os.environ.get(
            "DNS_DOCS_BASE_URL", DEFAULT_BASE_URL
        )).rstrip("/")
        self.timeout_sec = int(
            timeout_sec
            if timeout_sec is not None
            else os.environ.get("DNS_DOCS_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)
        )
        self._glossary_lock = threading.Lock()
        self._glossary_loaded_at = 0.0
        self._glossary: list[dict[str, Any]] = []

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        data = None
        headers = {
            "Accept": "application/json;charset=utf-8",
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json;charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise DocsClientError(
                "http_error",
                f"HTTP {e.code} на {method} {path}",
            ) from e
        except urllib.error.URLError as e:
            raise DocsClientError(
                "unreachable",
                f"docs.dns-shop.ru недоступен: {e.reason}",
            ) from e
        except json.JSONDecodeError as e:
            raise DocsClientError(
                "bad_response",
                f"Ответ не JSON: {e}",
            ) from e

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        payload = {
            "text": query,
            "searchType": 1,
            "searchFilterType": 15,
            "tags": [],
        }
        data = self._request("POST", "/fulltextsearch", body=payload)
        if not isinstance(data, dict):
            raise DocsClientError("bad_response", "Неожиданный ответ fulltextsearch")
        if data.get("code") not in (None, "Ok"):
            raise DocsClientError(
                "search_failed",
                data.get("message") or f"code={data.get('code')}",
            )
        items = data.get("data") or []
        if not isinstance(items, list):
            raise DocsClientError("bad_response", "data поиска не список")

        results: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("elementType") and item.get("elementType") != "Article":
                continue
            aid = item.get("elementId")
            if aid is None:
                continue
            aid_int = int(aid)
            results.append({
                "id": aid_int,
                "title": item.get("title") or "",
                "preview": item.get("previewText") or "",
                "path": item.get("treeNodePath") or "",
                "url": article_url(self.base_url, aid_int),
                "score": item.get("score"),
            })
            if len(results) >= limit:
                break
        return results

    def get_article(self, article_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/api/docs/{article_id}")
        if not isinstance(data, dict) or "article" not in data:
            raise DocsClientError(
                "not_found",
                f"Статья {article_id} не найдена или ответ без article",
            )
        article = data["article"]
        if not isinstance(article, dict):
            raise DocsClientError("bad_response", "article не объект")
        return article

    def glossary_all(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._glossary_lock:
            if self._glossary and (now - self._glossary_loaded_at) < GLOSSARY_TTL_SEC:
                return list(self._glossary)
        data = self._request("GET", "/api/glossary")
        if not isinstance(data, dict):
            raise DocsClientError("bad_response", "Неожиданный ответ glossary")
        if data.get("code") not in (None, "Ok"):
            raise DocsClientError(
                "glossary_failed",
                data.get("message") or f"code={data.get('code')}",
            )
        items = data.get("data") or []
        if not isinstance(items, list):
            raise DocsClientError("bad_response", "data glossary не список")
        cleaned: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()
            if not name:
                continue
            cleaned.append({
                "id": item.get("id"),
                "name": name,
                "description": item.get("description") or "",
            })
        with self._glossary_lock:
            self._glossary = cleaned
            self._glossary_loaded_at = time.time()
            return list(cleaned)

    def glossary_lookup(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        q = (query or "").strip().lower()
        if not q:
            return []
        terms = self.glossary_all()
        exact: list[dict[str, Any]] = []
        prefix: list[dict[str, Any]] = []
        substr: list[dict[str, Any]] = []
        desc: list[dict[str, Any]] = []
        for term in terms:
            name_l = term["name"].lower()
            if name_l == q:
                exact.append(term)
            elif name_l.startswith(q):
                prefix.append(term)
            elif q in name_l:
                substr.append(term)
            elif q in (term.get("description") or "").lower():
                desc.append(term)
        ranked = exact + prefix + substr + desc
        return ranked[:limit]
