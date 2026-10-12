---
name: 1c-confluence
description: Uses read-only MCP user-confluence for live corporate wiki pages. Use when the user asks for регламент, постановка, страница Confluence, «как у нас принято». Not for 1C code, MDD owners, or platform syntax.
---

# Confluence — живая wiki

Namespace: `user-confluence`. REST в Confluence Data Center. Запись выключена: create/update/delete нет и не предлагай.

Поиск — ключевые слова и CQL, не «семантика». Расплывчатая формулировка — упрости запрос или спроси название/пространство.

## Какие tools звать

1. `confluence_search` — текст или CQL, `limit` 5–10, не 50.
2. Из выдачи возьми id/title/url. Несколько страниц — список, не выбирай молча, если вопрос неоднозначный.
3. Нужен текст — `confluence_get_page` по id одной–двух страниц. Не выкачивай дерево целиком.

По необходимости: `confluence_get_page_children`, `confluence_get_space_page_tree` (узко), `confluence_get_labels`.

Не вызывать без прямой просьбы: `confluence_download_*`, `confluence_get_page_images`, история, diff, комментарии, restrictions.

## Правила

- Имена метаданных 1С здесь не ищи.
- Закрытые пространства, которых нет у PAT на ВМ, не откроются — так и скажи.
- Не цитируй многостраничные простыни: краткое резюме + ссылка + ключевые абзацы.
- CQL примеры: `type=page AND text ~ "переплата"`, `title~"Проформа"`. Личный ключ пространства с `~` в CQL — в кавычках.

## Ответ

**Источник:** Confluence

Таблица: заголовок, пространство (если видно), ссылка, 2–4 предложения сути.
