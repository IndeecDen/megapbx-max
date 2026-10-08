# Contributing

Спасибо за участие.

## Перед pull request

1. Создайте отдельную ветку от `main`.
2. Не добавляйте `.env`, `.venv`, `data/`, `*.sqlite3` и реальные токены.
3. Запустите:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
pytest -q
ruff check src tests
mypy src
pip-audit -r requirements.lock
python -m compileall -q src tests
bash -n install.sh
sha256sum -c SHA256SUMS
python -m pip wheel . --no-deps --wheel-dir dist
```

4. Опишите изменение и способ проверки.
5. Для webhook добавьте sanitized fixture без персональных данных.
6. Для изменений MAX-клиента сверьте контракт с актуальной [OpenAPI-схемой](https://github.com/max-messenger/api-schema).

## Manifest и приватность

После изменения release-файлов обновите их SHA-256 (пути уже перечислены в
`SHA256SUMS`):

```python
from hashlib import sha256
from pathlib import Path

manifest = Path("SHA256SUMS")
paths = [line.split(" *", 1)[1] for line in manifest.read_text().splitlines() if line]
manifest.write_bytes("".join(
    f"{sha256(Path(path).read_bytes()).hexdigest()} *{path}\n" for path in paths
).encode())
```

Используйте LF согласно `.gitattributes`; manifest проверяется также тестами.
Перед push проверьте `git diff --cached`, а не только рабочую копию.
Для поиска секретов рекомендуются `gitleaks git . --log-opts="--all" --redact`
и проверка подготовленного дерева файлов. Сканер не заменяет проверку адресов,
ID, телефонов и имён: используйте `example.com` и синтетические fixtures.
Для commit email можно включить GitHub noreply в настройках аккаунта.

## Стиль

- Python 3.11+.
- Не логировать токены, payload, телефоны и имена клиентов.
- Сохранять идемпотентность `callid` и `callback.callback_id`.
- Проверять HTTP status, `success:false` и форму успешного ответа отдельно.
- Не отправлять неоднозначный `POST /messages` повторно после read timeout.
- Сохранять Debian/Ubuntu-совместимость installer и проверять его на чистой VM перед релизом.
