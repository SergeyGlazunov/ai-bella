# deploy-ai2 — overlays целевого контура (УБД + КБ + klon_opl)

Эталон для **нового сервера**. **Не применяется автоматически** к уже запущенным контейнерам.

| Каталог | Роль | MCP host |
|---------|------|----------|
| `mcp_for_1c-ubd/` | Neo4j + graph + query УБД | `:8101` / `:8109` |
| `mcp_for_1c-kb/` | Neo4j + graph + query КБ | `:8201` / `:8209` |
| `bsl-ubd/` | code-index УБД | `:8111` |
| `bsl-kb/` | code-index КБ | `:8211` |
| `bsl-klon-opl/` | code-index клон оплат (без графа) | `:8311` |
| `ai-stack/` | Open WebUI, OmniRoute, MDD | см. `ai-stack/README.md` |

Выгрузки: `/srv/ai2/ubd/src` (git), `/srv/ai2/kb/src` (`tools/kb/…`), `/srv/ai2/klon_opl/src` (`tools/klon_opl/…`).

Устаревшие каталоги: `mcp_for_1c/`, `bsl-indexer/` — см. `DEPRECATED.md` внутри.

## Name map (эталон)

Префикс на ai2: `ai2-`. На чистом сервере можно сменить на нейтральный, сохранив суффиксы `-ubd` / `-kb`.

| Роль | УБД | КБ |
|------|-----|-----|
| Выгрузка XML | `/srv/ai2/ubd/src` | `/srv/ai2/kb/src` |
| Compose project (graph) | `ai2-mcp-ubd` | `ai2-mcp-kb` |
| Neo4j | `ai2-neo4j-ubd` | `ai2-neo4j-kb` |
| Graph MCP | `ai2-mcp-graph-ubd` `:8101` | `ai2-mcp-graph-kb` `:8201` |
| query-builder | `ai2-mcp-query-builder-ubd` `:8109` | `ai2-mcp-query-builder-kb` `:8209` |
| Network (graph) | `ai2-mcp-ubd_net` | `ai2-mcp-kb_net` |
| Volume Neo4j | `ai2-mcp-ubd_neo4j-data` | `ai2-mcp-kb_neo4j-data` |
| Neo4j Browser/Bolt | `127.0.0.1:7574` / `7787` | `127.0.0.1:7674` / `7887` |
| Compose project (BSL) | `ai2-bsl-ubd` | `ai2-bsl-kb` |
| BSL MCP | `ai2-bsl-ubd` `:8111` | `ai2-bsl-kb` `:8211` |

**Нельзя:** один Neo4j или один BSL home на две выгрузки; второй Neo4j в сети `1c-suite-net` рядом с prod (двойной DNS `neo4j`). К чату — `docker network connect` на `ai2-stack` (или `docker-compose.webui.yml`), не hardcode prod `ai-stack_default` в kit override.

RAM: один граф idle ~2–3 ГБ Neo4j; второй ≈ ещё столько же; BSL ~0.5 ГБ каждый. Хост ≥16 ГБ для WSL при двух графах + WebUI.

Установка: [USER.md](../USER.md) §«Две конфигурации». Graph/query: `compose/mcp_for_1c/docs/РАЗВЁРТЫВАНИЕ.md`. Грабли: `compose/mcp_for_1c/docs/ОШИБКИ-РАЗВЁРТЫВАНИЯ.md`. Чат / Vision Bridge: `ai-stack/README.md`.
