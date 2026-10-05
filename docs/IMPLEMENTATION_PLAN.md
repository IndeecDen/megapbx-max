# План переноса MegaPBX → MAX

Документ составлен по состоянию на **24 сентября 2026 года** на основании исходного проекта `megapbx-tg` и актуальной документации [MAX Bot API](https://dev.max.ru/docs/chatbots/bots-coding/prepare).

## Вывод о реализуемости

Перенос основного сценария возможен без потери функциональности:

- приём webhook MegaPBX остаётся HTTP `POST /megapbx/webhook` и проверяется через `X-CRM-Token`;
- фильтрация по группе и DID, локальная карта DID → название и обогащение через API MegaPBX переносятся без изменений;
- отправка пропущенного звонка в рабочий чат, inline-кнопка «Я наберу», редактирование уведомления и дедупликация по `callid` поддерживаются MAX;
- нажатие кнопки приходит как событие `message_callback`; значение `callback.callback_id` используется в `POST /answers`, а прикладной идентификатор звонка хранится в `callback.payload`;
- transient-ошибки HTTP, лимиты, фильтрация и очистка состояния можно перенести.

Основные отличия адаптации:

- вместо `TG_BOT_TOKEN` и `TG_CHAT_ID` используются `MAX_BOT_TOKEN` и положительный `MAX_CHAT_ID` (`int64`);
- официальной Python-библиотеки MAX нет, поэтому используется небольшой типизированный клиент на `httpx` по официальной OpenAPI-схеме;
- production-получение событий должно работать через HTTPS Webhook (`https://domain/max/webhook`, порт 443, доверенный TLS-сертификат); Long Polling оставляется только для разработки;
- endpoint MAX должен ответить `200 OK` не позднее 30 секунд;
- глобальный лимит Bot API — 30 запросов/с, а сообщения/редактирования в одном чате ограничены 2 операциями/с;
- API MAX с июня 2026 года не возвращает список чатов, где находится бот; `MAX_CHAT_ID` нужно получить из события `bot_added`/`bot_started` либо из диагностического сценария;
- в групповом чате бот должен быть администратором с правом чтения сообщений, чтобы получать события и нажатия кнопок.

Официальные ссылки:

- [создание и модерация бота](https://dev.max.ru/docs/chatbots/bots-create/create);
- [обзор Bot API](https://dev.max.ru/docs-api);
- [Webhook subscriptions](https://dev.max.ru/docs-api/methods/POST/subscriptions);
- [события Update](https://dev.max.ru/docs-api/objects/Update);
- [отправка сообщений](https://dev.max.ru/docs-api/methods/POST/messages);
- [редактирование сообщений](https://dev.max.ru/docs-api/methods/PUT/messages);
- [ответ на callback](https://dev.max.ru/docs-api/methods/POST/answers);
- [клавиатура](https://dev.max.ru/docs-api/use-cases/sending-messages/keyboard);
- [OpenAPI-схема](https://github.com/max-messenger/api-schema) — при разработке использована схема `0.0.33` от 18.09.2026.

## Этапы

### 0. Аудит исходника и контрактов — выполнено

- инвентаризировать все сценарии `megapbx-tg`;
- зафиксировать webhook-контракт MegaPBX и тестовые примеры;
- сопоставить Telegram API с MAX;
- зафиксировать несовместимости и ограничения.

**Критерий завершения:** карта функций, payload/event-контракты и этапы приняты.

### 1. Каркас нового проекта — выполнено

- модульная структура Python-пакета вместо монолита;
- строгая конфигурация и `.env.example`;
- FastAPI application factory и health endpoints;
- типизированные доменные модели;
- тестовая инфраструктура, Ruff и CI.

**Критерий завершения:** приложение импортируется, health-check проходит, тесты и lint зелёные.

### 2. MAX API-клиент и webhook transport — выполнено на уровне контрактных тестов

- HTTP-клиент с `Authorization: <token>` и базой `platform-api2.max.ru`;
- Pydantic-модели `Update`, `Message`, `MessageBody`, callback;
- отправка, редактирование и ответы на callback;
- безопасный retry, rate limiting и разбор `success=false`;
- endpoint `/max/webhook` с проверкой `X-Max-Bot-Api-Secret`;
- durable inbox/job queue: webhook атомарно сохраняет событие и отвечает до 30 секунд, worker обрабатывает его с retry;
- регистрация/проверка production-подписки;
- отдельный Long Polling runner для разработки.

**Критерий завершения:** round-trip тесты моделей, unit-тесты retry и проверка webhook-подписи.

### 3. Перенос бизнес-логики MegaPBX — выполнено

- внутренний неизменяемый `CallEvent`;
- фильтры группы/DID и fallback-аватары;
- обогащение имени клиента, группы и сотрудника через MegaPBX API;
- постоянное состояние в SQLite (улучшение относительно Telegram-версии) с дедупликацией и TTL;
- state machine доставки `prepared/inflight/unknown/completed` без автоматического повтора неоднозначного `POST /messages`;
- порог возраста пропущенного звонка;
- очистка старого состояния;
- безопасное логирование без payload, токенов и персональных данных.

**Критерий завершения:** исходные сценарии MegaPBX проходят перенесённые тесты.

### 4. Интерактивная кнопка и корреляция — выполнено на contract/mock-тестах

- версионированный callback payload с внутренним `record_id`;
- дедупликация `callback.callback_id` в постоянном состоянии;
- ownership token/lease и состояния `processing/external_confirmed/external_unknown/committed` для callback;
- подтверждение перезвона и редактирование сообщения через `/answers`;
- защита от чужого/устаревшего callback;
- идемпотентное закрытие уведомления;
- корректные статусы (`Busy`, `Missed`, `NotAvailable` и т. п.).

**Критерий завершения:** нажатие пользователя изменяет исходное сообщение ровно один раз.

### 5. Наблюдаемость, документация и эксплуатация — локально выполнено, production-проверка ожидается

- структурированные логи без персональных данных;
- `/healthz` и `/readyz`;
- runbook, troubleshooting и процедура получения `MAX_CHAT_ID`;
- Debian/Ubuntu installer, systemd, Nginx/TLS;
- `SHA256SUMS` и проверка manifest перед установкой;
- миграция промежуточной SQLite schema и проверка миграции в uvicorn-процессе;
- security/privacy/limitations docs.

**Критерий завершения:** сервис устанавливается на чистую VM, проходит health-check и принимает подписанный MAX Webhook. Локальные проверки выполнены; чистая Debian/Ubuntu VM и production MAX ещё не предоставлены.

### 6. Проверка совместимости и релиз — подготовлено, cutover ожидается

- контрактные тесты на sanitized fixtures MegaPBX;
- локальный smoke-test FastAPI/uvicorn, durable worker и SQLite migration;
- ручной smoke-test на опубликованном боте MAX;
- нагрузочная проверка лимитов и повторных webhook;
- release tag, changelog и инструкции отката.

**Критерий завершения:** паритет с `megapbx-tg-v0.1.1` подтверждён матрицей функций. Локальные contract/mock-проверки зелёные; production-критерий остаётся обязательным.

> Статусы `тесты ✅` означают локальную mock/contract-проверку. До релиза обязательны проверки на опубликованном MAX-боте, реальном MegaPBX webhook и production-подписке.

## Матрица паритета

| Возможность | Telegram-источник | Реализация в MAX | Статус |
|---|---|---|---|
| MegaPBX webhook | FastAPI endpoint | сохраняется | тесты ✅ |
| Фильтр group/DID | env allowlist | сохраняется | тесты ✅ |
| Обогащение данных | HTTP API MegaPBX | сохраняется | тесты ✅ |
| Уведомление о пропущенном | `sendMessage` | `POST /messages` | тесты ✅ |
| Кнопка «Я наберу» | `callback_query` | `inline_keyboard` + `message_callback` | тесты ✅ |
| Закрытие/статус | `editMessageText` | `PUT /messages` или `POST /answers` | тесты ✅ |
| Дедупликация `callid` | процессная память | SQLite + TTL | тесты ✅ |
| Retry временных ошибок | Telegram API | MAX API | тесты ✅ |
| Защита секретов | заголовок токена | два независимых заголовка webhook | тесты ✅ |
| Постоянное состояние | отсутствует | SQLite с TTL и callback idempotency | тесты ✅ |
