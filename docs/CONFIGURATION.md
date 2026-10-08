# Справочник конфигурации

[← README](../README.md)

Настройки читаются из окружения при запуске. `megapbx-max` не загружает `.env`
автоматически: для локального сервера используйте `uvicorn --env-file .env`
или экспорт переменных; installer создаёт `/etc/megapbx-max.env`, который
systemd подключает через `EnvironmentFile`. После изменения этого файла:

```bash
sudo systemctl restart megapbx-max
sudo systemctl status megapbx-max --no-pager
```

Пустые необязательные значения заменяются значениями по умолчанию.
Булевы параметры принимают `1/0`, `true/false`, `yes/no`, `on/off`.
Секреты не должны содержать переводов строк.

## Минимальная конфигурация сервера

Скопируйте [`.env.example`](../.env.example) и заполните:

```dotenv
MAX_BOT_TOKEN=<токен MAX>
MAX_CHAT_ID=<ID рабочего чата>
MAX_WEBHOOK_SECRET=<случайный секрет подписки>
MAX_WEBHOOK_URL=https://bot.example.com/max/webhook
MEGAPBX_CRM_TOKEN=<секрет входящего webhook>
MEGAPBX_ALLOWED_GROUP=Support
```

Значения в угловых скобках нужно заменить. Секрет MAX Webhook должен состоять
только из ASCII-букв, цифр, `_` и `-`. Например, новый секрет можно получить
командой `python -c "import secrets; print(secrets.token_urlsafe(32))"` и сохранить
в защищённом файле. Токены MAX API и MegaPBX API выдаются самими платформами.

## MAX

| Переменная | По умолчанию | Назначение и ограничения |
|---|---|---|
| `MAX_BOT_TOKEN` | обязательно | Токен отдельного MAX-бота; API получает его в `Authorization` без `Bearer` |
| `MAX_CHAT_ID` | обязательно | Ненулевой signed int64: от `-9223372036854775808` до `9223372036854775807`; отрицательный ID допустим |
| `MAX_API_BASE` | `https://platform-api2.max.ru` | HTTPS URL без credentials, query и fragment |
| `MAX_WEBHOOK_SECRET` | не задан | Обязателен для сервера; 5–256 символов `A-Z`, `a-z`, `0-9`, `_`, `-` |
| `MAX_WEBHOOK_URL` | не задан | Публичный `https://bot.example.com/max/webhook`, порт 443; нужен для `subscribe` и подписки installer |
| `MAX_WEBHOOK_UPDATE_TYPES` | см. ниже | CSV без дубликатов; обязательно включает `message_callback` |
| `MAX_WEBHOOK_BODY_BYTES` | `1048576` | Максимум входящего body, байты, минимум 1 |
| `MAX_API_MAX_RETRIES` | `2` | Число повторов допустимых API-запросов, минимум 0 |
| `MAX_API_RETRY_BASE_SEC` | `0.5` | Начальная задержка API-retry, минимум 0 |
| `MAX_API_RETRY_MAX_SEC` | `8` | Верхняя граница задержки API-retry, минимум 0 |
| `MAX_API_TIMEOUT_SEC` | `8` | Timeout HTTP-запроса, минимум 1 секунда |

Типы обновлений по умолчанию:

```text
message_callback,bot_started,bot_added,bot_removed,bot_admin_permissions_changed
```

Изменение `MAX_WEBHOOK_URL`, секрета или списка типов требует повторного
`megapbx-max subscribe` с новым окружением. Обычный рестарт приложения не
изменяет подписку в MAX. Webhook и Long Polling одного бота не совмещаются.

## MegaPBX

| Переменная | По умолчанию | Назначение и ограничения |
|---|---|---|
| `MEGAPBX_CRM_TOKEN` | обязательно | Общий секрет входящих событий из настроек CRM АТС |
| `MEGAPBX_ALLOWED_GROUP` | пусто | CSV точных названий разрешённых групп |
| `MEGAPBX_ALLOWED_DID` | пусто | CSV точных DID назначения (`telnum`/`diversion`) |
| `MEGAPBX_DID_NAMES` | пусто | Подписи `DID=Название;DID2=Название2`; запятая также разделяет пары |
| `MEGAPBX_ALLOW_ALL` | `0` | Разрешить любые направления независимо от фильтров |
| `MEGAPBX_ALLOW_QUERY_TOKEN` | `0` | Принимать CRM-секрет в `?token=...` для legacy-интеграций |
| `MEGAPBX_API_BASE` | не задан | HTTPS origin АТС, например `https://pbx.example.com`, **без `/crmapi/v1`** |
| `MEGAPBX_API_TOKEN` | не задан | Ключ REST API для обогащения, заголовок `X-API-KEY` |
| `MEGAPBX_ALLOW_HTTP_API` | `0` | Разрешить HTTP для MegaPBX REST API в локальной разработке |
| `MEGAPBX_WEBHOOK_BODY_BYTES` | `1048576` | Максимум входящего body, байты, минимум 1 |
| `MEGAPBX_ENRICHMENT_REFRESH_SEC` | `600` | Интервал обновления справочников, минимум 1 секунда |

Задайте хотя бы один фильтр или явно `MEGAPBX_ALLOW_ALL=1`, иначе запуск
завершится ошибкой. При одновременных фильтрах разрешено совпадение **группы
ИЛИ DID**, а не обязательное совпадение обоих. Пробелы вокруг элементов CSV
убираются; регистр названий и формат номера должны совпадать с payload.
Подписи DID не заменяют фильтры.

`MEGAPBX_API_BASE` и `MEGAPBX_API_TOKEN` задаются вместе. Они необязательны
для уведомлений: без них используются данные самого webhook.

Входящая авторизация проверяется в порядке: непустой `X-CRM-Token`, затем
разрешённый query-токен, затем `Authorization: Bearer ...` либо Basic
(секрет в части пароля). Неверный непустой заголовок не заменяется другим
правильным секретом из URL. MAX-токен, секрет MAX Webhook, CRM-секрет и
ключ REST API — разные настройки.

## SQLite, время и очередь

| Переменная | По умолчанию | Назначение и ограничения |
|---|---|---|
| `STATE_DB_PATH` | `data/state.sqlite3` | Постоянный файловый путь; `:memory:` запрещён; installer использует `/var/lib/megapbx-max/state.sqlite3` |
| `TZ_OFFSET_HOURS` | `3` | Фиксированное смещение UTC для текста, от -12 до +14; без сезонного перевода |
| `MISSED_MAX_AGE_SEC` | `3600` | Окно поиска по телефону для автозакрытия и возраст очистки закрытых уведомлений, минимум 0 |
| `MISSED_CLEANUP_INTERVAL_SEC` | `3600` | Интервал фоновой очистки, минимум 1 секунда |
| `MISSED_DEDUP_TTL_SEC` | `86400` | TTL дедупликации пропущенных звонков, минимум 0 |
| `JOB_MAX_ATTEMPTS` | `20` | Максимум попыток задания, минимум 1 |
| `JOB_RETRY_BASE_SEC` | `1` | Начальная задержка очереди, минимум 0 |
| `JOB_RETRY_MAX_SEC` | `300` | Верхняя граница задержки очереди, минимум 0 |
| `JOB_LEASE_SEC` | `300` | Время аренды задания worker, минимум 30 секунд |
| `SSL_CERT_FILE` | системное поведение HTTP-клиента | Путь к доверенному CA bundle; installer задаёт `/etc/ssl/certs/ca-certificates.crt` |

Повторы HTTP-клиента и очереди — разные уровни. Увеличение числа попыток
не разрешает автоматически повторять отправку сообщения с неизвестным
результатом. Для неё предусмотрена ручная сверка: [эксплуатация](OPERATIONS.md#8-диагностика).

Возраст очистки — не универсальный срок хранения всех данных: открытые
уведомления, pending/unknown-состояния и счётчики имеют отдельный жизненный
цикл. Подробности: [данные и приватность](PRIVACY.md).

## CLI с production environment file

Команды из обычной SSH-сессии не наследуют окружение systemd. Для установленного
по умолчанию сервиса можно запустить команду в краткоживущем unit:

```bash
sudo systemd-run --wait --pipe --collect \
  --property=EnvironmentFile=/etc/megapbx-max.env \
  /opt/megapbx-max/current/.venv/bin/megapbx-max check
```

Аналогично вызываются `subscriptions` или `subscribe`. Команда `check`
выводит сведения о боте, `subscriptions` — URL подписок: перед публикацией
диагностики обезличьте эти значения. Доступ к SQLite-командам также требует
прав на каталог состояния.
