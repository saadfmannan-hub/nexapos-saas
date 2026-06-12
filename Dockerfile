# NexaPOS production image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

WORKDIR /app

# System packages needed by Pillow / psycopg
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/ requirements/
RUN pip install --no-cache-dir -r requirements/production.txt

COPY . .

# Collect static at build time with a throwaway key
RUN SECRET_KEY=build-only DEBUG=False python manage.py collectstatic --noinput

RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fs http://localhost:8000/api/v1/health/ || exit 1

CMD ["sh", "docker/start.sh"]
