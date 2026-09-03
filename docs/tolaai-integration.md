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

Keep the existing TolaAI `TELEGRAM_API_BASE_URL` setting backward compatible:
it is a server root **without** `/bot`; the existing factory appends `/bot`.
Putting `/bot` in the environment creates an invalid doubled path.

```env
TELEGRAM_API_BASE_URL=http://tola-telegram-bot-api:8081
TELEGRAM_LOCAL_MODE=true
TELEGRAM_DELIVERY_URL=http://tola-telegram-delivery:8080
TELEGRAM_DELIVERY_TOKEN=<same value as APP_API_TOKEN in this service>
TELEGRAM_LARGE_DELIVERY_ENABLED=false
```

The shared factory must set `base_url=<root>/bot`,
`base_file_url=<root>/file/bot`, and `local_mode=true` for both `Bot` and
`Application`. Inventory all direct constructors and webhook/auth call sites,
including `backend/core/telegram_setup.py`, `backend/main.py`,
`backend/core/auth.py`, and `scripts/reset-telegram-webhook.sh`, in addition to
the bot, generation, payment, referral, broadcast, and menu-refresh modules.
After `logOut`, no process may use the same token against the cloud endpoint.

## 3. Choose the route before reading S3

TolaAI currently reads the complete object in the generation endpoint. Use
stored metadata before that call:

- below 50 MiB, retain direct upload only through the configured local server;
- at 50 MiB and above, enqueue this service;
- if size is unknown, enqueue this service and rely on its streamed hard limit.

The tested reference is `choose_delivery_route()` in
`examples/tolaai_client.py`. A queued `TolaFile` provides `mime_type`,
`size_bytes`, `s3_bucket`, and `s3_key`. Treat `original_name` as untrusted;
use a deterministic basename unless it has been sanitized separately.

```python
filename = f"tolaai-{job.id}.{'mp4' if job.media_type == 'video' else 'jpg'}"
delivery = await client.enqueue(
    chat_id=user.telegram_id,
    source_url=read_url_for_object(
        result_file.s3_bucket,
        result_file.s3_key,
        expires_in=8 * 60 * 60,
    ),
    filename=filename,
    media_kind=job.media_type,
    mime_type=result_file.mime_type,
    expected_size_bytes=result_file.size_bytes,
    caption="Генерация готова",
    reply_markup=reply_markup.to_dict() if reply_markup else None,
    idempotency_key=f"generation:{job.id}:recipient:{user.id}:telegram:v1",
)
```

`Idempotency-Key` is required. Repeating the same key and payload returns the
same job; a different payload returns `409`. A timeout may be retried only with
that same key and payload. A `202` means queued, not sent. For large or unknown
sizes, never fall back to `BytesIO`, cloud upload, or a signed URL passed to
Telegram. The delivery worker performs one document fallback only for known
media-format errors; transient errors use bounded retries.

Persist the generation-to-delivery mapping until `DELIVERY_JOB_TTL_SECONDS`.
Prefer a dedicated row unique on generation, recipient, and action revision,
containing only delivery ID, state, stable error code, and timestamps. A
temporary no-migration option is `payload["telegram_delivery"]`, updated with
row locking so concurrent payload writes are not lost. Never persist the
signed URL, token, or chat ID in that metadata. A background reconciliation
task must poll `GET /v1/deliveries/{id}` through `sent` or `failed`, and an
authenticated TolaAI endpoint should expose only sanitized delivery status to
the Mini App. Show `202` as queued, not sent. Do not enable the feature flag
without persistence and reconciliation.

## 4. Production-only rollout

There is no separate staging server. First deploy a compatibility TolaAI
release with the shared factory, delivery client, and a disabled feature flag,
while it still uses the cloud. Start the delivery API, Redis, and local Bot API
dark and verify local tests, Compose, liveness, Redis, disk, and sanitized logs.
The worker cannot pass its startup `getMe`, publish a heartbeat, or make
readiness healthy before the one-time `logOut`; do not claim those checks at
this stage. In a maintenance window stop every bot-token process, execute
`logOut`, switch all processes to the local root together, start the worker,
require heartbeat/readiness, restore the webhook, and smoke-test normal bot
functions plus 1/49 MiB. Only then enable queued delivery and test 50/51 MiB.

Stop rollout on unstable readiness, growing queue age, disk at 70%, increasing
terminal access/size errors, duplicates, or any failed normal bot function.

## 5. Required evidence

Local automated tests cover 49/50/51 MiB and unknown routing, idempotency and
conflict, exact downloaded size, timeout/retry-after/max attempts, safe media
fallback, atomic Redis recovery, and structured readiness failure. Real
webhook, post-cutover `getMe`, and large Telegram sends remain production smoke
tests and must not be reported as locally verified.

End-to-end 2 GB readiness is also blocked by TolaAI's current result archive:
`backend/tasks/generation_tasks.py::_archive_result` retains the provider body
and preview source in `response.content`. Address that in a separate streaming
archive-to-S3/temp-file change before claiming 2 GB generation support.

## 6. Stop and rollback

To stop only queued routing, disable the feature flag, stop new enqueue calls,
and drain or deliberately stop the queue. Keep the local Bot API running and
roll back only to the prevalidated compatibility release that routes every bot
client locally.

A full cloud return is a separate maintenance operation: stop all bot processes
and the worker, delete the webhook and call local `close` per Telegram guidance,
switch every process to cloud URLs together, restore the webhook, and verify
updates before stopping the local stack. There is no automatic cloud fallback
after `logOut`.
