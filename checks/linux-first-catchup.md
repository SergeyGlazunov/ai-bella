# Linux-first catch-up (controlled one-shot)

**Purpose:** Один контролируемый прогон `contour` по UBD / KB / `klon_opl` **без** включения timer и без постоянного отключения legacy (только fence на окно).  
**When:** После PASS preflight + backup/rollback drill. Task 12 Steps 3–6.  
**STAND ONLY** с явным разрешением.

---

## Stop conditions

- Legacy indexing writer всё ещё активен в том же окне, что и `contour update` — **стоп**.
- `contour update` пишет, а probes/verify падают — **не** `open` вручную «чтобы заработало»; оставить reads closed для затронутой базы, сохранить логи, откат по rollback point.
- Нехватка места / Docker engine down mid-run — **стоп**, не enable timer.
- Любое изменение chat-образов Open WebUI / OmniRoute — **стоп** (вне scope).
- Несовпадение revision между source и published layers после прогона — **стоп**, не объявлять catch-up PASS.

---

## Prerequisites

- PASS [`linux-first-preflight.md`](linux-first-preflight.md).
- Drill бэкапа SQLite (`contour backup-state`) и chat-layer (см. USER.md) записан в STATUS.
- Bootstrap уже ставил пакет (`/opt/ai-contour`), конфиг `/etc/ai-contour/contour.yaml`, user `ai-contour`; timer **не** enabled.
- Имена из конфига: `databases.*.services.*`, `runtime.*`.

---

## Rollback point

Зафиксировать **до** mutating steps:

1. `contour backup-state` (путь из `runtime.backups_path`) — снимок для справки, не единственный откат.
2. Список Scheduled Tasks (indexing vs export-only) и их State.
3. `git rev-parse` / package export_id текущих published sources.
4. `systemctl is-enabled ai-sync.timer` (= disabled).
5. Inventory активных writers: процессы PS/1С dump, `docker ps` indexer/job containers, SQLite `jobs` со status not in (ok,failed,removed).

### Откат: до записи vs после записи

**A. Отказ до activate / до старта indexer jobs** (source на диске ещё старый):

- Не enable timer.
- Можно оставить текущую SQLite (она ещё согласована со старым source).
- Снять fence legacy только после проверки, что contour writers нет (см. «Wait for idle writers» ниже).

**B. После activate или частичной индексации** (на диске уже новая revision / частичный Graph или BSL):

- **Не** восстанавливать старый `state.db` поверх живого сервиса — это сотрёт сведения о jobs/reads_closed и разъедет SQLite с диском.
- Предпочтительно: сохранить **актуальную** SQLite и довести **одну** базу через `contour update --database NAME` / recovery (достройка или пересборка слоя из сохранённых исходников) → probes → open.
- Возврат к legacy indexing — только если оператор явно выбирает dual-horizon rollback: сначала исключить активные contour writers (timer/service/jobs idle), согласовать source + required layers на выбранной revision (при необходимости пересборка), probes, затем чтение; **потом** включать legacy schedule. Не «включить PS и надеяться».

Полный snapshot всех индексных томов / вторые поколения — **не** требуются; достаточно явной достройки/пересборки из исходников.

---

## Mutating commands (ordered)

### 1. Fence legacy schedules (future starts only)

На Windows: временно **Disable** indexing tasks (не export-only). Export KB/`klon_opl` можно оставить или один раз прогнать вручную до intake.

```powershell
# Example names — replace with inventory from preflight
Disable-ScheduledTask -TaskName 'UBD Config Sync'
# Do NOT disable export-only tasks unless operator agrees
Get-ScheduledTask -TaskName 'UBD Config Sync' | Select-Object TaskName, State
```

`Disable-ScheduledTask` **не** останавливает уже бегущий процесс. Дальше — обязательный wait.

### 1b. Wait for idle writers (обязательно)

До `contour update` и до любого возврата legacy:

1. Дождаться конца текущих indexing tasks (State ≠ Running; нет дочерних dump/indexer процессов из inventory).
2. `docker ps` — нет one-shot index/job containers для этих баз; permanent MCP могут быть running.
3. SQLite: нет `jobs` в статусах created/running/paused для целевых databases (или понятный deferred после reap).
4. При **unknown** writer state — **стоп**, второй механизм не запускать. Не использовать kill indexer как штатное завершение.

Повторить эту проверку **непосредственно перед** шагом 4 (`contour update`).

**Evidence:** task State, `docker ps`, выборка jobs, timestamp.

### 2. Install / validate (if not already)

```bash
sudo ./bootstrap.sh   # must NOT enable timer
sudo -u ai-contour /opt/ai-contour/venv/bin/contour --config /etc/ai-contour/contour.yaml validate
sudo -u ai-contour docker info >/dev/null
```

### 3. Transfer test / real packages

```bash
# Windows exporters → inbox paths from config (databases.kb.source.inbox etc.)
# Then on Linux:
sudo -u ai-contour /opt/ai-contour/venv/bin/contour --config /etc/ai-contour/contour.yaml intake --database kb
sudo -u ai-contour /opt/ai-contour/venv/bin/contour --config /etc/ai-contour/contour.yaml intake --database klon_opl
```

Incomplete packages (no READY) must be ignored (non-zero intake / no claim).

### 4. One controlled update

Снова выполнить §1b (idle writers). Предпочтительно внутри окна `01:30–06:00` Asia/Vladivostok, иначе ожидать `deferred`.

```bash
sudo -u ai-contour /opt/ai-contour/venv/bin/contour --config /etc/ai-contour/contour.yaml update
# or per database:
# ... update --database ubd
```

### 5. Verify

```bash
sudo -u ai-contour /opt/ai-contour/venv/bin/contour --config /etc/ai-contour/contour.yaml status
# SQLite:
# sqlite3 /srv/ai-contour/state/state.db \
#  "select database, published_revision, lifecycle_status from database_state;"
#  "select database, layer, status, applied_revision from layer_state;"
```

Проверить MCP/WebUI smoke **без** смены образов чата.

---

## Concurrent writers

На всём catch-up окне: **только** contour (или только legacy). Запрет dual-write на одни и те же volumes.

---

## Evidence to save

- validate / update / status stdout
- `jobs.log_path` из SQLite для failed/ok writers
- backup-state path
- task disable/enable timestamps

---

## Pass criteria

- Все enabled profiles: `lifecycle_status=ready`, layers `ready`, probes ok.
- Timer всё ещё **disabled**.
- Legacy indexing остаётся disabled до cutover **или** явно возвращён только после успешного отката contour writers (оператор решает; default — держать fence до cutover Step 7).
