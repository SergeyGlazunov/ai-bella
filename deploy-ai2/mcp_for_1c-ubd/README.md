# ai2 graph — УБД

Compose project: **`ai2-mcp-ubd`**. Выгрузка: `/srv/ai2/ubd/src`.

Базовый compose — из комплекта `compose/mcp_for_1c/` (скопировать на сервер рядом с этими overlays).

```bash
# сеть (один раз)
docker network create ai2-mcp-ubd_net

# из каталога с docker-compose.yml комплекта + эти файлы:
cp .env.example .env   # заполнить секреты
docker compose -p ai2-mcp-ubd \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.ai2.yml \
  up -d neo4j mcp-metadata-graph mcp-query-builder

# indexer — когда есть Configuration.xml
docker compose -p ai2-mcp-ubd \
  -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.ai2.yml \
  run --rm metadata-indexer
```

| Сервис | Host |
|--------|------|
| Graph MCP | `:8101` |
| query-builder | `:8109` |
| Neo4j Browser / Bolt | `127.0.0.1:7574` / `7787` |

Подключить к чату (после подъёма), сеть WebUI — обычно `ai2-stack`:

```bash
docker network connect ai2-stack ai2-mcp-graph-ubd
docker network connect ai2-stack ai2-mcp-query-builder-ubd
```

Либо optional-файл из комплекта: `compose/mcp_for_1c/docker-compose.webui.yml` с `WEBUI_NETWORK=ai2-stack`.

Не монтировать КБ в этот стек. КБ — `../mcp_for_1c-kb/`.
Не цеплять к prod `ai-stack_default` / `1c-suite-net` без явного решения.
