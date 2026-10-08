# Changelog

Все значимые изменения проекта фиксируются здесь. Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-10-08

### Добавлено

- перенос интеграции MegaPBX → Telegram на MAX Bot API;
- типизированный Python-клиент MAX без `Bearer` и без неофициального SDK;
- MAX Webhook `/max/webhook` с проверкой `X-Max-Bot-Api-Secret`;
- команды `check`, `subscribe`, `subscriptions`, `unsubscribe`, `poll` и `discover-chat-id`;
- inline-кнопки «Я наберу» и «Перезвонил …»;
- автоматическое закрытие по событиям MegaPBX и успешному перезвону;
- статусы неудачного перезвона;
- постоянное SQLite-состояние, дедупликация `callid` и `callback_id`;
- обогащение сотрудников и групп через MegaPBX REST API;
- rate limiting, retry/backoff и проверка `success:false`;
- health/readiness endpoints, CI и тесты;
- миграция промежуточной SQLite schema и durable worker;
- `SHA256SUMS` для проверяемого release manifest;
- повторное открытие завершённого missed-job после истечения dedup TTL.
- подробный справочник конфигурации, диагностика и шаблоны issues/PR;
- SQLite Online Backup API перед обновлением с проверкой целостности.

### Исправлено

- лишние повторы `ACCEPTED`/`COMPLETED` исходящего звонка при отдельном `callid`;
- валидация отрицательных ID групповых чатов MAX;
- экспорт переменных installer, атомарная запись конфигурации и порядок проверки systemd unit;
- стандартные INFO/DEBUG HTTP-логи, раскрывавшие `callback_id`;
- неоднозначный HTTP 408 outcome нового сообщения теперь не считается гарантированно неотправленным;
- API URL с query/fragment/credentials отклоняются до запуска клиента;
- installer восстанавливает удалённый config при rollback и проверяет все записи checksum;
- installer bootstrap работает без предустановленного `flock`, создаёт backup root до transaction и применяет installer controls из env-файла;
- installer поддерживает явные `--tag`, `--branch` и `--commit`, валидирует runtime config и допускает внешний HTTPS webhook;
- installer отклоняет конфликт `server_name` с другими Nginx sites;
- `retry-unknown` освобождает pending-счётчик и возвращает связанный job в очередь, включая webhook без `callid`;
- `retry-unknown` больше не сообщает об успехе, если связанный job удалён или ещё processing;
- `resolve-unknown` завершает связанный job для webhook без `callid`, не допуская повторной отправки;
- callback в состоянии `processing` не исчерпывает лимит попыток job;
- неоднозначный HTTP 2xx без `success` для `/answers` переводится в `external_unknown`;
- `success` в MAX API принимается только как строгий boolean, без coercion строк и чисел;
- Retry-After ограничен retry budget, а non-finite retry/timeout значения отклоняются;
- callback с несовпадающим `message.body.mid` отклоняется;
- webhook с пустым MAX secret закрывается fail-closed;
- `application/json` с JSON `null` отклоняется как не-объектный payload.

### Безопасность

- отдельные секреты MAX, MegaPBX webhook и MegaPBX API;
- fail-closed authentication;
- постоянное сравнение webhook-секретов;
- ограничение размера body;
- HTML-экранирование;
- логирование без raw payload и ПДн.

### Ограничения

- один экземпляр приложения на SQLite-базу;
- внешний вызов `POST /messages` не имеет idempotency key.
