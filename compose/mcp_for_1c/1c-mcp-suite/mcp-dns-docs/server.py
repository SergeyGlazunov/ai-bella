"""
MCP-сервер: портал документации DNS (docs.dns-shop.ru)
======================================================
Read-only guest доступ к открытым статьям и глоссарию.

Инструменты:
  - dns_docs_search            — найти статью
  - dns_docs_get               — открыть и прочитать статью
  - dns_docs_glossary_lookup   — термин из глоссария портала

Не путать с mcp-platform-help / 1c-docs (синтакс-помощник) и Confluence.
Приватные статьи в v1 не поддерживаются (без SSO).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

try:
    from refusal import install_answerable_field, mark, refusal, MEANING_NOT_ASKED
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from refusal import install_answerable_field, mark, refusal, MEANING_NOT_ASKED

try:
    from html_text import html_to_text
    from client import (
        DnsDocsClient,
        DocsClientError,
        article_url,
        parse_article_id,
    )
except ImportError:  # pragma: no cover — в образе модули лежат плоско в /app
    from mcp_dns_docs_html_text import html_to_text  # type: ignore
    from mcp_dns_docs_client import (  # type: ignore
        DnsDocsClient,
        DocsClientError,
        article_url,
        parse_article_id,
    )

mcp = FastMCP("DNS Docs Portal")
install_answerable_field(mcp)

logger = logging.getLogger(__name__)

_client = DnsDocsClient()


def _clamp(value: Optional[int], default: int, lo: int, hi: int) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _from_client_error(exc: DocsClientError) -> dict:
    meaning = MEANING_NOT_ASKED if exc.code == "bad_request" else None
    kwargs = {
        "error": exc.code,
        "message": exc.message,
        "degraded": exc.code != "bad_request",
        "source": "dns-docs",
    }
    if meaning:
        kwargs["meaning"] = meaning
    return refusal(**kwargs)


@mcp.tool()
def dns_docs_search(query: str, limit: int = 10) -> dict:
    """
    Найти статьи на портале документации DNS (docs.dns-shop.ru).

    Полнотекстовый поиск по открытым инструкциям и регламентам.
    Не Confluence и не справка платформы 1С.
    """
    q = (query or "").strip()
    if not q:
        return refusal(
            "bad_request",
            "query пустой",
            meaning=MEANING_NOT_ASKED,
            degraded=False,
            source="dns-docs",
        )
    lim = _clamp(limit, 10, 1, 25)
    try:
        results = _client.search(q, limit=lim)
    except DocsClientError as e:
        return _from_client_error(e)
    return mark({
        "source": "dns-docs",
        "results": results,
    })


@mcp.tool()
def dns_docs_get(
    article_id: Optional[int] = None,
    url: Optional[str] = None,
) -> dict:
    """
    Открыть статью портала docs.dns-shop.ru и вернуть текст без HTML.

    Передай article_id или url вида https://docs.dns-shop.ru/ViewArticle/{id}.
    Приватные статьи в v1 недоступны.
    """
    try:
        aid = parse_article_id(article_id=article_id, url=url)
    except DocsClientError as e:
        return _from_client_error(e)

    try:
        article = _client.get_article(aid)
    except DocsClientError as e:
        return _from_client_error(e)

    if article.get("isPrivate") is True:
        return refusal(
            "private_article",
            f"Статья {aid} приватная — в v1 без SSO не читаем",
            degraded=False,
            source="dns-docs",
            id=aid,
            url=article_url(_client.base_url, aid),
        )

    text = html_to_text(article.get("text"))
    if not text and not (article.get("previewText") or "").strip():
        return refusal(
            "empty_article",
            f"Статья {aid} без текста",
            degraded=False,
            source="dns-docs",
            id=aid,
            url=article_url(_client.base_url, aid),
        )

    return mark({
        "source": "dns-docs",
        "id": int(article.get("id") or aid),
        "title": article.get("title") or "",
        "url": article_url(_client.base_url, aid),
        "updated": article.get("dateUpdated") or "",
        "editor": article.get("editorName") or "",
        "preview": article.get("previewText") or "",
        "text": text,
        "is_private": bool(article.get("isPrivate")),
        "access_level_name": article.get("accessLevelName") or "",
    })


@mcp.tool()
def dns_docs_glossary_lookup(query: str, limit: int = 10) -> dict:
    """
    Найти термин в глоссарии портала документации DNS.

    Сначала расшифровка сокращений (РМД, API, 3P…), потом при необходимости
    dns_docs_search / dns_docs_get. Не выгружает весь глоссарий целиком.
    """
    q = (query or "").strip()
    if not q:
        return refusal(
            "bad_request",
            "query пустой",
            meaning=MEANING_NOT_ASKED,
            degraded=False,
            source="dns-docs-glossary",
        )
    lim = _clamp(limit, 10, 1, 30)
    try:
        results = _client.glossary_lookup(q, limit=lim)
    except DocsClientError as e:
        err = _from_client_error(e)
        err["source"] = "dns-docs-glossary"
        return err
    return mark({
        "source": "dns-docs-glossary",
        "results": results,
    })
