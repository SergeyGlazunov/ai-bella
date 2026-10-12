# tools/kb — единственный разрешённый способ трогать хранилище КБ с этой машины

Скрипт `Update-And-Export-Kb.ps1` делает по порядку:

1. `/ConfigurationRepositoryUpdateCfg -force` + `/UpdateDBCfg` (обновить из хранилища и применить к ИБ)
2. `/DumpConfigToFiles` → staging → `/srv/ai2/kb/src`
3. **полная** переиндексация графа (`METADATA_FORCE_REINDEX=true` в one-shot `docker run` по образу контейнера `ai2-metadata-indexer-kb`)

Других ключей DESIGNER нет. Переключателей Skip* для store/dump нет.

`apply_changes` для КБ не используем: база маленькая, полный прогон после ночной выгрузки ~10 минут.

Профиль `kb-connection.json` (опционально):

| Поле | По умолчанию | Смысл |
|------|----------------|-------|
| `skipGraphReindex` | `false` | пропустить шаг 3 (отладка) |
| `graphIndexerContainer` | `ai2-metadata-indexer-kb` | эталон env/image/network |
| `graphNeo4jContainer` | `ai2-neo4j-kb` | ждать healthy перед индексом |

На стороне 1С у учётки хранилища (`storeUser` в `kb-connection.json`) выставьте минимальные права
(получение объектов / обновление из хранилища — без захвата, без помещения в хранилище, без админ-операций).
