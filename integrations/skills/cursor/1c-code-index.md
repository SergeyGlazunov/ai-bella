---
name: 1c-code-index
description: Uses MCP user-ubd-code-index for BSL procedure bodies, query text, and module fragments of UBD. Use after user-ubd-graph already gave the procedure name and lines, or when the user asks for формула, ТекстЗапроса, тело процедуры. Do not start with grep over the whole base. Not for MDD owners or platform syntax. Not vk_ubd.
---

# Code-index — тела BSL УБД

Namespace: `user-ubd-code-index`. Это не замена графа. Граф нашёл имя и место — сюда за текстом.

Единственный индекс: выгрузка УБД на ВМ. Локального ВК и `vk_ubd` нет.

У большинства tools обязателен `repo`. Не выдумывай путь. Сначала `get_stats` (один раз за чат) и подставь алиас из ответа. Обычно `ubd`.

## Какие tools звать

| Нужно | Tool |
|---|---|
| Алиас репо | `get_stats` |
| Тело по точному имени | `get_function` (`name` + `repo`, при неоднозначности `path_glob` из графа) |
| Поиск по словам, если имя неизвестно | `search_function` / `grep_body` — узкий запрос |
| Кусок файла по строкам графа | `read_file` |

Не вызывать без прямой просьбы: `bsl_sql`, `list_files`, `get_call_tree` на большую глубину, `find_path` вместо графа, `health`.

Не начинай с `grep_body` «переплата», если ещё нет имени процедуры.

## Порядок

1. Имя процедуры и путь уже есть из `user-ubd-graph`.
2. Один `get_function` (или два: расчёт + сборка запроса). Не по всей цепочке callees.
3. Не вызывай `get_function` повторно для той же процедуры.
4. Не крути `code_method_signature` графа, если тело уже есть.

Если имя неуникально — локации без тел. Уточни `path_glob` из графа, повтори один раз.

## Длинные запросы

Функция длиннее ~80 строк (типично `ТекстЗапроса…Часть1`) — не вставляй модуль целиком.

Выпиши: откуда берётся сумма / остаток; какие таблицы и поля; как получается результат.

Полный текст: «открой файл в EDT по строкам из графа».

## Ответ

Кратко логика + короткая цитата BSL + таблица мест из графа.

**Источники:** граф УБД и `user-ubd-code-index`.
