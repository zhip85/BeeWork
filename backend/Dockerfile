FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

ARG PIP_INDEX_URL
COPY requirements.txt .
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY alembic.ini .
COPY migrations ./migrations
COPY src ./src
COPY tests ./tests
ENV PYTHONPATH=/app/src

EXPOSE 8000

CMD ["uvicorn", "beework.main:app", "--host", "0.0.0.0", "--port", "8000"]
