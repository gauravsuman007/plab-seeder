FROM python:3.12-slim

WORKDIR /app

# Build deps for camoufox's indexed-zstd C extension (gcc, g++, python headers,
# libzstd), Xvfb (camoufox's headless="virtual" mode shells out to it), and
# Firefox's runtime shared libs -- camoufox is a patched Firefox, so it needs
# the same system packages as the vanilla browser.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ python3-dev libzstd-dev xvfb \
        libgtk-3-0 libasound2 libdbus-glib-1-2 libxt6 libxtst6 \
        libgl1 libegl1 libxcomposite1 libxdamage1 libxrandr2 libpango-1.0-0 \
        fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && camoufox fetch

COPY server.py .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
