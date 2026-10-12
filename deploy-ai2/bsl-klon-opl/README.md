# ai2 BSL — klon_opl (search MCP, без графа)

Project: **`ai2-bsl-klon-opl`**. Port **`:8311`**. Mount: `/srv/ai2/klon_opl/src`.

Alias в индексе: `klon_opl`. Имя `opl` зарезервировано под будущую боевую базу.

Выгрузка: `tools/klon_opl/Export-KlonOpl.ps1` (dump-only, без хранилища). Neo4j / graph MCP для этого контура не используются.

```bash
docker compose -p ai2-bsl-klon-opl -f docker-compose.yml up -d
```
