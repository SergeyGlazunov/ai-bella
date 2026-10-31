# ai2 BSL — КБ (search MCP)

Project: **`ai2-bsl-kb`**. Port **`:8211`**. Mount: `/srv/ai-contour/sources/kb` → `/repos/SRC` (must match `databases.kb.source.source_path` in contour.yaml).

Не монтировать legacy `/srv/ai2/kb/src`: после `activate_source` remount assert упадёт с `stale bind` (как klon_opl run 90). Workspace `.code-index` живёт рядом с published tree под contour sources.

После ночной выгрузки / `contour update` демон видит дерево из contour sources; первичная индексация — при первом старте или смене revision.

```bash
docker compose -p ai2-bsl-kb -f docker-compose.yml up -d
```
