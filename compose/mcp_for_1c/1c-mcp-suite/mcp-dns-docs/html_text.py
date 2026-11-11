"""HTML тела статьи docs.dns-shop.ru → читаемый plain text для модели."""
from __future__ import annotations

import html
import re

_BLOCK_BREAK = re.compile(
    r"</(p|div|h[1-6]|li|tr|section|article|br|table|ul|ol)\s*>",
    re.IGNORECASE,
)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\f\v]+")
_MULTI_NL = re.compile(r"\n{3,}")


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = _BR.sub("\n", raw)
    text = _BLOCK_BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return _MULTI_NL.sub("\n\n", text).strip()
