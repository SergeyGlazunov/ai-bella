# ai2 BSL — КБ (search MCP)

Project: **`ai2-bsl-kb`**. Port **`:8211`**. Mount: `/srv/ai2/kb/src`.

После ночной выгрузки (`tools/kb/Update-And-Export-Kb.ps1`) демон подхватит файлы / первичная индексация при первом старте.

```bash
docker compose -p ai2-bsl-kb -f docker-compose.yml up -d
```
