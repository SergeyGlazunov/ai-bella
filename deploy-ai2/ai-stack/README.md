# ai2 chat layer

Docker project `ai2-stack`. После cutover это живой чат-слой (контейнеры с префиксом `ai2-`).

| Сервис | Контейнер | Порт (host) |
|--------|-----------|-------------|
| Open WebUI | `ai2-open-webui` | `:3000` |
| OmniRoute | `ai2-omniroute` | `:20128` |
| MDD MCP | `ai2-mdd-mcp` | `:8080` (см. актуальный `docker-compose`) |

Compose: этот каталог / рабочий деплой `ai2-stack`. Секреты — в `.env` рядом с compose, не коммитить.

```bash
docker compose -p ai2-stack up -d
```

## Vision Bridge (glm-5.3 + скриншоты)

**Зачем.** `glm-5.3` — текстовая модель: вложение картинки даёт Z.ai/LiteLLM `400 messages.content.type is invalid, allowed values: ['text']`. `glm-5.3-flash` умеет vision. OmniRoute Vision/Modality Bridge в режиме **describe** описывает картинку через flash и подставляет текст в запрос к `glm-5.3`. Для клиента это одна модель.

**Дизайн и ops** — этот файл (ниже). Не дублировать в `docs/`.

### Включение (UI)

1. Открыть dashboard: `http://localhost:20128` (логин management password).
2. Settings → **Modality Bridge / Vision** (URL вида `/dashboard/settings/modality-bridge`).
3. Значения:
   - enabled: `true`
   - mode: **`describe`** (не `reroute`)
   - vision model: **`glm-5.3-flash`**
   - task-aware: `true` (желательно)
4. Сохранить. Настройки в SQLite OmniRoute (`key_value` / namespace `settings`), не в `.env`.

### Включение (DB, если UI недоступен)

Ключи (значения JSON-сериализованы, как у `updateSettings`):

- `modalityBridgeVisionEnabled` = `true`
- `modalityBridgeVisionMode` = `"describe"`
- `modalityBridgeVisionModel` = `"glm-5.3-flash"`
- `modalityBridgeVisionTaskAware` = `true`
- legacy: `visionBridgeEnabled` / `visionBridgeModel` тем же смыслом

После прямой правки DB: `docker restart ai2-omniroute` (сброс in-memory cache).

Скрипт в репо (dry-run / `--apply`): `workdir/omniroute_enable_vision_bridge.js` — копировать в контейнер и запускать с `-w /app`.

### Проверка

1. Open WebUI → модель `glm-5.3` → сообщение со скриншотом → нет ошибки `content.type`; ответ опирается на картинку.
2. То же без картинки — обычный текстовый ответ.
3. Модель `glm-5.3-flash` + картинка напрямую — работает.
4. Если после включения всё ещё 400: capability у `glm-5.3` ошибочно «vision» — forced bridge / снять vision capability в OmniRoute.

### Откат

UI: enabled = `false`, либо DB те же ключи в `false` + restart. Ожидаемо снова 400 на `glm-5.3` + image.
