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

## 4. Добавить настройки в TolaAI

В `backend/core/config.py`:

```python
TELEGRAM_BOT_API_BASE_URL: str = "https://api.telegram.org/bot"
TELEGRAM_BOT_API_FILE_BASE_URL: str = "https://api.telegram.org/file/bot"
TELEGRAM_BOT_LOCAL_MODE: bool = False
TELEGRAM_DELIVERY_URL: str = ""
TELEGRAM_DELIVERY_TOKEN: str = ""
```

Production `.env` при размещении на одном сервере:

```env
TELEGRAM_BOT_API_BASE_URL=http://tola-telegram-bot-api:8081/bot
TELEGRAM_BOT_API_FILE_BASE_URL=http://tola-telegram-bot-api:8081/file/bot
TELEGRAM_BOT_LOCAL_MODE=true
TELEGRAM_DELIVERY_URL=http://tola-telegram-delivery:8080
TELEGRAM_DELIVERY_TOKEN=<APP_API_TOKEN delivery-сервиса>
```

Для отдельного VDS заменить hostnames на приватный IP, например
`http://10.20.0.2:8081/bot` и `http://10.20.0.2:8080`.

В local mode метод Telegram `getFile` может вернуть абсолютный путь на диске
delivery-VDS. В текущем TolaAI вызовов `get_file` нет. Если они появятся,
backend на другом VDS не сможет открыть такой путь напрямую: чтение нужно
выполнять на delivery-VDS либо дать обеим машинам безопасное общее хранилище.

## 5. Создать единую фабрику Telegram-клиентов

Например, `backend/services/telegram_factory.py`:

```python
from telegram import Bot
from telegram.ext import Application

from backend.core.config import Settings


def build_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.TELEGRAM_BOT_TOKEN,
        base_url=settings.TELEGRAM_BOT_API_BASE_URL,
        base_file_url=settings.TELEGRAM_BOT_API_FILE_BASE_URL,
        local_mode=settings.TELEGRAM_BOT_LOCAL_MODE,
    )


def build_application(settings: Settings) -> Application:
    return (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .base_url(settings.TELEGRAM_BOT_API_BASE_URL)
        .base_file_url(settings.TELEGRAM_BOT_API_FILE_BASE_URL)
        .local_mode(settings.TELEGRAM_BOT_LOCAL_MODE)
        .updater(None)
        .build()
    )
```

Заменить прямые вызовы `Bot(...)`/`Application.builder()` минимум в:

- `backend/bot/handlers.py`;
- `backend/services/referral_service.py`;
- `backend/tasks/generation_tasks.py`;
- `backend/scripts/broadcast_security_notice.py`;
- `backend/scripts/refresh_miniapp_menu_buttons.py`;
- `backend/api/v1/endpoints/payment.py`;
- `backend/api/v1/endpoints/generation.py`.

Проверить ещё раз командой:

```bash
rg 'Bot\(|Application\.builder' backend
```

Все найденные production-вызовы должны использовать фабрику.

## 6. Подключить очередь больших файлов

Скопировать или адаптировать `examples/tolaai_client.py` в backend TolaAI.

Сейчас `backend/api/v1/endpoints/generation.py` читает результат из S3 целиком
в память и передаёт `BytesIO` в Telegram. Заменить эту ветку на enqueue:

```python
delivery = await delivery_client.enqueue(
    chat_id=current_user.telegram_id,
    source_url=read_url_for_object(
        bucket,
        object_key,
        expires_in=6 * 60 * 60,
    ),
    filename=tola_file.filename,
    media_kind="video",
    mime_type=tola_file.mime_type,
    expected_size_bytes=tola_file.size_bytes,
    caption=caption,
    reply_markup=reply_markup.to_dict() if reply_markup else None,
    idempotency_key=f"generation:{generation.id}:telegram:v1",
)
```

Точные имена моделей/полей разработчик должен сопоставить с текущей версией
TolaAI. Важные правила:

- не загружать S3-объект в `bytes`/`BytesIO`;
- подписанная ссылка должна жить дольше максимального времени очереди и retry;
- передавать `TolaFile.size_bytes`;
- idempotency key должен быть стабильным для generation + получатель;
- ответ `202 queued` означает принятие в очередь, а не успешную доставку;
- при необходимости сохранять `delivery["id"]` и опрашивать
  `GET /v1/deliveries/{id}`.

Для файлов меньше 50 MB тоже лучше использовать единый путь доставки: меньше
разветвлений и одинаковая диагностика. Если старый путь временно оставляется,
он всё равно обязан использовать local Bot API после переключения.

## 7. Проверить всё до переключения

```bash
docker compose ps
curl -fsS http://127.0.0.1:8080/health/live
docker compose logs --tail=100 api worker telegram-bot-api redis
```

На staging проверить:

1. `/start` и запуск Mini App.
2. Webhook/update handling.
3. Платёжные уведомления.
4. Реферальные уведомления.
5. Обновление menu button.
6. Документ 1 MB.
7. Видео 49 MB.
8. Видео 51 MB.
9. Репрезентативный большой файл.
10. Повтор одного `Idempotency-Key` не создаёт второе задание.
11. Остановка Bot API во время отправки приводит к retry, затем к `sent`.

## 8. Production-переключение

Проводить в maintenance window.

1. Развернуть новую версию TolaAI, но не запускать одновременно старые
   процессы, обращающиеся к облачному API.
2. Остановить backend/worker/скрипты бота старой версии.
3. Один раз выполнить:

```bash
docker compose run --rm api python -m app.cli logout-cloud --confirm
```

4. Запустить local Bot API, delivery API/worker и новую версию TolaAI.
5. Восстановить/проверить webhook через локальный сервер.
6. Проверить `/health/ready`:

```bash
curl -fsS http://127.0.0.1:8080/health/ready
```

7. Выполнить smoke-тест маленького и файла больше 50 MB.

`logOut` нельзя выполнять при каждом deploy.

## 9. Мониторинг

Алерты нужны на:

- `/health/ready` не отвечает 200;
- worker heartbeat отсутствует;
- очередь старше срока жизни подписанного S3 URL;
- рост `failed` или Telegram `429`;
- диск заполнен более чем на 70/85%;
- частые перезапуски worker/local Bot API;
- Redis AOF или PostgreSQL не резервируются.

Логи не должны содержать bot token и query string подписанных S3 URL.

## 10. Откат

Нельзя оставить часть процессов на локальном сервере, а часть вернуть на
`api.telegram.org`. Откат выполняется одной операцией:

1. остановить все процессы бота;
2. удалить webhook и вызвать `close` на локальном сервере согласно официальной
   инструкции Telegram;
3. вернуть cloud base URLs во всех процессах;
4. запустить TolaAI и восстановить webhook;
5. проверить получение updates до остановки local Bot API.

Для отката только версии приложения local Bot API можно оставить работающим —
предыдущая версия TolaAI должна быть способна использовать его base URLs.
