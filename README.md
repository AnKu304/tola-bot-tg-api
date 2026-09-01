# tola-bot-tg-api

Large-file Telegram delivery for TolaAI, built around the **official**
[`tdlib/telegram-bot-api`](https://github.com/tdlib/telegram-bot-api) server in
`--local` mode.

The official cloud Bot API accepts uploads up to 50 MB. A local Bot API server
accepts uploads up to 2000 MB. This repository adds a small authenticated queue
in front of that server so TolaAI can enqueue a private S3 result, return to the
Mini App immediately, and observe delivery status without holding a web request
open or loading the whole file into RAM.

## Components

- `telegram-bot-api`: official upstream source, pinned to a reviewed commit and
  compiled locally with `--local` enabled.
- `api`: authenticated FastAPI service for enqueueing and reading delivery jobs.
- `worker`: streams signed S3 URLs to a temporary shared volume and asks the
  local Bot API server to upload the local file.
- `redis`: persistent pending/processing queues, status store, idempotency keys,
  delayed retries, and recovery of unacknowledged work after worker restarts.

No bot token, Telegram API credentials, S3 credentials, or generated media are
stored in this repository.

## Delivery flow

```text
TolaAI backend
    │ POST /v1/deliveries (signed S3 URL, expected size, chat_id)
    ▼
Delivery API ──► Redis queue ──► Worker
                                  │ streaming download
                                  ▼
                           shared temporary file
                                  │ file:/// absolute path
                                  ▼
                     official telegram-bot-api --local
                                  │ upload up to 2000 MB
                                  ▼
                              Telegram chat
```

The temporary file is deleted after success or failure. Telegram stores the
sent file; the local copy is not required after the send finishes.

## Quick start

Requirements:

- Docker Engine with Compose v2;
- at least 4 GB RAM to build the official C++ Bot API server;
- `api_id` and `api_hash` from <https://my.telegram.org/apps>;
- the existing TolaAI BotFather token.

```bash
cp .env.example .env
openssl rand -hex 32
# Fill TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN,
# APP_API_TOKEN, and SOURCE_ALLOWED_HOSTS in .env.

docker compose build
docker compose up -d redis telegram-bot-api api worker
docker compose exec api python -m app.cli check-local
```

The first C++ build is intentionally slow; later builds use Docker cache.

## Mandatory cloud-to-local cutover

Telegram requires the bot to be logged out from `api.telegram.org` before all
requests are redirected to a local server. Do this once, in a maintenance
window, only after the local stack and TolaAI configuration are ready:

```bash
docker compose run --rm api python -m app.cli logout-cloud --confirm
```

After `logOut`, **every** TolaAI `Bot` and `Application` instance must use the
local base URLs. Do not split one bot between cloud and local Bot API servers.
See [TolaAI integration](docs/tolaai-integration.md) for the required factory,
Docker network, webhook, and delivery changes.

## API example

```bash
curl -sS http://127.0.0.1:8080/v1/deliveries \
  -H "Authorization: Bearer ${APP_API_TOKEN}" \
  -H "Idempotency-Key: generation-7c4f-send" \
  -H "Content-Type: application/json" \
  -d '{
    "chat_id": 123456789,
    "source_url": "https://trusted-s3.example/result.mp4?signature=...",
    "filename": "tolaai-7c4f.mp4",
    "media_kind": "video",
    "mime_type": "video/mp4",
    "expected_size_bytes": 524288000,
    "caption": "Генерация готова",
    "supports_streaming": true,
    "fallback_to_document": true
  }'
```

The API returns `202` with a job in `queued` state. Read its status with:

```bash
curl -sS http://127.0.0.1:8080/v1/deliveries/JOB_ID \
  -H "Authorization: Bearer ${APP_API_TOKEN}"
```

States: `queued`, `downloading`, `sending`, `retry_scheduled`, `sent`, `failed`.

## Safety defaults

- API listens on `127.0.0.1` by default and requires a constant-time Bearer
  token check.
- Signed download URLs are restricted to `SOURCE_ALLOWED_HOSTS`; redirects are
  revalidated to prevent SSRF.
- HTTP sources are disabled unless explicitly enabled for local MinIO.
- Expected and streamed sizes are checked before the Bot API send.
- The BotFather token never appears in queue payloads or API responses.
- HTTP client INFO logging is disabled because Telegram bot tokens are part of
  Bot API request paths.
- Redis uses AOF persistence and idempotency keys prevent duplicate enqueueing.
- A claimed job is acknowledged only after its terminal/retry state is stored;
  unacknowledged jobs are recovered after a worker restart.
- Retries are limited and delayed; Telegram `retry_after` is respected.

## Documentation

- [TolaAI integration](docs/tolaai-integration.md)
- [Инструкция интеграции на русском](docs/integration-ru.md)
- [Рекомендации по VPS/VDS](docs/vps-sizing-ru.md)
- [HTTP API](docs/api.md)
- [Operations and rollback](docs/operations.md)

## Development

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
```

## Upstream and limits

The local server is compiled from the official Telegram repository at the
commit in `TELEGRAM_BOT_API_COMMIT`. Review upstream changes before updating
that pin. Telegram documents a 2000 MB local upload maximum; this service uses
`2_000_000_000` bytes as its default hard ceiling.

## License

MIT for this repository. The bundled-at-build-time official Telegram Bot API
server retains its upstream Boost Software License.
