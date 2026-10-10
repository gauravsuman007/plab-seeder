FROM python:3.12-slim

WORKDIR /app

# xvfb is needed for the patchright fallback stage (headful Chromium under
# a virtual framebuffer). camoufox lives in the sidecar image now.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m patchright install --with-deps chromium

COPY app ./app

ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
