# Архитектурные решения переноса

## ADR-001. Handwritten Python-клиент MAX

**Решение:** использовать `httpx` + Pydantic-модели, собранные по официальной OpenAPI `0.0.33`.

**Причина:** на 24.09.2026 официального Python SDK нет. Автогенератор официально поддерживается, но не dispatch-ит `Update` и генерирует лишний код. Проекту нужны только `GET /me`, сообщения, `/answers` и subscriptions.

**Обязательные проверки клиента:**

- HTTP status;
- `success: false` при HTTP 200;
- наличие `message.body.mid` в ответе `POST /messages`;
- `Authorization` без `Bearer`;
- отсутствие retry неоднозначного read timeout для `POST /messages`.

## ADR-002. Два независимых входящих webhook

- `/megapbx/webhook` — `X-CRM-Token`;
- `/max/webhook` — `X-Max-Bot-Api-Secret`.

Секреты не смешиваются, не принимаются из query string и сравниваются в постоянном времени. Размер входящего тела ограничивается приложением независимо от того, что официально не указывает лимит MAX.

## ADR-003. Webhook — production, Long Polling — только разработка

FastAPI webhook отвечает в пределах 30 секунд. Long Polling остаётся отдельным development runner. Одновременная подписка и polling не используются.

## ADR-003a. Durable inbox и worker

MAX Webhook не ждёт внешнего API: после проверки подписи событие атомарно попадает в SQLite `jobs`, endpoint отвечает `200`, а один worker обрабатывает очередь с lease, backoff и дедупликацией. `callback.callback_id` и business event key не позволяют повторной доставке запустить вторую обработку.

Для `POST /messages` используется состояние `prepared → inflight → completed` или `unknown`. Неоднозначный network/HTTP outcome не ретраится автоматически: он остаётся в `unknown` до ручной сверки/resolve.

## ADR-004. SQLite вместо критического состояния в памяти

Telegram-версия хранит звонки и счётчики в памяти процесса. Для MAX это повышает риск на повторных Webhook-событиях и рестартах. В новой версии:

- notification/call state;
- `callback.callback_id` для idempotency;
- TTL и очистка;
- durable inbox/outbox jobs.

В SQLite сохраняется минимальный canonical payload входа, необходимый для повторной обработки; токены, заголовки авторизации и secrets туда не записываются. WAL и транзакции используются для конкурентных webhook.

## ADR-005. Идентификаторы

- `callid` MegaPBX остаётся бизнес-ключом;
- `message.body.mid` — идентификатор сообщения MAX;
- `callback.callback_id` — ключ идемпотентности нажатия;
- при отсутствии callback message и авторитетного `chat_id` callback fail-closed: внутренний `record_id` не заменяет проверку чата;
- `MAX_CHAT_ID` берётся из `bot_added`/`bot_started`, потому что `GET /chats` удалён.

## ADR-006. Callback

Нажатие обрабатывается через `POST /answers?callback_id=...`. В одном запросе исходное сообщение заменяется закрытым текстом без активной кнопки. Повторный `callback_id` не закрывает новый звонок и не отправляет второе уведомление.

Если поле `notification` окажется недоступно в фактическом API, отправка обновлённого сообщения остаётся достаточной: `message` в `CallbackAnswer` — основной контракт. Неоднозначный ответ `/answers` не ретраится автоматически: callback переходит в `external_unknown` и требует ручной сверки.

## ADR-007. Лимиты

Клиент ограничивает процесс глобально до 30 RPS, а сообщения/редактирования/callback — FIFO-подобным последовательным доступом на уровне 2 операции в секунду для одного `chat_id`. Transient-ошибки повторяются с exponential backoff и jitter. Неоднозначный read timeout нового сообщения не повторяется.

## ADR-008. Системный CA bundle

Installer задаёт `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`, а клиенты MAX/MegaPBX используют его при наличии. Это позволяет добавить сертификаты Минцифры/системного Debian/Ubuntu без отключения TLS-проверки.
