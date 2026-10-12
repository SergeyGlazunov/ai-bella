# Stand gates (до cutover оркестратора)

Процедуры проверки на **живом** стенде перед переключением на `contour`.
Cutover без PASS здесь + drill бэкапа чат-слоя + явного разрешения — нельзя.

| Gate | Файл | Суть |
| --- | --- | --- |
| BSL watch pause | [`stand_watch_pause.md`](stand_watch_pause.md) | Убрать `/repos/SRC` из `daemon.toml`, reload; touch не запускает index; `serve` жив; restore |
| Close reads | [`stand_close_reads.md`](stand_close_reads.md) | `docker stop` graph; disconnect BSL от сети чата; IdleProbe; restore. Firewall на publish-порт — optional |

**Имена контейнеров/сетей в процедурах — боевой контур** (`mcp-metadata-graph`, `bsl-indexer`, `ai-stack_default`, порт `8011`). Для ai2 / нового сервера подставляй имена из [deploy-ai2/README.md](../deploy-ai2/README.md) (например `ai2-mcp-graph-ubd`, `ai2-bsl-ubd`, `ai2-stack`).

Статус последнего proof и команды — в [DEVELOPMENT.md](../DEVELOPMENT.md) §«Стенд». Cutover runbook — [USER.md](../USER.md) §«Переключение стенда».

Не gates: unit-тесты `controller/` (только пакет); `bootstrap.sh` / timer сами по себе cutover не делают.
