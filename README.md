# MegaPBX → MAX

Бот уведомляет рабочий чат **MAX** о пропущенных входящих звонках MegaPBX. Оператор может нажать **«Я наберу»**, после чего исходное уведомление помечается как закрытое.

Проект переносит функциональность [`megapbx-tg`](https://github.com/IndeecDen/megapbx-tg) на официальный [MAX Bot API](https://dev.max.ru/docs/chatbots/bots-coding/prepare), не меняя контракт webhook MegaPBX.

## Возможности

- принимает JSON, URL-encoded form и form с вложенным JSON в `POST /megapbx/webhook`;
- проверяет `X-CRM-Token` и поддерживает Bearer/Basic-аутентификацию MegaPBX;
- фильтрует направления по группе и DID;
- показывает имя/номер клиента, группу, время ожидания и длительность;
- ведёт постоянные счётчики пропущенных звонков по номеру;
- дедуплицирует MegaPBX webhook по `callid`;
- хранит состояние уведомлений и callback idempotency в SQLite;
- отправляет сообщения и inline-кнопки через `POST /messages`;
- редактирует сообщения через `PUT /messages` и `POST /answers`;
- автоматически закрывает уведомление по событиям `ACCEPTED`/`COMPLETED` и успешному перезвону;
- показывает статусы `Busy`, `Missed`, `NotAvailable`, `NotAllowed`, `NotFound` и `Cancel`;
- принимает callback через production Webhook `POST /max/webhook`; durable SQLite inbox отвечает быстро, worker обрабатывает событие с retry;
- проверяет `X-Max-Bot-Api-Secret` постоянным сравнением;
- ограничивает MAX API глобально 30 RPS и 2 операциями/с на чат;
- повторяет временные ошибки с backoff/jitter и не повторяет неоднозначный read timeout отправки;
- не записывает в application-лог raw payload, токены, телефоны или имена клиентов;
- обогащает имена сотрудников и групп через MegaPBX REST API.

## Архитектура

```text
MegaPBX ──POST /megapbx/webhook──▶ FastAPI
                                      │
                                      ├─ MegaPBX parser/auth/filters
                                      ├─ SQLite state + deduplication
                                      └─ MAX HTTP client ──▶ chat_id в MAX

MAX ──POST /max/webhook──────────▶ FastAPI
                                      └─ message_callback ──▶ POST /answers
```

MAX не публикует список чатов бота после июня 2026 года, поэтому `MAX_CHAT_ID` получают из `bot_added`/`bot_started` и фиксируют в конфигурации. См. [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Требования

- Python 3.11+;
- верифицированный профиль MAX для партнёров и опубликованный бот;
- HTTPS Webhook на публичном порту 443 с доверенным сертификатом;
- для группового чата — бот-администратор с правами `read_all_messages` и `write`.

Официального Python SDK у MAX нет. Клиент написан на `httpx` и Pydantic по официальной OpenAPI-схеме `0.0.33` от 18.09.2026.

## Локальный запуск

### PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Установите как минимум:

```powershell
$env:MAX_BOT_TOKEN="..."
$env:MAX_CHAT_ID="..."
$env:MAX_WEBHOOK_SECRET="..."
$env:MEGAPBX_CRM_TOKEN="..."
$env:MEGAPBX_ALLOWED_DID="..."
```

Приложение не читает `.env` автоматически. Передавайте переменные через окружение, `--env-file`, systemd или контейнер. После заполнения `.env` его можно запустить так:

```powershell
uvicorn --env-file .env megapbx_max.main:create_app --factory --host 127.0.0.1 --port 8000
```

Либо штатной командой:

```powershell
$env:MAX_BOT_TOKEN="..."
megapbx-max check
megapbx-max serve
```

## Проверка MAX API и Webhook

Для API-команд достаточно `MAX_BOT_TOKEN`:

```powershell
megapbx-max check
megapbx-max subscriptions
$env:MAX_WEBHOOK_SECRET="webhook_secret-1"
megapbx-max subscribe --url https://bot.example.com/max/webhook
```

Для `subscribe` можно использовать `MAX_WEBHOOK_URL` и `MAX_WEBHOOK_SECRET`. Production использует только Webhook; Long Polling:

```powershell
megapbx-max poll
```

предназначен исключительно для разработки. Получить `chat_id` из событий запуска до production-подписки:

```powershell
megapbx-max discover-chat-id
```

## Health checks

```text
GET /healthz   — процесс работает
GET /readyz    — состояние и SQLite готовы
GET /          — краткий health response
```

## Установка на Debian/Ubuntu

Installer разворачивает отдельного системного пользователя, release-каталоги, SQLite state, systemd unit и опционально Nginx/Let's Encrypt. Installer всегда требует `MAX_WEBHOOK_SECRET`, даже если TLS и Nginx обслуживаются внешним load balancer: секрет нужен для подписки MAX. На чистой Debian/Ubuntu VM должны быть доступны `bash`, `apt-get` и systemd; installer сам устанавливает `curl`, `tar`, `flock` и Python-пакеты (curl также нужен для приведённой команды загрузки):

```bash
curl -fsSL https://raw.githubusercontent.com/IndeecDen/megapbx-max/main/install.sh -o /tmp/megapbx-max-install.sh
less /tmp/megapbx-max-install.sh
sudo bash /tmp/megapbx-max-install.sh \
  --ref main \
  --with-nginx \
  --domain bot.example.com \
  --enable-tls \
  --tls-email admin@example.com
```

Для private repository используйте PAT-файл с правами `0600`:

```bash
sudo bash /tmp/megapbx-max-install.sh \
  --ref main \
  --github-token-file /root/.megapbx-github-token \
  --with-nginx --domain bot.example.com \
  --enable-tls --tls-email admin@example.com
```

В production передавайте зафиксированный release tag через `--tag <tag>` (или `--commit <sha>`) вместо `--ref main`. Установщик не копирует `.env`, `.venv`, SQLite и backup-файлы, проверяет `SHA256SUMS` release manifest, создаёт `/etc/megapbx-max.env` с правами `0600` и сохраняет state DB при rollback. Shell syntax проверяется CI (`bash -n`); Python 3.11+ должен быть доступен системе, для нестандартной версии используйте `MEGAPBX_MAX_PYTHON=/path/to/python3.11`.

## Восстановление неоднозначной отправки

Если MAX принял запрос, но ответ потерялся, запись не повторяется автоматически:

```bash
megapbx-max deliveries
megapbx-max resolve-unknown --record-id <record> --message-mid <mid>
```

Команда `retry-unknown` используется только после ручной проверки, что сообщение не было создано; она освобождает claim и сразу возвращает связанный durable job в очередь. Связь хранится и для webhook без `callid`; если job уже удалён cleanup, команда завершается ошибкой, не удаляя unknown claim. Это предотвращает дубли уведомлений.

## Конфигурация

Полный шаблон: [`.env.example`](.env.example).

Основные переменные:

| Переменная | Назначение |
|---|---|
| `MAX_BOT_TOKEN` | токен MAX Bot API |
| `MAX_CHAT_ID` | положительный ID рабочего чата/канала |
| `MAX_WEBHOOK_SECRET` | 5–256 символов для `X-Max-Bot-Api-Secret` |
| `MAX_WEBHOOK_URL` | публичный URL для `megapbx-max subscribe` |
| `MEGAPBX_CRM_TOKEN` | общий секрет входящего MegaPBX webhook |
| `MEGAPBX_ALLOWED_GROUP` | CSV разрешённых групп |
| `MEGAPBX_ALLOWED_DID` | CSV разрешённых DID |
| `MEGAPBX_ALLOW_ALL` | явное разрешение всех направлений |
| `MEGAPBX_API_BASE` | HTTPS-база MegaPBX API без `/crmapi/v1` |
| `MEGAPBX_API_TOKEN` | отдельный `X-API-KEY` для MegaPBX API |
| `STATE_DB_PATH` | путь SQLite; локально `data/state.sqlite3`, installer — `/var/lib/megapbx-max/state.sqlite3` |

Пустой allowlist допустим только при явном `MEGAPBX_ALLOW_ALL=1`; без этого параметра старт отклоняет конфигурацию, чтобы не включить неявный fail-open режим.

## Тесты

```bash
pytest -q
ruff check src tests
mypy src
python -m compileall -q src tests
bash -n install.sh
sha256sum -c SHA256SUMS
python -m pip wheel . --no-deps --wheel-dir dist
```

## Документация и план

- [поэтапный план](docs/IMPLEMENTATION_PLAN.md);
- [подготовка и эксплуатация](docs/OPERATIONS.md);
- [архитектурные решения](docs/DECISIONS.md);
- [безопасность](SECURITY.md).

## Ограничения

- `GET /chats` удалён из MAX API; ID чата сохраняется в конфигурации;
- webhook и Long Polling нельзя использовать одновременно;
- постоянное состояние хранится в одном SQLite-файле; для одной VM используйте один worker;
- внешний вызов MAX API не поддерживает idempotency key, поэтому неоднозначный network outcome может потребовать ручной сверки;
- отправка номера в `<code>` не гарантирует click-to-call на клиенте MAX;
- installer реализован, но ещё должен быть проверен на чистой Debian/Ubuntu VM; финальный smoke-test на опубликованном MAX-боте остаётся обязательным.
