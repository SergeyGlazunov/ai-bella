# ai2 graph — КБ

Compose project: **`ai2-mcp-kb`**. Выгрузка: `/srv/ai2/kb/src`.

Ночной контур: `tools/kb/Update-And-Export-Kb.ps1` → `/srv/ai2/kb/src` + **полная** переиндексация графа
(`METADATA_FORCE_REINDEX`, без `apply_changes`). Ручной one-shot ниже — для первичного подъёма / отладки.

```bash
docker network create ai2-mcp-kb_net

cp .env.example .env   # заполнить секреты
docker compose -p ai2-mcp-kb \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.ai2.yml \
  up -d neo4j mcp-metadata-graph mcp-query-builder

docker compose -p ai2-mcp-kb \
  -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.ai2.yml \
  run --rm -e METADATA_FORCE_REINDEX=true metadata-indexer
```


| Сервис | Host |
|--------|------|
| Graph MCP | `:8201` |
| query-builder | `:8209` |
| Neo4j Browser / Bolt | `127.0.0.1:7674` / `7887` |

К чату:

```bash
docker network connect ai2-stack ai2-mcp-graph-kb
docker network connect ai2-stack ai2-mcp-query-builder-kb
```

Не смешивать с УБД. УБД — `../mcp_for_1c-ubd/`.
Не цеплять к prod `ai-stack_default` / `1c-suite-net` без явного решения.
