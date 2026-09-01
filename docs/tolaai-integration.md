# TolaAI integration

This document is deliberately written as a hand-off checklist. The delivery
service is a separate deployment and does not require copying its source into
TolaAI.

## 1. Join the shared Docker network

Start this repository first so Docker creates `tola-telegram`. Attach the
TolaAI `backend`, `worker`, and any other process which instantiates a Telegram
bot to that external network:

```yaml
# compose.telegram.override.yml in TolaAI
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

The stable service aliases on that network are:

- delivery queue: `http://tola-telegram-delivery:8080`;
- local Bot API: `http://tola-telegram-bot-api:8081`.

## 2. Route every Bot instance through the local server

Add settings equivalent to:

```env
TELEGRAM_BOT_API_BASE_URL=http://tola-telegram-bot-api:8081/bot
TELEGRAM_BOT_API_FILE_BASE_URL=http://tola-telegram-bot-api:8081/file/bot
TELEGRAM_BOT_LOCAL_MODE=true
TELEGRAM_DELIVERY_URL=http://tola-telegram-delivery:8080
TELEGRAM_DELIVERY_TOKEN=<same value as APP_API_TOKEN in this service>
```

Create one factory and use it everywhere:

```python
from telegram import Bot
from telegram.ext import Application


def bot_kwargs(settings) -> dict:
    return {
        "token": settings.telegram_bot_token,
        "base_url": settings.telegram_bot_api_base_url,
        "base_file_url": settings.telegram_bot_api_file_base_url,
        "local_mode": settings.telegram_bot_local_mode,
    }


def build_bot(settings) -> Bot:
    return Bot(**bot_kwargs(settings))


def build_application(settings) -> Application:
    return (
        Application.builder()
        .token(settings.telegram_bot_token)
        .base_url(settings.telegram_bot_api_base_url)
        .base_file_url(settings.telegram_bot_api_file_base_url)
        .local_mode(settings.telegram_bot_local_mode)
        .updater(None)
        .build()
    )
```

During the audit, direct bot construction was present in these TolaAI files and
must be replaced by that factory:

- `backend/bot/handlers.py`;
- `backend/services/referral_service.py`;
- `backend/tasks/generation_tasks.py`;
- `backend/scripts/broadcast_security_notice.py`;
- `backend/scripts/refresh_miniapp_menu_buttons.py`;
- `backend/api/v1/endpoints/payment.py`;
- `backend/api/v1/endpoints/generation.py`.

This is mandatory after calling `logOut`: one token must not be used against
the cloud and local Bot API servers at the same time.

## 3. Enqueue generated files instead of loading them into RAM

TolaAI currently reads the complete S3 object in the generation endpoint.
Replace only the Telegram delivery branch with a request to this service. Keep
the browser download/presigned URL behaviour unchanged.

Use the existing `read_url_for_object(...)` helper to create a signed URL that
remains valid for the queue and retries. Six hours is a reasonable starting
TTL. Pass the stored `TolaFile.size_bytes` as `expected_size_bytes`.

```python
from examples.tolaai_client import TolaTelegramDeliveryClient

client = TolaTelegramDeliveryClient(
    base_url=settings.telegram_delivery_url,
    token=settings.telegram_delivery_token,
)

delivery = await client.enqueue(
    chat_id=user.telegram_id,
    source_url=read_url_for_object(bucket, object_key, expires_in=6 * 60 * 60),
    filename=tola_file.filename,
    media_kind="video",
    mime_type=tola_file.mime_type,
    expected_size_bytes=tola_file.size_bytes,
    caption="Генерация готова",
    idempotency_key=f"generation:{generation.id}:telegram:v1",
)
```

Store `delivery["id"]` on the generation/job record if the UI needs delivery
progress. A `202 queued` response means the file was accepted, not delivered.
Poll `GET /v1/deliveries/{id}` or add a small background reconciliation task.

The idempotency key must be stable for the same generation and recipient. This
prevents a browser retry or task retry from sending the same result twice.

## 4. Deployment order

1. Configure credentials and trusted S3 hosts in this service.
2. Build and start the stack; verify Redis, worker heartbeat, and local `getMe`.
3. Attach a staging TolaAI deployment to `tola-telegram` and switch all bot
   constructors to the factory.
4. In a maintenance window, execute the one-time cloud `logOut` command.
5. Restart all TolaAI bot-using processes and run the smoke tests below.
6. Enable large-file delivery for a small cohort, then ramp to all users.

## 5. Required smoke tests

- Existing webhook/update handling still works through the local server.
- Bot menu refresh, payment notifications, referrals, and generation tasks all
  use the local URL (verify outbound traffic or logs).
- Send a 1 MB document, a 49 MB video, a 51 MB video, and a file larger than
  the old application threshold.
- Invalid media falls back to a document once, without duplicate messages.
- Repeating the same idempotency key returns the same delivery id.
- Stop the Bot API server during a send; the job becomes `retry_scheduled` and
  later reaches `sent`.
- Expired S3 URLs fail with a useful terminal status and do not expose the
  signed URL in the API response.

## 6. Rollback constraint

Do not point some TolaAI processes back to `api.telegram.org` while others use
the local server. If the local service must be rolled back, stop all bot
processes, follow Telegram's cloud re-login requirements, switch the base URLs
as one change, and restart them together.
