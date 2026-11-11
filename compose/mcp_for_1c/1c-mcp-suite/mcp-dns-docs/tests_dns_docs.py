"""Юниты mcp-dns-docs: parse, HTML→text, glossary filter, clamp."""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from html_text import html_to_text  # noqa: E402
from client import (  # noqa: E402
    DocsClientError,
    DnsDocsClient,
    article_url,
    parse_article_id,
)


class ParseArticleIdTests(unittest.TestCase):
    def test_from_id(self):
        self.assertEqual(parse_article_id(article_id=3941), 3941)

    def test_from_url(self):
        self.assertEqual(
            parse_article_id(url="https://docs.dns-shop.ru/ViewArticle/3941"),
            3941,
        )

    def test_from_path_only(self):
        self.assertEqual(parse_article_id(url="/ViewArticle/12"), 12)

    def test_conflict(self):
        with self.assertRaises(DocsClientError) as ctx:
            parse_article_id(
                article_id=1,
                url="https://docs.dns-shop.ru/ViewArticle/2",
            )
        self.assertEqual(ctx.exception.code, "bad_request")

    def test_missing(self):
        with self.assertRaises(DocsClientError) as ctx:
            parse_article_id()
        self.assertEqual(ctx.exception.code, "bad_request")

    def test_matching_id_and_url(self):
        self.assertEqual(
            parse_article_id(
                article_id=3941,
                url="https://docs.dns-shop.ru/ViewArticle/3941",
            ),
            3941,
        )


class HtmlTextTests(unittest.TestCase):
    def test_strips_tags_and_keeps_breaks(self):
        raw = "<h4>Заголовок</h4><p>Первый &amp; второй</p><br/>хвост"
        text = html_to_text(raw)
        self.assertIn("Заголовок", text)
        self.assertIn("Первый & второй", text)
        self.assertNotIn("<", text)

    def test_empty(self):
        self.assertEqual(html_to_text(""), "")
        self.assertEqual(html_to_text(None), "")


class GlossaryLookupTests(unittest.TestCase):
    def setUp(self):
        self.client = DnsDocsClient(base_url="https://docs.dns-shop.ru")
        self.client._glossary = [
            {"id": 60, "name": "API", "description": "Application programming interface"},
            {"id": 1, "name": "API Gateway", "description": "шлюз"},
            {"id": 2, "name": "РМД", "description": "Рабочее место доставки"},
            {"id": 3, "name": "3P", "description": "Third-Party селлер"},
        ]
        self.client._glossary_loaded_at = 10**12  # не истечёт в тесте

    def test_exact_first(self):
        hits = self.client.glossary_lookup("API", limit=5)
        self.assertEqual(hits[0]["name"], "API")
        self.assertTrue(any(h["name"] == "API Gateway" for h in hits))

    def test_substring_name(self):
        hits = self.client.glossary_lookup("Gateway", limit=5)
        self.assertEqual(hits[0]["name"], "API Gateway")

    def test_description_fallback(self):
        hits = self.client.glossary_lookup("доставки", limit=5)
        self.assertEqual(hits[0]["name"], "РМД")

    def test_limit(self):
        hits = self.client.glossary_lookup("a", limit=1)
        self.assertEqual(len(hits), 1)


class ArticleUrlTests(unittest.TestCase):
    def test_url(self):
        self.assertEqual(
            article_url("https://docs.dns-shop.ru/", 3941),
            "https://docs.dns-shop.ru/ViewArticle/3941",
        )


if __name__ == "__main__":
    unittest.main()
