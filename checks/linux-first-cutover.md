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

1. `contour backup-state` свежий
2. Chat volumes backup (throwaway restore уже доказан)
3. Список Scheduled Tasks: какие indexing останутся disabled
4. `systemctl is-enabled ai-sync.timer` = disabled
5. Published revisions всех трёх баз

**Rollback after enable:**

```bash
sudo systemctl disable --now ai-sync.timer
# Restore state.db from backup if contour night corrupted state
# Re-enable legacy indexing tasks only if operator accepts dual-horizon rollback
```

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

### 1. Final fence of Windows indexing

```powershell
# Disable indexing tasks from preflight inventory (not export-only)
Disable-ScheduledTask -TaskName 'UBD Config Sync'
# Keep KB / klon_opl *export* tasks if they only publish packages to inbox
```

### 2. Enable WSL timer (only after catch-up PASS)

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
