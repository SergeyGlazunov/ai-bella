# To-do: стенд → стабильные ночные индексы → (позже) linux-first

**Дата:** 2026-10-12  
**Контекст:** живой стенд ADM-SAPHX-TECH; ночные индексы УБД/КБ/OPL не доводятся до «ок»; дневной FULL УБД (catch-up) положил Docker/WSL и Open WebUI.  
**Канон runtime:** ai2 (`/srv/ai2/...`, порты чата без rename).  
**Не смешивать** с cutover на `contour`/systemd, пока нет зелёной ночи на текущем PS/ai2-пути.

Связанные docs:
- `docs/superpowers/specs/2026-10-12-ai2-nightly-consistency-design.md` (короткий горизонт)
- `For Cursor/LINUX-FIRST-*.md` (длинный горизонт, после стабилизации)

---

## 0. Факт по стенду (срез 12.10)

| База | Выгрузка | Graph | Code |
|------|----------|-------|------|
| **УБД** | git свежий; copy в `/srv/ai2/ubd/src` сделан утром | FULL нужен (~81% дельта); дневной FULL → timeout Neo4j / завис Docker | catch-up index+clean **успел** |
| **КБ** | store+dump ночью ок; src на linux обновляется | полной индексации после dump нет (задача exit≠0, нет успешных `graph-reindex-*`) | не вшит как обязательный шаг |
| **klon_opl** | dump+copy ок (без store — by design) | нет | не гарантирован в том же exit task |
| **Чат** | Open WebUI `:3000` восстановлен после рестарта WSL/Docker | — | — |

**Жёсткое правило:** тяжёлый FULL УБД / массовый catch-up графа — **только в ночном окне** (или при явном «можно ронять чат»). Днём — только подготовка и лёгкие проверки.

---

## 1. Подготовительные шаги — можно делать сейчас (без FULL)

### 1.1. Репозиторий и артефакты (безопасно)

- [x] Pre-git cleanup: расширен `.gitignore`; удалены caches/`.7z`/deprecated `deploy-ai2/mcp_for_1c`+`bsl-indexer`; live `.env`/secrets/connection оставлены на диске.
- [x] `git init` на `C:\AI-contour` (ветка `master`).
- [x] Первый commit (initial import, ~440 files). Remote ещё не привязан.
- [x] Не коммитить: `.env`, `*-connection.json`, `logs/`, `workdir/`, дампы, SQLite — закрыто `.gitignore`.
- [ ] Зафиксировать в `STATUS.md` (или журнал): порты чата `:3000` WebUI / `:8080` = MDD MCP (не путать).

### 1.2. Инвентаризация целей индексов (read-only)

- [ ] Таблица live-имён: graph/code контейнеры и сети для УБД/КБ/OPL (ai2-*), пути `/srv/ai2/{ubd,kb,klon_opl}/src`.
- [ ] Подтвердить: nightly УБД всё ещё смотрит на `C:\MCP\ubd_config` + старый `bsl-indexer` / compose-WSL, а MCP читает `/srv/ai2/ubd/src` + `ai2-bsl-indexer`.
- [ ] Список Task Scheduler: `UBD Config Sync`, `AI-contour KB…`, `klon_opl…`, `WSL Docker Engine` — кто под какой УЗ, LastResult.
- [ ] Не трогать prod teardown, пока нет зелёного catch-up ai2.

### 1.3. Код скриптов (можно править днём)

Цель: ночь сама отрабатывает без ручного FULL в рабочее время.

**Общее**

- [ ] `tools/common/` (или рядом): copy-to-linux без хрупкого `docker cp` на 65k файлов где возможно; `docker run` indexer; `docker exec` bsl; логирование; EAP так, чтобы stderr docker не ронял job.
- [ ] Preflight: PASS по docker-run path + живым ai2-именам; **не** требовать WSL compose.
- [ ] Жёсткий fail, если обязательный слой (graph/code) skipped.

**УБД**

- [x] UBD scripts → ai2 (`ai2-bsl-indexer` / `ai2-metadata-indexer` / `ai2-neo4j`) + copy git→`/srv/ai2/ubd/src`; hard-fail code-index.
- [x] Убрать зависимость nightly от `bsl-indexer` / prod `neo4j` (в lib/sync/preflight).
- [ ] Лимиты/таймауты Neo4j для FULL; не стартовать FULL, если чат-окно / мало RAM (или явный флаг `-ForceFull`).
- [ ] После успеха — обновлять state только по слоям; не писать «ok» при skip.

**КБ**

- [x] КБ: graph full + code (`ai2-bsl-kb`); copy через EAP-safe helper; exit≠0 при fail.
- [x] Root cause KB exit 1 (13.10): `docker inspect` в graph-prepare под Stop → NativeCommandError до Tee-Object; fixed Continue/`Invoke-DockerNative` + early log + `DOCKER_HOST`.

**klon_opl**

- [x] После copy: обязательный code (`ai2-bsl-klon-opl`).

### 1.4. Операционные предохранители (сейчас)

- [ ] Явный запрет в runbook: дневной FULL УБД без согласия.
- [ ] Скрипт/заметка recover DockerWSL (уже был опыт: VHD sharing violation → elevated kill/restart + portproxy).
- [ ] Убедиться, что `ubd-catchup-*` контейнеров нет; автозапуск catch-up не висит.
- [ ] Лёгкий smoke: `:3000` WebUI 200/редирект; MCP `:8001`/`:8011` отвечают (без полной переиндексации).

### 1.5. Что сознательно отложить

- Cutover на `contour` / systemd / inbox packages (linux-first Tasks 2–12).
- Prod empty (stop+rm `neo4j` и т.п.) — после зелёной ночи ai2.
- Rename портов на `:8101`/`:8111`.
- Дневной FULL УБД / массовый graph catch-up КБ+УБД.

---

## 2. Ближайшая ночь / окно обслуживания (когда можно грузить граф)

Порядок осторожный: сначала лёгкое, FULL УБД — с запасом по времени и с мониторингом чата.

1. [ ] Preflight ai2 (docker API, neo4j healthy, контейнеры code up).
2. [ ] УБД: убедиться src = текущий git HEAD на `/srv/ai2/ubd/src` (докопировать, если ночной pull ушёл вперёд).
3. [ ] УБД code: быстрый incremental (если утренний catch-up ещё валиден — можно skip).
4. [ ] УБД graph: `apply_changes`; при force → **FULL** (ожидать долго; не параллелить второй тяжёлый writer).
5. [ ] КБ: graph full + code от текущего `/srv/ai2/kb/src`.
6. [ ] OPL: code от `/srv/ai2/klon_opl/src`.
7. [ ] Verify: fingerprint/mtime/SHA; smoke MCP; WebUI `:3000` жив.
8. [ ] Зафиксировать результат в журнале / `STATUS.md`.

Если FULL снова валит Docker — **стоп**, восстановить чат, не крутить повтор в ту же ночь без разбора (OOM/timeout Neo4j).

---

## 3. После зелёной ночи (короткий горизонт закрыт)

- [x] `UBD Config Sync` зарегистрирован (Glazunov.SP, Interactive/Limited, 02:00 → sync-and-index.ps1); scripts+bootstrap на стенде синхронизированы (13.10).
- [ ] Prod УБД teardown (вариант B: stop+rm контейнеры, тома backup).
- [ ] Наблюдение 2–3 ночи без ручного catch-up.

---

## 4. Дальше — linux-first (только после §3)

См. `LINUX-FIRST-IMPLEMENTATION-PLAN.md`, начиная с Task 1 (git clean), **не** со стендового cutover.

Подготовка к нему заранее (можно параллельно с §1, без cutover):

- [x] Git remote `ai-bella` + branch `chore/pre-linux-migration-cleanup` (Task 1 docs).
- [x] Cleanup essentials already on main; Task 1 residual: STATUS/ROADMAP/VENDOR/manifest.
- [x] `STATUS.md` / `ROADMAP.md` / `VENDOR.md` — два горизонта.

---

## 5. Подготовительные шаги — приоритет «прямо сейчас»

Имеет смысл делать **сегодня/завтра днём**, пока FULL спит:

1. Инвентаризация имён/путей/задач (§1.2) → одна таблица в `STATUS.md` или здесь в appendix.
2. Правки скриптов УБД/КБ/OPL под ai2 + docker-run + hard-fail (§1.3) — **без запуска FULL**.
3. Починить KB nightly fail path (логирование шагов) и OPL code step.
4. УБД: ускорить/устойчивее сделать copy git→`/srv/ai2/ubd/src` (утро ушло ~24 мин на `docker cp`).
5. Предохранители: запрет дневного FULL, recover notes, smoke `:3000`.
6. Git на комплекте + не коммитить секреты.

**Не сейчас:** FULL УБД, teardown prod, contour timer, inbox packages.

---

## 6. Критерий «готовы к ночному прогону»

- [ ] Скрипты указывают на ai2 paths/containers.
- [ ] Preflight зелёный без WSL compose.
- [ ] WebUI `:3000` стабилен после последних рестартов.
- [ ] Есть runbook: «если Docker клинит — recover; FULL только ночью».
- [ ] Оператор понимает: первая ночь с FULL УБД может быть длинной и рискованной для чата — мониторинг обязателен.
