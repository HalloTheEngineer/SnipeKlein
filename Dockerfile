FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Berlin

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
# Only the curated price table goes into the image - the runtime state
# (database, backups, watchdog state) lives in the volume and must never be
# baked in from the build host.
COPY data/prices.json ./data/prices.json
COPY data/prices.json ./prices.json
COPY config.toml ./

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 snipeklein \
    && mkdir -p /app/data \
    && chown -R snipeklein:snipeklein /app

USER snipeklein

VOLUME ["/app/data"]

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD ["snipeklein", "health"]

ENTRYPOINT ["snipeklein"]
CMD ["poll"]
