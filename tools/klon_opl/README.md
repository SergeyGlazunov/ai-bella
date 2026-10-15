# tools/klon_opl — выгрузка клона оплат в immutable package

Скрипт `Export-KlonOpl.ps1` умеет **только**:

1. `/DumpConfigToFiles` из ИБ (профиль `server`/`ref`)
2. Публикация пакета в `packageInbox`: `.incoming-<id>` → `ready-<id>` с `READY` последним

Хранилища конфигурации **нет**. Индексацию BSL скрипт **не** запускает — это зона Linux/`contour`.

Имя контура `klon_opl` (не `opl`) — слот `opl` зарезервирован под будущую боевую базу.

## Профиль

Скопируйте `klon_opl-connection.example.json` → `klon_opl-connection.json` (секреты не коммитить).

| Поле | По умолчанию | Смысл |
|------|----------------|-------|
| `packageInbox` | `workdir/inbox/klon_opl` | корень inbox для `ready-*` |
| `exportStaging` | `workdir/klon_opl-export` | staging DumpConfigToFiles |
| `platformVersion` | из `exePath` | в `manifest.json` |

## Пакет

`ready-<export-id>/src` + `manifest.json` (operations: только `DumpConfigToFiles=ok`) + `READY`.

## Preflight

- Кластерный порт **1541** на хосте ИБ.
- Локальная платформа в `exePath`.

## Запуск

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\klon_opl\Export-KlonOpl.ps1
```

## Nightly

Task Scheduler: `AI-contour klon_opl nightly export` ≈ **02:00**.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\klon_opl\Register-NightlyTask.ps1
```
