# AI-Contour — для пользователя

Комплект оркестратора УБД (граф + code-index) и поставки на Windows 11 + WSL2.

Сведения о прежнем cutover и установке ниже сохранены как справка. Они не подтверждают текущий способ запуска. Перед применением сверить их с датированной проверкой стенда и [действующим порядком работы](../docs/production-workflow.md).

Для агента разработки: [AGENT.md](AGENT.md). Состояние и журнал: [DEVELOPMENT.md](DEVELOPMENT.md). Манифест состава: [release-manifest.json](release-manifest.json).

Не копировать leftover с `C:\MCP`, `C:\ai-stack`, `C:\instructions-main-system_analytics`.

---

## Эксплуатация (после bootstrap)

Пути по умолчанию: конфиг `/opt/ai-sync/config.yaml`, SQLite `/srv/ai/state.db`, lock `/srv/ai/sync.lock`, CLI `/opt/ai-sync/venv/bin/contour`.

### Окно записи

- **01:30–06:00** `Asia/Vladivostok` (начало включительно, конец исключительно).
- Вне окна `contour update` и timer → `deferred`, без **старта** новой записи.
- Уже запущенный indexer **не** убивается из‑за конца окна или порога длительности.

### `duration_warn_seconds`

- `null` не блокирует автоматические записи внутри окна.
- Число — порог **предупреждения** (один алерт на прогон), не hard-stop.
- `build_deadline_seconds` — устаревший алиас того же порога.
- `--measure` — необязательный замер; не единственный путь записи.

### Алерт

- `alert.command` в конфиге (пример: `["/opt/ai-sync/alert.sh"]`).
- Текст на **stdin**; таймаут 30 с. Stub из комплекта `validate` отвергает — замените канал.

### Команды

```bash
contour validate          # config + adapters + alert
contour status
contour update            # reconcile (timer — то же без --measure)
contour update --measure
contour open              # только если индексы согласованы и нет writer
contour cleanup --dry-run
```

Если `reads_closed=1`, MCP остаётся закрытым до успешной проверки / `contour open`. Ручное восстановление сети: [checks/stand_close_reads.md](checks/stand_close_reads.md) §6.

### systemd

- `ai-sync.service` — `contour update` (oneshot).
- `ai-sync.timer` — `OnCalendar=*-*-* 01:30:00 Asia/Vladivostok`, `Persistent=true`.
- `bootstrap.sh` ставит unit-файлы **без** `systemctl enable --now`.

```bash
sudo ./bootstrap.sh   # внутри WSL Ubuntu
```

Не делает `docker compose up` для `ai-stack`, не создаёт project `ai-contour`.

---

## Установка с комплекта

Два сценария:

| Сценарий | Действие |
| --- | --- |
| **Новая машина** | Compose из `compose/`, клон УБД на ext4, bootstrap, свои `.env` |
| **Живой стенд** | Не трогать тома `ai-stack_*`. Не `compose up` чат-слоя. Оркестратор + timer только после stand proof и разрешения |

Живой стенд этим разделом **не** переключать.

### Что есть / чего нет

См. [release-manifest.json](release-manifest.json).

**Есть:** `compose/mcp_for_1c/`, `compose/bsl-indexer/` (исходники образов), `compose/ai-stack/` (образы с registry), `controller/`, skills markdown.

**Нет в комплекте:** содержимое Git УБД, заполненные секреты, готовый zip+checksum, JSON skills WebUI, дамп `platform-help-data`.

### Windows / WSL

1. Windows 11, WSL2 Ubuntu, Docker Engine в Linux.
2. УБД на **ext4** (`/srv/ai/ubd`), не `/mnt/c`.
3. `systemd` включён. Sharing violation на `ext4.vhdx` — блокер; nsenter не использовать.

### Секреты

```bash
sudo mkdir -p /srv/ai/secrets /srv/ai/logs /srv/ai/compose
sudo chmod 700 /srv/ai/secrets
```

Скопируйте [secrets.example.yaml](secrets.example.yaml) и `compose/*/.env.example`. Не вставляйте token в URL `repo.remote`.

В `.env` графа: `MCP_SHARED_SECRET`, `NEO4J_PASSWORD`. `WORKSPACE_DIR=/srv/ai/ubd/src` после клона.

### Новая установка стека

```bash
sudo cp -r compose/ai-stack /srv/ai/compose/ai-stack
sudo cp -r compose/mcp_for_1c /srv/ai/compose/mcp_for_1c
sudo cp -r compose/bsl-indexer /srv/ai/compose/bsl-indexer

sudo mkdir -p /srv/ai/ubd
sudo git clone <ваш-remote> /srv/ai/ubd
```

```bash
cd /srv/ai/compose/ai-stack && cp .env.example .env   # заполнить
docker compose -p ai-stack up -d

cd /srv/ai/compose/mcp_for_1c && cp .env.example .env
# WORKSPACE_DIR=/srv/ai/ubd/src
docker compose -p mcp_for_1c build
docker compose -p mcp_for_1c up -d neo4j mcp-metadata-graph

cd /srv/ai/compose/bsl-indexer
docker compose -p bsl-indexer build && docker compose -p bsl-indexer up -d
```

Если чат не видит MCP: подключить к сети WebUI явно —
`docker network connect ai-stack_default mcp-metadata-graph` (и `bsl-indexer`,
и `mcp-query-builder` при необходимости), либо
`compose/mcp_for_1c/docker-compose.webui.yml` с `WEBUI_NETWORK`. На ai2 —
сеть `ai2-stack`, не prod. Не поднимать второй Neo4j в `1c-suite-net`.

### Оркестратор

Сначала замените канал алерта (kit stub отвергается `validate`), затем bootstrap:

```bash
# 1) Скопировать пакет / подготовить alert.sh (не stub)
sudo mkdir -p /opt/ai-sync
sudo cp integrations/alert.sh.example /opt/ai-sync/alert.sh   # затем заменить на реальный канал
sudo chmod +x /opt/ai-sync/alert.sh

# 2) Bootstrap (validate внутри; units без enable)
sudo ./bootstrap.sh

# 3) config.yaml: remote УБД, alert.command
sudo chmod 600 /opt/ai-sync/config.yaml
sudo /opt/ai-sync/venv/bin/contour --config /opt/ai-sync/config.yaml validate
```

Если bootstrap остановился на stub alert — замените `/opt/ai-sync/alert.sh` и **повторите** `bootstrap.sh`, не только отдельный `validate`.

Timer включайте после первого успешного `contour update` в окне.

### Обновление существующего стенда

1. Не `docker compose pull` Open WebUI / OmniRoute.
2. Не создавать project `ai-contour`.
3. В `config.yaml` — live-пути, если ходите в уже поднятый стенд.
4. Stand proof: [checks/README.md](checks/README.md).
5. Cutover — раздел ниже, только после явного разрешения.

### Skills

`integrations/skills/webui/` и `integrations/skills/cursor/`. Импорт в Open WebUI вручную.

---

## Две конфигурации (УБД + КБ) — целевой контур / ai2

Эталон для **нового сервера** и комплекта overlays. Живой Docker на текущем стенде этим разделом **не** переключается автоматически.

Overlays и name map: [deploy-ai2/README.md](deploy-ai2/README.md).

### Топология

| | УБД | КБ | klon_opl |
|--|-----|-----|----------|
| Выгрузка | `/srv/ai2/ubd/src` (git) | `/srv/ai2/kb/src` (Designer / скрипт) | `/srv/ai2/klon_opl/src` (Designer / dump-only) |
| Graph MCP | `:8101` (`ai2-mcp-ubd`) | `:8201` (`ai2-mcp-kb`) | — |
| query-builder | `:8109` | `:8209` | — |
| BSL / search MCP | `:8111` (`ai2-bsl-ubd`) | `:8211` (`ai2-bsl-kb`) | `:8311` (`ai2-bsl-klon-opl`) |

**Нельзя:** один Neo4j или один BSL home на несколько выгрузок. Имя `opl` зарезервировано под будущую боевую базу оплат.

### Preflight

- WSL2: в `%USERPROFILE%\.wslconfig` желательно `memory=16GB`, затем `wsl --shutdown` и перезапуск Docker (иначе лимит может остаться ~12 ГБ).
- Windows-платформа 1С той же сборки, что сервер ИБ КБ (Конфигуратор + желательно ibcmd).

### КБ: выгрузка

Скрипт (Windows PowerShell): [`tools/kb/Update-And-Export-Kb.ps1`](tools/kb/Update-And-Export-Kb.ps1), конфиг рядом: [`tools/kb/kb-connection.json`](tools/kb/kb-connection.json).

Порядок: store update → `/UpdateDBCfg` → dump XML → копия в `/srv/ai2/kb/src`.  
Опционально Task Scheduler: задача `AI-contour KB nightly export` в **01:00**.

После появления `Configuration.xml` в kb/src — indexer графа КБ и старт `ai2-bsl-kb`.

УБД по-прежнему только git→`ubd/src` (не путать с пайплайном КБ).

### klon_opl: выгрузка (без хранилища)

Скрипт: [`tools/klon_opl/Export-KlonOpl.ps1`](tools/klon_opl/Export-KlonOpl.ps1), конфиг: [`tools/klon_opl/klon_opl-connection.json`](tools/klon_opl/klon_opl-connection.json).

Порядок: `/DumpConfigToFiles` → копия в `/srv/ai2/klon_opl/src`. Хранилища нет.  
Task Scheduler: `AI-contour klon_opl nightly export` в **02:00**. После dump — `ai2-bsl-klon-opl` (`:8311`).

### Чеклист чистой установки УБД+КБ

1. WSL/Docker, memory≥16 ГБ в `.wslconfig`, перезапуск WSL.
2. `mkdir -p /srv/ai2/{ubd/src,kb/src,logs,compose}`.
3. Скопировать `compose/mcp_for_1c`, `compose/bsl-indexer` и overlays из `deploy-ai2/mcp_for_1c-ubd|kb`, `deploy-ai2/bsl-ubd|kb`; заполнить `.env` из `.env.example`.
4. Up `ai2-mcp-ubd` (neo4j + graph + query-builder; + indexer при наличии XML УБД).
5. Up `ai2-bsl-ubd`.
6. Настроить КБ dump → `/srv/ai2/kb/src`; up `ai2-mcp-kb` (graph + query) + indexer; up `ai2-bsl-kb`.
7. Подключить MCP к сети чата (`ai2-stack`); health MCP; smoke tool-call.
8. (Опц.) WebUI + карточки MCP (graph/query/BSL × базы) + Vision Bridge — `deploy-ai2/ai-stack/README.md`.
9. Nightly: KB script 01:00; УБД sync по своему окну / contour.

### Архив комплекта

```bash
tar -czf ai-contour-kit.tgz --exclude=.git --exclude='**/.env' --exclude='**/state.db' --exclude='**/target' .
sha256sum ai-contour-kit.tgz
```

---

## Переключение стенда (cutover)

**Не выполнять** без PASS stand gates, drill бэкапа чат-слоя и явного разрешения. Процедуры: [checks/stand_watch_pause.md](checks/stand_watch_pause.md), [checks/stand_close_reads.md](checks/stand_close_reads.md).

### Gate чат-слоя

1. Остановить вместе `open-webui` и `omniroute` (`C:\ai-stack`).
2. Скопировать тома `ai-stack_open-webui-data`, `ai-stack_omniroute-data`, `.env`, compose.
3. Поднять снова с теми же именами томов.

**Drill:** восстановить в **throwaway** тома/контейнеры с **другими** именами; вход существующим пользователем; роли; рабочие модель и MCP. Строк в БД users недостаточно.

### Порядок cutover (УБД и индексы)

1. Отключить `UBD Config Sync`; дождаться writers.
2. Закрыть чтение: graph stop + BSL network disconnect (+ optional host firewall) + IdleProbe. См. close-reads check.
3. Pause BSL watcher (`daemon.toml` без `/repos/SRC` + reload).
4. Зафиксировать SHA HEAD УБД.
5. Клон/копия SHA в `/srv/ai/ubd` (ext4); remount binds; compose up **только** remounted сервисов УБД (не `ai-stack`).
6. После recreate `bsl-indexer` — снова убедиться, что watcher paused.
7. Verify индексов; открыть чтение.
8. Включить `ai-sync.timer` только после шага 7.

### Откат

1. Отключить timer; закрыть чтение.
2. Вернуть bind недостаточно, если индексы уже писались под другим SHA — согласовать source и оба индекса на одном SHA + probes, затем open.
3. Чат-слой: восстановить тома `ai-stack_*`; проверить вход и подключения.

Cutover rollback не доказан, пока не отработан drill.
