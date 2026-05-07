FROM python:3.12-slim

ARG INSTALL_PLAYWRIGHT=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt
RUN if [ "$INSTALL_PLAYWRIGHT" = "1" ]; then \
    python -m playwright install --with-deps chromium \
    && chmod -R 755 /ms-playwright; \
    fi \
    && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8000
CMD ["gunicorn", "osprey.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
