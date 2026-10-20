FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV DATA_DIR=/data
VOLUME ["/data"]

# Announce listener (what trackers' clients hit) and the management UI are
# deliberately two ports: publish/reverse-proxy the first, keep the second private.
EXPOSE 8101 8102

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=6 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:%s/healthz' % os.environ.get('UI_PORT','8102'))"

CMD ["python", "-m", "app.main"]
