# Миграция с `megapbx-tg`

## Что переносится

Без изменения остаются:

- endpoint и формат MegaPBX webhook;
- события `history/Missed`, `event`, успешный/неуспешный перезвон;
- фильтры group/DID;
- нормализация телефона и fallback-поиск;
- enrichment `/crmapi/v1/users` и `/crmapi/v1/telnums`;
- тексты, статусы и HTML-экранирование.

Заменяются:

| Telegram | MAX |
|---|---|
| `TG_BOT_TOKEN` | `MAX_BOT_TOKEN` |
| `TG_CHAT_ID` | `MAX_CHAT_ID` |
| long polling `getUpdates` | production `/max/webhook` |
| `callback_query.id` | `callback.callback_id` |
| `callback_query.data` | `callback.payload` |
| числовой `message_id` | строковый `message.body.mid` |
| `sendMessage` | `POST /messages` |
| `editMessageText` | `PUT /messages` / `POST /answers` |
| `answerCallbackQuery` | notification в `POST /answers` |

## Что улучшено

- SQLite переживает рестарт;
- `callid` дедуплицируется на диске;
- `callback_id` дедуплицируется на диске;
- запись закрывается атомарным claim;
- failed edit не меняет сохранённый текст;
- счётчики имеют pending/success reservations и корректный rollback;
- MAX rate limits enforced;
- HTTP 200 с `success:false` не считается успехом;
- неоднозначный outcome не ретраится как гарантированный duplicate.

## Порядок cutover

1. Создать и опубликовать бота в MAX.
2. Разрешить добавление бота в группы.
3. Создать/выбрать рабочий MAX-чат и назначить бота администратором.
4. Получить `chat_id` и подготовить отдельные секреты.
5. Развернуть `megapbx-max`, TLS и SQLite.
6. Выполнить `megapbx-max check` и `megapbx-max subscribe`.
7. Прогнать ручную матрицу [`TESTING.md`](TESTING.md).
8. Переключить URL webhook в MegaPBX на новый HTTPS endpoint.
9. Наблюдать журналы и ошибки до cutover Telegram.
10. Оставить Telegram-конфигурацию только как согласованный rollback.

`MEGAPBX_API_BASE` теперь должен быть полным URL с `https://` и без `/crmapi/v1`. Для доверенного HTTP-LAN требуется явный `MEGAPBX_ALLOW_HTTP_API=1`.

Нельзя одновременно отправлять один и тот же поток звонков в оба мессенджера, если это не предусмотрено бизнес-процессом.

## Секреты

Не копируйте `.env` из `megapbx-tg`. В исходном локальном окружении были обнаружены непустые credentials; их нельзя считать безопасными только на основании `.gitignore`.

Если исходный каталог, архив или CI-лог когда-либо передавались третьим лицам, ротируйте:

- Telegram bot token через BotFather;
- MAX bot token после создания нового токена MAX;
- MegaPBX CRM token;
- MegaPBX API token.

Для MAX используйте отдельные `MAX_BOT_TOKEN`, `MAX_WEBHOOK_SECRET`, `MEGAPBX_CRM_TOKEN` и `MEGAPBX_API_TOKEN`, даже если старые значения совпадали.
