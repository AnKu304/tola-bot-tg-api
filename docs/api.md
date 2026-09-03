# Delivery API

## Authentication

All `/v1/*` calls require:

```http
Authorization: Bearer <APP_API_TOKEN>
```

`POST /v1/deliveries` also requires a non-blank `Idempotency-Key` header of at
most 160 characters. Use a stable value derived from generation, recipient,
and delivery revision. The same key and the same payload return the original
job. The same key with a different payload returns `409 Conflict`.

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
`expected_size_bytes` may be omitted for legacy sources; the worker still
streams with the configured hard ceiling. When it is supplied, the downloaded
byte count must match exactly or the job ends as `failed` with
`source_size_mismatch` before Telegram is called.

## GET `/v1/deliveries/{job_id}`

Returns public job state without the signed source URL or bot token. Terminal
states are `sent` and `failed`. Status is retained for
`DELIVERY_JOB_TTL_SECONDS`.

Failures include a stable `error_code` and a bounded `error_message`. A failed
job is not silently re-enqueued; TolaAI can request a fresh signed URL and
create a new delivery with a new idempotency key.

Stable client-relevant errors include `file_too_large`,
`source_size_mismatch`, `source_url_not_allowed`, `download_timeout`,
`download_failed`, `telegram_unavailable`, `telegram_invalid_response`, and
`telegram_<HTTP code>`. Network errors, 408/425/429/5xx, and total download
timeouts are retryable up to `DELIVERY_MAX_ATTEMPTS`; validation, access, size,
and ordinary Telegram 4xx errors are terminal. A media-to-document fallback is
attempted once only for known non-retryable media-format errors.

## Health

- `GET /health/live`: process is running.
- `GET /health/ready`: Redis responds, a worker heartbeat exists, and the bot
  answers `getMe` through the local Bot API server.
