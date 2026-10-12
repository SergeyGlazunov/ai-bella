# Stand gate: BSL file-watch pause

**Host:** ADM-SAPHX-TECH (боевой контур).  
**Purpose:** Доказать, что снятие `/repos/SRC` из `daemon.toml` + reload останавливает watcher без убийства `serve` / контейнера. Нужно перед cutover.  
**Package:** `contour.watch_bsl.pause_watch` / `restore_watch`.

Имена и пути ниже — **prod** (`bsl-indexer`, `C:\MCP\…`). Для ai2: контейнер/тома/выгрузка из [deploy-ai2/README.md](../deploy-ai2/README.md) и `deploy-ai2/bsl-ubd/`.

**Do not use:** `bsl-indexer daemon stop`, `docker stop bsl-indexer` (`entrypoint.sh` uses `wait -n`; stopping the daemon kills the container).

**Process probes:** в образе часто нет `pgrep` — предпочитай `docker top bsl-indexer` с паттерном `[b]sl-indexer …`. Не `sh -c 'pgrep … "bsl-indexer …"'` (wrapper сам попадает в match).

---

## Preconditions

- Docker Engine reachable (`DOCKER_HOST=tcp://127.0.0.1:2375` or local socket in WSL).
- Container `bsl-indexer` is **running**.
- Graph MCP and Open WebUI may stay up; this gate is about the BSL watcher only.
- Note current time; avoid running during active `UBD Config Sync` if possible (or accept that an in-flight nightly index may need to finish first).
- Paths (adjust only if your stand differs — record actual paths in `DEVELOPMENT.md` after the run):

| Item | Expected path |
| --- | --- |
| `daemon.toml` (host) | `C:\MCP\code_index\deploy\daemon.toml` |
| `daemon.toml` (WSL) | `/mnt/c/MCP/code_index/deploy/daemon.toml` |
| UBD tree to touch | `C:\MCP\ubd_config\src\` (bind → `/repos/SRC` in container) |
| Backup file | `C:\MCP\code_index\deploy\daemon.toml.bak.stand12` |

---

## 1. Backup

From WSL (Ubuntu) or PowerShell with access to the file:

```bash
cp /mnt/c/MCP/code_index/deploy/daemon.toml \
   /mnt/c/MCP/code_index/deploy/daemon.toml.bak.stand12
```

Verify backup contains a `[[paths]]` block with `path = "/repos/SRC"` (or equivalent quoting):

```bash
grep -F '/repos/SRC' /mnt/c/MCP/code_index/deploy/daemon.toml.bak.stand12
```

**Fail** if backup is missing or has no SRC path — do not edit live toml.

---

## 2. Pause watch (remove SRC path)

Edit **only** the live `daemon.toml`: remove the entire `[[paths]]` section whose `path` is `/repos/SRC`. Leave other `[[paths]]` entries and `listen` unchanged.

Example — before:

```toml
[[paths]]
path = "/repos/SRC"
```

After edit that block must be **absent**. Do not remove unrelated paths.

---

## 3. Reload daemon config

Trigger reload after saving the file. **Record the exact command that works** in `DEVELOPMENT.md` (stand section).

```bash
# Primary (needs CODE_INDEX_HOME; from Git Bash set MSYS_NO_PATHCONV=1):
docker exec -e CODE_INDEX_HOME=/data/code-index-home bsl-indexer bsl-indexer daemon reload

# Fallback if HTTP reload fails (stale daemon.json http_port after empty paths):
docker exec -e CODE_INDEX_HOME=/data/code-index-home bsl-indexer sh -c \
  'kill -HUP "$(cat /data/code-index-home/daemon.pid)"'

# Or: integrations/reload_bsl_watch.sh
```

---

## 4. Confirm reload (path absent)

All checks below must pass. Use whichever commands worked on the stand; placeholders until first run.

### 4a. Active config has no `/repos/SRC`

```bash
docker exec -e CODE_INDEX_HOME=/data/code-index-home bsl-indexer \
  bsl-indexer daemon status --json
# Pass: "paths": [] (no "/repos/SRC")
```

**Pass:** output / health / `config_reload` status shows **no** `/repos/SRC` in watched paths.

### 4b. Wait for in-flight index (if any)

Before the touch test, **poll until gone** — do not proceed while an index worker or lock remains:

```bash
while docker top bsl-indexer 2>/dev/null | grep -Eq '[b]sl-indexer index'; do
  echo "waiting: index worker still running..."
  sleep 2
done
while docker exec bsl-indexer test -f /data/code-index-home/index.lock; do
  echo "waiting: index.lock still present..."
  sleep 2
done
echo no-index-no-lock
```

**Note (stand 2026-09-21):** image has no `pgrep` — use `docker top`, not `docker exec … pgrep`.

**Pass:** no `bsl-indexer index` process **and** `index.lock` absent. Proceed only when both are true.

Sleep-N alone is **not** sufficient — wait on process/lock evidence. “Stale is OK” is **not** acceptable.

### 4c. Container + `serve` still alive (after reload)

Reload must not kill the container or the MCP server process:

```bash
[ "$(docker inspect -f '{{.State.Status}}' bsl-indexer)" = "running" ] && echo container-running
docker top bsl-indexer | grep -Eq '[b]sl-indexer serve' && echo serve-alive
```

**Pass:** container status is `running` **and** a `serve` (or health) process is present.

**Fail:** container not `running`, or `serve` gone → **blocker** (reload may have killed the daemon). Restore toml (section 6) before leaving the stand.

---

### 5. Touch test (watcher must stay silent)

На ext4 событие доходит с задержкой — один poll сразу после `touch` недостаточен. Снимок **до** touch, затем окно ~30 с.

```bash
# Предпочитай docker top (в образе может не быть pgrep)
docker top bsl-indexer 2>/dev/null | grep -E '[b]sl-indexer index' | sort \
  | tee /tmp/stand12-pre-index.txt || true

touch /mnt/c/MCP/ubd_config/src/.stand12-watch-test-$$

WATCH_FAIL=0
for _ in $(seq 1 15); do
  sleep 2
  docker top bsl-indexer 2>/dev/null | grep -E '[b]sl-indexer index' | sort \
    > /tmp/stand12-cur-index.txt || true
  if [ -s /tmp/stand12-pre-index.txt ] || [ -s /tmp/stand12-cur-index.txt ]; then
    if comm -13 /tmp/stand12-pre-index.txt /tmp/stand12-cur-index.txt 2>/dev/null | grep -q .; then
      echo "FAIL: new index cmdline after touch:"
      comm -13 /tmp/stand12-pre-index.txt /tmp/stand12-cur-index.txt
      WATCH_FAIL=1
      break
    fi
  fi
done
rm -f /tmp/stand12-pre-index.txt /tmp/stand12-cur-index.txt
[ "$WATCH_FAIL" -eq 0 ] && echo touch-ok-no-new-index

[ "$(docker inspect -f '{{.State.Status}}' bsl-indexer)" = "running" ] && echo container-still-running
docker top bsl-indexer | grep -Eq '[b]sl-indexer serve' && echo serve-still-alive
```

Optional — remove the test file after the check:

```bash
rm -f /mnt/c/MCP/ubd_config/src/.stand12-watch-test-$$
```

**Pass:** **no new** index cmdline during the poll window; container still `running`; `serve` still alive.

**Fail:** new index after touch; container/`serve` died → **cutover blocker**. Restore toml (section 6).

---

## 6. Restore

Always restore before ending the session (even on pass):

```bash
cp /mnt/c/MCP/code_index/deploy/daemon.toml.bak.stand12 \
   /mnt/c/MCP/code_index/deploy/daemon.toml
# TBD — same reload command as step 3 (integrations/reload_bsl_watch.sh)
```

Confirm `/repos/SRC` is back in active config (repeat step 4a inspect).

Keep `daemon.toml.bak.stand12` until results are recorded in `DEVELOPMENT.md`; delete only after operator sign-off.

---

## Pass / fail criteria

| Result | Criteria |
| --- | --- |
| **PASS** | Backup ok → SRC path removed → reload confirmed → in-flight index/lock gone → container `running` + `serve` alive after reload → touch poll window shows **no** new index cmdline/PID → container + `serve` still alive → restore ok |
| **FAIL (blocker)** | Reload does not drop SRC from active config; touch triggers index; in-flight index/lock not cleared; container not `running` or `serve` dead after reload/touch; cannot restore; used `daemon stop` or stopped `bsl-indexer` container |

On **FAIL:** do **not** proceed to cutover (Task 13). Record failure and commands tried in `DEVELOPMENT.md` (stand section). Leave watch restored if possible.

---

## Record results

After a live run, operator updates `DEVELOPMENT.md`:

- **Stand → BSL watch pause:** PASS or FAIL
- Exact reload command
- Exact config/health inspect command
- Touch test outcome
- Date / operator initials (no secrets)
