# Внедрение local Telegram Bot API в TolaAI

Инструкция рассчитана на разработчика, который получает два независимых
репозитория: TolaAI и `tola-bot-tg-api`.

## Что меняется

`tola-bot-tg-api` не заменяет существующий Telegram-бот. Он добавляет локальный
официальный Bot API и очередь больших файлов:

```text
TolaAI → Delivery API → Redis → Python worker → local Telegram Bot API → Telegram
```

Все обычные обращения существующего бота также должны идти через local Bot API
после переключения. Нельзя одновременно использовать `api.telegram.org` и
локальный сервер с одним bot token.

## 1. Подготовить доступы

Понадобятся:

1. Существующий `TELEGRAM_BOT_TOKEN` из BotFather.
2. Telegram `api_id` и `api_hash`, созданные на
   <https://my.telegram.org/apps>. Это не bot token.
3. Список точных доменов, с которых worker может скачивать подписанные S3 URL.
4. Новый внутренний токен минимум 32 байта:

```bash
openssl rand -hex 32
```

Не публиковать `.env`, токены, `api_hash` и подписанные S3 URL.

## 2. Запустить delivery-стек

```bash
git clone https://github.com/AnKu304/tola-bot-tg-api.git
cd tola-bot-tg-api
cp .env.example .env
```

Заполнить `.env`:

```env
TELEGRAM_API_ID=<api_id с my.telegram.org>
TELEGRAM_API_HASH=<api_hash с my.telegram.org>
TELEGRAM_BOT_TOKEN=<существующий bot token>
APP_API_TOKEN=<результат openssl rand -hex 32>
SOURCE_ALLOWED_HOSTS=storage.yandexcloud.net,<точный домен S3 проекта>
DELIVERY_WORKER_CONCURRENCY=2
```

Собрать и запустить:

```bash
docker compose build
docker compose up -d redis telegram-bot-api api worker
docker compose ps
curl -fsS http://127.0.0.1:8080/health/live
docker compose exec api python -m app.cli check-local
```

Первичная сборка официального C++ Bot API может занять 10–20 минут. Следующие
сборки используют Docker cache.

Пока не выполнен шаг `logOut`, текущий production-бот продолжает работать через
облачный API. Не выполнять `logOut` до готовности всех изменений TolaAI.
Команда `check-local` на этом этапе может сообщить об ошибке авторизации — это
ожидаемо до переноса bot token с облачного API. Контейнеры и `/health/live`
при этом должны быть исправны.

## 3. Подключить сеть

### Если оба репозитория находятся на одном сервере

Создать в TolaAI файл `compose.telegram.override.yml`:

```yaml
services:
  backend:
    networks:
      - tolaai
      - tola-telegram
  worker:
    networks:
      - tolaai
      - tola-telegram

networks:
  tola-telegram:
    external: true
    name: tola-telegram
```

Запускать TolaAI с этим override. Внутренние адреса:

```text
http://tola-telegram-delivery:8080
http://tola-telegram-bot-api:8081
```

### Если delivery-стек расположен на отдельном VDS

Python worker, Redis и официальный Bot API оставляются вместе на delivery-VDS.
Между VDS поднимается private VPC или WireGuard. Порты 8080/8081 разрешаются в
firewall только с приватного IP сервера TolaAI.

В `compose.yml` delivery-сервиса замените публикацию с `127.0.0.1` на IP
WireGuard/private-интерфейса, например:

```yaml
ports:
  - "10.20.0.2:8080:8080"
```

и для Bot API:

```yaml
ports:
  - "10.20.0.2:8081:8081"
```

Не публиковать 8081 на `0.0.0.0`: Telegram bot token находится в URL Bot API.
Официальный сервер принимает HTTP; для подключения через публичную сеть нужен
TLS reverse proxy, но приватная сеть предпочтительнее.

## 4. Добавить совместимые настройки в TolaAI

В текущем TolaAI уже есть `TELEGRAM_API_BASE_URL`. Это корневой URL **без
`/bot`**: существующая фабрика сама добавляет суффикс. Сохраните имя и семантику
для обратной совместимости. Если записать `/bot` в `.env`, получится ошибочный
путь `/bot/bot<TOKEN>`.

```python
TELEGRAM_API_BASE_URL: str = ""  # существующее поле, root без /bot
TELEGRAM_LOCAL_MODE: bool = False
TELEGRAM_DELIVERY_URL: str = ""
TELEGRAM_DELIVERY_TOKEN: str = ""
TELEGRAM_LARGE_DELIVERY_ENABLED: bool = False
TELEGRAM_DIRECT_UPLOAD_LIMIT_BYTES: int = 50 * 1024 * 1024
```

Production `.env` при размещении на одном сервере:

```env
TELEGRAM_API_BASE_URL=http://tola-telegram-bot-api:8081
TELEGRAM_LOCAL_MODE=true
TELEGRAM_DELIVERY_URL=http://tola-telegram-delivery:8080
TELEGRAM_DELIVERY_TOKEN=<APP_API_TOKEN delivery-сервиса>
TELEGRAM_LARGE_DELIVERY_ENABLED=false
```

Для отдельного VDS использовать root `http://10.20.0.2:8081`, без `/bot`.
В local mode `getFile` может вернуть путь на delivery-VDS. В текущем TolaAI
вызовов `get_file` нет; если они появятся, чтение должно выполняться на той же
машине либо через безопасное общее хранилище.

## 5. Создать единую фабрику Telegram-клиентов

```python
from telegram import Bot
from telegram.ext import Application

from backend.core.config import Settings


def build_bot(settings: Settings) -> Bot:
    kwargs = {"token": settings.TELEGRAM_BOT_TOKEN}
    if settings.TELEGRAM_API_BASE_URL:
        root = settings.TELEGRAM_API_BASE_URL.rstrip("/")
        kwargs.update(
            base_url=f"{root}/bot",
            base_file_url=f"{root}/file/bot",
            local_mode=settings.TELEGRAM_LOCAL_MODE,
        )
    return Bot(**kwargs)


def build_application(settings: Settings) -> Application:
    builder = Application.builder().token(settings.TELEGRAM_BOT_TOKEN)
    if settings.TELEGRAM_API_BASE_URL:
        root = settings.TELEGRAM_API_BASE_URL.rstrip("/")
        builder = (
            builder.base_url(f"{root}/bot")
            .base_file_url(f"{root}/file/bot")
            .local_mode(settings.TELEGRAM_LOCAL_MODE)
        )
    return builder.updater(None).build()
```

Проверить production-вызовы и webhook/auth contract минимум в:

- `backend/bot/handlers.py`;
- `backend/services/telegram_client.py`;
- `backend/services/referral_service.py`;
- `backend/tasks/generation_tasks.py`;
- `backend/scripts/broadcast_security_notice.py`;
- `backend/scripts/refresh_miniapp_menu_buttons.py`;
- `backend/api/v1/endpoints/payment.py`;
- `backend/api/v1/endpoints/generation.py`;
- `backend/core/telegram_setup.py`, `backend/main.py`, `backend/core/auth.py`;
- `scripts/reset-telegram-webhook.sh`.

Команда `rg 'Bot\(|Application\.builder' backend` не должна находить
production-конструкторы в обход фабрики. Проверка `backend/core/auth.py` не
означает изменение валидации `initData` без отдельной причины.

## 6. Подключить очередь больших файлов

Скопировать или адаптировать `examples/tolaai_client.py` в TolaAI. Маршрут
выбирается по сохранённому `TolaFile.size_bytes` **до** `read_object_bytes`:

- известный размер меньше 50 MiB (например, 49 MiB) — допустим прямой upload,
  после cutover только через local Bot API;
- ровно 50 MiB, 51 MiB и больше — delivery queue;
- неизвестный размер legacy `result_url` — delivery queue; читать его целиком
  в память «для определения размера» запрещено.

```python
expected_size = result_file.size_bytes if result_file else None
route = choose_delivery_route(expected_size)

if route is DeliveryRoute.QUEUE:
    # Не передавать original_name без очистки: используем предсказуемое имя
    # без пути и управляющих символов.
    filename = f"tolaai-{job.id}.{'mp4' if job.media_type == 'video' else 'jpg'}"
    delivery = await delivery_client.enqueue(
        chat_id=current_user.telegram_id,
        source_url=read_url_for_object(
            result_file.s3_bucket,
            result_file.s3_key,
            expires_in=8 * 60 * 60,
        ),
        filename=filename,
        media_kind=job.media_type,
        mime_type=result_file.mime_type,
        expected_size_bytes=result_file.size_bytes,
        caption=caption,
        reply_markup=reply_markup.to_dict() if reply_markup else None,
        idempotency_key=(
            f"generation:{job.id}:recipient:{current_user.id}:telegram:v1"
        ),
    )
```

Фактические поля текущего TolaAI: `TolaFile.original_name`, `mime_type`,
`size_bytes`, `s3_bucket`, `s3_key`; у задания — `GenerationJob.id`,
`media_type`, `result_file_id`, `result_url`. `original_name` нельзя считать
безопасным basename без отдельной очистки; пример сохраняет совместимое
детерминированное имя из UUID задания и типа медиа.

Правила безопасности и совместимости:

- срок signed URL должен превышать допустимый возраст очереди и retry;
- `Idempotency-Key` обязателен; повтор с тем же payload возвращает исходный
  job, а повтор с другим payload — `409`;
- timeout enqueue безопасно повторять только с тем же ключом и payload;
- `202 queued` означает принятие, не доставку;
- для большого/неизвестного размера запрещён fallback в `BytesIO`, cloud API
  или `send_document(result_url)`;
- media→document fallback выполняет worker один раз только для известных
  ошибок формата; 408/425/429/5xx используют bounded retry;
- HTTP-контракт текущего `/generate/{job_id}/send` можно оставить `204`, чтобы
  не ломать Mini App: для queue route это означает «принято в очередь».

После `202` TolaAI обязан сохранить связь generation ↔ delivery до истечения
`DELIVERY_JOB_TTL_SECONDS`. Предпочтительный контракт — отдельная запись с
уникальностью `(generation_id, recipient_user_id, action_version)` и полями
`delivery_id`, `state`, `error_code`, `updated_at`; signed URL, token и chat ID
там не хранятся. Временный совместимый вариант без миграции — вложенный
`payload["telegram_delivery"]`, обновляемый под блокировкой строки, чтобы не
затереть параллельные изменения payload.

Фоновая reconciliation-задача опрашивает `GET /v1/deliveries/{delivery_id}` до
`sent`/`failed`, сохраняет каждый переход и прекращает опрос до TTL. Отдельный
авторизованный endpoint TolaAI возвращает Mini App только очищенные
`route/state/error_code/delivery_id`. UI для `202` показывает «поставлено в
очередь», а не «отправлено»; terminal `failed` предлагает осознанный повтор с
новой signed URL и новой revision idempotency key. Без persistence и
reconciliation large-delivery flag включать нельзя.

Flag `TELEGRAM_LARGE_DELIVERY_ENABLED=false` сохраняет старую ветку до
maintenance cutover. После `logOut` откатываться можно только на
compatibility-релиз, который направляет каждый Bot/Application к local API;
текущий `main` без такой проверки не является безопасным полным rollback.

## 7. Локальные проверки до переключения

Отдельного staging/dev-сервера нет. До production допустимы только
неразрушающие локальные проверки:

```bash
uv run ruff check .
uv run pytest -q
ENV_FILE=.env.example docker compose config --quiet
docker compose build api worker
```

Автотесты должны подтверждать 49/50/51 MiB и unknown-size routing, обязательную
идемпотентность/conflict, exact-size check, timeout/retry-after/max-attempts,
безопасный media fallback, Redis recovery и structured readiness 503. Реальные
webhook, `getMe` после cutover и отправка больших файлов остаются production
smoke-test, а не локально доказанным результатом.

## 8. Production-переключение без staging

Проводить только в maintenance window:

1. Выложить compatibility-релиз TolaAI с единой фабрикой, delivery-клиентом и
   feature flag, оставив flag выключенным и cloud URL пустым. Проверить старые
   bot-функции.
2. Поднять delivery API, Redis и local Bot API «тёмными»: проверить Compose,
   `/health/live`, Redis, диск и обезличенность логов. До `logOut` worker не
   пройдёт startup `getMe`, поэтому heartbeat и `/health/ready` в этот момент
   не являются ожидаемыми проверками.
3. Остановить все процессы TolaAI, использующие bot token.
4. Один раз выполнить
   `docker compose run --rm api python -m app.cli logout-cloud --confirm`.
5. Установить root `TELEGRAM_API_BASE_URL`, включить `TELEGRAM_LOCAL_MODE` и
   запустить local Bot API и все процессы TolaAI только с этими настройками.
6. Восстановить webhook через local server и проверить `/health/ready`,
   `/start`, Mini App, платежи, referrals, menu button, 1 MiB и 49 MiB.
7. Включить large-delivery flag и проверить 50/51 MiB через очередь.

Остановить rollout и выключить flag при нестабильном readiness, росте возраста
очереди, диске от 70%, terminal access/size errors, дубликатах или отказе
обычных bot-функций. Не переключать отдельные процессы обратно в cloud и не
выполнять `logOut` при обычном deploy/restart.

## 9. Мониторинг

Алерты нужны на readiness, heartbeat, возраст очереди относительно signed URL,
рост `failed`/429, диск 70/85%, частые рестарты и состояние Redis AOF.

Каждый delivery log содержит `stage`, `error_class`, `release` и
`correlation_id` (UUID job), а также безопасные bytes/attempt/delay. В логах
запрещены `chat_id`, bot token, signed URL/query string и персональные данные.

Отдельное ограничение end-to-end: текущий
`backend/tasks/generation_tasks.py::_archive_result` сначала держит ответ
провайдера и source preview в `response.content`. До отдельного streaming
archive → S3/temp-file patch нельзя заявлять готовность генераций размером до
2 GB, даже если Telegram delivery уже потоковый.

## 10. Остановка и откат

Для отката только маршрута сначала выключить flag, остановить постановку новых
jobs и дождаться либо контролируемо остановить очередь. Local Bot API оставить
работающим; версия TolaAI для rollback обязана быть заранее проверенным
compatibility-релизом с local root URL во всех процессах.

Полный возврат в cloud — отдельная maintenance-операция: остановить все bot
процессы и worker, удалить webhook и вызвать `close` на local server по
официальной инструкции, одновременно убрать local root URL во всех процессах,
затем восстановить webhook и проверить updates. До подтверждения cloud updates
local стек не останавливать. Автоматического cloud-fallback после `logOut` нет.
