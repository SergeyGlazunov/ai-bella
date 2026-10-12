# Архитектура

Коротко: зачем этот каталог в комплекте AI-Contour и как устроен graph-стек.
Установка — [РАЗВЁРТЫВАНИЕ.md](docs/РАЗВЁРТЫВАНИЕ.md). Грабли — [ОШИБКИ-РАЗВЁРТЫВАНИЯ.md](docs/ОШИБКИ-РАЗВЁРТЫВАНИЯ.md). Вход — [README.md](README.md).

---

## Роль в полном контуре

Установочный набор ai2 (все ставятся сразу):

| Tool | Где |
|---|---|
| Graph | этот каталог → Neo4j + `mcp-metadata-graph` |
| query-builder | этот каталог → `mcp-query-builder` (тот же Neo4j, слой метаданных) |
| Code_index | `compose/bsl-indexer`, overlays `deploy-ai2/bsl-*` |
| MDD / SDMS / Confluence | live MCP вне этого compose |
| Skills | `integrations/skills/webui/`, `integrations/skills/cursor/` |

Базовый `docker-compose.yml` ещё содержит vendor-сервисы (platform-help, v8std, bsl-checker, qdrant). На стенде ai2 они обычно выключены профилем `unused` в override — не путать с обязательным набором из шести tools.

---

## Задача graph-стека

Агент не знает **вашу** конфигурацию. Graph отвечает «что есть и кто кого зовёт»; query-builder — «как собрать корректный запрос по этим метаданным». Тела BSL — у Code_index, не у графа.

```
   выгрузка 1С (XML+BSL)
            │
            ▼
    metadata-indexer ──► Neo4j
                           │
              ┌────────────┼────────────┐
              ▼                         ▼
      metadata-graph              query-builder
         :8001                       :8009
              │                         │
              └────────────┬────────────┘
                           ▼
              агент (Open WebUI / Cursor) + skills
```

Индексатор пишет, серверы только читают. Слой 1 (XML) — минуты, его хватает query-builder. Слой 2 (BSL call graph) — долго; покрытие резолва растёт до неподвижной точки.

---

## Решения, которые нельзя ломать

1. **Серверы независимы** — graph не ходит в query и наоборот.
2. **Транспорт** — Streamable HTTP, `/mcp`, stateless; порты на хосте осознанно (ai2: отдельные publish-порты на УБД/КБ).
3. **Отказ ≠ пустой успех** — в ответах `answerable` / `degraded` / `meaning`.
4. **Отдельные Neo4j на конфигурацию** — УБД и КБ не монтировать в один граф (`deploy-ai2/mcp_for_1c-ubd|kb`).
5. **Сеть ai2 ≠ prod `1c-suite-net`** — иначе двойной DNS `neo4j` (см. ОШИБКИ).

---

## Лицензии (кратко)

Свой код — MIT (`LICENSE`). Neo4j Community, образы и pip-зависимости — при сборке. Выгрузка конфигурации и `.hbk` в git не входят.
