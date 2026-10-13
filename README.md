# AI-Contour

Установочный **комплект** (исходники + рецепты). Живые MCP/индексы крутятся в Docker и `/srv/ai2` / `C:\MCP`, не внутри этой папки.

| Документ | Содержание |
|----------|------------|
| [STATUS.md](STATUS.md) | Текущий runtime, блокеры, следующий шаг |
| [ROADMAP.md](ROADMAP.md) | PS/ai2 stable → linux-first / contour |
| [VENDOR.md](VENDOR.md) | Vendor trees и неизвестные upstream pin |
| [AGENT.md](AGENT.md) | Контракт для агента |
| [USER.md](USER.md) | Установка, УБД+КБ, cutover |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Журнал решений |
| [checks/README.md](checks/README.md) | Stand gates |
| [deploy-ai2/README.md](deploy-ai2/README.md) | Overlays ai2: графы + query + BSL |
| [For Cursor/LINUX-FIRST-IMPLEMENTATION-PLAN.md](For%20Cursor/LINUX-FIRST-IMPLEMENTATION-PLAN.md) | План миграции на orchestrator |

## Карта каталогов

```text
C:\AI-contour\
  controller/          оркестратор contour
  compose/             шаблоны образов/compose
    mcp_for_1c/          → graph + query (vendor)
    bsl-indexer/         → search MCP (vendor)
    ai-stack/            → WebUI + OmniRoute (шаблон)
  deploy-ai2/          overlays целевого контура УБД|КБ|klon_opl|чат
  integrations/        probes, skills
  checks/              stand proof
  systemd/             unit-шаблоны
  tools/               выгрузка КБ / klon_opl
  logs/ workdir/ secrets/   локальный мусор стенда (gitignore)
```

Оркестратор в комплекте есть; на живой стенд не переключён без явного разрешения.
