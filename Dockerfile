FROM python:3.12-slim

WORKDIR /app

# xvfb is needed for the patchright fallback stage (headful Chromium under
# a virtual framebuffer). camoufox lives in the sidecar image now.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb curl \
    && rm -rf /var/lib/apt/lists/*

# Tailscale CLI only (no tailscaled here) -- this talks to the tailscale
# sidecar's daemon over a shared socket volume, so this container never
# needs NET_ADMIN or its own tailscaled.
RUN curl -fsSL https://pkgs.tailscale.com/stable/tailscale_1.78.1_amd64.tgz \
        -o /tmp/ts.tgz \
    && tar -xzf /tmp/ts.tgz -C /tmp \
    && mv /tmp/tailscale_1.78.1_amd64/tailscale /usr/local/bin/tailscale \
    && rm -rf /tmp/ts.tgz /tmp/tailscale_1.78.1_amd64

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m patchright install --with-deps chromium

COPY app ./app

ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
