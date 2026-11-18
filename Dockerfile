FROM python:3.15-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mailbackup ./mailbackup

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app app \
    && mkdir -p /data \
    && chown -R app:app /data /app

USER app

VOLUME ["/data"]
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=60s --timeout=10s --start-period=300s --retries=3 \
    CMD ["python", "-m", "mailbackup", "health", "--max-age", "900"]

ENTRYPOINT ["python", "-m", "mailbackup"]
CMD ["daemon"]
