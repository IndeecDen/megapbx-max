# MegaPBX → MAX

Самостоятельно развёртываемый сервис, который принимает события виртуальной АТС MegaPBX и
отправляет уведомления о пропущенных входящих звонках в рабочий чат MAX.
Оператор может нажать **«Я наберу»** — сообщение изменится на
**«Перезвонил …»**. Если MegaPBX сообщает об успешном исходящем перезвоне,
сервис закрывает исходное уведомление автоматически.

Проект переносит интеграцию [`megapbx-tg`](https://github.com/IndeecDen/megapbx-tg)
на официальный [MAX Bot API](https://dev.max.ru/docs/chatbots/bots-coding/prepare),
с поддержкой JSON и form webhook MegaPBX.

[![CI](https://github.com/IndeecDen/megapbx-max/actions/workflows/ci.yml/badge.svg)](https://github.com/IndeecDen/megapbx-max/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Навигация:** [возможности](#возможности) · [установка](#быстрый-старт) ·
[MegaPBX](#настройка-megapbx) · [MAX](#настройка-max) ·
[параметры](docs/CONFIGURATION.md) · [диагностика](docs/TROUBLESHOOTING.md)

Кнопка **«Я наберу»** — ручная отметка: она закрывает уведомление, но **не
инициирует телефонный звонок** и не проверяет, состоялся ли разговор.

> Документация и примеры не содержат production-адресов, идентификаторов чатов,
> телефонов, персональных данных или секретов. Все значения в командах ниже —
> placeholders.

## Возможности

### MegaPBX

- `POST /megapbx/webhook` принимает JSON, URL-encoded form и form с вложенным
  JSON в поле `payload`;
- аутентификация через `X-CRM-Token`, Bearer или Basic;
- совместимость с legacy MegaPBX, который передаёт CRM-ключ в
  `?token=...` — включается явно через `MEGAPBX_ALLOW_QUERY_TOKEN=1`;
- фильтрация по имени группы, DID или явное разрешение всех направлений;
- обработка `history/Missed` для входящих звонков;
- имя/номер клиента, группа, ожидание и длительность в уведомлении;
- постоянные счётчики пропущенных звонков по номеру за день и за всё время;
- обработка событий `event` и итогов исходящего перезвона; `contact` принимается
  в очередь без отправки уведомления и без CRM-обогащения ответа;
- автоматическое закрытие по `history/Success/out/missedStatus=2`;
- обновление текста при неудачном перезвоне (`Busy`, `Missed`, `Cancel`,
  `NotAvailable`, `NotAllowed`, `NotFound`);
- обогащение имён сотрудников и групп через MegaPBX REST API.

### MAX

- отправка HTML-сообщений с inline-кнопкой;
- callback-кнопка «Я наберу» с идемпотентной обработкой;
- callback Webhook `POST /max/webhook` с проверкой
  `X-Max-Bot-Api-Secret`;
- production Webhook и development Long Polling;
- команды CLI: `check`, `subscribe`, `subscriptions`, `unsubscribe`,
  `discover-chat-id`, `poll`, `deliveries`, `callback-unknown` и операции
  восстановления неоднозначных результатов.

### Надёжность и безопасность

- durable SQLite inbox и worker с повторной обработкой;
- дедупликация входящих событий по `callid` и MAX callbacks по `callback_id`;
- безопасная модель неоднозначного результата `POST /messages`: сервис не
  отправляет сообщение повторно вслепую;
- retry с backoff/jitter для временных ошибок;
- ограничение MAX API: глобально 30 RPS и 2 message-операции в секунду на чат;
- ограничение размера webhook body;
- HTML-экранирование пользовательских данных;
- application-логи без raw payload, токенов, телефонов и имён клиентов;
- Online Backup API SQLite перед обновлением, проверка `quick_check` и права
  `0600` для production-конфигурации и backup-файлов;
- systemd hardening, отдельный системный пользователь и HTTPS reverse proxy.

## Архитектура

```text
MegaPBX ──POST /megapbx/webhook──▶ HTTPS reverse proxy ──▶ FastAPI
                                                               │
                                             auth / parser / filter
                                                               │
                                                        SQLite inbox
                                                               │
                                                        durable worker
                                                               │
                                                               ▼
                                                        MAX Bot API

MAX ──POST /max/webhook──────────▶ HTTPS reverse proxy ──▶ FastAPI
                                                               │
                                                        callback worker
                                                               │
                                                               ▼
                                                        POST /answers
```

Один экземпляр приложения должен обслуживать одну SQLite-базу. В production
рекомендуется завершать TLS во внешнем Nginx Proxy Manager, Nginx или другом
reverse proxy, а приложение оставлять доступным только на loopback.

## Быстрый старт

### Требования

- Debian/Ubuntu с systemd для production;
- Python 3.11+ для локального запуска;
- опубликованный и верифицированный MAX-бот;
- публичный HTTPS на порту 443 с доверенным сертификатом;
- администраторские права MAX-бота в рабочем чате с `read_all_messages` и
  `write`;
- доступ MegaPBX к публичному адресу CRM Webhook.

### Локальный запуск

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Заполните `.env` только локально. Файл `.env` не читается автоматически
приложением, поэтому передайте его явно:

```powershell
uvicorn --env-file .env megapbx_max.main:create_app --factory `
  --host 127.0.0.1 --port 8000 --no-access-log
```

Или задайте переменные окружения и используйте CLI:

```powershell
$env:MAX_BOT_TOKEN = "<max-bot-token>"
$env:MAX_CHAT_ID = "<signed-int64-chat-id>"
$env:MAX_WEBHOOK_SECRET = "<random-max-webhook-secret>"
$env:MEGAPBX_CRM_TOKEN = "<crm-webhook-secret>"
$env:MEGAPBX_ALLOWED_DID = "<allowed-did>"

megapbx-max check
megapbx-max serve
```

### Production installer

Сначала скачайте installer, просмотрите его и используйте immutable release
(`--tag` или `--commit`) вместо изменяемой ветки:

```bash
curl -fsSL https://raw.githubusercontent.com/IndeecDen/megapbx-max/main/install.sh \
  -o /tmp/megapbx-max-install.sh
less /tmp/megapbx-max-install.sh

sudo bash /tmp/megapbx-max-install.sh \
  --tag v0.1.0 \
  --with-nginx \
  --domain bot.example.com \
  --enable-tls \
  --tls-email admin@example.com
```

Installer:

1. проверяет ОС, Python и аргументы;
2. загружает только release-файлы из manifest;
3. проверяет `SHA256SUMS`;
4. создаёт отдельного пользователя, virtualenv и systemd unit;
5. сохраняет существующую SQLite через SQLite Online Backup API перед заменой;
6. переключает `current` symlink на новый release;
7. проверяет конфигурацию тем же валидатором, что использует приложение;
8. запускает health check и проверяет Nginx;
9. при необходимости регистрирует MAX Webhook.

Подробнее: [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

При внешнем Nginx Proxy Manager используйте `--with-nginx` без
`--enable-tls`: внешний прокси завершает HTTPS и передаёт запросы по HTTP
на порт 80 VM. Пошаговая схема есть в разделе
[внешнего HTTPS-прокси](docs/OPERATIONS.md#внешний-https-прокси--nginx-proxy-manager).

## Настройка MegaPBX

В настройках CRM MegaPBX укажите:

```text
Адрес CRM: https://bot.example.com/megapbx/webhook
```

CRM-ключ должен совпадать с `MEGAPBX_CRM_TOKEN`. Сначала предпочтителен
заголовок `X-CRM-Token`. Некоторые legacy-конфигурации MegaPBX добавляют ключ
в URL как `?token=...`; для такого режима задайте:

```dotenv
MEGAPBX_ALLOW_QUERY_TOKEN=1
```

Query-токен менее безопасен: он может попасть в логи внешнего reverse proxy.
Используйте его только если MegaPBX не умеет передавать CRM-ключ заголовком,
отключите access log для webhook location и ротируйте ключ при раскрытии.

Пример фильтров:

```dotenv
MEGAPBX_ALLOWED_GROUP=Support,Sales
MEGAPBX_ALLOWED_DID=70000000001,70000000002
MEGAPBX_DID_NAMES="70000000001=Main line;70000000002=Support line"
```

Списки групп и DID разделяются запятыми. Разрешено совпадение **группы ИЛИ DID**.
Сравнение точное, с учётом регистра: DID в фильтре должен совпадать с `telnum`
или `diversion` в payload, включая формат номера. Это номер назначения, а не
телефон клиента. `MEGAPBX_DID_NAMES` меняет подписи, но не разрешения.
Укажите хотя бы группу, DID или явно:

```dotenv
MEGAPBX_ALLOW_ALL=1
```

Не включайте `MEGAPBX_ALLOW_ALL`, если требуется ограниченная рабочая группа.

## Настройка MAX

1. Создайте и опубликуйте MAX-бота.
2. Добавьте его в рабочий чат и назначьте необходимые права.
3. Получите `MAX_CHAT_ID` из `bot_added` или `bot_started` через development
   polling:

   ```bash
   megapbx-max discover-chat-id
   ```

4. Настройте `MAX_WEBHOOK_URL` и `MAX_WEBHOOK_SECRET`.
5. Зарегистрируйте production Webhook:

   ```bash
   megapbx-max subscribe
   megapbx-max subscriptions
   ```

CLI-команды используют переменные текущего окружения; файл `.env` сам по себе
их не устанавливает. Последовательность получения ID и настройки подписки:
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

MAX Webhook принимает только `POST /max/webhook`. Секрет передаётся в
`X-Max-Bot-Api-Secret`; открыть URL в браузере для проверки нельзя, потому что
браузер отправляет `GET`.

## Конфигурация

Полный шаблон находится в [`.env.example`](.env.example).

| Переменная | Назначение |
|---|---|
| `MAX_BOT_TOKEN` | токен MAX Bot API |
| `MAX_CHAT_ID` | знаковый non-zero `int64` ID чата |
| `MAX_API_BASE` | HTTPS base URL MAX API |
| `MAX_WEBHOOK_SECRET` | секрет заголовка MAX Webhook |
| `MAX_WEBHOOK_URL` | публичный URL MAX Webhook |
| `MAX_WEBHOOK_UPDATE_TYPES` | типы событий MAX |
| `MEGAPBX_CRM_TOKEN` | секрет входящего MegaPBX Webhook |
| `MEGAPBX_ALLOW_QUERY_TOKEN` | legacy `?token=...`, по умолчанию `0` |
| `MEGAPBX_ALLOWED_GROUP` | CSV разрешённых групп |
| `MEGAPBX_ALLOWED_DID` | CSV разрешённых DID |
| `MEGAPBX_DID_NAMES` | `DID=Название;DID2=Название2` |
| `MEGAPBX_ALLOW_ALL` | явное разрешение всех направлений |
| `MEGAPBX_API_BASE` | база MegaPBX API без `/crmapi/v1` |
| `MEGAPBX_API_TOKEN` | отдельный API-ключ MegaPBX (`X-API-KEY`) |
| `STATE_DB_PATH` | путь к SQLite |
| `TZ_OFFSET_HOURS` | локальный часовой пояс для текста |
| `MISSED_MAX_AGE_SEC` | окно поиска по телефону при автозакрытии; возраст очистки закрытых записей |
| `MISSED_DEDUP_TTL_SEC` | TTL дедупликации missed-событий |
| `JOB_MAX_ATTEMPTS` | предел попыток durable worker |
| `SSL_CERT_FILE` | необязательный CA bundle |

Все секреты передаются через environment file с правами `0600`, systemd или
защищённый secret manager. Не коммитьте `.env`, SQLite и реальные webhook
payloads.

## Health checks и диагностика

```bash
curl -fsS https://bot.example.com/healthz
curl -fsS https://bot.example.com/readyz
sudo systemctl status megapbx-max --no-pager
sudo journalctl -u megapbx-max -f
megapbx-max check
megapbx-max subscriptions
megapbx-max deliveries
megapbx-max callback-unknown
```

Безопасная проверка CRM Webhook должна быть `POST` с корректной авторизацией.
Тело `{` намеренно невалидно и не создаёт уведомление:

```bash
curl -i -X POST \
  'https://bot.example.com/megapbx/webhook' \
  -H "X-CRM-Token: ${MEGAPBX_CRM_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data-binary '{'
```

Команда предполагает, что `MEGAPBX_CRM_TOKEN` уже задан в окружении.
Ответ `400 Invalid webhook payload`
означает, что TLS, reverse proxy и авторизация прошли. Ответ `401` означает,
что секрет не принят. В production не отправляйте секреты в командной строке
или URL без необходимости.

События `ACCEPTED`/`COMPLETED` исходящего перезвона могут иметь отдельный
`callid`. Сервис сопоставляет их с сохранёнными событиями направления и не
пытается закрыть уведомление по чужому ID. Итоговое
`history/Success/out/missedStatus=2` закрывает исходное уведомление.

Не используйте `retry-unknown` без ручной проверки MAX: повторная отправка
может создать дубль, если внешний запрос уже был принят.

## Тестирование и качество

```bash
pytest -q
ruff check src tests
mypy src
python -m compileall -q src tests
bash -n install.sh
sha256sum -c SHA256SUMS
python -m pip wheel . --no-deps --wheel-dir dist
```

CI запускает эти проверки на Python 3.11, 3.12 и 3.13, а также `pip-audit`.
Тестовые fixtures используют только синтетические данные.

## Документация

- [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) — все параметры и значения по умолчанию;
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — HTTP-ошибки, DNS, TLS и очередь;
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — установка, Webhook, обновление,
  rollback и диагностика;
- [`docs/TESTING.md`](docs/TESTING.md) — контрактные и локальные проверки;
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — архитектурные решения;
- [`docs/MIGRATION.md`](docs/MIGRATION.md) — перенос с Telegram-бота;
- [`docs/PRIVACY.md`](docs/PRIVACY.md) — обрабатываемые данные и логирование;
- [`SECURITY.md`](SECURITY.md) — правила раскрытия уязвимостей;
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — требования к pull request.

## Ограничения

- MAX API не предоставляет универсальный способ перечислить все чаты бота;
  `MAX_CHAT_ID` нужно получить из события добавления/старта;
- Webhook и Long Polling нельзя использовать одновременно для одного бота;
- SQLite рассчитана на один экземпляр приложения на одну базу;
- MAX `POST /messages` не имеет idempotency key, поэтому неоднозначный network
  outcome требует ручной сверки;
- внешний reverse proxy должен передавать исходный body без преобразований и
  не должен писать секреты из query string в access log.

## Лицензия

MIT, см. [`LICENSE`](LICENSE).
