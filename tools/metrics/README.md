# Capacity metrics (Bella stand)

Недельный сбор телеметрии WSL/Docker/Windows + сжатие в Markdown-отчёт для capacity planning (Open WebUI + OmniRoute + MCP).

Без внешних Python-библиотек. Сырые CSV нейронке не скармливать — только `llm_capacity_report.md`.

## Состав

| Файл | Где крутится | Что делает |
|---|---|---|
| `collect_wsl.sh` | WSL (Ubuntu) | каждые 30 с: LoadAvg, MemAvailable, `docker stats` |
| `Collect-WinMetrics.ps1` | Windows | каждые 5 мин: CPU/RAM хоста, vmmem, disk queue |
| `Register-WinMetricsTask.ps1` | Windows (админ) | задача планировщика `AI_Contour_Metrics` |
| `build_llm_digest.py` | WSL, после недели | CSV + `webui.db` + `storage.sqlite` → отчёт |

## Пути на стенде (ai2-stack)

По умолчанию агрегатор читает Docker named volumes (не bind-mount `/srv/ai2/...`):

```text
/var/lib/docker/volumes/ai2-stack_open-webui-data/_data/webui.db
/var/lib/docker/volumes/ai2-stack_omniroute-data/_data/storage.sqlite
```

CSV:

```text
/srv/metrics/wsl_host.csv
/srv/metrics/docker_stats.csv
/srv/metrics/win_host.csv   # скопировать с Windows после сбора
```

Переопределение: `METRICS_DIR`, `WEBUI_DB_PATH`, `OMNIROUTE_DB_PATH`, `REPORT_FILE`, `DOCKER_HOST`.

## Шаг 1. WSL-сборщик

```bash
sudo mkdir -p /srv/metrics
sudo cp tools/metrics/collect_wsl.sh /srv/metrics/
sudo chmod +x /srv/metrics/collect_wsl.sh
# при необходимости: export DOCKER_HOST=tcp://127.0.0.1:2375
nohup /srv/metrics/collect_wsl.sh >/srv/metrics/collect_wsl.log 2>&1 &
echo $! | sudo tee /srv/metrics/collect_wsl.pid
```

Остановка: `kill $(cat /srv/metrics/collect_wsl.pid)`.

## Шаг 2. Windows-сборщик (ночные выгрузки 1С)

Из репозитория (PowerShell **от администратора**):

```powershell
cd C:\AI-contour\tools\metrics
.\Register-WinMetricsTask.ps1 -OutFile C:\Scripts\win_host.csv -Days 14
```

Или разово: `.\Collect-WinMetrics.ps1 -OutFile C:\Scripts\win_host.csv`.

После недели скопируйте CSV в WSL:

```powershell
wsl -d Ubuntu -- bash -lc "mkdir -p /srv/metrics && cp /mnt/c/Scripts/win_host.csv /srv/metrics/"
```

## Шаг 3. Отчёт для нейронки

```bash
# нужен доступ на чтение volume’ов Docker (обычно root или группа docker)
sudo python3 /path/to/ai-contour/tools/metrics/build_llm_digest.py
# → /srv/metrics/llm_capacity_report.md
```

Промпт к модели: idle-полка RAM/CPU; цена одного конкурентного запроса (P50/P95); потолок Concurrent/DAU и первое узкое место (контейнер vs rate-limit провайдера по latency при низком Docker CPU).

## Особенности OmniRoute (стенд)

- Latency в `call_logs` — поле **`duration`** (мс); токены — `tokens_in` / `tokens_out`.
- `usage_history` дополняет latency/токены (`latency_ms`, `tokens_input` / `tokens_output`).
- Строки с `model=connection-test` отфильтровываются.
- Триггер ring-buffer стоит на `request_detail_logs` (не на `call_logs`); env `CALL_LOGS_*` на стенде не заданы.
- Вызовы MCP берутся из JSON чатов Open WebUI (`tool_calls` / `<details type="tool_calls">`), не из пустой `mcp_tool_audit`.

## Разделы отчёта

1. WSL день vs ночь 01:00–04:00  
2. Профиль Docker-контейнеров  
3. Idle / чат без тулов / MCP-нагрузка + latency  
4. OmniRoute по моделям  
5. MCP-тулы + коэффициент мультипликации LLM на 1 вопрос  
6. Windows-хост (день / ночные Export KB + KlonOpl)  
7. Топ-10 пиковых минут  
