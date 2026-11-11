# Design: MCP портала документации DNS (`docs.dns-shop.ru`)

**Date:** 2026-11-11  
**Status:** approved — implementing in suite (`mcp-dns-docs`, port 8015)  
**Product name:** `mcp-dns-docs`  
**Related feedback:** чек-лист проекта, пункт про подключение docs (не Confluence, не синтакс-помощник 1С)

## 1. Problem

Аналитики просят Беллу читать внутренние инструкции с [портала документации](https://docs.dns-shop.ru/). Сейчас в контуре есть Confluence и `1c-docs` (справка платформы), но не `docs.dns-shop.ru`.

Официальной публичной документации API нет. Фронт портала ходит в рабочий HTTP API гостем (для открытых статей). Приватные статьи в v1 игнорируем; auth добавим позже при необходимости.

## 2. Goals / non-goals

**Goals (v1):**

1. Найти статью по тексту.
2. Открыть статью по id / URL и отдать читаемое содержимое модели.
3. Искать термин в глоссарии портала (расшифровка сокращений до похода в статьи/граф).

**Non-goals (v1):**

- Авторизация / приватные статьи
- Запись, комментарии, review, actuality
- Полное дерево каталога (`/api/tree`) на каждый запрос
- History / compare / BPMN / namelink / tags-filter (можно v2)
- Смешение с MCP `1c-docs` (синтакс-помощник) или Confluence

## 3. Approach

Отдельный FastMCP-сервер в общем слое чата (рядом с MDD / SDMS / Confluence по роли: не привязан к УБД/КБ). Транспорт Streamable HTTP `/mcp` + `/healthz`, как в `1c-mcp-suite`.

Источник: live HTTP к `docs.dns-shop.ru` (base URL из env). Read-only. HTML тела статьи очищается в plain text / упрощённый markdown на стороне MCP.

## 4. Discovered upstream API (guest)

| Need | Method | Path / body |
|------|--------|-------------|
| Search | `POST` | `/fulltextsearch` body `{ "text", "searchType": 1, "searchFilterType": 15, "tags": [] }` |
| Get article | `GET` | `/api/docs/{id}` → `article.title`, `article.previewText`, `article.text` (HTML), meta |
| Glossary | `GET` | `/api/glossary` → `{ code, data: [{ id, name, description }, ...] }` (~750 terms) |

Canonical UI URL: `https://docs.dns-shop.ru/ViewArticle/{id}`.

Verified: article `3941`, search for «Работа по API», glossary list — guest `200` without cookies.

## 5. Tool contract (v1)

Prefix: `dns_docs_*` — не пересекается с `docsearch` / `docinfo` (платформа) и `confluence_*`.

### 5.1 `dns_docs_search`

**In:**

| Param | Type | Notes |
|-------|------|--------|
| `query` | string | обязателен |
| `limit` | int | default `10`, max `25` |

**Out:**

```json
{
  "answerable": true,
  "source": "dns-docs",
  "results": [
    {
      "id": 3941,
      "title": "...",
      "preview": "...",
      "path": "Логистика / ...",
      "url": "https://docs.dns-shop.ru/ViewArticle/3941",
      "score": 287760.75
    }
  ]
}
```

Upstream fields: `elementId`, `title`, `previewText`, `treeNodePath`, `score`. Only `elementType == "Article"` (or equivalent) in v1.

### 5.2 `dns_docs_get`

**In:** ровно один из:

| Param | Type | Notes |
|-------|------|--------|
| `article_id` | int | id статьи |
| `url` | string | `.../ViewArticle/{id}` — id парсится |

**Out:**

```json
{
  "answerable": true,
  "source": "dns-docs",
  "id": 3941,
  "title": "...",
  "url": "https://docs.dns-shop.ru/ViewArticle/3941",
  "updated": "18.03.2025 в 09:25:35",
  "editor": "...",
  "preview": "...",
  "text": "plain text...",
  "is_private": false,
  "access_level_name": "..."
}
```

HTML → text: strip tags, decode entities, collapse whitespace; keep heading breaks where cheap. Do not return raw HTML to the model.

If both `article_id` and `url` are omitted, or both set to conflicting ids: `answerable: false` with a short validation `message`.

If article missing / empty body / clearly inaccessible / `isPrivate: true` while still returned: treat private as unreadable in v1 (`answerable: false`, message that private articles are out of scope). No invented content.

### 5.3 `dns_docs_glossary_lookup`

**In:**

| Param | Type | Notes |
|-------|------|--------|
| `query` | string | термин или подстрока |
| `limit` | int | default `10`, max `30` |

**Out:**

```json
{
  "answerable": true,
  "source": "dns-docs-glossary",
  "results": [
    { "id": 60, "name": "API", "description": "..." }
  ]
}
```

Implementation: `GET /api/glossary` once, cache in process memory with TTL (e.g. 1 hour). Match case-insensitive on `name` (prefix/substring) and optionally weak match on `description`. Do not dump all ~750 terms to the model in one call.

## 6. Skills

Add recipes (mirror Confluence style):

- `integrations/skills/webui/dns-docs.md`
- `integrations/skills/cursor/dns-docs.md`

Skill rules:

1. Instructions / регламенты «как у нас в docs» → этот MCP, не Confluence и не `1c-docs`.
2. Typical path: ambiguous term → `dns_docs_glossary_lookup`; find page → `dns_docs_search`; read → `dns_docs_get` (1–2 articles).
3. Always cite `url` in the answer.
4. Private / unreadable → say so; do not invent.
5. Do not call write-like upstream; MCP exposes only the three tools above.

Update `compose/mcp_for_1c/workspace/AGENTS.md` install-set table with a row for DNS Docs when the server is wired into the stand.

## 7. Runtime / packaging

| Item | Choice |
|------|--------|
| Package path | `compose/mcp_for_1c/1c-mcp-suite/mcp-dns-docs/` — рядом с другими FastMCP, переиспользует `mcp_http` / auth helpers |
| Entry | FastMCP server; `start` via existing suite patterns (`start.py` / Dockerfile.python) |
| Env | `DNS_DOCS_BASE_URL` (default `https://docs.dns-shop.ru`); `DNS_DOCS_TIMEOUT_SEC` (default `20`); MCP shared secret — как у остальных suite-серверов |
| Network | Container must reach `docs.dns-shop.ru` from the stand |
| Port | Зафиксировать при wire-up в compose общего слоя и в STATUS; в коде сервера порт только из env |
| Compose | Common chat layer (`ai-stack` / будущий `contour`), не overlay УБД/КБ |

Auth to MCP clients (WebUI bearer / shared secret): same as other suite servers. Auth to **docs portal**: none in v1.

## 8. Testing

- Unit: URL/`ViewArticle` parse; HTML strip; glossary filter; limit clamps.
- Integration (optional / network): live search «Работа по API» → id `3941`; get `3941` → non-empty `text`; glossary `API` → hit.
- Smoke: `/healthz` 200; tools/list contains exactly the three tools.

## 9. v2 candidates (explicitly out of v1)

- `tags` + search filter
- `category-page` browse
- article comments
- history / compare
- namelink resolve
- external / BPMN links
- portal SSO for private articles

## 10. Risks

| Risk | Mitigation |
|------|------------|
| Unofficial API shape changes | Thin client; pin expected fields; clear `answerable: false` on parse failure |
| Large glossary payload | Cache + filtered lookup tool only |
| Confusion with `1c-docs` | Distinct tool names + skills + AGENTS row |
| Stand cannot resolve internal DNS | Document network requirement; health check that hits base URL or a cheap endpoint |

## 11. Success criteria

1. From Bella chat: search finds a known open article; get returns readable text + link.
2. Glossary lookup returns a known term (e.g. API) without dumping the full glossary.
3. Skills steer «инструкция с docs» away from Confluence / platform help.
4. No credentials required for open articles.
