FROM python:3.11-slim

RUN groupadd -r mergen && useradd -r -g mergen mergen

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-tur \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY data ./data
COPY run_bot.py ./
COPY docker-entrypoint.sh ./
COPY docker-run-bot.sh ./

RUN sed -i 's/\r$//' /app/docker-entrypoint.sh /app/docker-run-bot.sh

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

RUN mkdir -p /app/data_csv /app/data /app/.mplconfig /app/.cache/fontconfig && chown -R mergen:mergen /app \
    && chmod +x /app/docker-entrypoint.sh /app/docker-run-bot.sh

ENV MPLCONFIGDIR=/app/.mplconfig
ENV XDG_CACHE_HOME=/app/.cache

USER mergen

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
