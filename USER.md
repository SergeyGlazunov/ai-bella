# AI-Contour — для пользователя

Комплект оркестратора УБД / КБ / `klon_opl` (граф + code-index) и поставки на Windows 11 + WSL2.

Сведения о прежнем cutover и установке ниже сохранены как справка. Они не подтверждают текущий способ запуска. Перед применением сверить их с датированной проверкой стенда и [действующим порядком работы](docs/production-workflow.md).

Порядок работы: [стенд и Git](docs/production-workflow.md). Для агента разработки: [AGENT.md](AGENT.md). Состояние и журнал: [DEVELOPMENT.md](DEVELOPMENT.md). Факты стенда: [STATUS.md](STATUS.md). Манифест состава: [release-manifest.json](release-manifest.json).

Не копировать leftover с `C:\MCP`, `C:\ai-stack`, `C:\instructions-main-system_analytics`.

---

## Эксплуатация (после bootstrap)

Пути по умолчанию:

| Что | Путь |
| --- | --- |
| Пакет / CLI | `/opt/ai-contour/`, `/opt/ai-contour/venv/bin/contour` |
| Конфиг | `/etc/ai-contour/contour.yaml` |
| Данные | `/srv/ai-contour/` |
| SQLite | `/srv/ai-contour/state/state.db` |
| Lock | `/srv/ai-contour/locks/sync.lock` |
| Логи прогонов | `/srv/ai-contour/logs/runs` |
| Бэкапы state | `/srv/ai-contour/backups/state` |
| Inbox пакетов | `/srv/ai-contour/inbox/{kb,klon_opl}` |
| Пользователь сервиса | `ai-contour` |

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

- `alert.command` в конфиге (пример: `["/opt/ai-contour/alert.sh"]`).
- Текст на **stdin**; таймаут 30 с. Stub из комплекта `validate` отвергает — замените канал.

### Команды

```bash
contour --config /etc/ai-contour/contour.yaml validate
contour status
contour status --database kb          # один профиль schema v2
contour update                        # все enabled databases по порядку
contour update --database ubd         # один профиль
contour update --measure
contour intake --database kb          # claim пакета из inbox; без индексации
contour backup-state                  # SQLite online backup + prune 14d
contour open --database ubd           # только если индексы согласованы и нет writer
contour cleanup --dry-run
```

`intake` только валидирует и переносит пакет в `accepted/`; индексация — через `contour update`.

Если `reads_closed=1`, MCP остаётся закрытым до успешной проверки / `contour open`. Ручное восстановление сети: [checks/stand_close_reads.md](checks/stand_close_reads.md) §6.

### systemd

- `ai-sync.service` — `contour update` (oneshot, `User=ai-contour`).
- `ai-sync.timer` — `OnCalendar=*-*-* 01:30:00 Asia/Vladivostok`, `Persistent=true`.
- `bootstrap.sh` ставит unit-файлы, tmpfiles и logrotate **без** `systemctl enable --now`.

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
2. УБД и прочие mutable данные на **ext4** (`/srv/ai-contour/...`), не `/mnt/c`.
3. `systemd` включён. Sharing violation на `ext4.vhdx` — блокер; nsenter не использовать.

### Секреты

```bash
sudo mkdir -p /srv/ai-contour/secrets /srv/ai-contour/logs /srv/ai-contour/compose
sudo chmod 700 /srv/ai-contour/secrets
```

Скопируйте [secrets.example.yaml](secrets.example.yaml) и `compose/*/.env.example`. Не вставляйте token в URL `repo.remote` / `databases.*.source.remote`.

В `.env` графа: `MCP_SHARED_SECRET`, `NEO4J_PASSWORD`. `WORKSPACE_DIR` — путь к `src` выбранной базы после клона/intake.

### Новая установка стека

```bash
sudo cp -r compose/ai-stack /srv/ai-contour/compose/ai-stack
sudo cp -r compose/mcp_for_1c /srv/ai-contour/compose/mcp_for_1c
sudo cp -r compose/bsl-indexer /srv/ai-contour/compose/bsl-indexer

sudo mkdir -p /srv/ai-contour/sources/ubd
sudo git clone <ваш-remote> /srv/ai-contour/sources/ubd
```

```bash
cd /srv/ai-contour/compose/ai-stack && cp .env.example .env   # заполнить
docker compose -p ai-stack up -d

cd /srv/ai-contour/compose/mcp_for_1c && cp .env.example .env
# WORKSPACE_DIR=/srv/ai-contour/sources/ubd/src
docker compose -p mcp_for_1c build
docker compose -p mcp_for_1c up -d neo4j mcp-metadata-graph

cd /srv/ai-contour/compose/bsl-indexer
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
# 1) Подготовить alert.sh (не stub; сервисный пользователь должен уметь читать/исполнять)
sudo mkdir -p /opt/ai-contour
sudo cp integrations/alert.sh.example /opt/ai-contour/alert.sh   # затем заменить на реальный канал
sudo chown root:ai-contour /opt/ai-contour/alert.sh
sudo chmod 750 /opt/ai-contour/alert.sh

# 2) Bootstrap (validate и docker info — от ai-contour; units/tmpfiles/logrotate без enable timer)
sudo ./bootstrap.sh
```

Bootstrap:
- создаёт пользователя `ai-contour` и добавляет его в группу Docker-сокета (обычно `docker`);
- проверяет `docker info` **от имени** `ai-contour` — при отказе останавливается с инструкцией (сокет world-writable не открывать);
- выставляет `alert.sh` в `root:ai-contour` `0750` даже если файл уже существовал;
- запускает `contour validate` от `ai-contour`.

Если bootstrap остановился на stub alert / Docker / правах — исправьте и **повторите** `bootstrap.sh`, не только отдельный `validate`. Секреты канала алерта должны быть читаемы `ai-contour`.

Smoke после успешного bootstrap (без enable timer):

```bash
sudo -u ai-contour /opt/ai-contour/venv/bin/contour --config /etc/ai-contour/contour.yaml validate
sudo -u ai-contour docker info >/dev/null
sudo -u ai-contour test -r /etc/ai-contour/contour.yaml -w /srv/ai-contour/logs/runs
sudo -u ai-contour test -x /opt/ai-contour/alert.sh
# после первого реального state.db:
sudo -u ai-contour /opt/ai-contour/venv/bin/contour --config /etc/ai-contour/contour.yaml backup-state
```

Периодический бэкап state (вне nightly, по желанию) — той же командой `backup-state`. Отсутствующий `state.db` — ошибка (пустой файл не создаётся).

Timer включайте после первого успешного `contour update` в окне.

### Обновление существующего стенда

1. Не `docker compose pull` Open WebUI / OmniRoute.
2. Не создавать project `ai-contour`.
3. В `contour.yaml` — live-пути, если ходите в уже поднятый стенд.
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

Порядок: store update → `/UpdateDBCfg` → dump XML → пакет в inbox Linux (`/srv/ai-contour/inbox/kb`), затем `contour intake` / nightly `update`.  
Опционально Task Scheduler: задача `AI-contour KB nightly export` в **01:00**.

УБД по-прежнему только git→`sources/ubd` (не путать с пайплайном КБ).

### klon_opl: выгрузка (без хранилища)

Скрипт: [`tools/klon_opl/Export-KlonOpl.ps1`](tools/klon_opl/Export-KlonOpl.ps1), конфиг: [`tools/klon_opl/klon_opl-connection.json`](tools/klon_opl/klon_opl-connection.json).

Порядок: `/DumpConfigToFiles` → пакет в inbox. Хранилища нет.  
Task Scheduler: `AI-contour klon_opl nightly export` в **02:00**.

### Чеклист чистой установки УБД+КБ

1. WSL/Docker, memory≥16 ГБ в `.wslconfig`, перезапуск WSL.
2. `sudo ./bootstrap.sh` (создаёт `/srv/ai-contour`, `/opt/ai-contour`, `/etc/ai-contour`).
3. Скопировать compose overlays; заполнить `.env` из `.env.example`.
4. Up graph/BSL стеков по профилям баз.
5. Настроить KB/`klon_opl` package export → inbox; git УБД → `sources/ubd`.
6. Подключить MCP к сети чата; health MCP; smoke tool-call.
7. (Опц.) WebUI + карточки MCP — `deploy-ai2/ai-stack/README.md`.
8. Nightly: package exporters + `ai-sync.timer` после proof.

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
5. Клон/копия SHA в `/srv/ai-contour/sources/ubd` (ext4); remount binds; compose up **только** remounted сервисов УБД (не `ai-stack`).
6. После recreate `bsl-indexer` — снова убедиться, что watcher paused.
7. Verify индексов; открыть чтение.
8. Включить `ai-sync.timer` только после шага 7.

### Откат

1. Отключить timer; закрыть чтение.
2. Вернуть bind недостаточно, если индексы уже писались под другим SHA — согласовать source и оба индекса на одном SHA + probes, затем open.
3. Чат-слой: восстановить тома `ai-stack_*`; проверить вход и подключения.

Cutover rollback не доказан, пока не отработан drill.
