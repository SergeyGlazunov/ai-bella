# Linux-first preflight (read-only inventory)

**Purpose:** Собрать факты стенда **до** catch-up / cutover. Ничего не менять.  
**When:** Перед Task 12 Step 1–2. После PASS kit Task 11.  
**Stand:** только с явным разрешением оператора. Этот чеклист можно прогнать и на throwaway WSL.

**Config source of truth:** `/etc/ai-contour/contour.yaml` (после bootstrap) или kit `contour.example.yaml` для ожидаемых имён. Не выдумывать container/volume names.

---

## Stop conditions (немедленная остановка)

- Любая команда ниже требует записи / restart / `systemctl enable` — **стоп**, это не preflight.
- Обнаружены **два writers** одновременно (legacy Task Scheduler indexing + `contour update` / timer) — **стоп**, зафиксировать evidence.
- Не хватает места на диске для `/srv/ai-contour` + индексов — **стоп**, не начинать catch-up.
- Секреты в Git / в argv / в chat log — **стоп**, вычистить до продолжения.

---

## Prerequisites

- Доступ read-only к Docker Engine и к WSL/Linux хосту контура.
- Известен путь конфига: `/etc/ai-contour/contour.yaml` или рабочая копия.
- Именование сервисов сверять с конфигом `databases.*.services.*`, не с памятью.

---

## 0. Evidence bundle

Сохранить вывод всех шагов в каталог evidence (пример):

```bash
EVIDENCE=~/contour-preflight-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$EVIDENCE"
```

Rollback point для preflight: **не требуется** (только чтение). Rollback assets для cutover фиксируются в catch-up/cutover runbook.

---

## 1. Host / disk (read-only)

```bash
uname -a | tee "$EVIDENCE/uname.txt"
df -h /srv /var / 2>/dev/null | tee "$EVIDENCE/df.txt"
id ai-contour 2>/dev/null | tee "$EVIDENCE/ai-contour-id.txt" || true
systemctl is-enabled ai-sync.timer 2>/dev/null | tee "$EVIDENCE/timer-enabled.txt" || echo "timer-absent" | tee "$EVIDENCE/timer-enabled.txt"
systemctl is-active ai-sync.timer 2>/dev/null | tee "$EVIDENCE/timer-active.txt" || true
```

**Pass:** `/srv` или целевой том имеет запас; timer **не** enabled до cutover.  
**Fail:** timer уже enabled без разрешения → stop.

---

## 2. Contour config inventory

```bash
CFG=${CFG:-/etc/ai-contour/contour.yaml}
test -r "$CFG" && echo "cfg-ok $CFG" | tee "$EVIDENCE/cfg-path.txt"
# List enabled databases and service names without dumping secrets:
python3 - <<'PY' | tee "$EVIDENCE/cfg-summary.txt"
import os, yaml
from pathlib import Path
p = Path(os.environ.get("CFG", "/etc/ai-contour/contour.yaml"))
raw = yaml.safe_load(p.read_text(encoding="utf-8"))
print("schema_version", raw.get("schema_version"))
rt = raw.get("runtime") or {}
for k in ("root", "db_path", "lock_path", "logs_path", "backups_path"):
    print(f"runtime.{k}", rt.get(k))
for name, db in (raw.get("databases") or {}).items():
    src = (db.get("source") or {})
    svc = (db.get("services") or {})
    print(name, "enabled=", db.get("enabled"), "source=", src.get("type"),
          "layers=", db.get("required_layers"),
          "graph_mcp=", svc.get("graph_mcp"),
          "bsl=", svc.get("bsl_container"))
alert = (raw.get("alert") or {}).get("command")
print("alert.command[0]", (alert or [None])[0])
PY
```

**Pass:** три профиля (`ubd`, `kb`, `klon_opl`) согласованы с ожиданиями; в summary нет token/password.  
**Fail:** в выводе есть credentials → stop, править remote/env.

---

## 3. Docker inventory (read-only)

```bash
docker info >/dev/null && echo docker-ok | tee "$EVIDENCE/docker-ok.txt"
docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}' | tee "$EVIDENCE/docker-ps.txt"
docker network ls | tee "$EVIDENCE/networks.txt"
docker volume ls | tee "$EVIDENCE/volumes.txt"
```

Сверить имена из шага 2 с `docker-ps` / volumes. Не `docker stop` / `rm` / `compose down`.

---

## 4. Legacy Windows writers (from Windows host)

Read-only:

```powershell
Get-ScheduledTask | Where-Object { $_.TaskName -match 'UBD|KB|klon|AI-contour' } |
  Select-Object TaskName, State |
  Format-Table -AutoSize |
  Out-String | Set-Content $env:TEMP\contour-tasks.txt
```

**Pass:** зафиксированы имена indexing vs export-only задач.  
**Fail:** неизвестные writers на indexing — уточнить до catch-up.

---

## 5. Source / index fingerprints (read-only)

```bash
# Paths from CFG runtime + databases.*.source — substitute from cfg-summary
# Example placeholders only:
# ls -la /srv/ai-contour/sources/ubd /srv/ai-contour/inbox/kb
# git -C /srv/ai-contour/sources/ubd rev-parse HEAD
# sqlite3 /srv/ai-contour/state/state.db 'select database, published_revision, lifecycle_status from database_state;'
```

Не запускать `contour update` / exporters в preflight.

---

## Mutating commands

**None.** Preflight is read-only.

---

## Concurrent writers

Запрещено: legacy indexing (PS nightly index) **и** `contour update` / `ai-sync.timer` в одном окне. Preflight только документирует текущее состояние; fence — в catch-up.

---

## Pass criteria

- Evidence directory заполнен.
- Timer не enabled (или explicit waiver в STATUS).
- Имена сервисов сверены с конфигом.
- Нет секретов в сохранённых логах.
