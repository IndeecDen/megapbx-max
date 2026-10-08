# Тестирование и приёмка

## Автоматические проверки

```bash
python -m pip install -e ".[dev]"
pytest -q
ruff check src tests
mypy src
python -m compileall -q src tests
bash -n install.sh
sha256sum -c SHA256SUMS
python -m pip wheel . --no-deps --wheel-dir dist
```

## Локальная проверка release

Последний локальный прогон на Windows/Python 3.12: `125 passed`; также зелёные
`ruff`, `mypy`, `compileall`, `bash -n install.sh` и `sha256sum -c SHA256SUMS`.
Собранный wheel установлен в чистое Python 3.11.16 environment, `pip check` не
обнаружил конфликтов. Запуск настоящего uvicorn-процесса проверил `/readyz`,
durable enqueue и миграцию legacy SQLite schema.

Единственное предупреждение тестового прогона — upstream `StarletteDeprecationWarning`
в `fastapi.testclient` о переходе на `httpx2`; production-код и HTTP-клиент
по-прежнему используют поддерживаемый `httpx`. На Debian отдельно проверены
установка/обновление и реальные уведомления опубликованного MAX-бота:
ручное закрытие, успешный перезвон и завершение очереди. Это не означает
ручного воспроизведения всех отказных сценариев в production.

Текущий набор покрывает:

- MAX `POST /messages`, `PUT /messages`, `POST /answers`;
- HTTP 200 с `success:false`;
- transient 5xx и non-JSON retry;
- отсутствие retry неоднозначного read timeout;
- webhook secret и ограничение body;
- JSON/form/nested form MegaPBX;
- HTML-экранирование и безопасные логи;
- exact/group/DID фильтры;
- enrichment `/users` и `/telnums`, включая пагинацию и IVR;
- concurrent duplicate `callid`;
- rollback счётчика при ошибке отправки;
- SQLite-счётчики, dedup TTL, record claims и callback idempotency;
- `OUTGOING`, replay старого `callid`, unknown `callid`;
- ручное закрытие через MAX callback;
- callback из чужого чата;
- статусы неудачного перезвона;
- health/readiness и CLI subscription;
- миграция промежуточной схемы SQLite и запуск durable worker в uvicorn;
- `success:false`, HTTP 408 и неоднозначные outcomes нового сообщения;
- URL с query/fragment/credentials и fail-closed пустого webhook secret;
- повторное открытие missed-job после dedup TTL;
- неоднозначный malformed 2xx `/answers` и ручное восстановление claim без `callid`;
- installer ShellCheck и проверка конфликта Nginx `server_name`.
- Online Backup API SQLite с committed WAL и удалением неполной копии при ошибке;
- исходящие `ACCEPTED`/`COMPLETED`, поздняя история направления и перезапуск;
- отрицательные signed int64 ID чатов;
- подавление стандартных HTTP-логов даже при CLI DEBUG.

## Локальный MegaPBX webhook

Сначала запустите приложение с тестовыми значениями. Затем:

```bash
curl -i http://127.0.0.1:8000/megapbx/webhook \
  -H 'X-CRM-Token: test-crm-secret' \
  -H 'Content-Type: application/json' \
  --data '{"cmd":"history","status":"Missed","callid":"test-call-1","phone":"+70000000000","telnum":"100","groupRealName":"Support","contact_name":"Test Client","wait":5,"duration":0}'
```

Ожидается HTTP 200:

```json
{"ok":true,"duplicate":false}
```

Повтор запроса с тем же `callid`:

```json
{"ok":true,"duplicate":true}
```

Не используйте реальные номера, имена или токены в тестовых payload.

## MAX setup

1. `megapbx-max check` — токен должен вернуть data бота.
2. `megapbx-max discover-chat-id` — получить ID development-чата.
3. Записать `MAX_CHAT_ID` и `MAX_WEBHOOK_SECRET`.
4. Развернуть HTTPS endpoint на 443.
5. `megapbx-max subscribe`.
6. `megapbx-max subscriptions` — URL и типы событий присутствуют.

Если существующая подписка обновляется, не удаляйте её перед повторным `POST /subscriptions`.

## Ручной smoke-test

| Сценарий | Ожидание |
|---|---|
| `history/Missed` | одно уведомление с именем/номером и кнопкой |
| тот же `callid` дважды | одно уведомление, webhook duplicate |
| три звонка подряд | сообщения идут с лимитом не быстрее 2/с на чат |
| `event/ACCEPTED` с ID уведомления | уведомление меняет кнопку на «Перезвонил …» |
| `event/COMPLETED` с ID уведомления | то же |
| `event/OUTGOING` | уведомление остаётся открытым |
| `ACCEPTED`/`COMPLETED` известного исходящего звонка | задание завершается без редактирования |
| `history/out/success/missedStatus=2` | авто-closing по телефону |
| `history/out/Busy/missedStatus=2` | строка `↩️ …: ☎️ Занято` |
| нажать «Я наберу» | `POST /answers`, запись закрыта один раз |
| повторное нажать | «Уже отмечено…», нового закрытия нет |
| callback из другого чата | «Это уведомление недоступно» |
| `bot_added` | безопасный лог содержит `chat_id` |
| webhook без secret | HTTP 401 |
| MegaPBX без CRM token | HTTP 401 |
| неверный MAX token | readiness приложения не ломается, API-команда завершается ошибкой |
| повтор MAX webhook | callback не применяется дважды |

## Проверки отказоустойчивости

Contract/mock-тесты должны оставаться зелёными при:

- HTTP 401/403/429/500/503 от MAX;
- `success:false` при HTTP 200;
- таймауте;
- non-JSON temporary response;
- отсутствии `message.body.mid`;
- недоступной SQLite;
- потере `Content-Length`;
- body больше лимита;
- неверном callback JSON;
- отсутствии `callback.message`;
- повторной доставке того же `callback_id`.

## Приёмочный критерий релиза

- все автоматические проверки зелёные на Python 3.11, 3.12 и 3.13;
- installer проходит на чистой Debian/Ubuntu VM;
- `systemctl is-active megapbx-max` и `/readyz` успешны;
- Nginx/TLS соответствует требованиям MAX;
- подписка MAX активна после рестарта;
- ручная матрица выше пройдена;
- application-лог не содержит токены, payload, телефоны и имена клиентов;
- rollback installer не удаляет state DB без явного подтверждения.
