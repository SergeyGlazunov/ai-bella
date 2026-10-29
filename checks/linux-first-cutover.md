# Linux-first scheduling cutover

**Purpose:** Передать nightly orchestration на WSL `ai-sync.timer`; Windows остаётся export-and-transfer only.  
**When:** После PASS catch-up + user-facing verify. Task 12 Steps 7–9.  
**STAND ONLY.** Без явного разрешения оператора — не выполнять.

См. также [USER.md](../USER.md) §«Переключение стенда» и gates [`stand_watch_pause.md`](stand_watch_pause.md), [`stand_close_reads.md`](stand_close_reads.md).

---

## Stop conditions

- Catch-up не PASS — **не** enable timer.
- Chat-layer backup drill не доказан — **стоп**.
- Обнаружен concurrent legacy indexing + contour — **стоп**, disable timer immediately.
- Первая ночь после enable: любой profile `failed` / probes fail / unknown writer — **стоп**, disable timer, leave reads closed per design, escalate.
- Любой `docker compose pull` Open WebUI / OmniRoute в этом runbook — **запрещено**.

---

## Prerequisites

- PASS [`linux-first-preflight.md`](linux-first-preflight.md)
- PASS [`linux-first-catchup.md`](linux-first-catchup.md)
- Backup/rollback drill записан в STATUS
- Имена сервисов и путей — только из `/etc/ai-contour/contour.yaml`

---

## Rollback point

До Step «enable timer»:

1. `contour backup-state` свежий (справка; не единственный откат после записи)
2. Chat volumes backup (throwaway restore уже доказан)
3. Список Scheduled Tasks: какие indexing останутся disabled
4. `systemctl is-enabled ai-sync.timer` = disabled
5. Published revisions всех трёх баз
6. Inventory writers (процессы, indexer containers, SQLite jobs)

### Rollback after enable — до vs после ночной записи

**Общее (сразу):**

```bash
sudo systemctl disable --now ai-sync.timer
# Останавливает timer и *текущий* oneshot ai-sync.service, если он ещё в unit.
# Не убивает уже отделившиеся indexer containers — их дождаться / проверить (§ wait).
```

Затем § «Wait for idle writers»: нет running `ai-sync.service`, нет job containers, SQLite jobs не created/running. При unknown — не включать legacy.

**A. Ночь ещё не писала / activate не было:** актуальная SQLite согласована со старым source — rollback без restore старого `state.db`.

**B. После activate / частичной индексации:** **не** накатывать старый `state.db` на живой контур. Сохранить текущую SQLite; довести базу через contour (достройка/пересборка из исходников) → probes → open. Legacy schedule — только после idle writers + согласованных source/layers/probes (см. catch-up rollback B и USER.md).

Не удалять rollback assets в observation period.

---

## Read-only inventory (re-check)

```bash
systemctl is-enabled ai-sync.timer
systemctl cat ai-sync.service ai-sync.timer
sudo -u ai-contour /opt/ai-contour/venv/bin/contour --config /etc/ai-contour/contour.yaml status
```

Windows: подтвердить, что indexing tasks disabled; export-only tasks enabled.

---

## Mutating commands (ordered)

### 1. Final fence of Windows indexing schedules

```powershell
# Disable indexing tasks from preflight inventory (not export-only)
Disable-ScheduledTask -TaskName 'UBD Config Sync'
Get-ScheduledTask -TaskName 'UBD Config Sync' | Select-Object TaskName, State
# Keep KB / klon_opl *export* tasks if they only publish packages to inbox

# Prefer direct GitLab remote in contour.yaml (token in /srv/ai-contour/secrets/git-credentials).
# Only if remote is still a Windows path mirror, register git-only sync:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\MCP\ubd_index\Register-UbdGitSync.ps1
```

Disable запрещает **будущие** старты. Уже бегущий PS/indexer продолжит работу — см. wait.
**Обязательно:** `databases.ubd.source.remote` = GitLab URL (или свежий Windows-клон + Git Sync). Без этого contour индексирует застывший tip.

### 1b. Wait for idle writers (обязательно)

Перед enable timer и перед любым возвратом legacy:

1. Scheduled indexing tasks не Running; нет процессов из preflight inventory.
2. `systemctl is-active ai-sync.service` → inactive (если unit есть).
3. `docker ps` — нет contour/index one-shot jobs; MCP permanent OK.
4. SQLite jobs не в created/running/paused (или понятный post-reap idle).
5. Unknown writer → **стоп**, timer не enable, legacy не возвращать. Kill indexer не штатный способ.

### 2. Enable WSL timer (only after catch-up PASS + §1b idle)

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai-sync.timer
systemctl list-timers ai-sync.timer
```

Записать next trigger time в STATUS.

### 3. Do not run ad-hoc update unless operator needs catch-up gap fill

Timer owns the night. Manual `contour update` только по явной команде.

---

## First scheduled night — verify

После срабатывания timer:

```bash
sudo -u ai-contour /opt/ai-contour/venv/bin/contour --config /etc/ai-contour/contour.yaml status
# sqlite3 ... database_state / layer_state / runs / jobs
journalctl -u ai-sync.service -n 200 --no-pager
ls -la /srv/ai-contour/logs/runs
```

**Pass:** все enabled databases published; layers ready; alerts понятны или отсутствуют; log_path указывает под `runtime.logs_path`.  
**Fail:** disable timer (см. rollback), не «чинить» параллельным PS indexing.

---

## Concurrent writers

После cutover: **единственный** nightly writer — `ai-sync.timer` → `contour update`.  
Windows: только package export / git fetch side-effects вне Linux index volumes по контракту Task 9.

---

## Evidence to save

- enable timestamp + next trigger
- first-night status / sqlite dumps / journalctl
- confirmation export-only tasks still run; indexing stay disabled
- STATUS.md update (stand proof, not kit-only)

---

## Pass criteria

- Timer enabled and first night green for ubd + kb + klon_opl
- Legacy indexing disabled for agreed observation period
- Rollback assets retained
- STATUS closed for migration stage with actual paths
