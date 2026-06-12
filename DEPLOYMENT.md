# Deployment

## Option A — Docker (recommended)

```bash
cp .env.example .env
# set: SECRET_KEY, ALLOWED_HOSTS, POSTGRES_PASSWORD, CSRF_TRUSTED_ORIGINS
docker compose up --build -d
docker compose exec web python manage.py createsuperuser
```

- `web` runs migrations then Gunicorn on :8000 (health:
  `/api/v1/health/`).
- Static files are baked into the image and served by WhiteNoise.
- Media lives in the `media` named volume — back it up.

Put nginx (or any reverse proxy) in front for TLS:

```nginx
server {
    listen 443 ssl http2;
    server_name pos.example.com;
    ssl_certificate     /etc/letsencrypt/live/pos.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pos.example.com/privkey.pem;
    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

`SECURE_PROXY_SSL_HEADER` is already configured. Use certbot for HTTPS:
`certbot --nginx -d pos.example.com`.

## Option B — Linux VPS (systemd)

```bash
sudo apt install python3.12-venv postgresql redis nginx
sudo -u postgres createuser -P nexapos && sudo -u postgres createdb -O nexapos nexapos

git clone <repo> /opt/nexapos && cd /opt/nexapos
python3 -m venv .venv && .venv/bin/pip install -r requirements/production.txt
cp .env.example .env   # set SECRET_KEY, DATABASE_URL, ALLOWED_HOSTS, REDIS_URL
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py createsuperuser
```

`/etc/systemd/system/nexapos.service`:

```ini
[Unit]
Description=NexaPOS Gunicorn
After=network.target postgresql.service

[Service]
WorkingDirectory=/opt/nexapos
EnvironmentFile=/opt/nexapos/.env
Environment=DJANGO_SETTINGS_MODULE=config.settings.production
ExecStart=/opt/nexapos/.venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 3
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

Celery (optional, for background jobs):

```ini
ExecStart=/opt/nexapos/.venv/bin/celery -A config worker -l info
# and a second unit with: celery -A config beat -l info
```

## Local Windows development

See README.md — SQLite, no Redis/Celery needed (`CELERY_TASK_ALWAYS_EAGER`
is automatic when no broker is configured).

## Backups & recovery

### PostgreSQL backup

```bash
# Docker
docker compose exec db pg_dump -U nexapos -Fc nexapos > backup-$(date +%F).dump
# VPS
pg_dump -U nexapos -Fc nexapos > /backups/nexapos-$(date +%F).dump
```

### Restore (platform operators only — never expose in the app)

```bash
docker compose exec -T db pg_restore -U nexapos -d nexapos --clean < backup.dump
```

### Media backup

```bash
docker run --rm -v nexapos_media:/m -v $(pwd):/out alpine tar czf /out/media-$(date +%F).tgz -C /m .
```

### Schedule & verify

- Cron daily dumps, keep 7 daily + 4 weekly + 12 monthly.
- **Verify** monthly by restoring into a scratch database and running
  `python manage.py check && python manage.py test tests.test_tenancy`.
- Store backups encrypted off-site; SQLite dev DB (`db.sqlite3`) is a
  simple file copy.

### Tenant export

Per-tenant data export can be produced with the reports module (CSV per
report) today; a full JSON dump per business is an architecture-ready
follow-up (all models are tenant-keyed, so `for_business()` querysets
enumerate everything a tenant owns).

## Operational notes

- Run `python manage.py seed_demo` only on demo/staging environments.
- Health endpoint for monitors/load balancers: `GET /api/v1/health/`.
- Logs go to stdout (12-factor); aggregate with journald/Docker logging.
