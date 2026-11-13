#!/usr/bin/env python3
"""Compress week of CSV + Open WebUI + OmniRoute SQLite into a ~10KB Markdown digest for LLM capacity planning.

Stand-specific defaults (ADM-SAPHX-TECH / ai2-stack):
  WEBUI     /var/lib/docker/volumes/ai2-stack_open-webui-data/_data/webui.db
  OmniRoute /var/lib/docker/volumes/ai2-stack_omniroute-data/_data/storage.sqlite

OmniRoute schema notes (verified on stand):
  call_logs.duration (ms), tokens_in/tokens_out — not latency_ms
  usage_history.latency_ms, tokens_input/tokens_output — preferred for tokens+latency
  Filter model == connection-test (health checks)
  mcp_tool_audit is empty; MCP tools come from Open WebUI chat JSON
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime

METRICS_DIR = os.environ.get("METRICS_DIR", "/srv/metrics")
WSL_CSV = os.path.join(METRICS_DIR, "wsl_host.csv")
DOCKER_CSV = os.path.join(METRICS_DIR, "docker_stats.csv")
WIN_CSV = os.path.join(METRICS_DIR, "win_host.csv")

WEBUI_DB_PATH = os.environ.get(
    "WEBUI_DB_PATH",
    "/var/lib/docker/volumes/ai2-stack_open-webui-data/_data/webui.db",
)
OMNIROUTE_DB_PATH = os.environ.get(
    "OMNIROUTE_DB_PATH",
    "/var/lib/docker/volumes/ai2-stack_omniroute-data/_data/storage.sqlite",
)

REPORT_FILE = os.environ.get(
    "REPORT_FILE", os.path.join(METRICS_DIR, "llm_capacity_report.md")
)
TMP_DIR = os.environ.get("METRICS_TMP", "/tmp")


def parse_size_to_mb(val_str: str) -> float:
    if not val_str:
        return 0.0
    part = val_str.split("/")[0].strip()
    m = re.match(r"([0-9.]+)\s*([a-zA-Z]+)", part)
    if not m:
        return 0.0
    num, unit = float(m.group(1)), m.group(2).lower()
    if "g" in unit:
        return num * 1024.0
    if "m" in unit:
        return num
    if "k" in unit:
        return num / 1024.0
    return num


def parse_pct(val_str: str) -> float:
    try:
        return float(str(val_str).replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


def percentile(data, p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = min(int(len(s) * p), len(s) - 1)
    return s[idx]


def normalize_ts(val):
    """Return (epoch_sec, 'YYYY-MM-DD HH:MM') or (None, None)."""
    if val is None or val == "":
        return None, None
    try:
        ts_val = float(val)
        if ts_val > 1e14:
            ts_val /= 1e9
        elif ts_val > 1e11:
            ts_val /= 1e3
        dt = datetime.fromtimestamp(ts_val)
        return ts_val, dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError, OverflowError):
        s = str(val).strip().replace("T", " ").replace("Z", "")
        try:
            dt = datetime.fromisoformat(s[:19])
            return dt.timestamp(), dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return None, None


def safe_sqlite_copy(src_path: str, tmp_name: str):
    """Online SQLite backup that merges WAL into a temp file."""
    if not src_path or not os.path.exists(src_path):
        return None
    tmp_path = os.path.join(TMP_DIR, tmp_name)
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    src_conn = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(tmp_path)
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()
    return tmp_path


def extract_tools_from_message(msg_obj):
    tools = []
    if not isinstance(msg_obj, dict):
        return tools

    for tc in msg_obj.get("tool_calls") or []:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name")
            if name:
                tools.append(str(name))

    content = msg_obj.get("content")
    if isinstance(content, str) and "tool_calls" in content:
        for m in re.finditer(r'<details[^>]*type=["\']tool_calls["\'][^>]*>', content):
            tag = m.group(0)
            nm = re.search(r'name=["\']([^"\']+)["\']', tag)
            tools.append(nm.group(1) if nm else "unnamed_tool")

    if msg_obj.get("role") == "tool" and msg_obj.get("name"):
        tools.append(str(msg_obj["name"]))

    return tools


def load_webui_data(db_path, min_epoch, max_epoch):
    activity = defaultdict(
        lambda: {
            "user_msgs": 0,
            "users": set(),
            "tool_calls": 0,
            "tools": defaultdict(int),
        }
    )
    tool_totals = defaultdict(int)
    calls_per_turn = []

    tmp_db = safe_sqlite_copy(db_path, "webui_backup.db")
    if not tmp_db:
        print(f"[WARN] webui.db not found: {db_path}")
        return activity, tool_totals, calls_per_turn

    try:
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        cur.execute("SELECT user_id, chat, updated_at FROM chat")
        for uid, chat_json, _updated_at in cur.fetchall():
            if not chat_json:
                continue
            try:
                data = json.loads(chat_json) if isinstance(chat_json, str) else chat_json
            except Exception:
                continue

            msgs = []
            if isinstance(data, dict):
                hist = (data.get("history") or {}).get("messages")
                if isinstance(hist, dict):
                    msgs = list(hist.values())
                elif isinstance(data.get("messages"), list):
                    msgs = data["messages"]

            for m in msgs:
                if not isinstance(m, dict):
                    continue
                ts_raw = m.get("timestamp") or m.get("created_at")
                epoch, min_key = normalize_ts(ts_raw)
                if not epoch:
                    continue
                if min_epoch and epoch < min_epoch:
                    continue
                if max_epoch and epoch > max_epoch:
                    continue

                role = m.get("role")
                if role == "user":
                    activity[min_key]["user_msgs"] += 1
                    if uid:
                        activity[min_key]["users"].add(uid)
                else:
                    t_list = extract_tools_from_message(m)
                    if t_list:
                        calls_per_turn.append(len(t_list))
                        activity[min_key]["tool_calls"] += len(t_list)
                        for tname in t_list:
                            activity[min_key]["tools"][tname] += 1
                            tool_totals[tname] += 1
                    elif role == "assistant":
                        calls_per_turn.append(0)
        conn.close()
    except Exception as e:
        print(f"[WARN] Error reading webui.db: {e}")
    finally:
        if os.path.exists(tmp_db):
            os.remove(tmp_db)

    return activity, tool_totals, calls_per_turn


def _row_get(d, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def load_omniroute_data(db_path, min_epoch, max_epoch):
    omni_by_min = defaultdict(
        lambda: {
            "reqs": 0,
            "errors": 0,
            "latencies": [],
            "in_tok": 0,
            "out_tok": 0,
            "tool_loop_reqs": 0,
        }
    )
    model_stats = defaultdict(
        lambda: {
            "reqs": 0,
            "errors": 0,
            "latencies": [],
            "in_tok": [],
            "out_tok": [],
        }
    )

    tmp_db = safe_sqlite_copy(db_path, "omniroute_backup.db")
    if not tmp_db:
        print(f"[WARN] OmniRoute storage.sqlite not found: {db_path}")
        return omni_by_min, model_stats

    try:
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        tables = {
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        if "call_logs" in tables:
            cur.execute("SELECT * FROM call_logs")
            for row in cur.fetchall():
                d = dict(row)
                model = str(_row_get(d, "model", "requested_model", default="") or "")
                if model.lower() == "connection-test":
                    continue

                epoch, min_key = normalize_ts(
                    _row_get(d, "timestamp", "created_at")
                )
                if not epoch:
                    continue
                if min_epoch and epoch < min_epoch:
                    continue
                if max_epoch and epoch > max_epoch:
                    continue

                provider = _row_get(d, "provider", default="unknown") or "unknown"
                m_key = f"{provider} / {model or 'unknown'}"

                status = int(_row_get(d, "status", default=200) or 200)
                # Stand schema: duration (ms); also accept latency_ms / latency
                lat = float(
                    _row_get(
                        d, "duration", "latency_ms", "latency", "duration_ms", default=0
                    )
                    or 0
                )
                in_t = int(
                    _row_get(d, "tokens_in", "tokens_input", "prompt_tokens", default=0)
                    or 0
                )
                out_t = int(
                    _row_get(
                        d, "tokens_out", "tokens_output", "completion_tokens", default=0
                    )
                    or 0
                )

                omni_by_min[min_key]["reqs"] += 1
                model_stats[m_key]["reqs"] += 1

                if status >= 400:
                    omni_by_min[min_key]["errors"] += 1
                    model_stats[m_key]["errors"] += 1
                elif lat > 0:
                    omni_by_min[min_key]["latencies"].append(lat)
                    model_stats[m_key]["latencies"].append(lat)

                if in_t > 0 or out_t > 0:
                    omni_by_min[min_key]["in_tok"] += in_t
                    omni_by_min[min_key]["out_tok"] += out_t
                    model_stats[m_key]["in_tok"].append(in_t)
                    model_stats[m_key]["out_tok"].append(out_t)

                payload_str = str(
                    _row_get(
                        d,
                        "request_summary",
                        "request_body",
                        "response_body",
                        "payload",
                        default="",
                    )
                    or ""
                )
                if '"tool_calls"' in payload_str or '"role":"tool"' in payload_str:
                    omni_by_min[min_key]["tool_loop_reqs"] += 1

        # usage_history: richer token/latency series; fill gaps and reinforce model stats
        if "usage_history" in tables:
            cur.execute("SELECT * FROM usage_history")
            for row in cur.fetchall():
                d = dict(row)
                model = str(_row_get(d, "model", default="") or "")
                if model.lower() == "connection-test":
                    continue

                epoch, min_key = normalize_ts(
                    _row_get(d, "timestamp", "created_at")
                )
                if not epoch:
                    continue
                if min_epoch and epoch < min_epoch:
                    continue
                if max_epoch and epoch > max_epoch:
                    continue

                provider = _row_get(d, "provider", default="unknown") or "unknown"
                m_key = f"{provider} / {model or 'unknown'}"
                in_t = int(
                    _row_get(d, "tokens_input", "tokens_in", "prompt_tokens", default=0)
                    or 0
                )
                out_t = int(
                    _row_get(
                        d, "tokens_output", "tokens_out", "completion_tokens", default=0
                    )
                    or 0
                )
                lat = float(_row_get(d, "latency_ms", "duration", default=0) or 0)
                ok = d.get("success")
                err = False
                if ok is not None:
                    err = not bool(ok)
                else:
                    st = str(_row_get(d, "status", "error_code", default="") or "")
                    err = st.upper() in ("ERROR", "FAIL", "FAILED") or st.startswith("5")

                # Prefer usage_history latency when call_logs minute has none
                if lat > 0 and not omni_by_min[min_key]["latencies"]:
                    omni_by_min[min_key]["latencies"].append(lat)
                if lat > 0 and not model_stats[m_key]["latencies"]:
                    model_stats[m_key]["latencies"].append(lat)

                if in_t or out_t:
                    if omni_by_min[min_key]["in_tok"] == 0:
                        omni_by_min[min_key]["in_tok"] += in_t
                        omni_by_min[min_key]["out_tok"] += out_t
                    if not model_stats[m_key]["in_tok"]:
                        model_stats[m_key]["in_tok"].append(in_t)
                        model_stats[m_key]["out_tok"].append(out_t)

                if "call_logs" not in tables:
                    omni_by_min[min_key]["reqs"] += 1
                    model_stats[m_key]["reqs"] += 1
                    if err:
                        omni_by_min[min_key]["errors"] += 1
                        model_stats[m_key]["errors"] += 1

        conn.close()
    except Exception as e:
        print(f"[WARN] Error reading OmniRoute DB: {e}")
    finally:
        if os.path.exists(tmp_db):
            os.remove(tmp_db)

    return omni_by_min, model_stats


def main():
    wsl_by_min = {}
    wsl_day, wsl_night = [], []
    min_epoch, max_epoch = None, None

    if os.path.exists(WSL_CSV):
        with open(WSL_CSV, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ts = row["timestamp"]
                epoch, min_key = normalize_ts(ts)
                if epoch:
                    min_epoch = epoch if min_epoch is None else min(min_epoch, epoch)
                    max_epoch = epoch if max_epoch is None else max(max_epoch, epoch)
                hour = int(ts[11:13])
                rec = {
                    "ts": ts,
                    "load1": float(row["load_1m"]),
                    "mem_total": float(row["mem_total_mb"]),
                    "mem_used": float(row["mem_used_real_mb"]),
                    "mem_avail": float(row["mem_avail_mb"]),
                    "swap": float(row["swap_used_mb"]),
                }
                wsl_by_min[min_key] = rec
                if 1 <= hour < 4:
                    wsl_night.append(rec)
                else:
                    wsl_day.append(rec)
    else:
        print(f"[WARN] Missing WSL CSV: {WSL_CSV}")

    activity, tool_totals, calls_per_turn = load_webui_data(
        WEBUI_DB_PATH, min_epoch, max_epoch
    )
    omni_by_min, model_stats = load_omniroute_data(
        OMNIROUTE_DB_PATH, min_epoch, max_epoch
    )

    cont_stats = defaultdict(lambda: {"cpu": [], "mem": []})
    docker_by_min = defaultdict(
        lambda: {"total_cpu": 0.0, "total_mem": 0.0, "containers": {}}
    )
    if os.path.exists(DOCKER_CSV):
        with open(DOCKER_CSV, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                min_key = row["timestamp"][:16]
                cname = row["container"]
                cpu = parse_pct(row["cpu_perc"])
                mem = parse_size_to_mb(row["mem_usage"])
                cont_stats[cname]["cpu"].append(cpu)
                cont_stats[cname]["mem"].append(mem)
                docker_by_min[min_key]["containers"][cname] = (cpu, mem)

    for _mkey, d in docker_by_min.items():
        d["total_cpu"] = sum(v[0] for v in d["containers"].values())
        d["total_mem"] = sum(v[1] for v in d["containers"].values())

    buckets = defaultdict(
        lambda: {"load1": [], "wsl_mem": [], "dock_cpu": [], "lat": [], "count": 0}
    )
    bucket_order = [
        "0 запросов/мин (Idle)",
        "Обычный чат (без тулов)",
        "1-2 вызова MCP-тулов/мин",
        "3+ вызовов MCP-тулов/мин (тяжёлый агентский цикл)",
    ]

    for mkey, wrec in wsl_by_min.items():
        hour = int(mkey[11:13])
        if 1 <= hour < 4:
            continue
        u_msgs = activity[mkey]["user_msgs"] if mkey in activity else 0
        t_calls = activity[mkey]["tool_calls"] if mkey in activity else 0
        o_reqs = omni_by_min[mkey]["reqs"] if mkey in omni_by_min else 0

        if u_msgs == 0 and t_calls == 0 and o_reqs == 0:
            b_name = "0 запросов/мин (Idle)"
        elif t_calls == 0:
            b_name = "Обычный чат (без тулов)"
        elif t_calls <= 2:
            b_name = "1-2 вызова MCP-тулов/мин"
        else:
            b_name = "3+ вызовов MCP-тулов/мин (тяжёлый агентский цикл)"

        b = buckets[b_name]
        b["count"] += 1
        b["load1"].append(wrec["load1"])
        b["wsl_mem"].append(wrec["mem_used"])
        if mkey in docker_by_min:
            b["dock_cpu"].append(docker_by_min[mkey]["total_cpu"])
        if mkey in omni_by_min and omni_by_min[mkey]["latencies"]:
            b["lat"].extend(omni_by_min[mkey]["latencies"])

    win_day, win_night = [], []
    if os.path.exists(WIN_CSV):
        with open(WIN_CSV, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                ts = row["timestamp"]
                hour = int(ts[11:13])
                rec = {
                    "ts": ts,
                    "cpu": float(row["win_cpu_perc"]),
                    "free_mb": float(row["win_ram_free_mb"]),
                    "total_mb": float(row["win_ram_total_mb"]),
                    "vmmem_mb": float(row["vmmem_ram_mb"]),
                    "disk_q": float(row["disk_queue"]),
                }
                if 1 <= hour < 4:
                    win_night.append(rec)
                else:
                    win_day.append(rec)

    os.makedirs(os.path.dirname(REPORT_FILE) or ".", exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as out:
        out.write(
            "# Сводка телеметрии стенда Белла "
            "(Железо + Open WebUI + OmniRoute + MCP)\n\n"
        )
        if min_epoch and max_epoch:
            out.write(
                f"- Окно сбора (по wsl_host.csv): "
                f"**{datetime.fromtimestamp(min_epoch):%Y-%m-%d %H:%M}** → "
                f"**{datetime.fromtimestamp(max_epoch):%Y-%m-%d %H:%M}**\n\n"
            )

        if wsl_day:
            out.write(
                "## 1. Сводка WSL2 (Дневной режим vs Ночное окно 01:00-04:00)\n"
            )
            out.write(
                f"- Выделено RAM под WSL2: **{wsl_day[0]['mem_total']:.0f} MB**\n\n"
            )
            out.write(
                "| Режим | Замеров | LoadAvg 1m (P50 / P95 / Max) | "
                "Реальная RAM MB (P50 / P95 / Max) | Мин. свободной RAM MB | Max Swap MB |\n"
            )
            out.write("|---|---|---|---|---|---|\n")
            for label, arr in [
                ("День (04:00-00:59)", wsl_day),
                ("Ночь 1С (01:00-03:59)", wsl_night),
            ]:
                if not arr:
                    continue
                l1 = [x["load1"] for x in arr]
                mu = [x["mem_used"] for x in arr]
                ma = [x["mem_avail"] for x in arr]
                sw = [x["swap"] for x in arr]
                out.write(
                    f"| {label} | {len(arr)} | "
                    f"{percentile(l1, 0.5):.2f} / {percentile(l1, 0.95):.2f} / {max(l1):.2f} | "
                    f"{percentile(mu, 0.5):.0f} / {percentile(mu, 0.95):.0f} / {max(mu):.0f} | "
                    f"{min(ma):.0f} | {max(sw):.0f} |\n"
                )
            out.write("\n")

        out.write("## 2. Профиль потребления контейнеров Docker\n")
        out.write(
            "| Контейнер | RAM Min (Idle) MB | RAM P50 MB | RAM P95 MB | RAM Max MB | "
            "CPU P50 % | CPU P95 % | CPU Max % |\n"
        )
        out.write("|---|---|---|---|---|---|---|---|\n")
        for cname, st in sorted(cont_stats.items()):
            m_arr, c_arr = st["mem"], st["cpu"]
            out.write(
                f"| {cname} | {min(m_arr):.0f} | {percentile(m_arr, 0.5):.0f} | "
                f"{percentile(m_arr, 0.95):.0f} | {max(m_arr):.0f} | "
                f"{percentile(c_arr, 0.5):.1f}% | {percentile(c_arr, 0.95):.1f}% | "
                f"{max(c_arr):.1f}% |\n"
            )
        out.write("\n")

        out.write("## 3. Влияние обычного чата и MCP-тулов на железо и Latency\n")
        out.write(
            "| Режим минуты | Минут | WSL LoadAvg (P50 / P95 / Max) | "
            "Сумма CPU Docker (P50 / P95 / Max) | Реальная RAM WSL (P50 / P95) | "
            "OmniRoute Latency сек (P50 / P95) |\n"
        )
        out.write("|---|---|---|---|---|---|\n")
        for b_name in bucket_order:
            b = buckets.get(b_name)
            if not b or not b["count"]:
                continue
            l1, dc, wm, lat = b["load1"], b["dock_cpu"], b["wsl_mem"], b["lat"]
            lat_str = (
                f"{percentile(lat, 0.5) / 1000:.1f}s / {percentile(lat, 0.95) / 1000:.1f}s"
                if lat
                else "—"
            )
            dc_str = (
                f"{percentile(dc, 0.5):.1f}% / {percentile(dc, 0.95):.1f}% / {max(dc):.1f}%"
                if dc
                else "—"
            )
            out.write(
                f"| {b_name} | {b['count']} | "
                f"{percentile(l1, 0.5):.2f} / {percentile(l1, 0.95):.2f} / {max(l1):.2f} | "
                f"{dc_str} | "
                f"{percentile(wm, 0.5):.0f} / {percentile(wm, 0.95):.0f} MB | {lat_str} |\n"
            )
        out.write("\n")

        out.write("## 4. Метрики LLM-шлюза OmniRoute (Latency, Токены, Ошибки)\n")
        out.write(
            "_Исключены `connection-test`. Latency: `call_logs.duration` / "
            "`usage_history.latency_ms`._\n\n"
        )
        if model_stats:
            out.write(
                "| Провайдер / Модель | Запросов | Ошибок (4xx/5xx) | "
                "Latency сек (P50 / P95 / Max) | Input Tokens (P50 / P95) | "
                "Output Tokens (P50 / P95) |\n"
            )
            out.write("|---|---|---|---|---|---|\n")
            for m_key, st in sorted(
                model_stats.items(), key=lambda x: x[1]["reqs"], reverse=True
            ):
                lats = st["latencies"]
                itok = st["in_tok"]
                otok = st["out_tok"]
                lat_s = (
                    f"{percentile(lats, 0.5) / 1000:.1f}s / "
                    f"{percentile(lats, 0.95) / 1000:.1f}s / "
                    f"{max(lats) / 1000:.1f}s"
                    if lats
                    else "—"
                )
                in_s = (
                    f"{percentile(itok, 0.5):.0f} / {percentile(itok, 0.95):.0f}"
                    if itok
                    else "—"
                )
                out_s = (
                    f"{percentile(otok, 0.5):.0f} / {percentile(otok, 0.95):.0f}"
                    if otok
                    else "—"
                )
                out.write(
                    f"| {m_key} | {st['reqs']} | {st['errors']} | {lat_s} | {in_s} | {out_s} |\n"
                )
        else:
            out.write(
                "*База OmniRoute не найдена или пуста за выбранный период.*\n"
            )
        out.write("\n")

        out.write(
            "## 5. Статистика вызовов MCP-тулов (Граф УБД, Индекс BSL и др.)\n"
        )
        total_user_msgs = sum(v["user_msgs"] for v in activity.values())
        total_omni_reqs = sum(v["reqs"] for v in omni_by_min.values())
        total_tools = sum(tool_totals.values())
        amp_factor = (total_omni_reqs / total_user_msgs) if total_user_msgs else 0.0

        out.write(
            f"- Сообщений от пользователей в Open WebUI: **{total_user_msgs}**\n"
        )
        out.write(
            f"- Всего LLM-вызовов через OmniRoute: **{total_omni_reqs}** "
            f"(коэффициент мультипликации: **{amp_factor:.2f}x** LLM-запросов на 1 вопрос юзера)\n"
        )
        out.write(f"- Всего вызовов MCP/инструментов: **{total_tools}**\n")
        if calls_per_turn:
            out.write(
                f"- Инструментов за один шаг ответа: P50 = "
                f"**{percentile(calls_per_turn, 0.5):.0f}**, P95 = "
                f"**{percentile(calls_per_turn, 0.95):.0f}**, Max = "
                f"**{max(calls_per_turn)}**\n\n"
            )
        else:
            out.write("\n")

        if tool_totals:
            out.write(
                "| Имя инструмента (MCP / Tool) | Количество вызовов | Доля от всех вызовов |\n"
            )
            out.write("|---|---|---|\n")
            for tname, cnt in sorted(
                tool_totals.items(), key=lambda x: x[1], reverse=True
            ):
                pct = (cnt / total_tools * 100.0) if total_tools else 0.0
                out.write(f"| `{tname}` | {cnt} | {pct:.1f}% |\n")
        out.write("\n")

        if win_day or win_night:
            out.write(
                "## 6. Метрики Windows-хоста "
                "(влияние ночных выгрузок 1С в 01:00 и 02:00)\n"
            )
            out.write(
                "| Окно | CPU Хоста (P50 / P95 / Max) | "
                "Свободная RAM Win MB (Min / P50) | vmmemWSL MB (Max) | "
                "Очередь диска (P95 / Max) |\n"
            )
            out.write("|---|---|---|---|---|\n")
            for label, arr in [
                ("День (работа Беллы)", win_day),
                ("Ночь 01:00-04:00 (Export KB + KlonOpl)", win_night),
            ]:
                if not arr:
                    continue
                c = [x["cpu"] for x in arr]
                fr = [x["free_mb"] for x in arr]
                vm = [x["vmmem_mb"] for x in arr]
                dq = [x["disk_q"] for x in arr]
                out.write(
                    f"| {label} | "
                    f"{percentile(c, 0.5):.1f}% / {percentile(c, 0.95):.1f}% / {max(c):.1f}% | "
                    f"{min(fr):.0f} / {percentile(fr, 0.5):.0f} | {max(vm):.0f} | "
                    f"{percentile(dq, 0.95):.2f} / {max(dq):.2f} |\n"
                )
            out.write("\n")

        out.write(
            "## 7. Топ-10 пиковых минут внутри WSL "
            "(с привязкой к тулам и Latency)\n"
        )
        out.write(
            "| Минута | Сообщений | Вызовов тулов | Запросов в OmniRoute | "
            "Ср. Latency сек | WSL Load1m | Docker CPU% | Главный потребитель CPU |\n"
        )
        out.write("|---|---|---|---|---|---|---|---|\n")
        top_mins = sorted(
            wsl_by_min.keys(), key=lambda k: wsl_by_min[k]["load1"], reverse=True
        )[:10]
        for mkey in top_mins:
            w = wsl_by_min[mkey]
            d = docker_by_min.get(mkey, {"total_cpu": 0.0, "containers": {}})
            u_msgs = activity[mkey]["user_msgs"] if mkey in activity else 0
            t_calls = activity[mkey]["tool_calls"] if mkey in activity else 0
            o_reqs = omni_by_min[mkey]["reqs"] if mkey in omni_by_min else 0
            lats = omni_by_min[mkey]["latencies"] if mkey in omni_by_min else []
            avg_lat = f"{(sum(lats) / len(lats)) / 1000:.1f}s" if lats else "—"
            top_c = max(
                d["containers"].items(),
                key=lambda x: x[1][0],
                default=("—", (0, 0)),
            )
            out.write(
                f"| {mkey} | {u_msgs} | {t_calls} | {o_reqs} | {avg_lat} | "
                f"{w['load1']:.2f} | {d['total_cpu']:.1f}% | "
                f"{top_c[0]} ({top_c[1][0]:.1f}%) |\n"
            )

    print(f"Готово! Отчёт сохранён в: {REPORT_FILE}")


if __name__ == "__main__":
    main()
