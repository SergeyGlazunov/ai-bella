# Stand gate: Close UBD reads

**Host:** ADM-SAPHX-TECH (боевой контур).  
**Purpose:** Доказать, что закрытие чтения MCP совпадает с `contour.reads.apply_close`: graph останавливается; BSL `serve` жив, но недоступен из сети чата; idle без `docker stop` контейнера. Нужно перед cutover.  
**Package:** `contour.reads` — `apply_close`, `verify_closed`, `wait_server_idle`.

Имена ниже — **prod**. Для ai2: graph/BSL/сеть из [deploy-ai2/README.md](../deploy-ai2/README.md); сеть чата обычно `ai2-stack`, не `ai-stack_default`.

Do not use: `docker stop bsl-indexer`, `bsl-indexer daemon stop`, sleep-N as drain, ESTABLISHED-only as idle proof.

**Contract:** required close = **graph stop + disconnect from WebUI network**. Host firewall DROP on published BSL port is **optional** (best-effort; not part of `verify_closed`).

| Config field | Stand value |
| --- | --- |
| `graph_mcp` | `mcp-metadata-graph` |
| `bsl_container` | `bsl-indexer` |
| `bsl_publish_port` | `8011` (optional firewall only) |
| `webui_network` | `ai-stack_default` |

Run from Windows + Git Bash (`MSYS_NO_PATHCONV=1`) or WSL with Docker CLI on the stand Engine.

---

## Preconditions

- `mcp-metadata-graph` and `bsl-indexer` containers exist and are normally **running**.
- `bsl-indexer` is attached to network `ai-stack_default` before the test (verify in step 0).
- Optional but useful: note baseline MCP clients / open WebUI sessions (they will lose graph/BSL reads during the test).
- Schedule a maintenance window; restore (section 6) is mandatory even on pass.

---

## 0. Baseline snapshot (optional, recommended)

```bash
docker inspect -f '{{.State.Status}}' mcp-metadata-graph
docker inspect -f '{{.State.Status}}' bsl-indexer
docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' bsl-indexer
```

Save output for the stand log in `DEVELOPMENT.md`.

---

## 1. Close graph MCP (cancel in-flight)

```bash
docker stop mcp-metadata-graph
```

Wait until the container is **not** running:

```bash
until [ "$(docker inspect -f '{{.State.Status}}' mcp-metadata-graph 2>/dev/null)" != "running" ]; do
  sleep 1
done
docker inspect -f '{{.State.Status}}' mcp-metadata-graph
```

**Pass:** status is `exited` (or anything other than `running`).

**Fail:** still `running` after reasonable wait → **blocker**; run restore (section 6) and stop.

Graph stop **cancels** in-flight graph handlers (process gone). This is the primary drain signal for graph.

---

## 2. Block BSL MCP from chat stack

### 2a. Firewall on host port 8011 — **optional**

Skip on ADM-SAPHX-TECH unless you deliberately harden host publish. If skipping, record `firewall=skipped` in `DEVELOPMENT.md`.

```bash
# Optional — Linux/WSL only; not required for package verify_closed
sudo iptables -I INPUT -p tcp --dport 8011 -j DROP
sudo iptables -C INPUT -p tcp --dport 8011 -j DROP && echo drop-ok
```

### 2b. Disconnect from WebUI network — **required**

Keeps the container up for `docker exec` but removes MCP from the chat stack network:

```bash
docker network disconnect ai-stack_default bsl-indexer
docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' bsl-indexer
```

**Pass:** `ai-stack_default` **not** listed.

**Do not** `docker stop bsl-indexer`.

---

## 3. Verify closed

Package `verify_closed` is **graph + network** (two checks). Container/`serve` liveness is a **stand-only extra**.

### 3a. Package `verify_closed` (required)

Both must be true:

```bash
# Graph not running
[ "$(docker inspect -f '{{.State.Status}}' mcp-metadata-graph)" != "running" ] && echo graph-closed

# BSL off ai-stack network
! docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' bsl-indexer | grep -q ai-stack_default && echo network-closed
```

**Fail any package check** → **blocker**; restore (section 6).

### 3b. Stand-only extra: BSL container / `serve` still alive

```bash
[ "$(docker inspect -f '{{.State.Status}}' bsl-indexer)" = "running" ] && echo container-running
docker top bsl-indexer | grep -Eq '[b]sl-indexer serve' && echo serve-alive
```

Container not `running` or `serve` gone after network steps means close-reads broke the daemon — not a stand PASS even if idle probe looks quiet.

---

## 4. Prove BSL server idle (IdleProbe)

After disconnect, prove **server-side work** is idle. ESTABLISHED on 8011 is auxiliary only.

### 4a. IdleProbe — proven 2026-09-21

```bash
# From kit root; Git Bash needs MSYS_NO_PATHCONV=1
./integrations/idle_bsl.sh
# Pass: exit 0
# Fail: exit 1 (fail-closed)
```

Wire the same script as `docker.idle_probe` for package `wait_server_idle`.

Sleep-N is **not** drain.

---

## 5. Optional: in-flight cancellation spot-check

If graph had active work before step 1, confirm handlers did not survive stop. Record in `DEVELOPMENT.md`. Not a substitute for step 4.

---

## 6. Restore (always)

Reverse order: start graph → reconnect network → remove optional firewall if applied.

```bash
docker start mcp-metadata-graph
until [ "$(docker inspect -f '{{.State.Status}}' mcp-metadata-graph 2>/dev/null)" = "running" ]; do sleep 1; done

docker network connect ai-stack_default bsl-indexer

# Only if step 2a was applied:
# sudo iptables -D INPUT -p tcp --dport 8011 -j DROP

docker inspect -f '{{.State.Status}}' mcp-metadata-graph
docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' bsl-indexer
```

**Pass restore:** graph `running`, `bsl-indexer` on `ai-stack_default`.

If restore fails, treat as **incident** — do not proceed to cutover until MCP is healthy again.

---

## Pass / fail criteria

| Result | Criteria |
| --- | --- |
| **PASS** | Package `verify_closed` (graph not running + network disconnect); stand-only: container `running` + `serve` alive; IdleProbe idle; restore successful. Firewall optional. |
| **FAIL (blocker)** | Graph won't stop; still on `ai-stack_default`; idle not provable; only ESTABLISHED=0 used as proof; container/`serve` dead; restore incomplete |

On **FAIL:** do **not** cut over. Record commands tried and failure mode in `DEVELOPMENT.md` (stand section).

---

## Record results

After a live run, operator updates `DEVELOPMENT.md`:

- **Stand → Close reads:** PASS or FAIL
- Exact **IdleProbe** command
- Whether firewall was skipped or applied
- Restore verified yes/no
- Date / operator initials (no secrets)
