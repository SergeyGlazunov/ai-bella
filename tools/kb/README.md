# tools/kb — выгрузка КБ в immutable package

Скрипт `Update-And-Export-Kb.ps1` делает по порядку:

1. `/ConfigurationRepositoryUpdateCfg -force` + `/UpdateDBCfg`
2. `/DumpConfigToFiles` → staging
3. Публикация пакета в `packageInbox`: `.incoming-<id>` → `ready-<id>` с `READY` **последним**
4. Доставка в Linux inbox через Docker API (`Publish-ReadyPackageViaDocker`: volume + `docker cp` + `chown`)

Индексацию графа и BSL скрипт **не** запускает — это зона Linux/`contour` после intake пакета.

Других ключей DESIGNER нет. Переключателей Skip* для store/dump нет.

## Профиль

Скопируйте `kb-connection.example.json` → `kb-connection.json` (gitignore / не коммитить секреты).

| Поле | По умолчанию | Смысл |
|------|----------------|-------|
| `packageInbox` | `workdir/inbox/kb` | Windows-корень для локального `ready-*` перед Docker-доставкой |
| `packageInboxLinux` | `/srv/ai-contour/inbox/kb` | Linux inbox на Docker-хосте |
| `exportStaging` | `workdir/kb-export` | временная выгрузка DumpConfigToFiles |
| `platformVersion` | из `exePath` | поле `platform_version` в `manifest.json` |

Путь `\\wsl$\...` / `Set-WslPackageOwner.ps1` на этом стенде **не** используются (VHD sharing; скрипт deprecated и не валит экспорт при сбое WSL).

На стороне 1С у учётки хранилища (`storeUser`) — минимальные права (получение изменений).

## Пакет

Структура `ready-<export-id>/`:

- `src/` — выгрузка (обязателен `Configuration.xml`)
- `manifest.json` — schema_version=1, operations: `DumpConfigToFiles`, `UpdateCfg`, `UpdateDBCfg` = `ok`
- `READY` — маркер готовности (создаётся после манифеста)

Checksum: SHA-256 контракт как в `controller/src/contour/packages.py`.

## Nightly

Task Scheduler: `AI-contour KB nightly export` ≈ **01:00**.
