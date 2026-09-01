# Operations and rollback

## Before the first production cutover

1. Create `api_id` and `api_hash` at <https://my.telegram.org/apps>.
2. Configure `.env`; use an exact S3 host allowlist.
3. Build and start the stack.
4. Confirm `docker compose exec api python -m app.cli check-local` can reach the
   local process. Before cloud `logOut`, Telegram may still refuse the bot login;
   that is expected.
5. Update TolaAI so every `Bot` and `Application` uses the local base URLs.
6. Update the TolaAI generation delivery endpoint to enqueue this service.
7. In a maintenance window, call `logout-cloud --confirm` once.
8. Restart TolaAI and verify its webhook, commands, payments, menu buttons, and
   a small delivery through the local server.
9. Send 49 MB, 51 MB, and a representative large video in a private test chat.

Do not call `logOut` during every deployment.

## Monitoring

Alert when:

- `/health/ready` is non-200 for more than two checks;
- Redis storage or the temporary volume approaches capacity;
- `failed` deliveries increase by error code;
- queue age is above the signed S3 URL lifetime;
- local Bot API logs repeated authorization, flood-control, or file errors.

The worker logs IDs, chat IDs, byte counts, and stable error codes. It never logs
the signed source URL query string or the bot token.

## Capacity

Each active worker download needs enough temporary disk for one complete file.
With concurrency `N`, reserve at least `N × maximum expected file size`, plus a
margin. Start with concurrency 2 and scale workers only after measuring outbound
bandwidth and Telegram flood limits.

Redis uses append-only persistence. Back up the `redis-data` and
`telegram-data` volumes before infrastructure migrations. The temporary
`delivery-files` volume does not need backup.

The worker moves each job from a pending list to a processing list and removes
it only after saving the outcome. A single worker service recovers remaining
processing entries on startup. Run concurrency inside that service using
`DELIVERY_WORKER_CONCURRENCY`; do not scale the worker container horizontally
without adding visibility leases and a leader-elected recovery pass.

Delivery is at-least-once around a hard process crash. A crash after Telegram
accepts a message but before Redis stores `sent` can produce one duplicate on
recovery; this is an unavoidable boundary without a Telegram-side idempotency
primitive. Monitor restarts and reconcile by `telegram_message_id` where
possible.

## Updating the official server

1. Review upstream `tdlib/telegram-bot-api` changes and license.
2. Set `TELEGRAM_BOT_API_COMMIT` to the reviewed full SHA.
3. Build in staging.
4. Run small/large delivery tests and webhook checks.
5. Roll out during a maintenance window.

Never build production from an unpinned `master` branch.

## Rollback

Do not route the same bot to local and cloud servers at the same time.

For an application-code rollback, keep the local Bot API stack running and
point the previous TolaAI release at the local base URL.

For a full return to the cloud Bot API:

1. stop webhook traffic and workers;
2. call `deleteWebhook` and `close` on the local server as described by the
   official Telegram migration guidance;
3. change every TolaAI bot client back to `https://api.telegram.org`;
4. restart TolaAI and restore its webhook;
5. verify updates and messages before stopping the local server.
