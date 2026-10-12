# tools/klon_opl — выгрузка клона оплат (без хранилища)

Скрипт `Export-KlonOpl.ps1` умеет **только**:

1. `/DumpConfigToFiles` из ИБ `adm-program-102\klon_opl`
2. Копия staging → `/srv/ai2/klon_opl/src` (WSL/Docker volume mount)

Хранилища конфигурации **нет** — обновление из store не выполняется и не планируется.
Имя контура `klon_opl` (не `opl`) — слот `opl` зарезервирован под будущую боевую базу.

## Preflight (проверено)

- Кластерный порт **1541** на `adm-program-102` открыт (также 1540/1560).
- Локальная платформа: **8.3.23.1865** (`exePath` в `klon_opl-connection.json`).

## Запуск

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\klon_opl\Export-KlonOpl.ps1
```

Профиль: `klon_opl-connection.json` рядом со скриптом.

## Code index (Open WebUI)

Контейнер `ai2-bsl-klon-opl` на сети `ai2-stack`, MCP URL внутри Docker:
`http://ai2-bsl-klon-opl:9003/mcp`.

Карточка в Open WebUI: **`klon_opl_code_index`** (id `klon-opl-code-index`).
Алиас репо: `klon_opl`.

## Nightly

Task Scheduler: `AI-contour klon_opl nightly export` ≈ **02:00**.

Регистрация/обновление задачи:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\klon_opl\Register-NightlyTask.ps1
```

## Code index

После выгрузки: Docker overlay `deploy-ai2/bsl-klon-opl` → MCP `:8311`, alias `klon_opl`.
Граф / Neo4j для этого контура **не** поднимается.
