# AI-Contour — журнал разработки

Для следующего агента: контракт продукта — [docs/superpowers/specs/2026-10-18-installable-contour-design.md](docs/superpowers/specs/2026-10-18-installable-contour-design.md) (вектор и алгоритм правки). Затем этот журнал и [AGENT.md](AGENT.md). История чата не нужна. Журнал не отменяет контракт.

Эксплуатация и установка — [USER.md](USER.md). Секреты, полные логи и переписку сюда не писать.

Правила ведения журнала: [AGENT.md](AGENT.md) §9.

---

## Сводка (2026-09-21, F01–F05 закрыты в пакете)

**Стенд:** живой контур **не** переключён. Stand gates watch pause + close reads — **PASS**. Cutover ждёт drill бэкапа чат-слоя / явный runbook в [USER.md](USER.md).

**Пакет:** F01–F05 (+ края R04/reopen/alert/argv) в коде; `python -m pytest` в `controller/` — **104 passed**. Firewall 8011 optional. Docs: [AGENT.md](AGENT.md) / [USER.md](USER.md). R08 архив — отдельно.

| Что | Где / как сейчас |
| --- | --- |
| Комплект ТЗ / будущий код | `C:\AI-contour` |
| Текущие скрипты развёртывания | `C:\instructions-main-system_analytics\system_analytics` |
| Рабочая копия ночного sync | `C:\MCP\ubd_index\scripts\sync-and-index.ps1` |
| Клон УБД | `C:\MCP\ubd_config` (git `main`; внутри `src` с `Configuration.xml`) |
| Стек графа | compose project `mcp_for_1c`, `C:\MCP\mcp_for_1c`, том `mcp_for_1c_neo4j-data` |
| Code-index | project `bsl-indexer`, `C:\MCP\code_index\deploy`, том `bsl-indexer_bsl-indexer-home`, bind `/mnt/c/MCP/ubd_config/src` → `/repos/SRC` |
| Open WebUI + OmniRoute | project `ai-stack`, `C:\ai-stack`, тома `ai-stack_open-webui-data`, `ai-stack_omniroute-data`; секреты `C:\ai-stack\.env` |
| Ночное обновление | задача Планировщика `UBD Config Sync` (учётка `ai-docker`): git pull → дельта BSL → `apply_changes.py` / FULL графа по коду 3 |
| Состояние sync | `C:\MCP\ubd_index\state\sync-state.json` (`last_ok_*` на SHA выгрузки) |
| Docker | Engine в WSL Ubuntu, `DOCKER_HOST=tcp://127.0.0.1:2375`, автостарт `WSL Docker Engine` → `C:\ProgramData\DockerWSL\start.ps1` |

**Ограничения и незавершённое**

- Пакет оркестратора лежит в `controller/`, **не** установлен на стенд; sync по-прежнему PowerShell + Планировщик.
- Stand: watch pause **PASS**; close reads **PASS** (firewall 8011 optional / skipped). Probes/idle/reload — PASS.
- В образе `bsl-indexer` нет `pgrep` — process probes через `docker top`. Git Bash: `MSYS_NO_PATHCONV=1`.
- `alert.sh.example` — stub; validate отвергает без замены канала.
- Согласованные копии томов WebUI/OmniRoute **не** сделаны. R08 архив — отдельно.

**Ближайший шаг:** положить выгрузку 1С в `/srv/ai2/ubd/src`, индекс ai2; contour на ai2 — позже. Prod не трогать.

---

### 2026-10-26 — Graph-fingerprint: контент вместо mtime (P2-1: ночная FULL на неизменных данных)

**Задача.** Убрать еженощную полную пересборку графа УБД (~3 ч Neo4j) при неизменном источнике: graph-этап каждую ночь падал exit 3 «изменились корневые файлы конфигурации» → graph_full.

**Причина.** Отпечатки в `partial_fingerprint.scan_workspace` и `graph_writer.fingerprint_workspace_multi` строились из кортежа (путь, размер, **st_mtime_ns**) — содержимое не читалось вовсе. Оркестратор перестейджирует дерево каждую ночь (git clone --no-hardlinks / копирование published): байты те же, mtime у всех файлов новые → `_root` всегда «изменён» → FULL. Воспроизведено замером на стенде: published и staged (байт-в-байт) дали разные digest `_root` (`efb0bd61` vs `71afcd9b`) — разница только в mtime. Дефолт stat был обоснован легаси-условиями (дерево на месте + windows bind-mount, 26 мс/файл → 12 мин чтения), в contour-пайплайне предпосылка неверна структурно. Отягчающее: `scan_workspace` не имел контентного режима вообще — `METADATA_FINGERPRINT_STRICT=true` не чинил частичный путь (apply_changes в strict даже отказывался обновлять глобальные ключи).

**Решение.** Content-режим (sha256 содержимого) — умолчание в обоих модулях; stat — opt-out через `METADATA_FINGERPRINT_STAT=true` для легаси. `scan_workspace(strict=None|True|False)` зеркалит `fingerprint_workspace_multi`; режим пишется в `meta["mode"]`; apply_changes передаёт единый режим в скан и всегда обновляет глобальные ключи (metadata_xml/bsl_source) с честной меткой режима; смена режима на сохранённом состоянии даёт одну полную пересборку (несравнимые значения — индексер пишет об этом в лог, режим в `:Fingerprint.mode`). Обёртки fingerprint_workspace/files — strict=None. Цена: чтение ~1,2 ГБ за скан — десятки секунд на ext4.

**Отвергнуто.** Сохранение/нормализация mtime при стейджинге (git clone mtime не сохраняет; любой touch извне снова даёт ложный FULL); индексация из published вместо staged (меняет контракт пайплайна).

**Изменено.** `compose/mcp_for_1c/1c-mcp-suite/mcp-metadata-graph-neo4j/`: partial_fingerprint.py (режимы+sha256+meta.mode), graph_writer.py (дефолт content, резолвер, обёртки), apply_changes.py (единый режим, запись глобальных ключей), indexer.py (env-резолв, лог, таблица env), tests_partial_fingerprint.py (3 новых), tests_fingerprint.py (дефолт content, stat opt-out, stat-тесты на strict=False), docker-compose.yml + .env.example (METADATA_FINGERPRINT_STAT), этот журнал.

**Проверено.** TDD: RED зафиксирован (touch-only менял digest; mode='stat' != 'content'), затем GREEN. Сюит метаданных на хосте: partial_fingerprint, fingerprint, graph_writer (4 skip — Neo4j), incremental (2 skip), indexer_phases — OK; полный список в коммит-сообщении. На стенде не развёрнуто: после мержа — синхронизация /srv-копии сюита и пересборка job-образа (иначе ночи продолжат идти на stat-коде).

**Откат.** `git revert` коммита; либо на стенде `METADATA_FINGERPRINT_STAT=true` (без отката кода) возвращает stat-семантику ценой возврата P2-1.

**Дальше.** PR → мерж → пересборка образа графа + /srv-синк → первая ночь: одна конверсионная FULL (смена режима), со второй ночи при неизменном источнике ожидаем «изменений нет» вместо graph-этапа. P2-2 (KB: «сохранённого состояния нет») чинится следующим — там состояние вообще не переживает ночь.

---

### 2026-10-16 — Правки по ревью ветки nightly-hardening (P1/P2/P3)

**Задача.** Закрыть три замечания ревью `refactor/nightly-hardening@4789d87`: P1 `jobs fail` снимал учёт writer без проверки его завершения; P2 `prune_run_history` удалял данные незавершённого восстановления; P3 duration-warn приходил только после завершения pipeline.

**Причина.** Lock оркестратора не доказывает, что отделённый Docker writer завершился — перевод job в failed открывал второго писателя. Очистка истории смотрела на возраст/статус отдельных jobs, а не на завершённость прогона и состояние базы: у старого незавершённого прогона удалялся успешный `bsl_index` (evidence для attempt-resume). Порог длительности проверялся только после `run_database_pipeline` — при этапе 5 ч уведомление приходило после его конца.

**Решение.** P1: `jobs fail` перед пометкой проверяет писателя (`_writer_gone`: exec inspect → `Running`/`ExecRecordGone`; docker inspect → running/paused/restarting отказ, exited/dead/created/removing — можно); аварийный обход — отдельным явным `--force --reason <текст>` с записью в meta `jobs_fail_override:<id>`. P2: `prune_run_history` удаляет только законченные (`finished_at`) прогоны здоровых баз (нет незавершённых прогонов, staged_revision, lifecycle needs_recovery/failed/recovering, слоёв building/failed/unknown — та же форма, что `required_recovery`); jobs удаляются только вместе со своим прогоном; retention считается от `finished_at`. P3: `wait`/`wait_exec` принимают `on_observe`-callback (вызов на каждом чанке/полле, ошибки callback не рвут ожидание); проводка как у `observe_cap` до BSL/Graph; v2-раннер строит callback из `_maybe_duration_warn` — предупреждение уходит, пока writer ещё работает; финальная проверка после pipeline сохранена как страховка (идемпотентна по флагу).

**Проверено.** `python -m pytest` в `controller/` — **334 passed, 8 skipped** (было 324/8 на этой машине). Добавлено/переписано: отказ `jobs fail` при живом/неизвестном писателе (контейнер и exec), `ExecRecordGone` → разрешение, `--force` без reason отказан, override записан; защита незавершённых прогонов и recovery-баз в retention (сценарии ревью воспроизведены), сохранение published-пакета на реальных пакетах; `on_observe` срабатывает до завершения writer, ошибка callback не рвёт wait.

**Стенд.** Не тронут.

**Откат.** `git revert` трёх коммитов; поведения CLI `jobs fail` без `--force` стало строже — при откате учесть, что сценарий «писатель точно мёртв, но Docker недоступен» снова потребует ручного UPDATE.

---

### 2026-10-16 — Ревью-правки раздела 1 (ночь / восстановление / диск / алерт)

**Задача.** Внедрить раздел 1 внешнего ревью v2 (`CODE-REVIEW-AI-contour-2026-10-16-v2.md`, вне репо): 1.1 наблюдение длинных этапов + исчезнувший exec; 1.2 retention/free-space; 1.3 cleanup protected; 1.4 видимость сбоя алерта; 1.5 duration_warn в v2 / убрать `measure_max_seconds`; 1.6 текст ошибок конфига. Ветка `refactor/nightly-hardening`, PR в main.

**Причина.** Бюджет наблюдения 300 с меньше реальных этапов (graph FULL ~5.3 ч) → deferred с закрытым чтением до следующей ночи; exec-GC демона делал отложенный добор вечным «unknown writer»; `.prev-*`/`accepted`/`runs` росли без уборки при отсутствии проверки места; cleanup мог `docker rm` остановленный сервисный контейнер профиля (v2 exec-job'ы пишут его имя в `container_id`); сбой доставки алерта был неотличим от тихой ночи; ручки длительности не работали в v2.

**Решение.** `dockerutil.wait/wait_exec`: полный бюджет наблюдения (`runtime.writer_max_observe_seconds`, дефолт 6 ч из `RuntimePolicy`) с перезарядкой 300-с чанков — этап наблюдается до конца в ту же ночь, задания не убиваются; явный `timeout` сохраняет старую семантику для тестов. `ExecRecordGone` (exec inspect 404) в reap → job failed + meta `last_exec_record_gone` (виден в `status`), выход — штатный resume/rebuild; операторская команда `contour jobs fail <id>`. Новый `contour/maintenance.py`: preflight `disk_usage` (`runtime.min_free_gb`, дефолт 5 GiB) до закрытия чтения/клона; post-publish retention (не портит published-результат): accepted пакеты → keep `runtime.retain_packages` (дефолт 3, пакет published-ревизии не удаляется), `.prev-*` → новейший 1 (rollback-хэндл), завершённые runs/jobs старше `runtime.retain_runs_days` (дефолт 90; незавершённые не трогаются). `protected_container_names(cfg)` — защищённые имена из конфига (bsl/graph/neo4j всех профилей + чат), cleanup inspect получил таймаут. Алерт: сбой доставки в stderr (journal) + meta `alert_delivery_last_error` в `status`. `duration_warn_seconds` работает в v2 (ключ `duration_warned:<db>`, один алерт на прогон); `schedule.measure_max_seconds` удалён (старые конфиги грузятся, ключ игнорируется). Ошибки `load_config` печатают причину.

**Проверено.** `python -m pytest` в `controller/` — **324 passed, 8 skipped** (было 303). Добавлено: бюджеты wait/wait_exec (re-arm + total + WriterStillAlive), ExecRecordGone в reap, `contour jobs fail`, maintenance (accepted/prev/history/free-space), cleanup protected из конфига, алерт stderr+meta, scoped duration warn, конфиг-ручки, текст ошибок конфига.

**Стенд.** Не тронут; kit-only, живой Scheduler не менялся. Раздел 2 ревью (dry-run: ExecIDs/exec-API на реальном демоне, фактические длительности, темп роста диска) остаётся входными данными для настройки `writer_max_observe_seconds`/`min_free_gb` перед Task 12.

**Откат.** `git revert` серии (4 коммита независимы по файлам, кроме соседних reconcile/cli); все новые ручки конфига опциональны — откат конфига не требуется.

---

### 2026-10-15 — Task 9: Windows package exporters (kit only)

**Задача.** KB / `klon_opl` на Windows только выгружают 1С и публикуют immutable package (`ready-*` + `READY` last); без Docker/index.

**Решение.** `tools/common/Export-Package.psm1` (`New-SourceChecksum` / `New-ExportManifest` / `Publish-ExportPackage`) — checksum как `packages.py`. Скрипты KB/`klon_opl` пишут в `packageInbox`. Примеры профилей `*.example.json`. Pester 5: 9 passed; parser OK.

**Проверено.** `Invoke-Pester tools/common/Export-Package.Tests.ps1`; Python `validate_package` на синтетическом пакете.

**Стенд.** Планировщик **не** переключали на package-only (нет intake/`contour`). Живые `*-connection.json` не трогали.

**Откат.** Вернуть прежние скрипты с `Docker-Index.ps1` / graph+BSL шагами из git.

---

### 2026-10-06 — Чистка `checks/`: оставить gates, убрать дубль статуса

**Задача.** Как с `docs/`: убрать лишнее относительно стенда и будущей установки.

**Решение.** Удалять нечего — оба gate нужны до cutover (`USER` ссылается). README сжат: статус только в DEVELOPMENT; в процедурах пометка prod vs ai2 names; touch-test на `docker top` вместо pgrep.

**Откат.** Не требуется (содержимое сохранено).

---

### 2026-10-06 — Чистка `docs/`: планы в основные файлы

**Задача.** Убрать архивные планы/спеки из `docs/`, оставить только то, что нужно стенду и новой установке.

**Решение.** Name map → `deploy-ai2/README.md`; Vision Bridge уже в `deploy-ai2/ai-stack/README.md`; ссылки в USER/README обновлены. Удалены: PARALLEL-CONTOUR-PLAN, REVIEW-PACK, dual-ubd-kb plan, omniroute vision design, cleanup A plan/spec. Каталог `docs/` пуст / убран.

**Откат.** Восстановить файлы из бэкапа при необходимости; содержание продублировано в основных доках.

---

### 2026-10-06 — Override канон B: query-builder + без prod WebUI по умолчанию

**Задача.** Выровнять `compose/mcp_for_1c/docker-compose.override.yml` и `deploy-ai2/mcp_for_1c-{ubd,kb}` под установочный набор (graph + query).

**Решение.** Query включён (УБД `:8109`, КБ `:8209`). Kit override больше не требует external `ai-stack_default`. Чат — optional `docker-compose.webui.yml` / `docker network connect`. Deprecated `deploy-ai2/mcp_for_1c` не канон.

**Откат.** Вернуть старые override с `webui: ai-stack_default` и `query profiles: unused`, если живой стенд ещё зависел от авто-сети в override (на стенде переподключить сеть вручную).

---

### 2026-10-06 — Чистка docs `compose/mcp_for_1c` (вариант A)

**Задача.** Убрать архив «заходов» и OpenCode; оставить актуальное для поддержки и установки ai2 (Graph + query-builder в этом каталоге; полный набор tools + skills снаружи).

**Решение.** Удалены PLAN/СТАТУС/ОТКРЫТОЕ/PROMPTS/КУДА-КЛАСТЬ, opencode-config + корневой Dockerfile/entrypoint + сервис `opencode`. Добавлен `docs/ОШИБКИ-РАЗВЁРТЫВАНИЯ.md`. Обновлены README, РАЗВЁРТЫВАНИЕ, ARCHITECTURE, `workspace/AGENTS.md`. Сторожа `tests_docs_canon` / `tests_client_configs` / `check_prereqs`. Skills не трогались.

**Откат.** Восстановить удалённые файлы из бэкапа/истории при необходимости.

---

**Задача.** Рецепты двух графов и двух BSL MCP в install-kit.

**Решение.** `deploy-ai2/mcp_for_1c-ubd|kb`, `deploy-ai2/bsl-ubd|kb`; дока в USER и `deploy-ai2/README.md`. Живые контейнеры не переименовывались.

**Откат.** Игнорировать новые каталоги; старые `deploy-ai2/mcp_for_1c` и `bsl-indexer` помечены DEPRECATED.

---

### 2026-09-21 — Параллельный контур ai2 (частично поднят)

**Задача.** Поднять второй стек рядом с боевым без cutover.

**Причина (установлено).** `wsl.exe` — sharing violation на `ext4.vhdx`, но Docker API `:2375` жив. Compose kit задаёт `networks.default.name: 1c-suite-net` — ai2-neo4j получил алиас `neo4j` в той же сети (двойной DNS у prod).

**Решение.** Каталоги `/srv/ai2`, overlays в `deploy-ai2/`; контейнеры `ai2-neo4j` (:7574), `ai2-mcp-metadata-graph` (:8101), `ai2-bsl-indexer` (:8111); сеть `ai2-mcp_net` (ai2 отключён от `1c-suite-net`); образы переиспользованы с prod (без rebuild). `ai2-metadata-indexer` остановлен до появления выгрузки.

**Проверено.** Prod: WebUI 200, `getent hosts neo4j` → один IP 172.20.0.2, bsl/neo4j/mcp Up. ai2: neo4j/mcp/bsl Up.

**Откат.** `docker compose -p ai2-bsl down`; `docker compose -p ai2-mcp … down`; тома `ai2-*` удалять только явно.

---

### 2026-09-21 — План параллельного контура ai2 (документ)

**Задача.** Развернуть второй контур рядом с боевым для обкатки `contour` без cutover.

**Причина (установлено).** Боевые `container_name`/порты/тома/`ai-stack_default` конфликтуют с «установкой поверх»; `contour update` close-reads бьёт по именам из config.

**Решение.** Префикс ai2, `/srv/ai2`, отдельные ports/volumes/network, timer disabled, Healthgate. Канон overlays — `deploy-ai2/`. (Исторический PARALLEL-CONTOUR-PLAN удалён 2026-10-06.)

**Проверено.** Документ; живой стенд не меняли.

**Откат.** Снять ai2 projects/volumes явно; не трогать prod.

---

### 2026-09-21 — F01–F05 (повторное ревью; стенд не трогали)

**Задача.** Закрыть блокеры согласованности и watcher после R01–R09: storage mismatch, probes fail-closed, reload `reloaded=true`, idle JSON, optional firewall verify-open; края (created jobs, reopen probes, alert order, no password in argv).

**Причина (установлено).** `_needs_index` игнорировал смену volume identity при том же SHA; probes трактовали docker 125 как absent; graph XML шёл в `source_path`; HUP не контракт daemon; idle парсил текст; `_mark_building` обнулял `applied_commit` → verify видел cold-start/`integrity_unknown` на пустом дереве после retry FF.

**Решение.** `_needs_index` + `_component_storage_stale`; probes exit 2 на control fail; graph `MetadataObject`; reload только при `reloaded=true` (без HUP); idle через `json.load`; `_verify_open` без iptables; `_mark_building` сохраняет prior `applied_commit` для diff samples.

**Проверено.** `python -m pytest` в `controller/` — **104 passed**. Живой стенд не меняли.

**Откат.** Откатить `controller/`, `integrations/probe_*.sh`, `idle_bsl.sh`, `reload_bsl_watch.sh`, docs к состоянию до этой записи.

---

### 2026-09-21 — R01–R06 по внешнему ревью (стенд не трогали)

**Задача.** Исправить блокеры приёмки: bootstrap CLI, adapters, BSL jobs, ready/open identity, window перед graph, watch reload/idle, stub alert.

**Причина (установлено).** `validate --config` не парсился; adapters были `RETURN 1`/`query` без семантики; `docker exec -d` + diff ExecIDs; ready проверял непустоту storage_id; graph delta без `may_write`; watch_reload=`lambda: None`; idle_bsl при ошибке docker давал 0.

**Решение.** Two-pass CLI globals; `probe_code_index.sh`/`probe_graph.sh` (neo4j, не остановленный MCP); `create_exec`→INSERT→`start_exec`; `_all_ready`/`_indexes_consistent` сравнивают live volume+image; `may_write` перед graph delta и fresh перед FULL; CLI передаёт `daemon reload`; idle fail-closed; alert stub → validate fail.

**Проверено.** `python -m pytest` в `controller/` — **99 passed**. Живой стенд не меняли.

**Откат.** Откатить `controller/`, `bootstrap.sh`, `contour.example.yaml`, `integrations/*` к состоянию до этой записи.

---

## Сводка (2026-09-20, комплект + оркестратор) — архив

**Стенд:** живой контур не переключён. Open WebUI/OmniRoute не трогали. `UBD Config Sync` не отключали.

**Пакет (на 2026-09-20):** Tasks 1–13 старого плана не доказательство готовности. См. запись 2026-09-21 выше для актуального статуса тестов и контрактов.
**Ближайший шаг:** не cutover. Drill бэкапа чат-слоя; явный запуск runbook в [USER.md](USER.md). Живой стенд не менять без разрешения.

Живой стенд этим этапом **не** менялся (кроме случайного `docker pull alpine:3.20` при прерванной инвентаризации; образы WebUI/OmniRoute не обновлялись).

---

## Стенд (stand proof — Task 12)

**Процедуры:** `checks/stand_watch_pause.md`, `checks/stand_close_reads.md` (см. `checks/README.md`).

**Статус proof (2026-09-21, ADM-SAPHX-TECH, operator agent):** частично выполнен. `UBD Config Sync` не отключали. Cutover **не** делали.

| Gate | Процедура | Proof |
| --- | --- | --- |
| BSL watch pause | `checks/stand_watch_pause.md` | **PASS** |
| Close reads | `checks/stand_close_reads.md` | **PASS** (firewall optional / skipped) |
| Probes / idle / reload | `integrations/*` | **PASS** (см. журнал) |

**Зафиксированные команды**

- Reload: `docker exec -e CODE_INDEX_HOME=/data/code-index-home bsl-indexer bsl-indexer daemon reload`; fallback `kill -HUP $(cat /data/code-index-home/daemon.pid)` (`integrations/reload_bsl_watch.sh`).
- Inspect watch: `daemon status --json` — после pause `"paths": []`.
- Process probes: `docker top bsl-indexer | grep -E '[b]sl-indexer (serve|index)'`.
- Touch-test: `C:\MCP\ubd_config\src\.stand12-watch-test-*`; ~30s — no new index.
- IdleProbe: `integrations/idle_bsl.sh` (exit 0 = idle).
- Close reads: `docker stop mcp-metadata-graph` + `docker network disconnect ai-stack_default bsl-indexer` + idle + serve + restore. **Firewall 8011:** skipped (accepted risk: no host/LAN MCP clients; off-hours).

**Cutover:** runbook в [USER.md](USER.md) §«Переключение стенда». **Не выполнялся.** Stand gates больше не blocker; остаются drill бэкапа чат-слоя и явное решение оператора.

---

## Журнал

### 2026-09-21 — Документация: AGENT.md / USER.md, удаление архивов

**Задача.** Свести описательные документы комплекта к AGENT.md + USER.md; убрать устаревшие дубли и сессионный мусор.

**Причина (установлено).** START-HERE + alt-pragmatic + README + INSTALL дублировали вход/ТЗ/эксплуатацию; битые ссылки на Gemini/installation-final; `.superpowers/sdd` и `docs/superpowers` — артефакты Task 1–13; `mcp_for_1c/docs/archive` — журналы «заходов», не поставка.

**Решение.** `AGENT.md` (контракт + правила агента), `USER.md` (установка + эксплуатация + cutover), `README.md` stub, `DEVELOPMENT.md` оставлен. Удалены: `AI-contour-START-HERE.md`, `AI-contour-alt-pragmatic.md`, `INSTALL.md`, `.superpowers/sdd/`, `docs/superpowers/`, `compose/mcp_for_1c/docs/archive/`. Vendor README в `compose/bsl-indexer` и живые `mcp_for_1c/docs/*.md` не трогали. Ссылки в bootstrap, contour.example.yaml, release-manifest, checks, compose/README обновлены.

**Проверено.** Корневые md: AGENT, USER, DEVELOPMENT, README. Старые пути отсутствуют. Исторические упоминания в старых записях журнала не переписывались.

**Откат.** Восстановить удалённые файлы из VCS/бэкапа; вернуть ссылки.

**Дальше.** Drill бэкапа чат-слоя; cutover по USER.md только явно.

---

### 2026-09-21 — Ослабление close-reads: firewall optional

**Задача.** Зафиксировать: host DROP на 8011 не обязателен для `verify_closed` / stand PASS.

**Причина (установлено).** На ADM-SAPHX-TECH нет host/LAN MCP-клиентов на published 8011; обновление вне рабочего времени. WebUI режется `network disconnect`; graph — `docker stop`. Firewall был формальным блокером без бизнес-риска.

**Решение.** `verify_closed` = graph ≠ running + BSL не в `webui_network`. `apply_close`/`open_reads`: iptables DROP/undrop best-effort (ошибки игнорируются). Checks/README/DEVELOPMENT: close-reads **PASS**, firewall=skipped. Риск чтения через host:8011 принят оператором.

**Проверено.** `python -m pytest` в `controller/` — **102 passed**.

**Откат.** Вернуть проверку firewall в `verify_closed` и обязательный §2a в `stand_close_reads.md`.

**Дальше.** Drill бэкапа чат-слоя / R08; cutover по явному решению.

---

### 2026-09-21 — Stand proof: probes + watch PASS, close-reads FAIL (firewall)


**Задача.** Прогнать `integrations/probe_*`, `idle_bsl`, `reload_bsl_watch` и stand gates на живом Docker без cutover.

**Причина (установлено).** Git Bash ломает `CODE_INDEX_HOME=/data/...` (MSYS pathconv) и `case` с `\"` в probe/idle. В образе нет `pgrep`. После pause со снятием всех paths HTTP `daemon reload` может умереть на stale `http_port` из `daemon.json` — нужен SIGHUP. Package `verify_closed` требует host `iptables`; на Windows host без elevated WinFW firewall не доказан.

**Решение.** `MSYS_NO_PATHCONV=1`; idle/probe на `grep -E` вместо хрупкого `case`; process checks через `docker top`; `reload_bsl_watch.sh` — HTTP затем HUP. Watch pause: strip `[[paths]]` SRC → reload → status без SRC → touch 30s без index → restore (HUP при сбое HTTP). Close reads: graph stop + network disconnect + idle + serve + restore; firewall — blocker.

**Проверено.** idle/reload PASS; BSL/graph present|absent (`.bsl` paths; `.xml` в Neo4j `source_path` нет) PASS; watch pause PASS; close-reads FAIL_FIREWALL; restore graph+network OK. Cutover не делали.

**Откат.** Не требуется для стенда (восстановлен). Пакет: откатить правки `integrations/idle_bsl.sh`, `probe_code_index.sh`, `reload_bsl_watch.sh` при необходимости.

**Дальше.** Elevated firewall proof или WSL iptables + опционально Windows path в `reads.py`; R08 архив. Не cutover.

---

### 2026-09-20 — В комплект добавлены деревья сборки графа и BSL


**Задача.** Самостоятельная установка без leftover `C:\MCP`: в комплект нужны исходники образов, не только compose-рецепты.

**Причина (установлено).** `mcp_for_1c/docker-compose.yml` собирает сервисы из `./1c-mcp-suite`. Dockerfile BSL делает `COPY . .` из корня Cargo-репо `C:\MCP\code_index\repo`, не из `deploy/`.

**Решение.** `tools/copy-kit-artifacts.py` копирует оба дерева без `.git`, `.env`, `target/`, проприетарной справки платформы. Overlay: `compose/bsl-indexer/docker-compose.yml` собирает `bsl-indexer:local` из этого каталога, монтирует `/srv/ai/ubd/src`, порт 8011. `contour.example.yaml` указывает `/srv/ai/compose/...`.

**Отвергнуто.** Копирование live `.env` и `.git` (токены в remote). Подмена исходников чужим Dockerfile.

**Проверено.** В комплекте есть `apply_changes.py`, `Dockerfile.python`, `crates/bsl-indexer/Cargo.toml`; файлов `.env` и `.git` в `compose/` нет.

### 2026-09-20 — Оркестратор B01–B10 + установочный комплект (стенд не трогали)

**Задача.** Задание перекрывает старый план: починить оркестратор без поколений и без универсального installer; собрать комплект, который другой сотрудник разворачивает по [INSTALL.md](INSTALL.md) со своими секретами, без leftover `C:\MCP`.

**Причина (установлено).** Пакет Tasks 1–13 — черновик. Null deadline больше не блокирует timer. B05 auto-kill отменён. Skills markdown входят в комплект. `contour open` не лечит смешанные индексы.

**Решение.** `may_write` только окно. Duration warn — один алерт. BSL `docker exec -d` + `exec_id`. Reap: busy/unknown блокирует нового писателя. Cleanup без `rm -f`, только завершённые. Checkpoint пишет `storage_id=name|CreatedAt` и image id. CLI строит adapters; отсутствие — failed до close. Комплект: compose без `.env`, skills markdown, манифест `missing` для Cargo/mcp source. 96 pytest на фейках.

**Отвергнуто / неудачно.** Live cutover; копирование `.env`; подмена отсутствующих исходников «похожим» Dockerfile; git commit.

**Проверено.** `python -m pytest tests -q` в `controller/` — 96 passed (Windows). `bootstrap.sh` / timer на стенде не запускали. Архив и sha256 не упаковывали.

### 2026-09-19 — Зафиксированы требования v1 (in-place), код не писали

**Задача.** Новый контур: одна копия УБД/индексов, дельта Git и индексов как на стенде, Python-оркестратор в WSL + systemd timer, SQLite, поставка чеклист + `bootstrap.sh`. Данные УБД на ext4, не `/mnt/c`.

**Причина (установлено).** Текущий sync — Windows PowerShell и NTFS bind `/mnt/c/...`. Целевой документ отвергает поколения и полную пересборку каждую ночь. Команды индексаторов сняты со стенда, не выдуманы.

**Решение.** Источник правды — `AI-contour-alt-pragmatic.md`. Реализация — в `C:\AI-contour`. Живой стенд не переключать, пока пакет не готов и явно не сказано. Карта стенда и запреты записаны в ТЗ, не в чат.

**Отвергнуто / неудачно**

- Поколения индексов, `active.json`, blue-green, Golden Image, универсальный Windows-installer, КБ/МДМ, webhook, каталог Skills — вне v1.
- Обещание «обновление не доехало — поиск жив на старых данных»: при одном индексе неверно; дельта и `index --force` (снос `index.db`) могут дать смешанный набор. Чтение закрывать **до** записи, открывать после проверки дельты.
- FULL графа по словам `force reindex` в логе — хрупко; только код выхода **3** у `apply_changes.py`. Код 2 — Neo4j недоступен, не FULL. Авто-FULL BSL в ночном пути стенда нет.
- `project: ai-contour` в примере конфига: новый project создаст пустые тома. Имена брать у живых проектов (`ai-stack`, `mcp_for_1c`, `bsl-indexer`).
- `docker stop bsl-indexer` / `daemon stop` на время обновления: `entrypoint.sh` делает `wait -n`, падение демона роняет контейнер.
- Начать живую инвентаризацию/миграцию по формулировке про WebUI: это было уточнение ТЗ, не старт работ. Команда `du` через alpine прервана. `wsl.exe` упёрся в sharing violation VHD.

**Изменено в пакете (стенд не трогали).** Файлы: `AI-contour-alt-pragmatic.md`, `AI-contour-START-HERE.md`, `README.md`, этот `DEVELOPMENT.md`. Оркестратора нет. Commit на момент записи может отсутствовать.

**Как отменить.** Вернуть указанные markdown к предыдущей версии в git, если появится история. Откат ТЗ на стенд не влияет. Откат случайного образа `alpine:3.20` не делали (не проверено, на сервисы не влияет).

**Пересмотр.** Если позже изменят закрытие чтения или политику FULL — не стирать эту запись, добавить новую со ссылкой сюда.

### 2026-09-19 — Спека оркестратора (черновик, код не писали)

**Задача.** Спека реализации под целевой документ и факты стенда, без кода.

**Причина (установлено).** Команды и пути сняты с живых файлов: `C:\MCP\ubd_index\scripts\sync-and-index.ps1`, `apply_changes.py` (код 3), `C:\MCP\code_index\deploy\docker-compose.yml`, `entrypoint.sh` (`wait -n`), compose projects `ai-stack` / `mcp_for_1c` / `bsl-indexer`.

**Решение.** Один оркестратор в WSL; те же команды дельты/FULL; закрытие чтения до записи; FULL графа только exit 3; BSL `--force` не в ночи; тома чат-слоя не трогать. Свобода: git `merge --ff-only`; BSL MCP закрывать firewall+network, не stop контейнера; пост-проверка через `docker exec`/cypher при закрытом MCP; CLI `contour update` обходит окно, timer — нет.

**Отвергнуто.** Regex `force reindex` в логе; `daemon stop`; новый Compose project; MCP HTTP для verify во время закрытия.

**Изменено.** `docs/superpowers/specs/2026-09-19-ai-contour-orchestrator-design.md`, этот журнал. Commit не делали. Живой стенд не меняли.

**Откат.** Удалить/вернуть файл спеки в git, когда появится история. На стенд не влияет.

**Пересмотр.** 2026-09-20: CLI больше не обходит окно; journal не заменяет алерт; nsenter отвергнут. См. запись ниже.

### 2026-09-20 — Правки спеки по ревью (код не писали)

**Задача / симптом.** Черновик спеки менял окно/deadline/алерт, путал порядок cutover, закрывал чтение без намерения-сначала, проверял `HEAD..target`, надеялся на `index.lock` против watcher.

**Причина (установлено).** File-watch BSL на ext4 стартует от событий git после проверки процессов (`operations.md`: убрали `[[paths]]` — демон перестаёт следить, `serve` жив). `--rm` на стенде есть, но тогда нет сохранённого container id. Имя volume не меняется при пересоздании пустого тома.

**Решение.** Cutover: сначала стоп старого sync. Watch: pause `daemon.toml` до git. Reads: intent → apply → verify; drain по соединениям; ready-ветка открывает доступ. Verify: `applied_commit..target` по каждому индексу. Timer и CLI в окне; `build_deadline_seconds: null` блокирует timer. Один `alert.command`. Писатель: create → job → start. Storage: CreatedAt + проба данных. Бэкап: drill на throwaway-томах. WSL: чинить доступ, не nsenter.

**Отвергнуто.** Обход окна ручным `contour update`; вечный FULL при null deadline; journal как алерт; privileged nsenter.

**Изменено.** Спека; согласован `AI-contour-alt-pragmatic.md`. Живой стенд нет. Commit нет.

**Откат.** Вернуть оба markdown. На стенд не влияет.

**Пересмотр.** Следующая запись: drill бэкапа = вход+роли+подключения; drain не ESTABLISHED; cutover watcher; откат индексов; `--measure`.

### 2026-09-20 — Уточнения спеки (пакет можно планировать)

**Задача.** Одобрена основа пакета; cutover ещё с дырами в drain/watcher/откате/замере.

**Решение.** ESTABLISHED вспомогателен; граф `docker stop` = отмена; BSL idle доказать на стенде. Cutover: pause watcher до копии и после recreate. Откат: индексы к SHA возвращаемого исходника. `--measure` — единственная запись при null deadline. Бэкап: вход, роли, права, настройки, рабочие подключения. DEVELOPMENT.md — обязанность вести, не «файл есть».

**Изменено.** Спека, целевой документ, план `docs/superpowers/plans/2026-09-20-ai-contour-orchestrator.md`. Кода нет. Стенд не меняли.

**Откат.** Вернуть markdown. Не проверено на стенде.

**Дальше.** Реализация Tasks 1–11 плана. Task 12 (стенд) до cutover.

### 2026-09-20 — Task 1: каркас пакета + config (стенд не трогали)

**Задача.** Первый шаг плана: `controller/` с `pyproject.toml`, `contour.config.load_config`, пример `contour.example.yaml`, unit-тесты валидации.

**Причина (установлено).** Спека §5.3 задаёт форму конфига; последующие Tasks (cli, db, reconcile) должны читать один `Config`. Запрет `kb`/`mdm` и обязательный `alert.command` — из целевого документа и спеки §5.2.

**Решение.** TDD: `test_config.py` (reject `kb`, reject `mdm`, пустой alert) → `config.py` с frozen dataclasses по секциям yaml, `build_deadline_seconds: null` → `None`, `measure_max_seconds` по умолчанию 43200. Зависимости: `pyyaml`; dev: `pytest`. Установка: `cd controller && pip install -e ".[dev]"`.

**Отвергнуто.** Любые изменения Docker/стенда, git init/commit, секреты в yaml.

**Изменено в пакете.** `controller/pyproject.toml`, `controller/src/contour/{__init__.py,config.py}`, `controller/tests/test_config.py`, `contour.example.yaml`, этот журнал. Живой стенд, WSL Ubuntu (sharing violation), задача `UBD Config Sync` — без изменений.

**Проверено.** Windows: Python 3.12 установлен через winget (на машине не было интерпретатора). `python -m pytest tests/test_config.py -v` — 3 passed. Ручная загрузка `contour.example.yaml`: `build_deadline_seconds is None`, `measure_max_seconds == 43200`.

**Откат.** Удалить каталог `controller/` и `contour.example.yaml`; вернуть сводку в начале файла. На стенд не влияет.

**Дальше.** Task 2+ по плану (следующий модуль после утверждения плана).

### 2026-09-20 — Task 2: SQLite schema (стенд не трогали)

**Задача.** TDD: `contour.db` — `init_db(path)`, таблицы §6 (`components`, `runs`, `jobs`), KV `meta` с пустыми `reads_close_requested` / `reads_closed`, helpers `get_component`, `set_meta`, `get_meta`.

**Причина (установлено).** Спека §5.4 и целевой документ §6: состояние оркестратора в SQLite; намерение закрытия чтения (`reads_close_requested`) и факт (`reads_closed`) — не колонки `components`. `storage_id` хранит `name|created_at` (идентификатор экземпляра тома).

**Решение.** TDD: `test_db.py` (схема, meta keys, round-trip, `get_component`) → `db.py` с `CREATE TABLE IF NOT EXISTS`, `INSERT OR IGNORE` для meta keys, upsert в `set_meta`. Колонки `components` — порядок из task brief (status перед applied_commit). Тесты на Windows через полный путь к Python 3.12.

**Отвергнуто.** CLI, flock, docker, reconcile; git init/commit; изменения стенда.

**Изменено в пакете.** `controller/src/contour/db.py`, `controller/tests/test_db.py`, этот журнал. Task 1 файлы не трогали.

**Проверено.** `python -m pytest tests/test_db.py -v` — 6 passed; `tests/test_config.py` — 3 passed. RED: `ModuleNotFoundError: No module named 'contour.db'` до реализации.

**Откат.** Удалить `db.py`, `test_db.py`; вернуть сводку. На стенд не влияет.

**Дальше.** Task 3 (flock) по плану.

### 2026-09-20 — Task 3: flock (стенд не трогали)

**Задача.** TDD: `contour.lock` — non-blocking exclusive lock `acquire(path) -> Lock | None`; context manager releases; injectable `LockBackend` (tests: `MemoryBackend`; production WSL: `FcntlBackend` с `fcntl.flock`).

**Причина (установлено).** Спека §5.4: один процесс reconcile держит `/srv/ai/sync.lock`; занятый lock → exit без записи. На Windows unit-тесты не могут вызывать `fcntl` — нужен `MemoryBackend`.

**Решение.** TDD: `test_lock.py` (второй acquire на том же path → `None`; release через context manager → повторный acquire успешен) → `lock.py` с `LockBackend` Protocol, `MemoryBackend`, `FcntlBackend` (`LOCK_EX | LOCK_NB`), `Lock` как context manager. Параметр `backend=` для инъекции в тестах; по умолчанию lazy singleton `FcntlBackend`.

**Отвергнуто.** CLI, reconcile, docker, schedule; git init/commit; изменения стенда; nsenter.

**Изменено в пакете.** `controller/src/contour/lock.py`, `controller/tests/test_lock.py`, этот журнал. Task 1–2 файлы не трогали.

**Проверено.** `python -m pytest tests/test_lock.py -v` — 2 passed; полный suite — 11 passed. RED: `ModuleNotFoundError: No module named 'contour.lock'`.

**Откат.** Удалить `lock.py`, `test_lock.py`; вернуть сводку. На стенд не влияет.

**Дальше.** Task 4+ по плану.

### 2026-09-20 — Task 4: Window, deadline, `--measure` (стенд не трогали)

**Задача.** TDD: `contour.schedule.may_write(now, cfg, is_timer, measure) -> "ok" | "deferred_window" | "deferred_deadline"`. Окно 01:30–06:00 `Asia/Vladivostok`; null `build_deadline_seconds` блокирует timer и plain update; `--measure` (только `measure=True`) — исключение в окне при `measure_max_seconds > 0`.

**Причина (установлено).** Спека §5.3–5.4 и §6: timer и CLI не обходят окно; автоматическая запись при null deadline запрещена; operator `--measure` — единственный путь записи до установки deadline.

**Решение.** TDD: `test_schedule.py` (4 кейса с frozen `now` во Владивостоке, конфиг через `load_config` на temp yaml от `contour.example.yaml`) → `schedule.py` с проверкой окна (start inclusive, end exclusive), затем deadline/measure. `is_timer` в сигнатуре для вызывающего кода; timer всегда передаёт `measure=False`.

**Отвергнуто.** CLI, reconcile, запись deadline в yaml, git init/commit; изменения стенда.

**Изменено в пакете.** `controller/src/contour/schedule.py`, `controller/tests/test_schedule.py`, этот журнал. Task 1–3 файлы не трогали.

**Проверено.** `python -m pytest tests/test_schedule.py -v` — 4 passed; полный suite — 15 passed. RED: `ModuleNotFoundError: No module named 'contour.schedule'`. На Windows для `ZoneInfo("Asia/Vladivostok")` нужен pip-пакет `tzdata` (на WSL/Linux — системная tzdata).

**Откат.** Удалить `schedule.py`, `test_schedule.py`; вернуть сводку. На стенд не влияет.

**Дальше.** Task 7 (reads + watch BSL) по плану.

### 2026-09-20 — Task 5: Docker jobs без `--rm` (стенд не трогали)

**Задача.** TDD: `contour.dockerutil` — `ping`, `build_create_cmd`, `compose_create` (container id), `start`, `wait`. Новый graph writer: `docker compose create` без `--rm`, затем (в reconcile) запись job → `start` → `wait`. Тесты через injectable `DockerRunner`, не живой Engine.

**Причина (установлено).** Спека §2.2 и §5.2: на стенде `docker compose run --rm` не сохраняет container id; новый путь — `create` + id в SQLite `jobs` + `start` + `wait`. Project name — существующий (`mcp_for_1c`, `bsl-indexer`), не `ai-contour`. `ping` при зависшем API — raise, без restart Engine.

**Решение.** TDD: `test_dockerutil.py` (strip `--rm` из cmd; `record_then_start` — запись job до `start`; fake runner для create/ps/start/wait/ping) → `dockerutil.py` с `DockerRunner` Protocol, `SubprocessRunner`, `build_create_cmd`/`build_ps_cmd`, service name из args, `compose_create` = create + ps -q.

**Отвергнуто.** SQLite job writes в dockerutil (caller reconcile); graph FULL (Task 8); `docker compose up`/pull/recreate WebUI/OmniRoute; restart dockerd в `ping`; git init/commit; изменения стенда.

**Изменено в пакете.** `controller/src/contour/dockerutil.py`, `controller/tests/test_dockerutil.py`, этот журнал. Task 1–4 файлы не трогали.

**Проверено.** `python -m pytest tests/test_dockerutil.py -v` — 8 passed; полный suite — 23 passed. RED: `ImportError: cannot import name 'dockerutil'`.

**Review fix (2026-09-20):** `build_create_cmd` — только compose-legal `create` argv (service + `--force-recreate` и т.п.; без run-флагов); `build_ps_cmd` — `ps --all -q` для created-not-started контейнеров.

**Откат.** Удалить `dockerutil.py`, `test_dockerutil.py`; вернуть сводку. На стенд не влияет.

**Дальше.** Task 7 (reads + watch BSL) по плану.

### 2026-09-20 — Task 6: git ff-only (стенд не трогали)

**Задача.** TDD: `contour.gitutil` — `fetch(repo)` (`git fetch --prune origin`), `is_dirty(repo) -> bool` (working tree или index), `ff_only(repo, branch) -> str` (merge `--ff-only origin/<branch>`, новый HEAD SHA), `safe_remote(url)` (убрать userinfo из URL для логов).

**Причина (установлено).** Спека §2.2 и §5.2: reconcile делает fetch + dirty check + ff-only merge, не `reset --hard` и не `git pull` с merge commit. Remote URL с токенами не должен попадать в логи.

**Решение.** TDD: `test_gitutil.py` (temp bare remote + clone; dirty/clean; ff после второго commit; `safe_remote("https://oauth2:x@host/r")`) → `gitutil.py` с subprocess git и `urllib.parse` для `safe_remote`. Тесты не трогают `C:\MCP\ubd_config`.

**Отвергнуто.** reconcile, reads, watch pause; git init/commit пакета; clone/fetch живого УБД; `git pull`.

**Изменено в пакете.** `controller/src/contour/gitutil.py`, `controller/tests/test_gitutil.py`, этот журнал. Task 1–5 файлы не трогали.

**Проверено.** `python -m pytest tests/test_gitutil.py -v` — 6 passed; полный suite — 31 passed. RED: `ImportError: cannot import name 'gitutil'`.

**Откат.** Удалить `gitutil.py`, `test_gitutil.py`; вернуть сводку. На стенд не влияет.

**Дальше.** Task 7 (reads + pause BSL watch) по плану.

### 2026-09-20 — Task 7: close reads + pause BSL watch (стенд не трогали)

**Задача.** TDD: `contour.reads` — persist `reads_close_requested` до мутаций, `apply_close` (stop graph, iptables DROP 8011, `docker network disconnect` `ai-stack_default`), `verify_closed`, `wait_server_idle` (graph inspect ≠ running; BSL IdleProbe, ESTABLISHED не достаточен), `open_reads`. `contour.watch_bsl` — `pause_watch` / `restore_watch` / `watch_is_paused` на копии `daemon.toml`, без `daemon stop`.

**Причина (установлено).** Спека §2.2 / §5.4 шаги 8–9: закрыть MCP до записи; BSL-контейнер не стопать (`entrypoint.sh` `wait -n`); watcher — убрать `/repos/SRC` из toml и reload, не `daemon stop`.

**Решение.** TDD: `test_reads.py` (spy: meta `1` до `apply_close`; IdleProbe busy + ESTABLISHED=0 → raise) и `test_watch_bsl.py` (sample toml в tmp, strip SRC, reload после записи) → `reads.py` / `watch_bsl.py`. Docker — Protocol `ReadsDocker` + `idle_probe`; тесты не ходят в Engine и не пишут `C:\MCP\code_index\deploy\daemon.toml`.

**Отвергнуто.** indexer delta (Task 8), reconcile (Task 10), `docker stop bsl-indexer`, `daemon stop`, sleep-N как drain, git commit, изменения стенда.

**Изменено в пакете.** `controller/src/contour/reads.py`, `controller/src/contour/watch_bsl.py`, `controller/tests/test_reads.py`, `controller/tests/test_watch_bsl.py`, этот журнал. Task 1–6 файлы не трогали.

**Проверено.** `python -m pytest tests/test_reads.py tests/test_watch_bsl.py -v` — 13 passed; полный suite — 44 passed. RED: `ModuleNotFoundError` затем assertion (meta пустой; DID NOT RAISE на busy probe; SRC не снят). Живой стенд не меняли.

**Откат.** Удалить `reads.py`, `watch_bsl.py`, `test_reads.py`, `test_watch_bsl.py`; вернуть сводку. На стенд не влияет.

**Дальше.** Task 8+ по плану (index BSL/graph). Не cutover до Task 12.

**Review fix (Task 7).** `_firewall_blocked`: `SubprocessRunner(check=True)` бросает `CalledProcessError` на `iptables -C` miss — ловим и возвращаем False; тест `CheckTrueReadsDocker` поднимает на miss.

### 2026-09-20 — Task 8: Index BSL + graph (стенд не трогали)

**Задача.** TDD: `contour.index_bsl` — точные `docker exec` index затем clean (спека §2.2), без `--force`. `contour.index_graph` — `compose_create` → INSERT `jobs.container_id` → `start` → `wait`; FULL только при exit 3 (compose override `METADATA_FORCE_REINDEX=true`, не `-e` на create); exit 2 и текст лога `force reindex` — не FULL; в argv нет `--rm`.

**Причина (установлено).** Стендовый `compose run --rm` не сохраняет container id. Task 5 вырезает `-e`/`--entrypoint` из `create`, поэтому FULL env и `python /app/apply_changes.py` задаются временным compose override + `files=` / `-f`. FULL по regex лога запрещён.

**Решение.** Фейковый `DockerRunner`: exit 3 затем 0 → два create (delta + FULL); exit 0 + лог `force reindex` → один create. Job row пишется до `start`. BSL argv без `--force`. Тесты не ходят в Engine и не пишут в `C:\MCP` / `C:\ai-stack`.

**Отвергнуто.** `create -e`; `--rm`; grep `force reindex`; BSL `--force`; reconcile/CLI (Task 10); verify samples (Task 9); git commit; живой Engine.

**Изменено в пакете.** `controller/src/contour/index_bsl.py`, `controller/src/contour/index_graph.py`, `controller/tests/test_index_graph.py`, `compose_create`/`build_*_cmd` получили опциональный `files=`, этот журнал.

**Проверено.** `python -m pytest tests/test_index_graph.py -v` — 4 passed; полный suite — 49 passed. RED: `ModuleNotFoundError: No module named 'contour.index_bsl'`. Живой стенд не меняли.

**Откат.** Удалить `index_bsl.py`, `index_graph.py`, `test_index_graph.py`; вернуть `files=` в `dockerutil.py` и сводку. На стенд не влияет.

**Дальше.** Task 9 (verify from `applied_commit`). Не cutover до Task 12.

**Review fix (Task 8).** FULL и delta больше не `compose create` одного `metadata-indexer`: у каждого job свой `container_name` и отдельный service (`extends` базы), id — `docker inspect` этого имени, не `ps --all -q` blob. Без `--force-recreate`/`--rm`. Тест: имена разные; FULL start/job id = inspect(FULL), не canned второй `ps` id.

### 2026-09-20 — Task 9: verify from `applied_commit` (стенд не трогали)

**Задача.** TDD: `contour.verify` — `sample_paths(repo, old_sha, new_sha)` по diff `applied_commit..target` (не только `HEAD..target`); `verify_indexes(samples, probes)` с injectable probes; три вида sample (`.bsl`, `.xml`, deletion); пустой diff + unknown integrity → fail.

**Причина (установлено).** Спека §5.4 шаг 14 и §6: post-index проверка по delta каждого индекса; пустой git diff при неизвестной целостности — не успех; verify через `docker exec`/cypher, не MCP HTTP во время закрытия чтения.

**Решение.** TDD: `test_verify.py` (temp git repo: bsl+xml+deletion; empty diff + `integrity_unknown` raises; empty + all `ready` ok; probe dispatch) → `verify.py` с `Samples` dataclass, `GitDiffRunner` Protocol, `IndexProbes` Protocol. Deletions через `--diff-filter=D`. Тесты не читают `C:\MCP\ubd_config`, не ходят в Neo4j/BSL MCP.

**Отвергнуто.** reconcile/CLI (Task 10); recovery tree sampling; git commit; живой стенд; MCP HTTP для verify.

**Изменено в пакете.** `controller/src/contour/verify.py`, `controller/tests/test_verify.py`, этот журнал. Task 1–8 файлы не трогали.

**Проверено.** `python -m pytest tests/test_verify.py -v` — 5 passed; полный suite — 54 passed. RED: `ModuleNotFoundError: No module named 'contour.verify'`. Живой стенд не меняли.

**Откат.** Удалить `verify.py`, `test_verify.py`; вернуть сводку. На стенд не влияет.

**Дальше.** Task 10 (reconcile + CLI + alert). Не cutover до Task 12.

### 2026-09-20 — Task 10: Reconcile + CLI + alert (стенд не трогали)

**Задача.** TDD: `contour.reconcile` по спеке §5.4, `contour.alert.send` (stdin), `contour.cli` (`validate|status|update [--measure]|cleanup [--dry-run]`), entry point `[project.scripts] contour`.

**Причина (установлено).** Нормативный порядок: lock → ping → reap → reads intent vs actual → fetch → ready-at-target → `may_write` → close intent first → pause watch → ff → bsl → restore watch только если bsl ok → graph → verify `applied_commit..target` → checkpoints → open. Timer и plain `update` не пишут при null deadline; `--measure` — единственный write-путь. Алерт — один `alert.command`, не journal.

**Решение.** Тесты на фейках (`MemoryBackend`, FakeGit/FakeDocker, tmp toml/db): вне окна → `deferred` без ff-only; timer+null → `deferred` + alert; `--measure`+null+в окне → git write; второй lock → не success; intent close до apply; dirty не закрывает чтение; hung ping → `failed`+alert. CLI: `validate` = `load_config`; `status` через `safe_remote`; `--measure` только у `update`; cleanup `--dry-run` печатает id, без `--rm` на start. Пути db/lock — `RuntimePaths` / флаги `--db`/`--lock`, не секции yaml. Timer всегда `measure=False`.

**Отвергнуто.** systemd/bootstrap (Task 11); cutover runbook; git commit; живой Engine/УБД/WebUI/`C:\MCP`; второй канал алерта; nsenter; `docker compose -p ai-contour`.

**Изменено в пакете.** `controller/src/contour/{reconcile,alert,cli}.py`, `controller/tests/{test_reconcile,test_cli}.py`, `controller/pyproject.toml` (`[project.scripts]`), этот журнал. Живой стенд не меняли.

**Проверено.** `python -m pytest tests/test_reconcile.py tests/test_cli.py -v` — 16 passed; полный suite — 70 passed. RED: `ModuleNotFoundError: No module named 'contour.alert'` / `contour.cli`.

**Откат.** Удалить `reconcile.py`, `alert.py`, `cli.py`, `test_reconcile.py`, `test_cli.py`; убрать `[project.scripts]` из `pyproject.toml`; вернуть сводку. На стенд не влияет.

**Дальше.** Task 11 (systemd + bootstrap + README). Не cutover до Task 12.

### 2026-09-20 — Task 10 review: backup, empty applied_commit, fail-closed probes

**Задача.** Правки после review Task 10 (без git commit, стенд не трогали).

**Причина (установлено).** `copy2(toml → .bak)` до `pause_watch` на повторном прогоне затирал pre-pause backup уже paused-файлом. Пустой `applied_commit` шёл как empty Samples + `integrity_unknown` и всегда падал. `probes=None` подменялся no-op `_DbProbes`, verify «проходил», reads открывались.

**Решение.** Backup не перезаписывать, если watch уже paused или `.bak` уже содержит pre-pause toml. Пустой `applied_commit` — `sample_current_tree` + indexer probes, не forced empty-diff. `probes is None` → `failed`, reads остаются закрытыми; CLI `update` probes не инжектит (fail closed). Пустой git diff между двумя SHA + unknown по-прежнему fail в `verify_indexes`.

**Отвергнуто.** git commit; живой Engine/`C:\MCP`/`C:\ai-stack`; ослабление window/`--measure`/alert-stdin/lock тестов.

**Изменено в пакете.** `controller/src/contour/reconcile.py`, `controller/src/contour/verify.py` (`sample_current_tree`), `controller/tests/test_reconcile.py`, `controller/tests/test_cli.py`, этот журнал.

**Проверено.** `python -m pytest tests/test_reconcile.py tests/test_cli.py -v` — 23 passed. `tests/test_verify.py` — 5 passed (empty-diff + unknown не ослаблен).

**Откат.** Вернуть `_DbProbes` no-op fallback и безусловный `copy2`; удалить `sample_current_tree`. На стенд не влияет.

**Дальше.** Task 11 (systemd + bootstrap + README). Не cutover до Task 12.

### 2026-09-20 — Task 11: systemd + bootstrap + README (стенд не трогали)

**Задача.** Поставка: `systemd/ai-sync.{service,timer}`, `bootstrap.sh`, ops в `README.md`, stand gates в `checks/README.md`.

**Причина (установлено).** Спека §8 и план Task 11: timer вызывает `contour update` без `--measure`; при `build_deadline_seconds: null` timer deferred (без записи); `--measure` — единственный write-путь до установки deadline. Bootstrap через normal `systemctl`; sharing violation — блокер, nsenter отвергнут.

**Решение.** `ai-sync.service`: `Type=oneshot`, `ExecStart=/opt/ai-sync/venv/bin/contour update`. Timer: `OnCalendar=*-*-* 01:30:00`, `Persistent=true`. `bootstrap.sh`: `/srv/ai/{secrets,logs}`, копия `controller/` + `config.yaml` в `/opt/ai-sync`, venv + `pip install -e`, units в `/etc/systemd/system/`, `enable --now ai-sync.timer` только если `systemctl --version` ok; иначе сообщение про sharing violation и exit 1. README: окно 01:30–06:00 Vladivostok, `--measure`, один `alert.command`, пакет не live. `checks/README.md`: Task 12 gates как blockers.

**Отвергнуто.** Запуск bootstrap на живом стенде; nsenter; `docker compose up` ai-stack; `compose -p ai-contour`; git commit.

**Изменено в пакете.** `systemd/ai-sync.service`, `systemd/ai-sync.timer`, `bootstrap.sh`, `README.md`, `checks/README.md`, этот журнал. Живой стенд, `UBD Config Sync` — без изменений.

**Проверено.** `bash -n bootstrap.sh` (если bash доступен); grep unit files: `contour update` без `--measure`. Bootstrap не выполняли.

**Откат.** Удалить `systemd/`, `bootstrap.sh`, `checks/README.md`; вернуть краткий `README.md` и сводку. На стенд не влияет.

**Дальше.** Task 12 (stand proof). Не cutover до PASS.

### 2026-09-20 — Task 11 review fix: timer timezone

**Исправлено.** `OnCalendar=*-*-* 01:30:00 Asia/Vladivostok` в `ai-sync.timer` (host-local UTC давал 11:30 Vladivostok → `deferred_window` вместо старта окна); README — явная зона таймера.

### 2026-09-20 — Task 12: stand gate procedures (стенд не трогали)

**Задача.** Процедуры stand proof: pause BSL file-watch через `daemon.toml` и close reads (graph stop + firewall 8011 + network disconnect + IdleProbe).

**Причина (установлено).** Спека §5.4 шаги 8–9 и §5.5: cutover заблокирован, пока reload watcher и drain BSL не доказаны на живом стенде; mock pytest Task 7 не заменяет operator proof.

**Решение.** Два markdown-процедуры в `checks/`: backup/restore toml, reload/config inspect (placeholder TBD), touch под `ubd_config/src` без нового index cmdline; graph `docker stop mcp-metadata-graph`; BSL DROP 8011 + `docker network disconnect ai-stack_default`; IdleProbe — placeholder до stand run; ESTABLISHED только вспомогательно; restore обязателен. Секция **Стенд** в этом файле: proof **не выполнялся**.

**Отвергнуто.** Выполнение proof в этой сессии; правка живого `C:\MCP\code_index\deploy\daemon.toml`; `docker stop` graph на стенде; iptables на стенде; `daemon stop`; `docker stop bsl-indexer`; sleep-N как drain; git commit.

**Изменено в пакете.** `checks/stand_watch_pause.md`, `checks/stand_close_reads.md`, `checks/README.md` (stand proofs not run yet), секция **Стенд** и эта запись в `DEVELOPMENT.md`. Живой стенд, `UBD Config Sync` — без изменений.

**Проверено.** Файлы процедур созданы; stand proof на ADM-SAPHX-TECH **не запускали**.

**Откат.** Удалить `checks/stand_watch_pause.md`, `checks/stand_close_reads.md`; убрать секцию **Стенд** и эту запись. На стенд не влияет.

**Дальше.** Operator/agent на стенде: PASS обоих gate → записать команды в **Стенд**; затем Task 13 (cutover runbook). При FAIL — blocker, cutover запрещён.

### 2026-09-20 — Task 12 review fix: touch poll, liveness, lock wait (стенд не трогали)

**Задача.** Правки процедур после review Task 12 (без git commit, stand proof не выполняли).

**Причина (установлено).** Touch-test был `sleep 5` + один `pgrep` — на ext4 watcher может стартовать позже. Шаг 4b допускал «stale is OK» для index/lock. PASS не проверял, что reload не убил контейнер/`serve`; `verify_closed` не требовал container running + serve alive.

**Решение.** `stand_watch_pause.md`: poll до исчезновения index cmdline и `index.lock` (без «stale is OK»); pre-touch snapshot + poll window (~30s) для новых PID/cmdline; liveness container + `serve` после reload и после touch. `stand_close_reads.md`: `verify_closed` дополнен container `running` + `serve` alive. `checks/README.md` — критерии PASS обновлены. Журнал Task 12: typo «проcedura» → «процедуры»; README отмечен как изменённый.

**Отвергнуто.** Stand proof на живом стенде; правка `daemon.toml`; git commit.

**Изменено в пакете.** `checks/stand_watch_pause.md`, `checks/stand_close_reads.md`, `checks/README.md`, этот журнал.

**Проверено.** Markdown-only; live stand не трогали.

**Откат.** Вернуть процедуры к версии до review fix. На стенд не влияет.

**Дальше.** Operator stand run → PASS обоих gate → выполнение cutover по README §«Переключение стенда».

### 2026-09-20 — Task 13: cutover runbook (docs only; cutover не выполнялся)

**Задача.** Runbook переключения стенда в README по spec §5.5: gate чат-слоя (drill бэкапа), порядок cutover (watcher pause до копии, re-check после recreate), откат (индексы к SHA исходника).

**Причина (установлено).** Спека §5.5 и §8: оператору нужен явный порядок до live cutover; execution заблокирован до Task 12 PASS.

**Решение.** Раздел «Переключение стенда» в `README.md`: ссылки на `checks/stand_watch_pause.md` и `checks/stand_close_reads.md`; gate чат-слоя с throwaway drill (вход + роли + рабочие подключения); 9 шагов cutover; rollback с reconcile индексов. Живой стенд, `UBD Config Sync`, тома, remount — **не трогали**.

**Отвергнуто.** Выполнение cutover в этой сессии; отключение `UBD Config Sync`; remount/copy production volumes; git commit.

**Изменено в пакете.** `README.md` (§«Переключение стенда»), этот журнал. `checks/`, `controller/`, systemd, bootstrap — без изменений.

**Проверено.** Markdown-only; cutover не выполнялся; stand proof Task 12 по-прежнему TBD.

**Откат.** Вернуть краткий placeholder в README §«Переключение стенда»; убрать эту запись и правки сводки. На стенд не влияет.

**Дальше.** Operator: PASS stand gates → drill бэкапа чат-слоя → выполнение cutover по runbook (отдельная сессия).

### 2026-09-20 — Whole-branch review fix (стенд не трогали)

**Задача.** Критические и важные правки после whole-branch review: reopen, closed-reads alert, alert не бросает, persist reap, gate graph FULL, bootstrap без enable timer; дешёвые: empty tree unknown, poll idle, job logs, wording `verify_closed`.

**Причина (установлено).** Default `idle_probe` False и CLI не инжектит probes — `contour update --measure` может закрыть чтение и упасть без операторского reopen. `alert.send` на отсутствующий бинарник ронял исходную ошибку. `reap_jobs` ждал контейнер и забывал `exit_code`; exit 3 терял FULL. `index_graph` стартовал FULL без проверки окна/deadline. `bootstrap.sh` делал `enable --now` до cutover шага 9. Пустое дерево в `sample_current_tree` проходило как success.

**Решение.** CLI `contour open` → `open_reads` + verify, флаги только после успеха. Алерт при любом конце с `reads_closed=1` (включая deferred). README → `checks/stand_close_reads.md` §6. `status` печатает flag vs graph inspect. `alert.send` ловит FileNotFoundError/OSError; fetch/rev-parse в try; `validate` падает если `alert.command[0]` не исполняем. Reap пишет `exit_code`/`status`/`finished_at`/`log_path`; exit 3 → `graph_full_needed`. FULL только при `may_write=ok` и remaining budget > 0. Bootstrap копирует units, не enable. `sample_current_tree` → `integrity_unknown=True`. `wait_server_idle` — poll с cap. Логи job в `/srv/ai/logs` если каталог есть.

**Отвергнуто.** git commit; живой Docker/`C:\MCP`/`C:\ai-stack`/`daemon.toml`/`UBD Config Sync`; выдуманные IdleProbe/content-probe команды.

**Изменено в пакете.** `controller/src/contour/{alert,cli,reconcile,index_graph,verify,reads,schedule}.py`, тесты, `bootstrap.sh`, `README.md`, `checks/stand_close_reads.md`, этот журнал. Отчёт: `.superpowers/sdd/final-fix-report.md`.

**Проверено.** `python -m pytest tests/test_reconcile.py tests/test_cli.py tests/test_index_graph.py tests/test_verify.py tests/test_reads.py -v` — 52 passed (7.16s). Живой стенд не меняли.

**Откат.** Вернуть перечисленные файлы к версии до этой записи. На стенд не влияет.

**Дальше.** Stand proof Task 12 на ADM-SAPHX-TECH; cutover не начинать.

### 2026-10-09 — KB nightly: полная переиндексация графа после выгрузки

**Задача.** После ночной выгрузки КБ гонять полный `metadata-indexer` (`METADATA_FORCE_REINDEX`), не `apply_changes` (база маленькая, ~10 мин).

**Причина.** Ночной `Update-And-Export-Kb.ps1` заканчивался на copy в `/srv/ai2/kb/src`; `ai2-metadata-indexer-kb` последний раз отрабатывал 2026-09-24 — граф КБ устаревал. На Windows-клиенте нет `docker compose` plugin; WSL сейчас с sharing violation — one-shot через `docker run` по эталону контейнера.

**Решение.** Шаг 3 в `tools/kb/Update-And-Export-Kb.ps1`: healthy `ai2-neo4j-kb` → `docker run --rm` с env/image/network от `ai2-metadata-indexer-kb`, `METADATA_FORCE_REINDEX=true`, лог `logs/kb/graph-reindex-*.log`. Опции профиля: `skipGraphReindex`, `graphIndexerContainer`, `graphNeo4jContainer`.

**Отвергнуто.** `apply_changes` для КБ; зависимость ночного скрипта от `docker compose` / WSL.

**Изменено.** `tools/kb/Update-And-Export-Kb.ps1`, `tools/kb/README.md`, `deploy-ai2/mcp_for_1c-kb/README.md`, этот журнал.

**Проверено.** Parse скрипта OK; neo4j-kb healthy. Полный прогон индексации в этой сессии не запускали (ждёт ближайшую ночную задачу / ручной запуск скрипта).

**Дальше.** Дождаться ночного `AI-contour KB nightly export` или руками прогнать скрипт; убедиться что `graph-reindex-*.log` exit 0.

### 2026-10-09 — UBD nightly: graph apply/FULL через `docker run` (без WSL compose)

**Задача.** Убрать зависимость ночного УБД sync от `wsl.exe` / compose plugin (Q3 FAIL при ERROR_SHARING_VIOLATION на `ext4.vhdx`).

**Причина.** Windows `docker.exe` без compose plugin; `Test-UbdComposeAvailable` требовал WSL; Docker API `:2375` при этом жив.

**Решение.** В `C:\MCP\ubd_index\scripts`: `Test-UbdIndexerDockerRunAvailable` / `New-UbdIndexerDockerRunArgs` (эталон `metadata-indexer`); `sync-and-index.ps1` — apply_changes и FULL через `docker run` + heartbeat; preflight Q3 — PASS по docker-run path. `Invoke-Compose` оставлен для `03-build-and-index.ps1`.

**Отвергнуто.** Установка compose plugin; починка VHD как единственный unblock.

**Проверено.** Parse OK; Q3 PASS (`mcp_for_1c-metadata-indexer` / `1c-suite-net`); smoke `docker run … print DOCKER_RUN_OK`. Полный nightly/apply в этой сессии не гоняли. Q5 под текущей УЗ может FAIL (задачи видны учёту `ai-docker`) — на ночном запуске раньше PASS.

**Дальше.** Ночной `UBD Config Sync` или ручной sync; отдельно code-index (`bsl-indexer` не running).
