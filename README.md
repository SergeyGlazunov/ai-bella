# AI-Contour

Установочный **комплект** (исходники + рецепты). Живые MCP/индексы крутятся в Docker под `/opt/ai-contour` и `/srv/ai2`, не внутри этой папки.

| Документ | Содержание |
|----------|------------|
| [docs/superpowers/specs/2026-10-18-installable-contour-design.md](docs/superpowers/specs/2026-10-18-installable-contour-design.md) | Контракт продукта: что ставим, вектор, алгоритм правки |
| [STATUS.md](STATUS.md) | Текущий runtime, блокеры, следующий шаг |
| [ROADMAP.md](ROADMAP.md) | Пройденный путь к оркестратору |
| [VENDOR.md](VENDOR.md) | Vendor trees и неизвестные upstream pin |
| [AGENT.md](AGENT.md) | Контракт для агента |
| [USER.md](USER.md) | Установка, УБД+КБ, cutover |
| [checks/README.md](checks/README.md) | Stand gates |
| [deploy-ai2/README.md](deploy-ai2/README.md) | Overlays ai2: графы + query + BSL |

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

Контракт продукта — [устанавливаемый контур](docs/superpowers/specs/2026-10-18-installable-contour-design.md). Факты стенда — [STATUS.md](STATUS.md). Живой стенд не переустанавливать без явного разрешения.
