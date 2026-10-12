---
name: 1c-sdms
description: Uses read-only MCP user-sdms for SDMS DNS Shop — задачи, спринты, канбан, трудозатраты, заявки З/ЗД/ЗР. Use when the user asks статус задачи, исполнитель, часы, спринт. Not 1C code, not MDD, not Confluence. Do not log time, create tasks, comment, or move kanban.
---

# SDMS — задачи и спринты (только чтение)

Namespace: `user-sdms`. Live API SDMS на ВМ. Запись отрезана: write-тулов нет, не предлагай списать часы, создать задачу, комментарий или сдвиг канбана. Правки — в самом SDMS.

Все клиенты видят данные **от имени JWT на ВМ**. «Мои задачи» = учётка токена в контейнере, не обязательно собеседник. Конкретный человек — email (`Фамилия.Инициалы@…` в транслите, как в SDMS).

## Номера

- Задача: `З0000372144` (иногда `ЗД-…`)
- Заявка: `ЗР000…` / `ЗР-…`
- Из URL `?ref=32 hex` — сначала `sdms_convert_ref`, не путай ref с GUID

Номер → GUID: `sdms_task_guid` / `sdms_devrequest_guid` / `sdms_internalwork_guid`. Многие get-тулы принимают и номер.

## Какие tools звать

1. `sdms_get_task` по номеру (или guid → get).
2. Трудозатраты — `sdms_get_timesheet`, суммируй `duration`. Фильтр людей — подстрока в `performer`.
3. Комментарии — `sdms_comment_counts`, при необходимости `sdms_list_comments`. `sdms_mark_read` нет.

Список: `sdms_list_tasks` нужен `performer` (email) или `team` (GUID). Без обоих API ошибётся. Статус/спринт на сервере не фильтруются.

Спринты — `sdms_branch_sprints`, состав — `sdms_sprint_objects`. ЗР — `sdms_get_devrequest` / `sdms_get_devrequest_tasks`. Канбан — `sdms_kanban_lists` / `sdms_kanban_data`. Проверка связи — `sdms_version`.

Не вызывать без нужды: `*_files`, справочники пачкой, длинный канбан целиком. После 6–8 вызовов — ответ.

## Ответ

**Источник:** SDMS (чтение)

Таблица: номер, название, статус / исполнитель, часы (факт). Оговорка: чего API не вернул; что «мои» = токен на ВМ.
