# Ночное и внеплановое обновление

Команды стенда ADM-SAPHX-TECH. Если нужен прогон, брать команды отсюда. Не собирать их поиском по репозиторию и не запускать индексаторы контейнерами вручную.

Окно и порядок баз читаются из живого `/etc/ai-contour/contour.yaml`. В комплекте и в отчёте стенда от 2026-11-17 окно — **01:30–06:00** `Asia/Vladivostok` (конец не входит). Порядок баз на том стенде был **ubd → kb → klon_opl**. Пример `contour.example.yaml` в репозитории задаёт другой порядок ключей; для прогона он не источник.

## Откуда что запускается

| Когда | Где | Что |
| --- | --- | --- |
| 01:00 | Windows, Task Scheduler `AI-contour KB nightly export` | `Update-And-Export-Kb.ps1`: 1С, хранилище, выгрузка, копия пакета в Linux inbox. Индексации нет |
| 01:30 | WSL, `ai-contour.timer` → `ai-contour.service` | `contour update` без флагов, пользователь `ai-contour` |
| 02:00 | Windows, Task Scheduler `AI-contour klon_opl nightly export` | `tools/klon_opl/Export-KlonOpl.ps1` — только выгрузка в inbox |

УБД из git забирает сам `contour update` внутри WSL. К информационной базе 1С УБД этот контур не подключается. Задачи Windows `UBD Config Sync` и `UBD Config Git Sync` остаются **Disabled**. Повторно их не включать и не регистрировать.

## КБ и 1С

Это часть ночи, и это не часть `contour update`. Скрипт на Windows в 01:00 делает четыре шага сам, без флагов «пропустить хранилище» или «только выгрузка». Профиль — `tools/kb/kb-connection.json` (сервер и имя ИБ, пользователь ИБ, адрес хранилища). Лог шага — `logs/kb/`.

1. Конфигуратор подключается к ИБ КБ (`/S сервер\имя`) и забирает конфигурацию из хранилища: `/ConfigurationRepositoryUpdateCfg -force`, затем `/UpdateDBCfg`. База 1С становится той же версии, что хранилище.
2. Тот же конфигуратор выгружает файлы: `/DumpConfigToFiles` в `workdir/kb-export`. В каталоге обязан появиться `Configuration.xml`.
3. Рядом собирается пакет `ready-<время>/`: каталог `src/`, `manifest.json`, файл `READY` последним.
4. Пакет копируется в Linux inbox `/srv/ai-contour/inbox/kb` через Docker (`Publish-ReadyPackageViaDocker`). Путь `\\wsl$` на этом стенде не используется.

После шага 4 исходники лежат в inbox. Граф и code-index ещё старые, пока WSL не выполнит `contour update` и не заберёт этот `ready-*`.

Днём свежую КБ собирают с Windows, тем же скриптом, что задача в 01:00. Contour из WSL этот скрипт не вызывает: пользователь `ai-contour` не запускает `powershell.exe`.

Сначала выгрузка. Ненулевой код или отсутствие нового `ready-*` в `C:\AI-contour\workdir\inbox\kb` — индекс не запускать, вчерашний пакет не пересобирать.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\AI-contour\tools\kb\Update-And-Export-Kb.ps1
```

Когда скрипт закончился и в inbox появился новый `ready-*`, индекс только КБ. Маркер `__CONTOUR_EXIT__` обязателен: после `wsl.exe` с перехваченным выводом `$LASTEXITCODE` на этом стенде врёт.

```powershell
$out = & wsl.exe -d Ubuntu -u ai-contour -- bash -lc 'contour --config /etc/ai-contour/contour.yaml update --database kb --force-window; echo __CONTOUR_EXIT__:$?'
```

`contour` в этой строке — `/opt/ai-contour/venv/bin/contour`, если в `ai-contour` он не в `PATH`. В выводе нужна строка `__CONTOUR_EXIT__:0`. Эту пару не запускать, пока ночная задача 01:00 ещё держит конфигуратор.

`klon_opl` к хранилищу не ходит. `Export-KlonOpl.ps1` только делает `/DumpConfigToFiles` из своей ИБ и так же копирует пакет в `/srv/ai-contour/inbox/klon_opl`.

Пакет КБ и klon_opl, уже лежащий в inbox, ночной `contour update` забирает сам: выбирает самый новый годный `ready-*` и переносит в `accepted/`. Отдельный `intake` для обычной ночи не нужен.

Внутри одной базы порядок такой: подготовить источник → закрыть чтение этой базы → активировать дерево → перемонтировать BSL → слои `code_index`, затем `graph` → проверка → открыть чтение. У `klon_opl` в профиле только `code_index`. Граф exit **3** значит «нужен FULL»; это решает сам contour в том же прогоне, отдельной команды на FULL нет.

Уже начатый индексатор в 06:00 не убивается. Следующая база в это время получает `deferred` / `write window closed` и в эту ночь не стартует. Поэтому длинный `graph_full` УБД часто оставляет КБ на утро без нового графа.

## Где выполнять contour

Только в WSL, от пользователя сервиса. Не из Windows PowerShell и не через `wsl -e` с подстановками: кавычки съедаются, прогон выглядит как старт и ничего не делает.

```bash
sudo -u ai-contour /opt/ai-contour/venv/bin/contour \
  --config /etc/ai-contour/contour.yaml status
```

Дальше в этом файле та же команда сокращена до `contour`. Префикс `sudo -u ai-contour` и `--config /etc/ai-contour/contour.yaml` остаются.

## Команды

| Зачем | Команда |
| --- | --- |
| Состояние всех баз | `contour status` |
| Одна база | `contour status --database kb` |
| Ночь, как таймер | `contour update` |
| Днём, все включённые базы, игнорируя окно | `contour update --force-window` |
| Днём, одна база | `contour update --database kb --force-window` |
| Забрать пакет в `accepted/` и не индексировать | `contour intake --database kb` |
| Открыть чтение, если индексы согласованы и писателя нет | `contour open --database kb` |
| Журнал ночи | `journalctl -u ai-contour.service --since 01:00` |
| Логи индексаторов | `/srv/ai-contour/logs/runs` |

Имена баз: `ubd`, `kb`, `klon_opl`. Других флагов у `update` нет. `--measure` только пишет замер времени, индекс от него не меняется.

`contour open --force` открывает чтение, даже если слой не `ready`. Для догона это не команда. `contour jobs fail <id> --force --reason "..."` — только когда запись в state зависла, а контейнер писателя уже отсутствует.

## Когда что запускать

Ночь сама. Днём `contour update` без `--force-window` не начинает запись: в state будет `deferred`.

Внепланово обновляется одна база, та, которую попросили:

```bash
contour status --database kb
contour update --database kb --force-window
contour status --database kb
```

Сначала status. Если по этой базе уже есть живой writer или держится lock (`/srv/ai-contour/locks/sync.lock`), второй `update` не запускать.

Свежая КБ днём — пара команд из раздела «КБ и 1С»: сначала скрипт Windows, и только после нового `ready-*` индекс `--database kb`. Команда `contour update --force-window` без имени базы сюда не подходит: она не ходит в 1С и сначала занимает машину графом УБД.

klon_opl так же: сначала `Export-KlonOpl.ps1`, затем `contour update --database klon_opl --force-window`. Если пакет уже лежит в inbox и новая выгрузка не нужна, достаточно одного contour. `intake` — только когда пакет надо принять, а индекс сознательно отложить.

УБД днём — тот же `--database ubd --force-window`. Отдельной Windows-команды для git УБД нет.

Полный `contour update --force-window` днём запускать, только если нужны все три базы и есть несколько часов. `graph_full` УБД занимает окно целиком; КБ после него часто не стартует.

## Что не делать

- Не `docker restart` графа, чтобы «подтянуть данные». MCP читает Neo4j, содержимое графа пишет только job contour.
- Не вызывать `metadata-indexer` и `bsl-indexer index` руками.
- Не включать `UBD Config Sync` / `UBD Config Git Sync`.
- Не останавливать чат, Open WebUI и OmniRoute ради индекса.
- Не считать код свежим по code-index, если просили граф: демон BSL обновляет индекс сам после смены файлов, Neo4j — нет.
