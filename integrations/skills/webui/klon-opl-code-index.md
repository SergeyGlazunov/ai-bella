---
name: Code Index klon_opl
description: Рецепт MCP klon_opl_code_index — тела процедур BSL и текст запросов конфигурации клона оплат (klon_opl). Не путать с ubd_code_index и KB-code-index. Графа для этой базы нет.
---

# Code-index — klon_opl (оплаты)

Карточка в WebUI: `klon_opl_code_index`. Алиас репо: **`klon_opl`** (сначала `get_stats`).

## Какие tools

| Нужно | Tool |
|---|---|
| Алиас репо | `get_stats` |
| Тело по точному имени | `get_function` (`name` + `repo=klon_opl`) |
| Поиск по словам | `search_function` / `grep_body` — узкий запрос |
| Кусок файла | `read_file` |

Не начинай с grep «по всей базе». Не вызывай tools УБД/КБ для этой конфигурации.

## Ответ

Кратко логика + короткий фрагмент BSL. Длинный `ТекстЗапроса` не вставляй целиком.
