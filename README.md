# vikuget

`vikuget` — узкий GET-шлюз к одному проекту Vikunja. Он не является универсальным прокси:
адрес Vikunja, его API token и ID проекта никогда не приходят от клиента.

Код шлюза живёт в этом каталоге как обычный Python-пакет `src/vikuget`. В корне репозитория
остаются compose и `.env`: там же находятся данные Vikunja и SQLite с идемпотентностью шлюза.

## Настройка и запуск

В корне репозитория создай `.env` из [`../.env.example`](../.env.example), сохранив также
переменные compose: `ACME_EMAIL`, `POSTGRES_PASSWORD`, `VIKUNJA_SERVICE_SECRET`.

```env
VIKUNJA_TOKEN=...       # отдельный API token Vikunja
VIKUNJA_PROJECT_ID=123  # единственный разрешённый проект
ACCESS_TOKEN=...        # openssl rand -hex 32
```

`ACCESS_TOKEN` должен состоять из URL-safe символов (`A-Z`, `a-z`, `0-9`, `_`, `-`) и быть не
короче 32 символов, поскольку он является одним сегментом URL. `VIKUNJA_PROJECT_VIEW_ID`
обычно не нужен: шлюз сам выберет List view проекта.

Для token Vikunja создай отдельного пользователя с доступом только к этому проекту и выдай ему
минимально нужные права на задачи и метки.

```bash
docker compose up -d --build
docker compose ps
```

SQLite находится в `./vikuget-data/` в корне репозитория. Его нужно резервировать вместе с
`db/`, `files/` и `letsencrypt/`.

## Авторизация и кэширование

Рабочий URL всегда начинается так:

```text
https://vikuget.mymaterials.ru/v1/<ACCESS_TOKEN>/...
```

Это сознательно path-token, чтобы подойти инструментам, умеющим делать только обычные GET.
У Traefik access-log отключён конкретно для роутера vikuget, а access-log Uvicorn выключен:
секрет не должен попадать в журналы. Не вставляй этот URL в публичные страницы, логи или историю
команд, которую могут читать другие.

Все изменяющие запросы требуют `request_tag`. Это произвольная непустая строка до 1024 символов,
например `1723363200:17`; после URL-кодирования она становится частью URL и отличает одну
пользовательскую интенцию от другой даже при неконтролируемом промежуточном кэшировании.

Ответы содержат `Cache-Control: no-store, no-cache, max-age=0, private`, `Pragma: no-cache` и
`Expires: 0`. Повтор с тем же `request_tag` и теми же параметрами вернёт сохранённый ответ без
повторного вызова Vikunja (`Idempotent-Replay: true`). С тем же тегом и другими параметрами будет
`409`. Если исход операции нельзя безопасно установить, повтор также вернёт `409`, а не создаст
дубликат.

## API

Во всех путях ниже `BASE` — `https://vikuget.mymaterials.ru/v1/<ACCESS_TOKEN>`. Все методы —
только `GET`; даты указываются как `YYYY-MM-DD`.

| Действие | URL и параметры |
| --- | --- |
| Список | `BASE/tasks?page=1&per_page=100` |
| Поиск | `BASE/tasks/search?q=...&page=1&per_page=100` |
| Одна задача | `BASE/tasks/{id}` |
| Создать | `BASE/tasks/create?title=...&description=...&due_date=...&request_tag=...` |
| Изменить | `BASE/tasks/{id}/update?title=...&due_date=...&request_tag=...` |
| Выполнить / открыть | `BASE/tasks/{id}/complete?request_tag=...` / `BASE/tasks/{id}/reopen?request_tag=...` |
| Удалить | `BASE/tasks/{id}/delete?request_tag=...` |
| Добавить комментарий | `BASE/tasks/{id}/comment/add?text=...&request_tag=...` |
| Добавить / снять метку | `BASE/tasks/{id}/label/add?label=...&request_tag=...` / `BASE/tasks/{id}/label/remove?label=...&request_tag=...` |

Например:

```bash
curl --get 'https://vikuget.mymaterials.ru/v1/<ACCESS_TOKEN>/tasks/create' \
  --data-urlencode 'title=Купить SSD' \
  --data-urlencode 'due_date=2026-08-14' \
  --data-urlencode 'request_tag=1723363200:17'
```

Успешный ответ всегда простой JSON:

```json
{
  "ok": true,
  "action": "task_created",
  "task": {
    "id": 42,
    "title": "Купить SSD",
    "description": "",
    "done": false,
    "due_date": "2026-08-14",
    "labels": []
  }
}
```

Перед действием с `task_id` шлюз читает задачу и проверяет её `project_id`; задача из другого
проекта выглядит как несуществующая. Метка добавляется по имени: существующая переиспользуется,
отсутствующая создаётся в Vikunja.

## Локальная проверка

```bash
cd vikuget
uv sync --group dev
uv run ruff check .
uv run pytest
```

Из корня проверяется compose:

```bash
docker compose config
```
