# ai2 BSL — УБД (search MCP)

Project: **`ai2-bsl-ubd`**. Port **`:8111`**. Mount: `/srv/ai2/ubd/src`.

На сервере: скопировать `daemon.toml` в путь, который монтирует compose (или править volume на этот файл). Образ — сборка из `compose/bsl-indexer/`.

```bash
docker compose -p ai2-bsl-ubd -f docker-compose.yml up -d
```
