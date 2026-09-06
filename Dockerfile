FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y build-essential && apt-get autoremove -y

COPY sniperbot/ ./sniperbot/
COPY config/ ./config/

RUN useradd --create-home --uid 10001 sniper \
    && mkdir -p /app/data && chown -R sniper:sniper /app
USER sniper

VOLUME ["/app/data"]

CMD ["python", "-m", "sniperbot"]
