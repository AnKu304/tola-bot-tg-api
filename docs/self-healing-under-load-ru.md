# Автовосстановление TolaAI после перегрузки

Эта инструкция описывает лёгкую схему без Kubernetes для одного production-VPS:

```text
ограничение входящей нагрузки
        ↓
Docker healthcheck + restart policy
        ↓
Monit проверяет готовность и запускает восстановление
        ↓
systemd поднимает Docker после сбоя или перезагрузки
        ↓
внешний монитор сообщает, если весь VPS недоступен
```

Цель — автоматически восстанавливать контейнеры после OOM, зависания процесса,
исчерпания подключений или временного скачка нагрузки. Схема не защищает от
поломки физического узла/дата-центра: для этого нужен второй VDS.

## Главное правило

Нельзя перезагружать сервер только потому, что CPU достиг 90–100%. Во время
полезной обработки больших файлов это может быть нормальным. Решение о restart
принимается по readiness-check и продолжительности отказа. Reboot используется
только при отказе Docker/ОС или аппаратным watchdog.

Также restart без ограничения нагрузки не решает проблему: после запуска тот же
поток запросов снова положит приложение. Поэтому сначала настраиваются
backpressure и лимиты, затем автовосстановление.

## 1. Защитить сервис от повторного падения

Для начальной production-конфигурации:

- Celery TolaAI: `--concurrency=2`;
- delivery worker: `DELIVERY_WORKER_CONCURRENCY=2`;
- Nginx: ограничение частоты и числа одновременных запросов;
- тяжёлая генерация принимается в очередь, а не держит HTTP-запрос открытым;
- большие файлы не читаются целиком в `bytes`/`BytesIO`;
- на хосте остаётся минимум 2 GB RAM для ОС, Docker и Monit;
- swap 2–4 GB используется как аварийный буфер, а не как рабочая память.

Пример Celery:

```yaml
services:
  worker:
    command: >
      celery -A backend.tasks.celery_app.celery_app worker
      -Q generation -l info --concurrency=2
```

Пример базовой защиты Nginx. Значения нужно подтвердить нагрузочным тестом, а
webhook/payments вынести в отдельные location с собственными лимитами:

```nginx
limit_req_zone $binary_remote_addr zone=tola_api:10m rate=10r/s;
limit_conn_zone $binary_remote_addr zone=tola_conn:10m;

location /api/ {
    limit_req zone=tola_api burst=30 nodelay;
    limit_conn tola_conn 20;
    proxy_pass http://backend:8000;
}
```

Ответ при перегрузке должен быть управляемым `429`/`503`, а не OOM всего VPS.

## 2. Добавить restart policy и healthchecks

У всех долгоживущих контейнеров:

```yaml
restart: unless-stopped
```

Это уже настроено у контейнеров `tola-bot-tg-api`. Проверить TolaAI backend,
Celery, PostgreSQL, Redis и Nginx.

Для каждого HTTP-сервиса добавить healthcheck. Пример:

```yaml
services:
  backend:
    restart: unless-stopped
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - >-
          import urllib.request;
          urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)
      interval: 15s
      timeout: 5s
      retries: 4
      start_period: 40s
```

Важно: Docker restart policy перезапускает завершившийся контейнер, но сам по
себе не перезапускает процесс только из-за статуса `unhealthy`. Эту ситуацию
обрабатывает Monit через recovery script.

## 3. Сделать настоящий readiness endpoint TolaAI

Текущий `/health` TolaAI — простой liveness-check. Добавить `/health/ready`,
который с короткими timeout проверяет:

1. event loop и приложение;
2. `SELECT 1` в PostgreSQL;
3. `PING` основного Redis;
4. свежий heartbeat Celery worker;
5. критичную конфигурацию без вывода секретов.

Ответ:

```json
{
  "status": "ready",
  "postgres": "ok",
  "redis": "ok",
  "celery": "ok"
}
```

При отказе обязательной зависимости возвращать HTTP 503. Проверка не должна
обращаться к внешнему AI-провайдеру или Telegram на каждый запрос healthcheck.

Delivery уже предоставляет `/health/live` и `/health/ready`. Внешний монитор
проверяет readiness обоих стеков.

## 4. Ограничить память и логи

Лимиты задавать через отдельный production override и подбирать по метрикам.
Для VDS 8 vCPU / 16 GB возможная стартовая раскладка:

| Сервис | Стартовый лимит RAM |
| --- | ---: |
| TolaAI backend | 1.5 GB |
| TolaAI Celery | 2 GB |
| PostgreSQL | 3 GB |
| основной Redis | 768 MB |
| delivery API | 512 MB |
| delivery worker | 1.5 GB |
| local Bot API | 1.5 GB |
| delivery Redis | 512 MB |

Это стартовые ограничения, а не универсальные значения. Суммарно нужно
оставить запас ОС. Если контейнер регулярно получает `OOMKilled`, сначала
найти утечку/неограниченную конкурентность, а не бесконечно поднимать лимит.

Для каждого контейнера включить ротацию:

```yaml
logging:
  driver: json-file
  options:
    max-size: "50m"
    max-file: "5"
```

Redis настроить с осознанной `maxmemory` policy. Для очереди доставки нельзя
использовать политику, которая незаметно удаляет незавершённые jobs. Диск
алертить при 70%, критически — при 85%. Очистку временных delivery-файлов
выполнять штатным cleanup сервиса с учётом активных jobs.

## 5. Установить Monit

Ubuntu 24.04:

```bash
sudo apt update
sudo apt install -y monit curl
sudo systemctl enable --now monit
```

Создать два root-owned скрипта:

- `/usr/local/sbin/check-tola-ready` — проверяет TolaAI и delivery readiness;
- `/usr/local/sbin/recover-tola` — выполняет ступенчатое восстановление.

Алгоритм `recover-tola`:

1. взять `flock`, чтобы не запускать два восстановления одновременно;
2. повторно проверить readiness, исключив кратковременный сбой;
3. перезапустить только нездоровый backend/worker;
4. подождать 30–60 секунд и снова проверить;
5. если Docker daemon недоступен — выполнить `systemctl restart docker`;
6. выполнить `docker compose up -d` для обоих проектов;
7. проверить readiness;
8. записать результат в journald и отправить alert;
9. при повторном отказе прекратить циклические restarts и эскалировать человеку.

В скрипте должны быть:

- абсолютные пути к обоим репозиториям и compose-файлам;
- timeout на каждую команду;
- cooldown минимум 5 минут;
- максимум 3 автоматических восстановления за 30 минут;
- отсутствие токенов и signed URL в логах;
- никаких `docker system prune`, удаления volumes или файлов PostgreSQL.

Пример `/etc/monit/conf-enabled/tola`:

```monit
set daemon 15

check host tola_backend with address 127.0.0.1
    if failed
       port 80
       protocol http
       request "/health/ready"
       for 3 cycles
    then exec "/usr/local/sbin/recover-tola backend"

check host tola_delivery with address 127.0.0.1
    if failed
       port 8080
       protocol http
       request "/health/ready"
       for 3 cycles
    then exec "/usr/local/sbin/recover-tola delivery"

check filesystem rootfs with path /
    if space usage > 70% then alert
    if space usage > 85% for 2 cycles then alert
```

Проверить конфиг перед reload:

```bash
sudo monit -t
sudo systemctl reload monit
sudo monit summary
```

Если Nginx не публикует `/health/ready`, Monit должен обращаться к локальному
порту backend. Не открывать внутренний health endpoint в интернет без
ограничения доступа.

## 6. Автозапуск Docker и стеков

```bash
sudo systemctl enable docker
systemctl is-enabled docker
```

Создать systemd unit для запуска Compose после Docker и сети. Использовать
реальные абсолютные пути сервера:

```ini
[Unit]
Description=TolaAI production stacks
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/tolaai
ExecStart=/usr/bin/docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
ExecStart=/usr/bin/docker compose -f /opt/tola-bot-tg-api/compose.yml up -d
ExecStop=/usr/bin/docker compose -f /opt/tola-bot-tg-api/compose.yml stop
ExecStop=/usr/bin/docker compose -f docker-compose.yml -f docker-compose.prod.yml stop
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

Установить:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tola-stacks.service
```

Не выполнять `logout-cloud` Telegram при каждом старте. Это одноразовый шаг
первичного переноса; обычный reboot должен использовать уже сохранённые данные
local Bot API.

## 7. Включить watchdog ОС

Проверить поддержку:

```bash
ls -l /dev/watchdog*
```

Если VPS/провайдер предоставляет watchdog, создать
`/etc/systemd/system.conf.d/watchdog.conf`:

```ini
[Manager]
RuntimeWatchdogSec=30s
RebootWatchdogSec=5min
```

После изменения проверить документацию конкретного провайдера и протестировать
на staging. Не включать слепо, если `/dev/watchdog` отсутствует.

## 8. Поставить внешний монитор

Uptime Kuma или внешний SaaS должен работать не на production-VPS. Минимум:

- HTTPS Mini App/API — каждые 30 секунд;
- TolaAI readiness — через защищённый endpoint;
- delivery readiness — через VPN/private probe;
- срок TLS-сертификата;
- уведомления в отдельный Telegram-чат и резервный канал;
- подтверждение восстановления.

После трёх неуспешных проверок — alert. Автоматический reboot через API
провайдера разрешать только отдельному защищённому runner с cooldown и
идемпотентностью. Сам Uptime Kuma не должен хранить root SSH key.

## 9. Проверить на staging

Не проводить fault injection впервые на production.

| Сценарий | Ожидаемый результат |
| --- | --- |
| `SIGKILL` backend | Docker перезапускает контейнер, readiness возвращается |
| OOM одного worker | Перезапускается только worker, ОС и PostgreSQL живы |
| Зависший HTTP handler | health становится unhealthy, Monit запускает recovery |
| Restart Docker | все контейнеры автоматически возвращаются |
| Reboot VPS | Docker, Compose и Monit стартуют без ручного входа |
| Нагрузка выше лимита | клиент получает 429/503, сервер не падает |
| Повторный crash-loop | после лимита restarts отправляется alert, цикл прекращается |
| Диск 70/85% | приходят warning/critical alert, данные не удаляются вслепую |

После каждого теста проверить PostgreSQL, Redis, очередь, webhook, Mini App,
маленький файл и файл больше 50 MB.

## 10. Что считать готовым

Система считается готовой, когда на staging пять раз подряд успешно проходит:

```text
нагрузка → падение одного контейнера → автоматический restart
          → readiness 200 → smoke-тест → уведомление о восстановлении
```

Целевой срок восстановления для контейнера — до 2 минут, после reboot VPS — до
10 минут. После каждого автоматического восстановления сохранять причину,
`OOMKilled`, CPU/RAM/disk, длительность отказа и выполненное действие. Без этой
истории нельзя понять, лечит автоматика проблему или только маскирует её.

## Полезные источники

- Docker restart policies:
  <https://docs.docker.com/engine/containers/start-containers-automatically/>
- Monit documentation: <https://www.mmonit.com/monit/documentation/monit.html>
- Uptime Kuma: <https://github.com/louislam/uptime-kuma>
