# AI-Contour — для агента

Источник правды по продукту и контрактам. Состояние стенда/пакета и журнал решений — **[DEVELOPMENT.md](DEVELOPMENT.md)** (сначала его). Эксплуатация и установка для человека — **[USER.md](USER.md)**.

Не называть код пакета внедрённым, пока он не переключён на стенде. Живой стенд не переключать без явного разрешения. История чата не нужна.

Обычные технические решения внутри этого документа — самостоятельно. Блокер — только нет доступа к стенду/данным или подтверждённая невозможность требования. Нужен доступ к **существующей реализации стенда** (команды дельты/FULL индексаторов); не выдумывать API.

## 1. Что строим

Один внутренний веб-сервис для аналитиков по выгрузке УБД: метаданные, BSL; опционально Confluence, SDMS, MDD.

- Git — только изменения (fetch + FF), не полная выгрузка каждый раз.
- Индексаторы — дельта по возможности; FULL только по контракту индексатора (не еженочная норма).
- Одна рабочая копия и одни индексы; **вторых поколений нет**.
- Чтение УБД закрывается на время записи; открывается только после проверки согласованности.
- Живые Open WebUI и OmniRoute сохранить; переустановка/смена образов чат-слоя в миграцию **не** входят.

**Не входит в v1:** поколения индексов, marketplace MCP/Skills, КБ/МДМ, webhook, Golden Image, универсальный Windows-installer, HA. Skills/Tools **в комплекте** — markdown в `integrations/skills/` (не отдельный продукт).

## 2. Принципы

1. Чиним то, что ломало стенд: DrvFs, portproxy, «git pull = успех».
2. Один оркестратор; PowerShell не дублирует git/index.
3. Одна копия данных; дельта по умолчанию; без blue-green поколений индексов.
4. Незавершённый или непроверенный индекс не маскировать успехом; при необходимости отключать только затронутый инструмент.
5. До записи индекса — прежние данные доступны. После частичной перезаписи — затронутый Graph/BSL может быть временно недоступен; остальные независимые базы продолжают работать. Исходники сохранять для достройки/пересборки.
6. Секреты не в образе, не в argv и не в тексте ошибок.
7. Приоритет: штатная ночь, восстановление после stop/reboot, понятная ошибка, повтор одной базы, данные для дашборда. Не раздувать защиту от злонамеренных/экзотических FS-сценариев на контролируемом стенде.
8. Чат-слой не трогать без явного задания.
9. Живой стенд не переключать без отдельного разрешения.

## 3. Стек и пути

| Слой | Решение |
| --- | --- |
| Хост | Windows 11 + WSL2 |
| УБД (целевое) | `/srv/ai/ubd` на ext4 (не `/mnt/c`) |
| Sync | Python в WSL + `systemd.timer` |
| Состояние | SQLite `/srv/ai/state.db`, lock `/srv/ai/sync.lock` |
| Пакет | `/opt/ai-sync/`, `config.yaml`, `bootstrap.sh` |

Живой стенд (факт): projects `ai-stack`, `mcp_for_1c`, `bsl-indexer`; тома `ai-stack_*`, `mcp_for_1c_neo4j-data`, `bsl-indexer_bsl-indexer-home`. Не создавать project `ai-contour`.

Код: `controller/`. Адаптеры: `integrations/`. Stand gates: `checks/`. Vendor compose: `compose/` (их upstream README не схлопывать).

## 4. Конфиг (суть)

См. [contour.example.yaml](contour.example.yaml). Ключевое:

- окно `01:30–06:00` `Asia/Vladivostok`; вне окна — `deferred`, без старта новой записи;
- `duration_warn_seconds: null` не блокирует запись в окне (не hard-stop);
- `alert.command` — один канал, текст на stdin;
- `kb` / `mdm` в v1 — ошибка validate.

## 5. SQLite / ready

Компоненты `source | code_index | graph`: `applied_commit`, `status`, `storage_id`, `indexer_version`, container/exec ids.

```text
ready =
  status == ready
  AND applied_commit == target
  AND storage instance совпадает (не только имя volume)
  AND indexer_version совпадает
```

Писатель: create контейнер/exec → записать id в job → start. Не `--rm` без учёта.

## 6. Reconcile (инварианты)

Полный порядок — в коде `controller/src/contour/reconcile.py`. Контракты:

1. `flock`; Docker ping; добить/наблюдать живые jobs.
2. Адаптеры probes/idle **до** close.
3. **Close reads (обязательное):** graph `docker stop` до not running; BSL — `docker network disconnect` от WebUI-сети; контейнер BSL и `serve` живы. Host firewall DROP на publish-порт **optional** (best-effort); `verify_closed` = graph + network. IdleProbe: `integrations/idle_bsl.sh`. Не `daemon stop` / не `docker stop bsl-indexer`.
4. Pause BSL watch: убрать `/repos/SRC` из `daemon.toml`, reload (`integrations/reload_bsl_watch.sh`; fallback SIGHUP).
5. Git FF на target; BSL index без `--force` из лога; graph FULL только exit **3**.
6. Verify по diff `applied_commit..target` каждого индекса + content probes; затем open + verify open.
7. Успех только при согласованных индексах; иначе чтение закрыто + алерт.

## 7. Запреты

- Поколения / `active.json` / вторая копия индексов.
- `compose pull`/recreate open-webui/omniroute; `down -v`; новый project с пустыми томами чата.
- Cutover без PASS gates + drill бэкапа чат-слоя + явного разрешения.
- Открыть MCP после bind-swap без reconcile индексов на одном SHA.
- Секреты в DEVELOPMENT / логах / argv.

## 8. Приёмка (кратко)

Существующий пользователь WebUI; те же модели; УБД-инструменты отвечают; вне окна запись не стартует; kill оркестратора не даёт второго писателя; failed → алерт с базой/этапом/revision/логом; затронутый инструмент недоступен до успешного восстановления; остальные независимые базы не маскируются как упавшие вместе с ним.

## 9. Журнал

Правила ведения [DEVELOPMENT.md](DEVELOPMENT.md): сводка в начале; датированные записи (задача, причина, решение, проверка, откат); различать пакет и живой стенд; без секретов и пересказа чата.
