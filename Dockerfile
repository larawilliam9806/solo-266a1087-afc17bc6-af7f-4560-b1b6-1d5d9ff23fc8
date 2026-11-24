FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARCHIVE_DB=/data/archive.sqlite3

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY archive_app ./archive_app
COPY wsgi.py .

RUN mkdir -p /data && useradd --create-home --uid 10001 app && chown -R app:app /data /app
USER app

EXPOSE 8000
VOLUME ["/data"]

# One worker with threads: each request opens its own SQLite connection and a
# CSV download holds only its own fixed cut-off, so concurrency is safe.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", \
     "--timeout", "0", "--access-logfile", "-", "wsgi:app"]
