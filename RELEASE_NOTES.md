# Release notes — megapbx-max-v0.1.0 (draft)

**Дата:** ещё не назначена

**Статус:** draft; локальные проверки завершены, требуется production smoke-test MAX.

## Что входит

- уведомления о пропущенных звонках MegaPBX в рабочий чат MAX;
- фильтры group/DID и обогащение MegaPBX;
- SQLite-состояние и дедупликация;
- callback «Я наберу»;
- автоматическое закрытие и статусы перезвонка;
- production MAX Webhook и development Long Polling;
- health checks, CLI и тесты.

## До выпуска

- создать и опубликовать бота MAX;
- получить `MAX_CHAT_ID`;
- проверить права администратора в рабочем чате;
- развернуть HTTPS на 443 с доверенным сертификатом;
- выполнить `megapbx-max check` и `megapbx-max subscribe`;
- прогнать sanitized и реальный MegaPBX webhook;
- проверить серию из трёх звонков, callback, duplicate delivery и `429/500/503`;
- подготовить installer и release tag.

Локально уже проверены: `104 passed`, Ruff, mypy, `compileall`, shell syntax,
`sha256sum -c SHA256SUMS`, wheel build/install на Python 3.11 и uvicorn smoke
с legacy SQLite schema. Остаются только production/VM checks, commit и push
после согласования.
