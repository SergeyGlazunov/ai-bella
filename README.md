# AI-Contour

Установочный **комплект** оркестратора УБД / КБ / `klon_opl` (исходники + рецепты compose + overlays). Живые MCP и индексы крутятся в Docker под `/opt/ai-contour` и `/srv/ai2`, не внутри этой папки Git.

Контракт продукта — [устанавливаемый контур](docs/superpowers/specs/2026-10-18-installable-contour-design.md): что ставим, вектор, алгоритм правки. Его читают до кода; при расхождении с остальными документами верен он. Живой стенд не переустанавливать и не переключать без явного разрешения.

## Кому какой файл

| Документ | Для кого | Содержание |
|----------|----------|------------|
| [AGENT.md](AGENT.md) | Агент разработки | Инварианты, запреты, reconcile, приёмка |
| [USER.md](USER.md) | Оператор / установка | Пути, команды, bootstrap, новая машина, УБД+КБ |
| [NIGHTLY.md](NIGHTLY.md) | Оператор ночи / догона | Готовые команды прогона; не собирать поиском по репо |
| [STATUS.md](STATUS.md) | Все | Текущий runtime стенда, блокеры, следующий шаг |
| [checks/README.md](checks/README.md) | Ops / миграция | Процедуры reboot, close-reads, watch pause; runbooks миграции |
| [deploy-ai2/README.md](deploy-ai2/README.md) | Установка ai2 | Overlays и name map графов / BSL / чата |
| [release-manifest.json](release-manifest.json) | Сборка комплекта | Что входит в kit и чего нет |

Команды ночи и дневного догона — только в [NIGHTLY.md](NIGHTLY.md). Факты стенда — только в [STATUS.md](STATUS.md).

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
  checks/              stand procedures + migration runbooks
  systemd/             unit-шаблоны
  tools/               выгрузка КБ / klon_opl
  logs/ workdir/ secrets/   локальный мусор стенда (gitignore)
```

## Vendor

Vendor trees в комплекте. Не выдумывать pinned SHA — отсутствующие pin блокируют release packaging (`release-manifest.json` → `missing`).

### `compose/bsl-indexer/`

| Field | Value |
|-------|--------|
| Upstream | https://github.com/Regsorm/code-index-mcp |
| Role | BSL / code-index MCP + daemon; build context for `ai2-bsl-*` images |
| Local pin (commit/tag) | **UNKNOWN** — tree is a vendored copy without `.git`; resolve before release packaging |
| Local patches | Tracked only as diffs in this repo; prefer upstream PR when possible |
| Update method | Compare against upstream tag/release; replace tree; rebuild `ai2-bsl-indexer:local`; smoke MCP `:8011`/`:8211`/`:8311` |

### `compose/mcp_for_1c/`

| Field | Value |
|-------|--------|
| Upstream (historical stand reference) | `https://github.com/shultzMAN87/mcp_for-1C.git` (from prior UBD deploy notes / `GraphStackGitUrl`) |
| Role | Metadata indexer, graph MCP, query-builder; Neo4j-facing jobs |
| Local pin (commit/tag) | **UNKNOWN** — vendored `1c-mcp-suite` without verified upstream SHA in-kit |
| Local patches | AI-Contour overlays live under `deploy-ai2/mcp_for_1c-*`, not silent edits to vendor when avoidable |
| Update method | Diff against known upstream commit; rebuild `ai2-metadata-indexer` / related images; smoke graph MCP |

### `compose/ai-stack/`

| Field | Value |
|-------|--------|
| Role | Template for Open WebUI + OmniRoute (not necessarily bit-identical to live `deploy-ai2/ai-stack`) |
| Pin | Follow image tags in compose / `.env.example`; live stand may pin differently |

### Policy

1. Never commit filled `.env` or connection JSON.
2. Prefer overlays in `deploy-ai2/` over rewriting vendor sources.
3. Before a “release kit” tarball: fill **Local pin** rows or list them under `release-manifest.json` → `missing`.
