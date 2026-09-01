# Delivery API

## Authentication

All `/v1/*` calls require:

```http
Authorization: Bearer <APP_API_TOKEN>
```

Health endpoints do not require authentication and expose no secrets.

## POST `/v1/deliveries`

Enqueue one file. Use a stable `Idempotency-Key` derived from the TolaAI
generation and delivery action. Repeating the same key returns the original job
instead of sending a duplicate Telegram message.

Required JSON fields:

| Field | Description |
| --- | --- |
| `chat_id` | Telegram numeric chat ID or channel username |
| `source_url` | HTTPS URL on a host listed in `SOURCE_ALLOWED_HOSTS` |
| `filename` | Basename only, up to 180 characters |
| `media_kind` | `document`, `video`, `photo`, `audio`, or `animation` |

Recommended fields:

| Field | Description |
| --- | --- |
| `expected_size_bytes` | TolaAI already stores this in `files.size_bytes`; allows rejection before download |
| `mime_type` | Stored result MIME type |
| `caption` | Telegram caption, max 1024 characters |
| `reply_markup` | Bot API inline/reply keyboard JSON object |
| `fallback_to_document` | Retry media-format failures as a document; default `true` |

The API returns `202` for both new and idempotently repeated jobs.

## GET `/v1/deliveries/{job_id}`

Returns public job state without the signed source URL or bot token. Terminal
states are `sent` and `failed`. Status is retained for
`DELIVERY_JOB_TTL_SECONDS`.

Failures include a stable `error_code` and a bounded `error_message`. A failed
job is not silently re-enqueued; TolaAI can request a fresh signed URL and
create a new delivery with a new idempotency key.

## Health

- `GET /health/live`: process is running.
- `GET /health/ready`: Redis responds, a worker heartbeat exists, and the bot
  answers `getMe` through the local Bot API server.
