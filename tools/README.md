# tools/

| Путь | Назначение |
|------|------------|
| `kb/Update-And-Export-Kb.ps1` | Ночной/ручной цикл КБ → `/srv/ai2/kb/src` |
| `klon_opl/Export-KlonOpl.ps1` | Ночной/ручной dump клона оплат → `/srv/ai2/klon_opl/src` (без store) |
| `kb/kb-connection.json` | Параметры ИБ/хранилища (рядом со скриптом) |
| `copy-kit-artifacts.py` | Вспомогательная упаковка комплекта |

После переноса скрипта обновите Task Scheduler на:

`C:\AI-contour\tools\kb\Update-And-Export-Kb.ps1`

klon_opl (02:00):

`C:\AI-contour\tools\klon_opl\Export-KlonOpl.ps1`
