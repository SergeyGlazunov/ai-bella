# Compose в комплекте

| Каталог | Назначение |
| --- | --- |
| `ai-stack/` | Open WebUI + OmniRoute (образы с registry, свои `.env`) |
| `mcp_for_1c/` | Граф Neo4j + MCP + индексатор (`1c-mcp-suite`, `docker compose build`) |
| `bsl-indexer/` | Cargo-репозиторий code-index; `docker compose build` из корня каталога |

Секреты: только `.env.example`. Рабочий `.env` получатель создаёт сам. Процедура: [USER.md](../USER.md).
