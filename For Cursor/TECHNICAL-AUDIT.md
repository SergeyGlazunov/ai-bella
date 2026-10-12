# AI-Contour — описание технического решения для аудита

**Дата среза:** 2026-10-12  
**Источник:** установочный комплект `C:\AI-contour` (+ факты живого стенда из `DEVELOPMENT.md` / `USER.md` / `AGENT.md`).  
**Назначение документа:** передать аудитору карту системы, состав файлов комплекта, текущее развёртывание и механизмы обновления.

---

## 1. Кратко: что это

**AI-Contour** — внутренний контур для аналитиков 1С: чат (Open WebUI + OmniRoute) + MCP-инструменты над выгрузками конфигураций.

| Слой | Назначение |
| --- | --- |
| Чат | Open WebUI (UI) → OmniRoute (маршрутизация моделей) |
| Graph MCP | Neo4j + `mcp-metadata-graph` (+ `query-builder`) по XML/BSL выгрузке |
| Code-index MCP | `bsl-indexer` — поиск/сводка по BSL |
| Источник УБД | git-клон выгрузки на ext4 (целевое); на живом стенде — `C:\MCP\ubd_config` |
| Источник КБ / klon_opl | Designer dump-скрипты Windows → `/srv/ai2/.../src` (целевой контур ai2) |
| Оркестратор `contour` | Python-пакет в `controller/`: окно записи, close-reads, git FF, дельта индексов, verify, open |

Комплект `C:\AI-contour` — **исходники + рецепты**. Живые контейнеры и индексы крутятся в Docker / `/srv/ai*` / `C:\MCP`, не как runtime внутри этой папки.

### 1.1. Статус внедрения (критично для аудита)

| Факт | Состояние |
| --- | --- |
| Оркестратор `contour` на живом стенде | **Не переключён** (код в комплекте есть; timer не enable) |
| Ночной sync УБД на живом стенде | PowerShell + задача Планировщика `UBD Config Sync` |
| Stand gates (watch pause, close reads) | **PASS** (по журналу) |
| Cutover | Ждёт drill бэкапа чат-слоя + явное разрешение |
| Параллельный контур ai2 | Частично поднят рядом с prod (отдельные имена/порты/тома) |
| `live_cutover` в `release-manifest.json` | `false` |

---

## 2. Архитектура (как устроено)

```text
Аналитик
   │
   ▼
Open WebUI ──► OmniRoute (модели) ──► LLM providers
   │
   ├─ MCP Graph (metadata) ──► Neo4j (том данных)
   ├─ MCP Query-builder
   └─ MCP BSL / code-index ──► индекс на volume bsl-*-home
              ▲
              │ bind/mount выгрузки Configuration.xml + BSL
         УБД (git) / КБ (Designer) / klon_opl (dump)

Ночное обновление (целевое):
  systemd timer → contour update → flock → close reads →
  pause BSL watch → git FF → index delta/FULL → verify → open

Ночное обновление (факт на живом стенде):
  Task Scheduler «UBD Config Sync» → sync-and-index.ps1
```

### 2.1. Принципы целевого оркестратора

1. Одна рабочая копия данных и одни индексы (**поколений нет**).
2. Git — fetch + fast-forward, не полная выгрузка каждый раз.
3. Индексаторы — дельта по возможности; FULL графа только по контракту (exit code 3).
4. Перед записью — **close reads** (graph stop + disconnect BSL от сети чата); после сбоя чтение остаётся закрытым.
5. Успех только при согласованности `source | code_index | graph` (один SHA + совпадение storage identity + indexer version).
6. Окно записи `01:30–06:00` Asia/Vladivostok; вне окна — `deferred`.
7. Чат-слой (WebUI/OmniRoute) в миграцию оркестратора **не** входит; `compose pull`/recreate чата запрещены без отдельного задания.
8. Секреты не в образе и не в argv.

### 2.2. Ключевые модули `controller/`

| Модуль | Роль |
| --- | --- |
| `cli.py` | CLI: `validate` / `status` / `update` / `open` / `cleanup` |
| `reconcile.py` | Главный цикл ночного sync |
| `reads.py` | Close/open чтения MCP |
| `index_graph.py` / `index_bsl.py` | Запуск индексаторов |
| `watch_bsl.py` | Pause/restore watcher `daemon.toml` |
| `verify.py` | Проверки согласованности + content probes |
| `db.py` | SQLite состояние (`/srv/ai/state.db`) |
| `lock.py` | `flock` `/srv/ai/sync.lock` |
| `schedule.py` | Окно записи |
| `config.py` | Загрузка `config.yaml` |
| `adapters.py` | Внешние probe/idle скрипты |
| `alert.py` | Канал алерта (stdin → command) |
| `gitutil.py` / `dockerutil.py` | Git FF и Docker ops |

---

## 3. Как сейчас развёрнуто

Платформа: **Windows 11 + WSL2 Ubuntu**, Docker Engine в Linux, API часто через `DOCKER_HOST=tcp://127.0.0.1:2375`.

### 3.1. Живой стенд (prod, факт)

| Компонент | Где |
| --- | --- |
| Клон УБД | `C:\MCP\ubd_config` (ветка `main`, внутри `src`) |
| Ночной sync | `C:\MCP\ubd_index\scripts\sync-and-index.ps1`, задача `UBD Config Sync` (учётка `ai-docker`) |
| Состояние sync | `C:\MCP\ubd_index\state\sync-state.json` |
| Graph stack | compose project `mcp_for_1c`, `C:\MCP\mcp_for_1c`, том `mcp_for_1c_neo4j-data` |
| BSL / code-index | project `bsl-indexer`, `C:\MCP\code_index\deploy`, том `bsl-indexer_bsl-indexer-home`; bind `/mnt/c/MCP/ubd_config/src` → `/repos/SRC` |
| Чат | project `ai-stack`, `C:\ai-stack`; тома `ai-stack_open-webui-data`, `ai-stack_omniroute-data`; секреты `C:\ai-stack\.env` |
| Docker autostart | `WSL Docker Engine` → `C:\ProgramData\DockerWSL\start.ps1` |
| Старые скрипты развёртывания | `C:\instructions-main-system_analytics\system_analytics` (не копировать leftover) |

Типичные имена контейнеров/сети prod: `mcp-metadata-graph`, `bsl-indexer`, сеть `ai-stack_default`, publish BSL `:8011`.

### 3.2. Целевая установка «с комплекта» (одна база УБД)

| Путь | Назначение |
| --- | --- |
| `/opt/ai-sync/` | Пакет оркестратора после `bootstrap.sh` |
| `/opt/ai-sync/config.yaml` | Конфиг (из `contour.example.yaml`) |
| `/opt/ai-sync/venv/bin/contour` | CLI |
| `/srv/ai/ubd` | Git-клон УБД на **ext4** (не `/mnt/c`) |
| `/srv/ai/state.db` / `sync.lock` | Состояние и lock |
| `/srv/ai/secrets/` | Секреты (chmod 700) |
| `/srv/ai/compose/{ai-stack,mcp_for_1c,bsl-indexer}` | Рабочие compose-копии |
| `/etc/systemd/system/ai-sync.{service,timer}` | Units (ставится bootstrap, **без** enable) |

### 3.3. Целевой контур ai2 (УБД + КБ + klon_opl)

Overlays: `deploy-ai2/`. **Не применяется автоматически** к уже запущенным prod-контейнерам.

| | УБД | КБ | klon_opl |
| -- | --- | --- | --- |
| Выгрузка | `/srv/ai2/ubd/src` (git) | `/srv/ai2/kb/src` (Designer) | `/srv/ai2/klon_opl/src` (dump) |
| Graph MCP | `:8101` (`ai2-mcp-ubd`) | `:8201` (`ai2-mcp-kb`) | — |
| query-builder | `:8109` | `:8209` | — |
| BSL MCP | `:8111` | `:8211` | `:8311` |
| Чат (опц.) | project `ai2-stack` (`deploy-ai2/ai-stack`) |||

Инвариант: **нельзя** делить один Neo4j или один BSL home на несколько выгрузок; нельзя второй Neo4j в сети `1c-suite-net` рядом с prod (двойной DNS `neo4j`).

---

## 4. Как обновляется

### 4.1. Живой стенд (сейчас)

1. Планировщик `UBD Config Sync` запускает `sync-and-index.ps1`.
2. `git pull` рабочей копии УБД.
3. Дельта BSL-индекса.
4. Graph: `apply_changes.py` / FULL при коде 3.
5. Состояние пишется в `sync-state.json`.

Чат-слой при этом **не** обновляется ночным sync (образы WebUI/OmniRoute не входят в цикл УБД).

### 4.2. Целевое (пакет `contour`)

| Механизм | Детали |
| --- | --- |
| Timer | `ai-sync.timer` → `OnCalendar=*-*-* 01:30:00 Asia/Vladivostok`, `Persistent=true` |
| Service | `ai-sync.service` oneshot → `contour update` |
| Ручной запуск | `contour update` / `contour update --measure` |
| Вне окна | `deferred`, новая запись не стартует |
| Установка пакета | `sudo ./bootstrap.sh` → копирует controller/integrations, venv, validate, кладёт units **без enable** |
| Алерт | `alert.command`; kit stub `alert.sh.example` отвергается `validate` до замены канала |

Порядок reconcile (сжато): `flock` → Docker ping → добить jobs → idle probes → **close reads** → pause BSL watch → git FF → index → verify по diff SHA → open → успех только при согласованных индексах.

### 4.3. КБ и klon_opl (ai2)

| Источник | Скрипт | Расписание (рекоменд.) |
| --- | --- | --- |
| КБ | `tools/kb/Update-And-Export-Kb.ps1` | Task Scheduler `AI-contour KB nightly export` **01:00** |
| klon_opl | `tools/klon_opl/Export-KlonOpl.ps1` | `AI-contour klon_opl nightly export` **02:00** |

КБ: store update → `/UpdateDBCfg` → dump XML → копия в `/srv/ai2/kb/src`.  
klon_opl: `/DumpConfigToFiles` → `/srv/ai2/klon_opl/src` (хранилища нет).

### 4.4. Обновление комплекта / стенда (правила)

- Не `docker compose pull` / recreate Open WebUI / OmniRoute в рамках миграции оркестратора.
- Не создавать Docker project `ai-contour`.
- Не `down -v` на томах чата.
- Cutover только после PASS gates + drill бэкапа чат-слоя + явного разрешения (см. `USER.md` § cutover, `checks/`).
- Архив комплекта: `tar` + `sha256sum` (в `release-manifest` пока нет готового checksum).

---

## 5. Состав комплекта (логическая карта)

```text
C:\AI-contour\
  controller/          оркестратор contour (Python)
  compose/             vendor/шаблоны образов
    mcp_for_1c/          graph + query (+ suite)
    bsl-indexer/         search MCP (Rust crates)
    ai-stack/            WebUI + OmniRoute (registry images)
  deploy-ai2/          overlays УБД|КБ|klon_opl|чат
  integrations/        probes, idle, reload, skills md
  checks/              stand proof gates
  systemd/             ai-sync.service + .timer
  tools/               выгрузка КБ / klon_opl
  bootstrap.sh         установка в /opt/ai-sync
  contour.example.yaml пример конфига
  USER.md / AGENT.md / DEVELOPMENT.md
  logs/ workdir/ secrets/   локальный мусор стенда (gitignore, вне inventory)
```

### 5.1. Сводка по ролям файлов

| Роль | Файлов |
| --- | ---: |
| vendor: graph/query MCP | 172 |
| vendor: BSL code-index | 157 |
| overlays ai2 | 29 |
| skills markdown | 16 |
| оркестратор (код) | 16 |
| оркестратор (тесты) | 12 |
| корень комплекта | 11 |
| выгрузка КБ/klon_opl | 9 |
| адаптеры/probes | 5 |
| stand gates | 3 |
| systemd units | 2 |
| шаблон чат-слоя | 2 |
| compose kit | 1 |
| оркестратор (пакет) | 1 |
| **Итого в inventory** | **436** |

Суммарный размер перечисленных файлов: **7.89 MiB** (без баз/выгрузок/секретов).

### 5.2. Что намеренно исключено из таблицы

| Исключено | Почему |
| --- | --- |
| `logs/`, `workdir/`, `secrets/` | Локальный runtime-мусор / секреты (gitignore) |
| Заполненные `.env` | Секреты; в комплекте есть только `.env.example`. Локально обнаружены: `deploy-ai2/ai-stack/.env`, `deploy-ai2/mcp_for_1c/.env`, `deploy-ai2/mcp_for_1c-kb/.env` — содержимое в audit-пакет не включать |
| `*.db`, дампы, архивы (`.7z`, `.zip`, …), `platform-help` data | Базы/выгрузки/проприетарные дампы |
| Содержимое git-выгрузок УБД/КБ | Клонируется/дампится получателем отдельно |
| `target/`, `__pycache__`, `.pytest_cache`, `egg-info` | Артефакты сборки/тестов |
| `.git/`, `.cursor/` | VCS / IDE |

### 5.3. Чего нет в комплекте (из `release-manifest.json`)

- Содержимое Git УБД
- Заполненные секреты
- JSON exports skills из живого Open WebUI (есть markdown-копии)
- Дамп `platform-help-data`
- Готовый tarball + checksum

---

## 6. Project map — таблица системных файлов

Полный перечень файлов комплекта, относящихся к системному решению (код, compose, overlays, docs, units, tools). Путь относительно `C:\AI-contour`.

| # | Путь | Размер, байт | Роль |
| ---: | --- | ---: | --- |
| 1 | `.gitignore` | 91 | корень комплекта |
| 2 | `AGENT.md` | 7057 | корень комплекта |
| 3 | `DEVELOPMENT.md` | 67010 | корень комплекта |
| 4 | `README.md` | 1472 | корень комплекта |
| 5 | `USER.md` | 13124 | корень комплекта |
| 6 | `bootstrap.sh` | 2450 | корень комплекта |
| 7 | `checks/README.md` | 1453 | stand gates |
| 8 | `checks/stand_close_reads.md` | 6539 | stand gates |
| 9 | `checks/stand_watch_pause.md` | 8044 | stand gates |
| 10 | `compose/README.md` | 552 | compose kit |
| 11 | `compose/ai-stack/.env.example` | 329 | шаблон чат-слоя |
| 12 | `compose/ai-stack/docker-compose.yml` | 1587 | шаблон чат-слоя |
| 13 | `compose/bsl-indexer/.dockerignore` | 469 | vendor: BSL code-index |
| 14 | `compose/bsl-indexer/.github/workflows/publish-registry.yml` | 8636 | vendor: BSL code-index |
| 15 | `compose/bsl-indexer/.github/workflows/release.yml` | 5154 | vendor: BSL code-index |
| 16 | `compose/bsl-indexer/CHANGELOG.md` | 602565 | vendor: BSL code-index |
| 17 | `compose/bsl-indexer/CHANGELOG_EN.md` | 389378 | vendor: BSL code-index |
| 18 | `compose/bsl-indexer/CODE_OF_CONDUCT.md` | 2290 | vendor: BSL code-index |
| 19 | `compose/bsl-indexer/CONTRIBUTING.md` | 5281 | vendor: BSL code-index |
| 20 | `compose/bsl-indexer/Cargo.lock` | 90837 | vendor: BSL code-index |
| 21 | `compose/bsl-indexer/Cargo.toml` | 3526 | vendor: BSL code-index |
| 22 | `compose/bsl-indexer/LICENSE` | 1085 | vendor: BSL code-index |
| 23 | `compose/bsl-indexer/README.md` | 14936 | vendor: BSL code-index |
| 24 | `compose/bsl-indexer/README_EN.md` | 61277 | vendor: BSL code-index |
| 25 | `compose/bsl-indexer/README_RU.md` | 90888 | vendor: BSL code-index |
| 26 | `compose/bsl-indexer/SECURITY.md` | 4201 | vendor: BSL code-index |
| 27 | `compose/bsl-indexer/crates/bsl-extension/Cargo.toml` | 3305 | vendor: BSL code-index |
| 28 | `compose/bsl-indexer/crates/bsl-extension/src/code_usages.rs` | 29208 | vendor: BSL code-index |
| 29 | `compose/bsl-indexer/crates/bsl-extension/src/enrichment/batch.rs` | 25882 | vendor: BSL code-index |
| 30 | `compose/bsl-indexer/crates/bsl-extension/src/enrichment/cli.rs` | 7633 | vendor: BSL code-index |
| 31 | `compose/bsl-indexer/crates/bsl-extension/src/enrichment/client.rs` | 8696 | vendor: BSL code-index |
| 32 | `compose/bsl-indexer/crates/bsl-extension/src/enrichment/mod.rs` | 1871 | vendor: BSL code-index |
| 33 | `compose/bsl-indexer/crates/bsl-extension/src/enrichment/prompt.rs` | 8678 | vendor: BSL code-index |
| 34 | `compose/bsl-indexer/crates/bsl-extension/src/enrichment/signature.rs` | 5350 | vendor: BSL code-index |
| 35 | `compose/bsl-indexer/crates/bsl-extension/src/form_bindings.rs` | 13243 | vendor: BSL code-index |
| 36 | `compose/bsl-indexer/crates/bsl-extension/src/index_extras.rs` | 29480 | vendor: BSL code-index |
| 37 | `compose/bsl-indexer/crates/bsl-extension/src/index_extras/call_graph.rs` | 72646 | vendor: BSL code-index |
| 38 | `compose/bsl-indexer/crates/bsl-extension/src/index_extras/common.rs` | 21977 | vendor: BSL code-index |
| 39 | `compose/bsl-indexer/crates/bsl-extension/src/index_extras/exported.rs` | 8989 | vendor: BSL code-index |
| 40 | `compose/bsl-indexer/crates/bsl-extension/src/index_extras/full_scan.rs` | 67416 | vendor: BSL code-index |
| 41 | `compose/bsl-indexer/crates/bsl-extension/src/index_extras/incremental.rs` | 76255 | vendor: BSL code-index |
| 42 | `compose/bsl-indexer/crates/bsl-extension/src/index_extras/modules.rs` | 34354 | vendor: BSL code-index |
| 43 | `compose/bsl-indexer/crates/bsl-extension/src/index_extras/tests.rs` | 212623 | vendor: BSL code-index |
| 44 | `compose/bsl-indexer/crates/bsl-extension/src/lib.rs` | 1470 | vendor: BSL code-index |
| 45 | `compose/bsl-indexer/crates/bsl-extension/src/module_constants.rs` | 6447 | vendor: BSL code-index |
| 46 | `compose/bsl-indexer/crates/bsl-extension/src/parse_collector.rs` | 13476 | vendor: BSL code-index |
| 47 | `compose/bsl-indexer/crates/bsl-extension/src/processor.rs` | 20274 | vendor: BSL code-index |
| 48 | `compose/bsl-indexer/crates/bsl-extension/src/qualified_callers.rs` | 9869 | vendor: BSL code-index |
| 49 | `compose/bsl-indexer/crates/bsl-extension/src/schema.rs` | 66193 | vendor: BSL code-index |
| 50 | `compose/bsl-indexer/crates/bsl-extension/src/terms.rs` | 14932 | vendor: BSL code-index |
| 51 | `compose/bsl-indexer/crates/bsl-extension/src/tools/bsl_sql.rs` | 48839 | vendor: BSL code-index |
| 52 | `compose/bsl-indexer/crates/bsl-extension/src/tools/find_data_path.rs` | 11370 | vendor: BSL code-index |
| 53 | `compose/bsl-indexer/crates/bsl-extension/src/tools/find_path_bsl.rs` | 18720 | vendor: BSL code-index |
| 54 | `compose/bsl-indexer/crates/bsl-extension/src/tools/find_references.rs` | 26290 | vendor: BSL code-index |
| 55 | `compose/bsl-indexer/crates/bsl-extension/src/tools/get_data_links.rs` | 29628 | vendor: BSL code-index |
| 56 | `compose/bsl-indexer/crates/bsl-extension/src/tools/get_event_subscriptions.rs` | 18579 | vendor: BSL code-index |
| 57 | `compose/bsl-indexer/crates/bsl-extension/src/tools/get_form_handlers.rs` | 22341 | vendor: BSL code-index |
| 58 | `compose/bsl-indexer/crates/bsl-extension/src/tools/get_object_profile.rs` | 44068 | vendor: BSL code-index |
| 59 | `compose/bsl-indexer/crates/bsl-extension/src/tools/get_object_structure.rs` | 49800 | vendor: BSL code-index |
| 60 | `compose/bsl-indexer/crates/bsl-extension/src/tools/get_register_writers.rs` | 10133 | vendor: BSL code-index |
| 61 | `compose/bsl-indexer/crates/bsl-extension/src/tools/get_role_rights.rs` | 13229 | vendor: BSL code-index |
| 62 | `compose/bsl-indexer/crates/bsl-extension/src/tools/investigate_object.rs` | 29210 | vendor: BSL code-index |
| 63 | `compose/bsl-indexer/crates/bsl-extension/src/tools/mod.rs` | 19589 | vendor: BSL code-index |
| 64 | `compose/bsl-indexer/crates/bsl-extension/src/tools/search_terms.rs` | 14328 | vendor: BSL code-index |
| 65 | `compose/bsl-indexer/crates/bsl-extension/src/xml/config_dump_info.rs` | 18846 | vendor: BSL code-index |
| 66 | `compose/bsl-indexer/crates/bsl-extension/src/xml/configuration.rs` | 12421 | vendor: BSL code-index |
| 67 | `compose/bsl-indexer/crates/bsl-extension/src/xml/edt_mdo.rs` | 77256 | vendor: BSL code-index |
| 68 | `compose/bsl-indexer/crates/bsl-extension/src/xml/event_subscriptions.rs` | 15045 | vendor: BSL code-index |
| 69 | `compose/bsl-indexer/crates/bsl-extension/src/xml/forms.rs` | 25139 | vendor: BSL code-index |
| 70 | `compose/bsl-indexer/crates/bsl-extension/src/xml/metadata_refs.rs` | 26922 | vendor: BSL code-index |
| 71 | `compose/bsl-indexer/crates/bsl-extension/src/xml/mod.rs` | 2158 | vendor: BSL code-index |
| 72 | `compose/bsl-indexer/crates/bsl-extension/src/xml/object_attributes.rs` | 114831 | vendor: BSL code-index |
| 73 | `compose/bsl-indexer/crates/bsl-extension/src/xml/object_uuid.rs` | 11518 | vendor: BSL code-index |
| 74 | `compose/bsl-indexer/crates/bsl-extension/tests/tools_integration.rs` | 46669 | vendor: BSL code-index |
| 75 | `compose/bsl-indexer/crates/bsl-indexer/Cargo.toml` | 1903 | vendor: BSL code-index |
| 76 | `compose/bsl-indexer/crates/bsl-indexer/build.rs` | 573 | vendor: BSL code-index |
| 77 | `compose/bsl-indexer/crates/bsl-indexer/src/main.rs` | 4030 | vendor: BSL code-index |
| 78 | `compose/bsl-indexer/crates/bsl-parse/Cargo.toml` | 360 | vendor: BSL code-index |
| 79 | `compose/bsl-indexer/crates/bsl-parse/src/lib.rs` | 67265 | vendor: BSL code-index |
| 80 | `compose/bsl-indexer/crates/code-index-core/Cargo.toml` | 3749 | vendor: BSL code-index |
| 81 | `compose/bsl-indexer/crates/code-index-core/benches/parse_benchmark.rs` | 228 | vendor: BSL code-index |
| 82 | `compose/bsl-indexer/crates/code-index-core/src/cli.rs` | 79279 | vendor: BSL code-index |
| 83 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/cache_client.rs` | 10600 | vendor: BSL code-index |
| 84 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/client.rs` | 5973 | vendor: BSL code-index |
| 85 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/commands.rs` | 1488 | vendor: BSL code-index |
| 86 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/config.rs` | 44791 | vendor: BSL code-index |
| 87 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/ipc.rs` | 10577 | vendor: BSL code-index |
| 88 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/language_detect.rs` | 17140 | vendor: BSL code-index |
| 89 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/lock.rs` | 2961 | vendor: BSL code-index |
| 90 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/mod.rs` | 696 | vendor: BSL code-index |
| 91 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/paths.rs` | 3565 | vendor: BSL code-index |
| 92 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/runner.rs` | 57106 | vendor: BSL code-index |
| 93 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/server.rs` | 8556 | vendor: BSL code-index |
| 94 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/state.rs` | 11101 | vendor: BSL code-index |
| 95 | `compose/bsl-indexer/crates/code-index-core/src/daemon_core/worker.rs` | 124151 | vendor: BSL code-index |
| 96 | `compose/bsl-indexer/crates/code-index-core/src/extension/context.rs` | 2864 | vendor: BSL code-index |
| 97 | `compose/bsl-indexer/crates/code-index-core/src/extension/mod.rs` | 1672 | vendor: BSL code-index |
| 98 | `compose/bsl-indexer/crates/code-index-core/src/extension/processor.rs` | 27525 | vendor: BSL code-index |
| 99 | `compose/bsl-indexer/crates/code-index-core/src/extension/tool.rs` | 3438 | vendor: BSL code-index |
| 100 | `compose/bsl-indexer/crates/code-index-core/src/federation/client.rs` | 8606 | vendor: BSL code-index |
| 101 | `compose/bsl-indexer/crates/code-index-core/src/federation/config.rs` | 15877 | vendor: BSL code-index |
| 102 | `compose/bsl-indexer/crates/code-index-core/src/federation/dispatcher.rs` | 3504 | vendor: BSL code-index |
| 103 | `compose/bsl-indexer/crates/code-index-core/src/federation/mod.rs` | 1117 | vendor: BSL code-index |
| 104 | `compose/bsl-indexer/crates/code-index-core/src/federation/reload.rs` | 22118 | vendor: BSL code-index |
| 105 | `compose/bsl-indexer/crates/code-index-core/src/federation/repos.rs` | 11326 | vendor: BSL code-index |
| 106 | `compose/bsl-indexer/crates/code-index-core/src/federation/server.rs` | 19148 | vendor: BSL code-index |
| 107 | `compose/bsl-indexer/crates/code-index-core/src/federation/whitelist.rs` | 5845 | vendor: BSL code-index |
| 108 | `compose/bsl-indexer/crates/code-index-core/src/indexer/config.rs` | 25580 | vendor: BSL code-index |
| 109 | `compose/bsl-indexer/crates/code-index-core/src/indexer/file_types.rs` | 22143 | vendor: BSL code-index |
| 110 | `compose/bsl-indexer/crates/code-index-core/src/indexer/hasher.rs` | 3817 | vendor: BSL code-index |
| 111 | `compose/bsl-indexer/crates/code-index-core/src/indexer/mod.rs` | 126813 | vendor: BSL code-index |
| 112 | `compose/bsl-indexer/crates/code-index-core/src/lib.rs` | 1364 | vendor: BSL code-index |
| 113 | `compose/bsl-indexer/crates/code-index-core/src/logging.rs` | 46453 | vendor: BSL code-index |
| 114 | `compose/bsl-indexer/crates/code-index-core/src/mcp/cap.rs` | 56719 | vendor: BSL code-index |
| 115 | `compose/bsl-indexer/crates/code-index-core/src/mcp/config_watch.rs` | 26750 | vendor: BSL code-index |
| 116 | `compose/bsl-indexer/crates/code-index-core/src/mcp/mod.rs` | 156804 | vendor: BSL code-index |
| 117 | `compose/bsl-indexer/crates/code-index-core/src/mcp/tools.rs` | 162115 | vendor: BSL code-index |
| 118 | `compose/bsl-indexer/crates/code-index-core/src/parser/bsl.rs` | 32911 | vendor: BSL code-index |
| 119 | `compose/bsl-indexer/crates/code-index-core/src/parser/c.rs` | 12774 | vendor: BSL code-index |
| 120 | `compose/bsl-indexer/crates/code-index-core/src/parser/c_sharp.rs` | 15925 | vendor: BSL code-index |
| 121 | `compose/bsl-indexer/crates/code-index-core/src/parser/callee.rs` | 6241 | vendor: BSL code-index |
| 122 | `compose/bsl-indexer/crates/code-index-core/src/parser/cpp.rs` | 2097 | vendor: BSL code-index |
| 123 | `compose/bsl-indexer/crates/code-index-core/src/parser/go.rs` | 25406 | vendor: BSL code-index |
| 124 | `compose/bsl-indexer/crates/code-index-core/src/parser/html.rs` | 21573 | vendor: BSL code-index |
| 125 | `compose/bsl-indexer/crates/code-index-core/src/parser/java.rs` | 19396 | vendor: BSL code-index |
| 126 | `compose/bsl-indexer/crates/code-index-core/src/parser/javascript.rs` | 25340 | vendor: BSL code-index |
| 127 | `compose/bsl-indexer/crates/code-index-core/src/parser/mod.rs` | 13767 | vendor: BSL code-index |
| 128 | `compose/bsl-indexer/crates/code-index-core/src/parser/php.rs` | 19927 | vendor: BSL code-index |
| 129 | `compose/bsl-indexer/crates/code-index-core/src/parser/python.rs` | 25288 | vendor: BSL code-index |
| 130 | `compose/bsl-indexer/crates/code-index-core/src/parser/ruby.rs` | 14987 | vendor: BSL code-index |
| 131 | `compose/bsl-indexer/crates/code-index-core/src/parser/rust_lang.rs` | 32442 | vendor: BSL code-index |
| 132 | `compose/bsl-indexer/crates/code-index-core/src/parser/swift.rs` | 18099 | vendor: BSL code-index |
| 133 | `compose/bsl-indexer/crates/code-index-core/src/parser/text.rs` | 470 | vendor: BSL code-index |
| 134 | `compose/bsl-indexer/crates/code-index-core/src/parser/types.rs` | 5784 | vendor: BSL code-index |
| 135 | `compose/bsl-indexer/crates/code-index-core/src/parser/typescript.rs` | 25470 | vendor: BSL code-index |
| 136 | `compose/bsl-indexer/crates/code-index-core/src/parser/xml_1c.rs` | 31222 | vendor: BSL code-index |
| 137 | `compose/bsl-indexer/crates/code-index-core/src/serve_cache.rs` | 32656 | vendor: BSL code-index |
| 138 | `compose/bsl-indexer/crates/code-index-core/src/serve_dedup.rs` | 31222 | vendor: BSL code-index |
| 139 | `compose/bsl-indexer/crates/code-index-core/src/storage/memory.rs` | 9207 | vendor: BSL code-index |
| 140 | `compose/bsl-indexer/crates/code-index-core/src/storage/mod.rs` | 284028 | vendor: BSL code-index |
| 141 | `compose/bsl-indexer/crates/code-index-core/src/storage/models.rs` | 12825 | vendor: BSL code-index |
| 142 | `compose/bsl-indexer/crates/code-index-core/src/storage/pool.rs` | 19756 | vendor: BSL code-index |
| 143 | `compose/bsl-indexer/crates/code-index-core/src/storage/schema.rs` | 28584 | vendor: BSL code-index |
| 144 | `compose/bsl-indexer/crates/code-index-core/src/watcher.rs` | 30705 | vendor: BSL code-index |
| 145 | `compose/bsl-indexer/crates/code-index/Cargo.toml` | 1287 | vendor: BSL code-index |
| 146 | `compose/bsl-indexer/crates/code-index/build.rs` | 572 | vendor: BSL code-index |
| 147 | `compose/bsl-indexer/crates/code-index/src/main.rs` | 1676 | vendor: BSL code-index |
| 148 | `compose/bsl-indexer/deploy/docker/Dockerfile` | 1522 | vendor: BSL code-index |
| 149 | `compose/bsl-indexer/deploy/docker/README.md` | 5229 | vendor: BSL code-index |
| 150 | `compose/bsl-indexer/deploy/docker/daemon.toml` | 137 | vendor: BSL code-index |
| 151 | `compose/bsl-indexer/deploy/docker/docker-compose.yml` | 1588 | vendor: BSL code-index |
| 152 | `compose/bsl-indexer/deploy/docker/entrypoint.sh` | 2784 | vendor: BSL code-index |
| 153 | `compose/bsl-indexer/deploy/systemd/bsl-indexer-daemon.service` | 2681 | vendor: BSL code-index |
| 154 | `compose/bsl-indexer/docker-compose.yml` | 573 | vendor: BSL code-index |
| 155 | `compose/bsl-indexer/docs/PROGRAM_OVERVIEW.md` | 42865 | vendor: BSL code-index |
| 156 | `compose/bsl-indexer/docs/bench-summary-dark.svg` | 1784 | vendor: BSL code-index |
| 157 | `compose/bsl-indexer/docs/bench-summary-light.svg` | 1784 | vendor: BSL code-index |
| 158 | `compose/bsl-indexer/docs/bsl-indexer-architecture.md` | 42108 | vendor: BSL code-index |
| 159 | `compose/bsl-indexer/docs/bsl-indexer.md` | 15235 | vendor: BSL code-index |
| 160 | `compose/bsl-indexer/docs/operations.md` | 19716 | vendor: BSL code-index |
| 161 | `compose/bsl-indexer/docs/tz-search-results-omission.md` | 11609 | vendor: BSL code-index |
| 162 | `compose/bsl-indexer/install.ps1` | 13276 | vendor: BSL code-index |
| 163 | `compose/bsl-indexer/npm/README.md` | 1744 | vendor: BSL code-index |
| 164 | `compose/bsl-indexer/npm/bin/cli.js` | 1443 | vendor: BSL code-index |
| 165 | `compose/bsl-indexer/npm/package.json` | 1153 | vendor: BSL code-index |
| 166 | `compose/bsl-indexer/npm/scripts/postinstall.js` | 5212 | vendor: BSL code-index |
| 167 | `compose/bsl-indexer/scripts/install-daemon-autostart.ps1` | 6772 | vendor: BSL code-index |
| 168 | `compose/bsl-indexer/scripts/test_phase1_e2e.py` | 33271 | vendor: BSL code-index |
| 169 | `compose/bsl-indexer/server.json` | 1276 | vendor: BSL code-index |
| 170 | `compose/mcp_for_1c/.env.example` | 14885 | vendor: graph/query MCP |
| 171 | `compose/mcp_for_1c/.gitattributes` | 1975 | vendor: graph/query MCP |
| 172 | `compose/mcp_for_1c/.github/workflows/tests.yml` | 4807 | vendor: graph/query MCP |
| 173 | `compose/mcp_for_1c/.gitignore` | 2525 | vendor: graph/query MCP |
| 174 | `compose/mcp_for_1c/1c-mcp-suite/.env.example` | 754 | vendor: graph/query MCP |
| 175 | `compose/mcp_for_1c/1c-mcp-suite/.gitignore` | 5 | vendor: graph/query MCP |
| 176 | `compose/mcp_for_1c/1c-mcp-suite/Dockerfile.bsl` | 4756 | vendor: graph/query MCP |
| 177 | `compose/mcp_for_1c/1c-mcp-suite/Dockerfile.embeddings` | 5051 | vendor: graph/query MCP |
| 178 | `compose/mcp_for_1c/1c-mcp-suite/Dockerfile.python` | 8463 | vendor: graph/query MCP |
| 179 | `compose/mcp_for_1c/1c-mcp-suite/Dockerfile.v8std` | 2604 | vendor: graph/query MCP |
| 180 | `compose/mcp_for_1c/1c-mcp-suite/README.md` | 6352 | vendor: graph/query MCP |
| 181 | `compose/mcp_for_1c/1c-mcp-suite/graph_integrity.py` | 13442 | vendor: graph/query MCP |
| 182 | `compose/mcp_for_1c/1c-mcp-suite/mcp-bsl-checker/bsl_config.py` | 26355 | vendor: graph/query MCP |
| 183 | `compose/mcp_for_1c/1c-mcp-suite/mcp-bsl-checker/bsl_health.py` | 33513 | vendor: graph/query MCP |
| 184 | `compose/mcp_for_1c/1c-mcp-suite/mcp-bsl-checker/bsl_lsp.py` | 40346 | vendor: graph/query MCP |
| 185 | `compose/mcp_for_1c/1c-mcp-suite/mcp-bsl-checker/bsl_warmup.py` | 12023 | vendor: graph/query MCP |
| 186 | `compose/mcp_for_1c/1c-mcp-suite/mcp-bsl-checker/server.py` | 39343 | vendor: graph/query MCP |
| 187 | `compose/mcp_for_1c/1c-mcp-suite/mcp-bsl-checker/tests_bsl_config.py` | 26638 | vendor: graph/query MCP |
| 188 | `compose/mcp_for_1c/1c-mcp-suite/mcp-bsl-checker/tests_bsl_health.py` | 17290 | vendor: graph/query MCP |
| 189 | `compose/mcp_for_1c/1c-mcp-suite/mcp-bsl-checker/tests_bsl_lsp.py` | 32672 | vendor: graph/query MCP |
| 190 | `compose/mcp_for_1c/1c-mcp-suite/mcp-bsl-checker/tests_diagnostics_inventory.py` | 15114 | vendor: graph/query MCP |
| 191 | `compose/mcp_for_1c/1c-mcp-suite/mcp-bsl-checker/tests_warmup.py` | 15823 | vendor: graph/query MCP |
| 192 | `compose/mcp_for_1c/1c-mcp-suite/mcp-config-localhost.json` | 1391 | vendor: graph/query MCP |
| 193 | `compose/mcp_for_1c/1c-mcp-suite/mcp-config.json` | 919 | vendor: graph/query MCP |
| 194 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/README.md` | 24969 | vendor: graph/query MCP |
| 195 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/apply_changes.py` | 16881 | vendor: graph/query MCP |
| 196 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/bsl_parser.py` | 64773 | vendor: graph/query MCP |
| 197 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/bsl_resolver.py` | 106793 | vendor: graph/query MCP |
| 198 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/calibrate.py` | 3648 | vendor: graph/query MCP |
| 199 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/calibrate_inference.py` | 18274 | vendor: graph/query MCP |
| 200 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/graph_writer.py` | 88907 | vendor: graph/query MCP |
| 201 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/incremental.py` | 50793 | vendor: graph/query MCP |
| 202 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/indexer.py` | 33759 | vendor: graph/query MCP |
| 203 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/metadata_xml.py` | 47366 | vendor: graph/query MCP |
| 204 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/partial_fingerprint.py` | 24019 | vendor: graph/query MCP |
| 205 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/progress_log.py` | 8186 | vendor: graph/query MCP |
| 206 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/smoke_resolver.py` | 3070 | vendor: graph/query MCP |
| 207 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/tests_bsl_parser.py` | 58953 | vendor: graph/query MCP |
| 208 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/tests_bsl_resolver.py` | 67695 | vendor: graph/query MCP |
| 209 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/tests_fingerprint.py` | 43951 | vendor: graph/query MCP |
| 210 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/tests_fix8_kinds.py` | 7927 | vendor: graph/query MCP |
| 211 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/tests_graph_writer.py` | 36837 | vendor: graph/query MCP |
| 212 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/tests_incremental.py` | 53935 | vendor: graph/query MCP |
| 213 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/tests_indexer_phases.py` | 11942 | vendor: graph/query MCP |
| 214 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/tests_metadata_xml.py` | 28057 | vendor: graph/query MCP |
| 215 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/tests_ownership_integrity.py` | 11452 | vendor: graph/query MCP |
| 216 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/tests_partial_fingerprint.py` | 24600 | vendor: graph/query MCP |
| 217 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/tests_tally_wiring.py` | 8793 | vendor: graph/query MCP |
| 218 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/tests_write_counters.py` | 21745 | vendor: graph/query MCP |
| 219 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/what_changed.py` | 6586 | vendor: graph/query MCP |
| 220 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph/graph_state.py` | 13976 | vendor: graph/query MCP |
| 221 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph/search_fulltext.py` | 12358 | vendor: graph/query MCP |
| 222 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph/server.py` | 66285 | vendor: graph/query MCP |
| 223 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph/server_v3_code_tools.py` | 34203 | vendor: graph/query MCP |
| 224 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph/server_v3_tools.py` | 25617 | vendor: graph/query MCP |
| 225 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph/server_v3_watch_tools.py` | 9933 | vendor: graph/query MCP |
| 226 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph/subsystem_scope.py` | 7818 | vendor: graph/query MCP |
| 227 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph/tests_graph_contract.py` | 19425 | vendor: graph/query MCP |
| 228 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph/tests_graph_state.py` | 14799 | vendor: graph/query MCP |
| 229 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph/tests_search_fulltext.py` | 11303 | vendor: graph/query MCP |
| 230 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph/tests_stats_batch.py` | 27214 | vendor: graph/query MCP |
| 231 | `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph/tests_subsystem_scope.py` | 8357 | vendor: graph/query MCP |
| 232 | `compose/mcp_for_1c/1c-mcp-suite/mcp-platform-help-embeddings/hbk_chunker.py` | 14856 | vendor: graph/query MCP |
| 233 | `compose/mcp_for_1c/1c-mcp-suite/mcp-platform-help-embeddings/hbk_indexer.py` | 30387 | vendor: graph/query MCP |
| 234 | `compose/mcp_for_1c/1c-mcp-suite/mcp-platform-help-embeddings/hbk_parser.py` | 36439 | vendor: graph/query MCP |
| 235 | `compose/mcp_for_1c/1c-mcp-suite/mcp-platform-help-embeddings/hbk_reader.py` | 25962 | vendor: graph/query MCP |
| 236 | `compose/mcp_for_1c/1c-mcp-suite/mcp-platform-help-embeddings/tests_hbk_reader.py` | 18523 | vendor: graph/query MCP |
| 237 | `compose/mcp_for_1c/1c-mcp-suite/mcp-platform-help/help_ranking.py` | 16435 | vendor: graph/query MCP |
| 238 | `compose/mcp_for_1c/1c-mcp-suite/mcp-platform-help/model_warmup.py` | 24482 | vendor: graph/query MCP |
| 239 | `compose/mcp_for_1c/1c-mcp-suite/mcp-platform-help/server.py` | 78944 | vendor: graph/query MCP |
| 240 | `compose/mcp_for_1c/1c-mcp-suite/mcp-platform-help/tests_fail1_fast_fail.py` | 23427 | vendor: graph/query MCP |
| 241 | `compose/mcp_for_1c/1c-mcp-suite/mcp-platform-help/tests_help_ranking.py` | 20895 | vendor: graph/query MCP |
| 242 | `compose/mcp_for_1c/1c-mcp-suite/mcp-platform-help/tests_model_warmup.py` | 16639 | vendor: graph/query MCP |
| 243 | `compose/mcp_for_1c/1c-mcp-suite/mcp-query-builder/calibrate_query.py` | 8462 | vendor: graph/query MCP |
| 244 | `compose/mcp_for_1c/1c-mcp-suite/mcp-query-builder/query_check.py` | 31783 | vendor: graph/query MCP |
| 245 | `compose/mcp_for_1c/1c-mcp-suite/mcp-query-builder/query_optimize_rules.py` | 7989 | vendor: graph/query MCP |
| 246 | `compose/mcp_for_1c/1c-mcp-suite/mcp-query-builder/query_parser.py` | 30115 | vendor: graph/query MCP |
| 247 | `compose/mcp_for_1c/1c-mcp-suite/mcp-query-builder/server.py` | 40317 | vendor: graph/query MCP |
| 248 | `compose/mcp_for_1c/1c-mcp-suite/mcp-query-builder/tests_query_parser.py` | 39568 | vendor: graph/query MCP |
| 249 | `compose/mcp_for_1c/1c-mcp-suite/mcp-query-builder/tests_tool2_fields.py` | 14350 | vendor: graph/query MCP |
| 250 | `compose/mcp_for_1c/1c-mcp-suite/mcp_auth.py` | 13700 | vendor: graph/query MCP |
| 251 | `compose/mcp_for_1c/1c-mcp-suite/mcp_cache.py` | 11757 | vendor: graph/query MCP |
| 252 | `compose/mcp_for_1c/1c-mcp-suite/mcp_http.py` | 7240 | vendor: graph/query MCP |
| 253 | `compose/mcp_for_1c/1c-mcp-suite/mcp_metrics.py` | 17173 | vendor: graph/query MCP |
| 254 | `compose/mcp_for_1c/1c-mcp-suite/mcp_pagination.py` | 10972 | vendor: graph/query MCP |
| 255 | `compose/mcp_for_1c/1c-mcp-suite/mcp_tool_filter.py` | 12201 | vendor: graph/query MCP |
| 256 | `compose/mcp_for_1c/1c-mcp-suite/refusal.py` | 13266 | vendor: graph/query MCP |
| 257 | `compose/mcp_for_1c/1c-mcp-suite/requirements-bsl.lock.txt` | 2139 | vendor: graph/query MCP |
| 258 | `compose/mcp_for_1c/1c-mcp-suite/requirements-bsl.txt` | 763 | vendor: graph/query MCP |
| 259 | `compose/mcp_for_1c/1c-mcp-suite/requirements-embeddings.lock.txt` | 4776 | vendor: graph/query MCP |
| 260 | `compose/mcp_for_1c/1c-mcp-suite/requirements-embeddings.txt` | 6118 | vendor: graph/query MCP |
| 261 | `compose/mcp_for_1c/1c-mcp-suite/requirements-v8std.lock.txt` | 1756 | vendor: graph/query MCP |
| 262 | `compose/mcp_for_1c/1c-mcp-suite/requirements-v8std.txt` | 888 | vendor: graph/query MCP |
| 263 | `compose/mcp_for_1c/1c-mcp-suite/requirements.lock.txt` | 2091 | vendor: graph/query MCP |
| 264 | `compose/mcp_for_1c/1c-mcp-suite/requirements.txt` | 1676 | vendor: graph/query MCP |
| 265 | `compose/mcp_for_1c/1c-mcp-suite/shortfall.py` | 15539 | vendor: graph/query MCP |
| 266 | `compose/mcp_for_1c/1c-mcp-suite/start.py` | 9723 | vendor: graph/query MCP |
| 267 | `compose/mcp_for_1c/1c-mcp-suite/tests_delivery.py` | 13957 | vendor: graph/query MCP |
| 268 | `compose/mcp_for_1c/1c-mcp-suite/tests_graph_integrity.py` | 9754 | vendor: graph/query MCP |
| 269 | `compose/mcp_for_1c/1c-mcp-suite/tests_lazy_singletons.py` | 16824 | vendor: graph/query MCP |
| 270 | `compose/mcp_for_1c/1c-mcp-suite/tests_pagination.py` | 11789 | vendor: graph/query MCP |
| 271 | `compose/mcp_for_1c/1c-mcp-suite/tests_refusal.py` | 10922 | vendor: graph/query MCP |
| 272 | `compose/mcp_for_1c/1c-mcp-suite/tests_shortfall.py` | 10883 | vendor: graph/query MCP |
| 273 | `compose/mcp_for_1c/1c-mcp-suite/tests_tool_filter.py` | 13858 | vendor: graph/query MCP |
| 274 | `compose/mcp_for_1c/1c-mcp-suite/tests_tool_usage.py` | 10134 | vendor: graph/query MCP |
| 275 | `compose/mcp_for_1c/1c-mcp-suite/tool_journal.py` | 9482 | vendor: graph/query MCP |
| 276 | `compose/mcp_for_1c/1c-mcp-suite/tool_usage.py` | 9681 | vendor: graph/query MCP |
| 277 | `compose/mcp_for_1c/1c-mcp-suite/v8std_entrypoint.py` | 7819 | vendor: graph/query MCP |
| 278 | `compose/mcp_for_1c/1c-mcp-suite/workspace_watcher.py` | 25693 | vendor: graph/query MCP |
| 279 | `compose/mcp_for_1c/ARCHITECTURE.md` | 3448 | vendor: graph/query MCP |
| 280 | `compose/mcp_for_1c/LICENSE` | 1068 | vendor: graph/query MCP |
| 281 | `compose/mcp_for_1c/Makefile` | 6215 | vendor: graph/query MCP |
| 282 | `compose/mcp_for_1c/README.md` | 55145 | vendor: graph/query MCP |
| 283 | `compose/mcp_for_1c/bsl-config/README.md` | 4486 | vendor: graph/query MCP |
| 284 | `compose/mcp_for_1c/bsl-config/bsl-language-server.json` | 3400 | vendor: graph/query MCP |
| 285 | `compose/mcp_for_1c/docker-compose.override.yml` | 2638 | vendor: graph/query MCP |
| 286 | `compose/mcp_for_1c/docker-compose.webui.yml` | 657 | vendor: graph/query MCP |
| 287 | `compose/mcp_for_1c/docker-compose.yml` | 32652 | vendor: graph/query MCP |
| 288 | `compose/mcp_for_1c/docs/ОШИБКИ-РАЗВЁРТЫВАНИЯ.md` | 5880 | vendor: graph/query MCP |
| 289 | `compose/mcp_for_1c/docs/РАЗВЁРТЫВАНИЕ.md` | 4353 | vendor: graph/query MCP |
| 290 | `compose/mcp_for_1c/evals/baseline.json` | 1579 | vendor: graph/query MCP |
| 291 | `compose/mcp_for_1c/evals/datasets/README.md` | 3722 | vendor: graph/query MCP |
| 292 | `compose/mcp_for_1c/evals/datasets/bsl_checker.jsonl` | 14158 | vendor: graph/query MCP |
| 293 | `compose/mcp_for_1c/evals/datasets/metadata_graph.jsonl` | 10858 | vendor: graph/query MCP |
| 294 | `compose/mcp_for_1c/evals/datasets/platform_help.jsonl` | 24318 | vendor: graph/query MCP |
| 295 | `compose/mcp_for_1c/evals/datasets/probe.jsonl` | 447 | vendor: graph/query MCP |
| 296 | `compose/mcp_for_1c/evals/datasets/query_builder.jsonl` | 22025 | vendor: graph/query MCP |
| 297 | `compose/mcp_for_1c/evals/datasets/v8std.jsonl` | 11777 | vendor: graph/query MCP |
| 298 | `compose/mcp_for_1c/evals/journal/README.md` | 1591 | vendor: graph/query MCP |
| 299 | `compose/mcp_for_1c/evals/manual-prompts.md` | 25933 | vendor: graph/query MCP |
| 300 | `compose/mcp_for_1c/evals/reports/.gitkeep` | 0 | vendor: graph/query MCP |
| 301 | `compose/mcp_for_1c/evals/runner/Dockerfile` | 1129 | vendor: graph/query MCP |
| 302 | `compose/mcp_for_1c/evals/runner/mcp_client.py` | 5312 | vendor: graph/query MCP |
| 303 | `compose/mcp_for_1c/evals/runner/metrics.py` | 3987 | vendor: graph/query MCP |
| 304 | `compose/mcp_for_1c/evals/runner/predicates.py` | 23263 | vendor: graph/query MCP |
| 305 | `compose/mcp_for_1c/evals/runner/report.py` | 7987 | vendor: graph/query MCP |
| 306 | `compose/mcp_for_1c/evals/runner/requirements.txt` | 1276 | vendor: graph/query MCP |
| 307 | `compose/mcp_for_1c/evals/runner/run_eval.py` | 12900 | vendor: graph/query MCP |
| 308 | `compose/mcp_for_1c/evals/runner/tests.py` | 23741 | vendor: graph/query MCP |
| 309 | `compose/mcp_for_1c/evals/schema.md` | 9294 | vendor: graph/query MCP |
| 310 | `compose/mcp_for_1c/platform-help-data/.gitkeep` | 0 | vendor: graph/query MCP |
| 311 | `compose/mcp_for_1c/platform-help-data/README.md` | 1205 | vendor: graph/query MCP |
| 312 | `compose/mcp_for_1c/scripts/archive_docs.py` | 13634 | vendor: graph/query MCP |
| 313 | `compose/mcp_for_1c/scripts/check_baseline.py` | 10193 | vendor: graph/query MCP |
| 314 | `compose/mcp_for_1c/scripts/check_prereqs.py` | 32015 | vendor: graph/query MCP |
| 315 | `compose/mcp_for_1c/scripts/check_publish.py` | 21246 | vendor: graph/query MCP |
| 316 | `compose/mcp_for_1c/scripts/eval.py` | 15894 | vendor: graph/query MCP |
| 317 | `compose/mcp_for_1c/scripts/eval_all.py` | 17449 | vendor: graph/query MCP |
| 318 | `compose/mcp_for_1c/scripts/fetch_v8std.py` | 11456 | vendor: graph/query MCP |
| 319 | `compose/mcp_for_1c/scripts/gen_archive_index.py` | 7876 | vendor: graph/query MCP |
| 320 | `compose/mcp_for_1c/scripts/gen_lockfiles.ps1` | 6651 | vendor: graph/query MCP |
| 321 | `compose/mcp_for_1c/scripts/gen_lockfiles.sh` | 4337 | vendor: graph/query MCP |
| 322 | `compose/mcp_for_1c/scripts/journal_report.py` | 16508 | vendor: graph/query MCP |
| 323 | `compose/mcp_for_1c/scripts/list_tools.py` | 2083 | vendor: graph/query MCP |
| 324 | `compose/mcp_for_1c/scripts/probe_hbk.py` | 8920 | vendor: graph/query MCP |
| 325 | `compose/mcp_for_1c/scripts/run_all_tests.ps1` | 998 | vendor: graph/query MCP |
| 326 | `compose/mcp_for_1c/scripts/run_all_tests.py` | 30044 | vendor: graph/query MCP |
| 327 | `compose/mcp_for_1c/scripts/smoke_zahod1.sh` | 7723 | vendor: graph/query MCP |
| 328 | `compose/mcp_for_1c/scripts/soft_misses.py` | 6349 | vendor: graph/query MCP |
| 329 | `compose/mcp_for_1c/scripts/tests_client_configs.py` | 4238 | vendor: graph/query MCP |
| 330 | `compose/mcp_for_1c/scripts/tests_commands_cross_platform.py` | 11970 | vendor: graph/query MCP |
| 331 | `compose/mcp_for_1c/scripts/tests_docs_canon.py` | 4855 | vendor: graph/query MCP |
| 332 | `compose/mcp_for_1c/scripts/tests_eval_all_freshness.py` | 8151 | vendor: graph/query MCP |
| 333 | `compose/mcp_for_1c/scripts/tests_eval_routing.py` | 7669 | vendor: graph/query MCP |
| 334 | `compose/mcp_for_1c/scripts/tests_host_scripts.py` | 21569 | vendor: graph/query MCP |
| 335 | `compose/mcp_for_1c/scripts/tests_journal_report.py` | 16453 | vendor: graph/query MCP |
| 336 | `compose/mcp_for_1c/scripts/tests_lockfile_pairs.py` | 13852 | vendor: graph/query MCP |
| 337 | `compose/mcp_for_1c/scripts/tests_manual_prompts.py` | 10490 | vendor: graph/query MCP |
| 338 | `compose/mcp_for_1c/scripts/tests_publish.py` | 6068 | vendor: graph/query MCP |
| 339 | `compose/mcp_for_1c/setup-deps.ps1` | 14481 | vendor: graph/query MCP |
| 340 | `compose/mcp_for_1c/workspace/.gitkeep` | 0 | vendor: graph/query MCP |
| 341 | `compose/mcp_for_1c/workspace/AGENTS.md` | 1931 | vendor: graph/query MCP |
| 342 | `contour.example.yaml` | 2231 | корень комплекта |
| 343 | `controller/pyproject.toml` | 441 | оркестратор (пакет) |
| 344 | `controller/src/contour/__init__.py` | 62 | оркестратор (код) |
| 345 | `controller/src/contour/adapters.py` | 4290 | оркестратор (код) |
| 346 | `controller/src/contour/alert.py` | 1714 | оркестратор (код) |
| 347 | `controller/src/contour/cli.py` | 8376 | оркестратор (код) |
| 348 | `controller/src/contour/config.py` | 7911 | оркестратор (код) |
| 349 | `controller/src/contour/db.py` | 1896 | оркестратор (код) |
| 350 | `controller/src/contour/dockerutil.py` | 11406 | оркестратор (код) |
| 351 | `controller/src/contour/gitutil.py` | 1567 | оркестратор (код) |
| 352 | `controller/src/contour/index_bsl.py` | 2711 | оркестратор (код) |
| 353 | `controller/src/contour/index_graph.py` | 7164 | оркестратор (код) |
| 354 | `controller/src/contour/lock.py` | 1980 | оркестратор (код) |
| 355 | `controller/src/contour/reads.py` | 4379 | оркестратор (код) |
| 356 | `controller/src/contour/reconcile.py` | 32822 | оркестратор (код) |
| 357 | `controller/src/contour/schedule.py` | 2681 | оркестратор (код) |
| 358 | `controller/src/contour/verify.py` | 5432 | оркестратор (код) |
| 359 | `controller/src/contour/watch_bsl.py` | 2232 | оркестратор (код) |
| 360 | `controller/tests/test_cli.py` | 9458 | оркестратор (тесты) |
| 361 | `controller/tests/test_config.py` | 1354 | оркестратор (тесты) |
| 362 | `controller/tests/test_db.py` | 4001 | оркестратор (тесты) |
| 363 | `controller/tests/test_dockerutil.py` | 5300 | оркестратор (тесты) |
| 364 | `controller/tests/test_gitutil.py` | 3897 | оркестратор (тесты) |
| 365 | `controller/tests/test_index_graph.py` | 13134 | оркестратор (тесты) |
| 366 | `controller/tests/test_lock.py` | 626 | оркестратор (тесты) |
| 367 | `controller/tests/test_reads.py` | 12458 | оркестратор (тесты) |
| 368 | `controller/tests/test_reconcile.py` | 36919 | оркестратор (тесты) |
| 369 | `controller/tests/test_schedule.py` | 1860 | оркестратор (тесты) |
| 370 | `controller/tests/test_verify.py` | 5618 | оркестратор (тесты) |
| 371 | `controller/tests/test_watch_bsl.py` | 2866 | оркестратор (тесты) |
| 372 | `deploy-ai2/README.md` | 2668 | overlays ai2 |
| 373 | `deploy-ai2/ai-stack/.env.example` | 287 | overlays ai2 |
| 374 | `deploy-ai2/ai-stack/README.md` | 3129 | overlays ai2 |
| 375 | `deploy-ai2/ai-stack/docker-compose.yml` | 2378 | overlays ai2 |
| 376 | `deploy-ai2/bsl-indexer/DEPRECATED.md` | 250 | overlays ai2 |
| 377 | `deploy-ai2/bsl-indexer/daemon.toml` | 128 | overlays ai2 |
| 378 | `deploy-ai2/bsl-indexer/docker-compose.yml` | 621 | overlays ai2 |
| 379 | `deploy-ai2/bsl-kb/README.md` | 371 | overlays ai2 |
| 380 | `deploy-ai2/bsl-kb/daemon.toml` | 127 | overlays ai2 |
| 381 | `deploy-ai2/bsl-kb/docker-compose.yml` | 593 | overlays ai2 |
| 382 | `deploy-ai2/bsl-klon-opl/README.md` | 519 | overlays ai2 |
| 383 | `deploy-ai2/bsl-klon-opl/daemon.toml` | 133 | overlays ai2 |
| 384 | `deploy-ai2/bsl-klon-opl/docker-compose.yml` | 629 | overlays ai2 |
| 385 | `deploy-ai2/bsl-ubd/README.md` | 405 | overlays ai2 |
| 386 | `deploy-ai2/bsl-ubd/daemon.toml` | 128 | overlays ai2 |
| 387 | `deploy-ai2/bsl-ubd/docker-compose.yml` | 599 | overlays ai2 |
| 388 | `deploy-ai2/check-neo4j-auth.sh` | 382 | overlays ai2 |
| 389 | `deploy-ai2/mcp_for_1c-kb/.env.example` | 503 | overlays ai2 |
| 390 | `deploy-ai2/mcp_for_1c-kb/README.md` | 1297 | overlays ai2 |
| 391 | `deploy-ai2/mcp_for_1c-kb/docker-compose.ai2.yml` | 967 | overlays ai2 |
| 392 | `deploy-ai2/mcp_for_1c-kb/docker-compose.override.yml` | 1715 | overlays ai2 |
| 393 | `deploy-ai2/mcp_for_1c-ubd/.env.example` | 508 | overlays ai2 |
| 394 | `deploy-ai2/mcp_for_1c-ubd/README.md` | 1540 | overlays ai2 |
| 395 | `deploy-ai2/mcp_for_1c-ubd/docker-compose.ai2.yml` | 1051 | overlays ai2 |
| 396 | `deploy-ai2/mcp_for_1c-ubd/docker-compose.override.yml` | 1848 | overlays ai2 |
| 397 | `deploy-ai2/mcp_for_1c/DEPRECATED.md` | 598 | overlays ai2 |
| 398 | `deploy-ai2/mcp_for_1c/docker-compose.ai2.yml` | 881 | overlays ai2 |
| 399 | `deploy-ai2/mcp_for_1c/docker-compose.override.yml` | 1432 | overlays ai2 |
| 400 | `deploy-ai2/run-bsl-index.sh` | 144 | overlays ai2 |
| 401 | `integrations/alert.sh.example` | 433 | адаптеры/probes |
| 402 | `integrations/idle_bsl.sh` | 957 | адаптеры/probes |
| 403 | `integrations/probe_code_index.sh` | 1867 | адаптеры/probes |
| 404 | `integrations/probe_graph.sh` | 5666 | адаптеры/probes |
| 405 | `integrations/reload_bsl_watch.sh` | 836 | адаптеры/probes |
| 406 | `integrations/skills/cursor/1c-analyst.md` | 4100 | skills markdown |
| 407 | `integrations/skills/cursor/1c-code-index.md` | 2875 | skills markdown |
| 408 | `integrations/skills/cursor/1c-confluence.md` | 2185 | skills markdown |
| 409 | `integrations/skills/cursor/1c-docs.md` | 2530 | skills markdown |
| 410 | `integrations/skills/cursor/1c-mdd.md` | 6972 | skills markdown |
| 411 | `integrations/skills/cursor/1c-sdms.md` | 2684 | skills markdown |
| 412 | `integrations/skills/cursor/1c-ubd-graph.md` | 3269 | skills markdown |
| 413 | `integrations/skills/webui/1c-docs.md` | 2404 | skills markdown |
| 414 | `integrations/skills/webui/code-index.md` | 3129 | skills markdown |
| 415 | `integrations/skills/webui/confluence.md` | 2586 | skills markdown |
| 416 | `integrations/skills/webui/klon-opl-code-index.md` | 1080 | skills markdown |
| 417 | `integrations/skills/webui/mdd.md` | 8121 | skills markdown |
| 418 | `integrations/skills/webui/query-builder.md` | 3336 | skills markdown |
| 419 | `integrations/skills/webui/sa-1c.md` | 3429 | skills markdown |
| 420 | `integrations/skills/webui/sdms.md` | 4140 | skills markdown |
| 421 | `integrations/skills/webui/ubd-graph.md` | 3667 | skills markdown |
| 422 | `release-manifest.json` | 1509 | корень комплекта |
| 423 | `requirements.lock` | 77 | корень комплекта |
| 424 | `secrets.example.yaml` | 432 | корень комплекта |
| 425 | `systemd/ai-sync.service` | 255 | systemd units |
| 426 | `systemd/ai-sync.timer` | 196 | systemd units |
| 427 | `to-do.txt` | 870 | корень комплекта |
| 428 | `tools/README.md` | 706 | выгрузка КБ/klon_opl |
| 429 | `tools/copy-kit-artifacts.py` | 4538 | выгрузка КБ/klon_opl |
| 430 | `tools/kb/README.md` | 1626 | выгрузка КБ/klon_opl |
| 431 | `tools/kb/Update-And-Export-Kb.ps1` | 11691 | выгрузка КБ/klon_opl |
| 432 | `tools/kb/kb-connection.json` | 447 | выгрузка КБ/klon_opl |
| 433 | `tools/klon_opl/Export-KlonOpl.ps1` | 5641 | выгрузка КБ/klon_opl |
| 434 | `tools/klon_opl/README.md` | 1756 | выгрузка КБ/klon_opl |
| 435 | `tools/klon_opl/Register-NightlyTask.ps1` | 575 | выгрузка КБ/klon_opl |
| 436 | `tools/klon_opl/klon_opl-connection.json` | 348 | выгрузка КБ/klon_opl |

---

## 7. Материалы для углублённого аудита

| Документ | Зачем читать |
| --- | --- |
| `AGENT.md` | Контракт продукта, reconcile-инварианты, запреты |
| `USER.md` | Установка, эксплуатация, cutover, УБД+КБ |
| `DEVELOPMENT.md` | Журнал решений и фактическое состояние стенда |
| `release-manifest.json` | Состав present/missing комплекта |
| `checks/` | Stand gates до cutover |
| `deploy-ai2/README.md` | Name map ai2, порты, volumes |
| `compose/mcp_for_1c/ARCHITECTURE.md` | Архитектура graph/query suite |
| `compose/mcp_for_1c/docs/` | Развёртывание и типовые ошибки |
| `compose/bsl-indexer/docs/` | Code-index / operations |
| `controller/tests/` | Unit-тесты оркестратора (пакет; не stand proof) |

---

## 8. Риски / открытые вопросы для аудитора

1. **Два режима эксплуатации:** пакетный `contour` готов в коде, но prod всё ещё на PowerShell — риск расхождения контрактов.
2. **Cutover не выполнен:** нет drill-доказанного отката томов чата.
3. **ai2 рядом с prod:** требует дисциплины сетей/DNS (уже был инцидент с `1c-suite-net` / алиасом `neo4j`).
4. **УБД на DrvFs vs ext4:** целевое требование — ext4; живой клон исторически на `C:\MCP`.
5. **Секреты:** примеры в комплекте; живые `.env` не должны уезжать в audit-пакет как есть.
6. **Vendor-объём:** `compose/mcp_for_1c` и `compose/bsl-indexer` — полные upstream-деревья; аудит оркестратора и аудит MCP-suite — разные плоскости.

---

*Сгенерировано для технического аудита. Секреты и содержимое баз/выгрузок в документ не включены.*
