# iCloud to Immich — run it next to your Immich server, use it, then remove it.
#
#   docker run --rm -p 8080:8080 -v icloud2immich:/data ghcr.io/xkenricx/icloud-to-immich:latest
#
# The log prints the address (with its one-time key) to open in your browser.
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/xkenricx/icloud-to-immich" \
      org.opencontainers.image.description="Run-once wizard to move iCloud Photos into Immich" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ICLOUD2IMMICH_DATA=/data

WORKDIR /opt/icloud-to-immich
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY app ./app

RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "8080", "--no-browser", "--no-idle-exit"]
