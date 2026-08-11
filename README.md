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
ALLOWED_IPS=203.0.113.25,198.51.100.0/24
```

`ALLOWED_IPS` — либо ровно `*`, чтобы разрешить доступ отовсюду, либо непустой список IP-адресов
и CIDR через запятую. Например: `203.0.113.25,198.51.100.0/24,2001:db8:1234::/48`.
`VIKUNJA_PROJECT_VIEW_ID` обычно не нужен: шлюз сам выберет List view проекта.

Для token Vikunja создай отдельного пользователя с доступом только к этому проекту и выдай ему
минимально нужные права на задачи и метки.

```bash
docker compose up -d --build
docker compose ps
```

SQLite находится в `./vikuget-data/` в корне репозитория. Его нужно резервировать вместе с
`db/`, `files/` и `letsencrypt/`.

## Доступ по IP, журналирование и кэширование

Рабочий URL всегда начинается так:

```text
https://vikuget.mymaterials.ru/<REQUEST_TAG>/...
```

Для совместимости также принимается старый версионный префикс
`https://vikuget.mymaterials.ru/v1/<REQUEST_TAG>/...`: он обрабатывается внутри шлюза без
redirect и возвращает тот же HTTP `200`. Token в этом fallback не используется.

Доступ определяется `ALLOWED_IPS`, а не URL-token. Контейнер vikuget находится в отдельной
внутренней Docker-сети, доступной только Traefik; Traefik добавляет адрес подключившегося клиента
в `X-Forwarded-For`. Шлюз берёт правый адрес из этого заголовка, сверяет его с allowlist и пишет
в лог каждое завершённое внешнее обращение — без URL и query-параметров:

```text
vikuget response client_ip=203.0.113.25 method=GET status=200
```

Смотреть журнал можно так:

```bash
docker compose logs -f vikuget
```

Traefik access-log для этого роутера остаётся выключенным. Эта конфигурация рассчитана на прямое
подключение клиента к Traefik. Если перед сервером есть другой прокси или CDN, сначала настрой
доверенную цепочку forwarded-заголовков у обоих компонентов; не включай
`forwardedHeaders.insecure` в production.

`REQUEST_TAG` обязателен в любом запросе. Это произвольная непустая строка до 1024 символов,
например `1723363200:17`; после URL-кодирования она идёт сразу после домена и отличает одну
пользовательскую интенцию от другой даже при неконтролируемом промежуточном кэшировании.

Ответы содержат `Cache-Control: no-store, no-cache, max-age=0, private`, `Pragma: no-cache` и
`Expires: 0`. Для изменяющих операций повтор с тем же tag и теми же параметрами вернёт сохранённый
ответ без повторного вызова Vikunja (`Idempotent-Replay: true`). С тем же tag и другими параметрами
вернётся ошибочная HTML-страница с кодом `request_tag_reused`. Если исход операции нельзя
безопасно установить, повтор вернёт `request_in_progress`, а не создаст дубликат.

## API

Во всех путях ниже `BASE` —
`https://vikuget.mymaterials.ru/<REQUEST_TAG>`. Все методы — только `GET`;
даты указываются как `YYYY-MM-DD`.

| Действие | URL и параметры |
| --- | --- |
| Список | `BASE/tasks?page=1&per_page=100` |
| Поиск | `BASE/tasks/search?q=...&page=1&per_page=100` |
| Одна задача | `BASE/tasks/{id}` |
| Создать | `BASE/tasks/create?title=...&description=...&due_date=...` |
| Изменить | `BASE/tasks/{id}/update?title=...&due_date=...` |
| Выполнить / открыть | `BASE/tasks/{id}/complete` / `BASE/tasks/{id}/reopen` |
| Удалить | `BASE/tasks/{id}/delete` |
| Добавить комментарий | `BASE/tasks/{id}/comment/add?text=...` |
| Добавить / снять метку | `BASE/tasks/{id}/label/add?label=...` / `BASE/tasks/{id}/label/remove?label=...` |

Например:

```bash
curl --get 'https://vikuget.mymaterials.ru/1723363200:17/tasks/create' \
  --data-urlencode 'title=Купить SSD' \
  --data-urlencode 'due_date=2026-08-14'
```

Каждый ответ — простая HTML-страница без CSS и JavaScript. На ней обычными заголовками, списками
и списками определений показаны результат действия, поля задачи, комментарий, метки и навигация.
Например, после создания задачи ответ выглядит так:

```html
<!doctype html>
<html lang="ru">
  <head><meta charset="utf-8"><title>Задача создана</title></head>
  <body><main>
    <h1>Задача создана</h1>
    <section><h2>Задача</h2>
      <dl>
        <dt>ID</dt><dd>42</dd>
        <dt>Название</dt><dd>Купить SSD</dd>
        <dt>Описание</dt><dd>—</dd>
        <dt>Выполнена</dt><dd>Нет</dd>
        <dt>Срок</dt><dd>2026-08-14</dd>
      </dl>
      <h3>Метки</h3><p>Нет</p>
    </section>
  </main></body>
</html>
```

Страница ошибки содержит заголовок, понятное сообщение и машинный код ошибки. HTTP-статус всегда
`200`.

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
