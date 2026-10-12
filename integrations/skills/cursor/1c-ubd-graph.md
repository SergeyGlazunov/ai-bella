---
name: 1c-ubd-graph
description: Uses MCP user-ubd-graph (Neo4j) for UBD metadata, attributes, call graph, register writers, and code locations (file + lines). Use when the question is about this configuration — реквизиты, кто вызывает, какие документы пишут в регистр, где лежит логика. Does not return BSL bodies; then use skill 1c-code-index. Not MDD owners, not platform syntax.
---

# Граф УБД — места и связи

Namespace: `user-ubd-graph`. Источник — Neo4j после индекса XML+BSL выгрузки УБД на ВМ.

Отдаёт: объекты, реквизиты, модули, вызовы, движения, `source_path`, `line_start`, `line_end`.

Не отдаёт: тела процедур, текст запросов, владельцев MDD, методы платформы.

Не вызывай MDD и справку 1С на этом слое. Конфигурации в этом git-репо нет.

## Имена

Английский тип: `Catalog.Номенклатура`, `Document.Проформа`, `DataProcessor.КонтрольПереплат`, `InformationRegister.ПереплатыПоПроформам`, `CommonModule.АукОбщийКлиент`.

Синоним без типа — сначала `metadata_search`, не выдумывай полное имя.

## Какие tools звать

Обычный путь «как считается / укажи места»:

1. `metadata_search` — объект.
2. `metadata_object_attributes` — если спросили реквизиты / ТЧ (не для формулы).
3. `code_procedures_operating_on` или `code_callees` / `code_callers` — ключевые процедуры, не все подряд.
4. `code_method_signature` **один–два раза** на ключевую функцию — из ответа возьми файл и строки.
5. Сразу таблица мест. Если нужна формула или текст запроса — скилл `1c-code-index`, `get_function` по уже найденному имени в `user-ubd-code-index`.

Связи метаданных: `metadata_find_link_path`, `metadata_referrers`, `metadata_attribute_type`.

Не использовать для поиска формулы: `code_dead_procedures`, `code_unresolved_callsites`, `metadata_dead`, `metadata_stats`, `metadata_subsystem_tree` (если не спросили подсистемы).

`code_method_signature` повторно для той же процедуры — запрещён. Это не исходник.

## Ответ про места

```
**Источник:** граф УБД

| Что | Где |
|---|---|
| Объект | DataProcessor.КонтрольПереплат |
| Процедура | ПолучитьПлоскиеДанныеПереплат |
| Файл | DataProcessors/КонтрольПереплат/Ext/ManagerModule.bsl |
| Строки | 1885–1953 |
```

Цепочку вызовов — 3–6 узлов, не дерево на 500 callees. Спросили только «где» — этого достаточно. Не обещай тело из графа.
