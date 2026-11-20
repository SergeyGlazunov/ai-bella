# Stand procedures and migration runbooks

Контракт продукта — [../docs/superpowers/specs/2026-10-18-installable-contour-design.md](../docs/superpowers/specs/2026-10-18-installable-contour-design.md). Файлы ниже — процедуры на **живом** стенде и runbooks миграции, не новая цель разработки.

Стенд уже на `ai-contour.timer`. Миграцию с legacy, аварийный порядок и mutating gates — только после явного разрешения.

## Штатные ops

| Процедура | Файл | Суть |
| --- | --- | --- |
| Reboot bring-up | [`stand_reboot_bringup.md`](stand_reboot_bringup.md) | Ручной запуск после перезагрузки: `DockerWSL\start.ps1`, `docker start` ai2, smoke `:3000`/MCP |
| BSL watch pause | [`stand_watch_pause.md`](stand_watch_pause.md) | Убрать `/repos/SRC` из `daemon.toml`, reload; touch не запускает index; `serve` жив; restore |
| Close reads | [`stand_close_reads.md`](stand_close_reads.md) | `docker stop` graph; disconnect BSL от сети чата; IdleProbe; restore. Firewall на publish-порт — optional |

**Имена контейнеров/сетей в процедурах — боевой контур** (`mcp-metadata-graph`, `bsl-indexer`, `ai-stack_default`, порт `8011`). Для ai2 / нового сервера подставляй имена из [deploy-ai2/README.md](../deploy-ai2/README.md) (например `ai2-mcp-graph-ubd`, `ai2-bsl-ubd`, `ai2-stack`).

## Миграция / повторная постановка

Не ежедневная работа. Нужны при переносе с legacy или повторной установке оркестратора на стенд.

| Stage | Файл | Суть |
| --- | --- | --- |
| Preflight | [`linux-first-preflight.md`](linux-first-preflight.md) | Read-only inventory; stop if dual writers / secrets / timer already enabled |
| Catch-up | [`linux-first-catchup.md`](linux-first-catchup.md) | One controlled `contour update`; timer stays disabled |
| Cutover | [`linux-first-cutover.md`](linux-first-cutover.md) | Enable `ai-contour.timer`; Windows = export-only |

Каждый runbook содержит: read-only inventory, mutating commands, evidence, rollback point, stop conditions, запрет concurrent legacy+contour writers.

Kit executable proof (не stand): `python -m pytest tests/integration -q` в `controller/`.

Факты стенда — [STATUS.md](../STATUS.md). Эксплуатация — [USER.md](../USER.md). Команды ночи — [NIGHTLY.md](../NIGHTLY.md).

Не gates: unit-тесты `controller/` сами по себе миграцию не делают; `bootstrap.sh` ставит units **без** `enable` timer.
