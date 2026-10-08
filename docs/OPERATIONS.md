# Подготовка и эксплуатация MAX

Инструкция основана на документации MAX, актуальной на 24 сентября 2026 года.

## 1. Создание бота

1. Верифицируйте профиль организации, ИП или самозанятого на [платформе MAX для партнёров](https://business.max.ru/self).
2. Создайте бота и дождитесь модерации.
3. Скопируйте токен в разделе настроек бота.
4. Не добавляйте токен в Git, URL или application-лог.

Токен передаётся в API только заголовком:

```http
Authorization: <MAX_BOT_TOKEN>
```

Префикс `Bearer` и query-параметры не используются.

## 2. Получение `MAX_CHAT_ID`

Для отправки сообщения нужен ненулевой знаковый `int64` идентификатор чата; у группового чата он может быть отрицательным.

Основной способ:

1. в настройках бота разрешить добавление в групповые чаты (по умолчанию оно запрещено);
2. временно добавить бота в нужный групповой чат или канал;
3. подписаться на событие `bot_added` по [инструкции ниже](#3-подписка-webhook);
4. получить событие с полем `chat_id`;
5. записать его в `MAX_CHAT_ID` и перезапустить сервис.

Альтернатива для личного диалога: событие `bot_started` также содержит `chat_id`. Идентификатор группового чата и личного диалога различаются.

До создания production-подписки запустите отдельный development-цикл и отправьте боту стартовое сообщение или добавьте его в чат:

```bash
megapbx-max discover-chat-id
```

Предварительно задайте `MAX_BOT_TOKEN` в окружении; этой команде ещё не нужны
ID чата и фильтры АТС. Команда выводит только найденные `chat_id`. С июня
2026 года `GET /chats` больше не поддерживается, поэтому автоматически
перечислить все доступные боту чаты нельзя.

Для работы в группе назначьте бота администратором с правами чтения сообщений (`read_all_messages`) и изменения сообщений (`write`).

## 3. Подписка Webhook

Webhook должен:

- использовать `https://`;
- публично принимать соединения на порту 443;
- иметь сертификат доверенного ЦС или Минцифры с полной цепочкой;
- на сервере иметь системный CA bundle с доверенными корнями; installer передаёт `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`;
- отвечать HTTP 200 не позднее 30 секунд.

Секрет подписки должен содержать 5–256 символов из `A-Z`, `a-z`, `0-9`, `_`, `-`. MAX передаёт его в `X-Max-Bot-Api-Secret`; endpoint обязан сравнивать его в постоянном времени.

Пример JSON-контракта `POST /subscriptions` (в production используйте `megapbx-max subscribe`, чтобы не передавать токен в argv):

```json
{
  "url": "https://bot.example.com/max/webhook",
  "update_types": [
    "message_callback",
    "bot_started",
    "bot_added",
    "bot_removed",
    "bot_admin_permissions_changed"
  ],
  "secret": "replace_with_a_random_secret"
}
```

Если подписка уже существует, обновите её повторным вызовом `POST /subscriptions`. Удалить конкретную подписку можно через `megapbx-max unsubscribe` или `DELETE /subscriptions?url=...`.

После запуска HTTPS endpoint зарегистрируйте подписку отдельной идемпотентной командой:

```bash
megapbx-max subscribe
```

Команда использует `MAX_WEBHOOK_URL`, `MAX_WEBHOOK_SECRET` и `MAX_WEBHOOK_UPDATE_TYPES`; URL можно переопределить через `--url`, а секрет — через защищённый `--secret-file`. Приложение не удаляет подписку при старте, чтобы рестарт не отключил production Webhook. Installer регистрирует подписку при заданном `MAX_WEBHOOK_URL`, в том числе если TLS завершается на внешнем load balancer; локальный флаг `--enable-tls` не является обязательным для уже настроенного HTTPS endpoint.

Webhook и Long Polling одновременно не работают. Long Polling допустим только для разработки.

## 4. Nginx

Пример location без access log, чтобы случайные query-параметры и секреты не попали в журнал:

```nginx
location = /max/webhook {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Max-Bot-Api-Secret $http_x_max_bot_api_secret;
    proxy_read_timeout 35s;
    client_max_body_size 1m;
    access_log off;
}

location = /megapbx/webhook {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-CRM-Token $http_x_crm_token;
    proxy_read_timeout 35s;
    client_max_body_size 1m;
    access_log off;
}
```

Не проксируйте эти маршруты через CDN, который изменяет тело запроса, и не ограничивайте ответ менее чем 30 секундами.

## 5. Развёртывание

Рекомендуемый путь для Debian 12+/Ubuntu 24.04+ с Python 3.11+:

```bash
curl -fsSL https://raw.githubusercontent.com/IndeecDen/megapbx-max/main/install.sh \
  -o /tmp/megapbx-max-install.sh
less /tmp/megapbx-max-install.sh
sudo bash /tmp/megapbx-max-install.sh \
  --tag v0.1.0 --with-nginx \
  --domain bot.example.com --enable-tls \
  --tls-email admin@example.com
```

Для private repository добавьте `--github-token-file /root/.megapbx-github-token`. В production используйте `--tag <release-tag>` или `--commit <sha>`, а не изменяемый `main`. Installer сохраняет `/var/lib/megapbx-max/state.sqlite3` при обновлении и rollback, приводит конфигурацию к `root:root` и `0600`, а перед стартом проверяет её тем же `Settings.from_env`, что и приложение.

### Внешний HTTPS-прокси / Nginx Proxy Manager

Если TLS завершается на другом хосте, установите локальный HTTP Nginx:

```bash
sudo bash /tmp/megapbx-max-install.sh \
  --tag v0.1.0 --with-nginx --domain bot.example.com
```

В NPM создайте Proxy Host для `bot.example.com`, назначьте доверенный
сертификат и задайте upstream `http://<внутренний-IP-VM>:80`. Локальный
Nginx передаёт запросы приложению на `127.0.0.1:8000`. Доступ к порту 80 VM
ограничьте хостом прокси. В конфигурации приложения остаётся публичный
`MAX_WEBHOOK_URL=https://bot.example.com/max/webhook`.

Если внешний HTTPS ещё не готов, при установке добавьте `--no-subscribe`,
а после проверки маршрута выполните `subscribe` с production-окружением.
При legacy query-токене отключите запись query string и на внешнем прокси.

### Пути и unattended-установка

| Путь | Содержимое |
|---|---|
| `/etc/megapbx-max.env` | Защищённая конфигурация, `0600` |
| `/opt/megapbx-max/releases/` | Установленные выпуски с virtualenv |
| `/opt/megapbx-max/current` | Ссылка на активный выпуск |
| `/var/lib/megapbx-max/state.sqlite3` | SQLite состояния, счётчиков и очереди |
| `/var/backups/megapbx-max/transaction.*/` | Резервные копии перед обновлением |
| `megapbx-max.service` | systemd-сервис |

Для автоматизированной установки подготовьте защищённый env-файл и используйте
`--non-interactive --env-file /root/megapbx-max.env --yes` вместе с выбранными
параметрами reverse proxy. Перед применением можно добавить `--dry-run`.
Полный список флагов: `bash install.sh --help`.

## 6. Переменные окружения

Обязательные:

- `MAX_BOT_TOKEN` — токен бота;
- `MAX_CHAT_ID` — ID рабочего чата;
- `MEGAPBX_CRM_TOKEN` — общий секрет webhook MegaPBX;
- хотя бы один фильтр направления либо явный `MEGAPBX_ALLOW_ALL=1`.

Webhook MAX включается через `MAX_WEBHOOK_SECRET`. Полный шаблон находится
в [`.env.example`](../.env.example), описание всех параметров — в
[CONFIGURATION.md](CONFIGURATION.md). Там же приведён запуск CLI с
production environment file: обычная SSH-сессия не наследует окружение сервиса.

## 7. Обновление и rollback

Installer перед изменением release/systemd/Nginx создаёт transaction в
`/var/backups/megapbx-max` и сохраняет существующую БД в `transaction.*/state.sqlite3`
через SQLite Online Backup API (включая данные WAL), затем выполняет `quick_check`.
Копия имеет права `0600`; при ошибке резервного копирования обновление прерывается.
Путь к БД берётся из прежней конфигурации. При первой установке копии БД ещё нет.
Для production-обновления используйте `--tag` или `--commit` и не удаляйте
transaction-каталоги до успешной проверки. При
ошибке installer автоматически возвращает предыдущий symlink `current`, unit и
конфигурацию; SQLite state не откатывается. Ручной rollback выпуска выполняется
снова через installer с предыдущим immutable ref после проверки совместимости
schema.

Автоматический rollback не восстанавливает БД из копии, чтобы не потерять события,
принятые после создания снимка. Восстановление БД — отдельная ручная операция
при остановленном сервисе. Для локальных установок храните актуальный `install.sh`
вместе с проверенным исходным release; не используйте старые отладочные копии.

## 8. Диагностика

Пошаговый разбор HTTP, DNS, TLS и фильтров: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

События `ACCEPTED`/`COMPLETED` могут относиться к исходящему перезвону с отдельным
`callid`. Если в durable inbox уже есть `OUTGOING` или `history/type=out` этого
звонка, промежуточное событие завершается без изменения сообщения. Успешный
перезвон закрывает уведомление по `history/Success/out/missedStatus=2`.
Если направление ещё неизвестно, событие ожидает повторной обработки; соответствие
другому звонку только по номеру для таких промежуточных событий не используется.

- `/healthz` — процесс работает;
- `/readyz` — конфигурация загружена и SQLite доступна;
- `megapbx-max deliveries` — записи с неоднозначным результатом `POST /messages`;
- `megapbx-max callback-unknown` — callback'ы с неоднозначным ответом `/answers`;
- `megapbx-max resolve-callback-unknown --callback-id ...` — после ручной сверки пометить неопределённый callback завершённым **без** повторного `/answers` и **без** закрытия уведомления в SQLite. Если MAX уже изменил сообщение, отдельно сверьте и устраните расхождение состояния;
- `megapbx-max retry-callback-unknown --callback-id ...` — только после проверки, что MAX **не** применил ответ: вернуть сохранённый webhook в очередь для повторного `/answers`. При неизвестном результате не применяйте эту команду вслепую: возможно повторное действие или отказ MAX;
- `megapbx-max resolve-unknown --record-id ... --message-mid ...` — подтвердить, что сообщение существует;
- `megapbx-max retry-unknown --record-id ...` — только осознанно разрешить повторную отправку; команда освобождает claim, возвращает связанный job в очередь и закрывает pending-счётчик. Для claim без `callid` связь также хранится в SQLite; если job уже удалён cleanup, команда возвращает ошибку и сохраняет claim для ручной сверки;
- `megapbx-max check` — отдельная проверка актуальности MAX-токена;
- `journalctl -u megapbx-max` — журнал без token, raw payload и персональных данных;
- `GET /me` вручную — проверка токена;
- `GET /subscriptions` — проверка production-подписки.

Ошибки `401` обычно означают неверный токен, `403` — недостаток прав, `405` — конфликт Webhook/Long Polling, `429` — превышение лимита, `503` — недоступность MAX.

CLI ограничивает журналы `httpx`/`httpcore` уровнем WARNING даже при `--log-level DEBUG`,
чтобы стандартные HTTP-логи не раскрывали query-параметры, включая `callback_id`.
