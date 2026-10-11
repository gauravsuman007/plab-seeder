FROM python:3.12-slim

WORKDIR /app

# No browser here any more -- registration (camoufox/patchright/Xvfb) moved
# to the separate plab-register service, which owns the only browser in the
# deployment. This image is just the seeding app + the tailscale CLI.
# iproute2 is for the `ip route` call in app/tailscale.py that re-pins the
# LAN route after an exit node takes over the default route (see that
# module).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Tailscale CLI only (no tailscaled here) -- this talks to the tailscale
# sidecar's daemon over a shared socket volume, so this container never
# needs NET_ADMIN or its own tailscaled.
# TARGETARCH is set automatically by buildx/the multi-platform CI build
# (amd64/arm64) -- the project's own hosts include both.
ARG TARGETARCH
RUN TS_ARCH=$([ "$TARGETARCH" = "arm64" ] && echo arm64 || echo amd64) \
    && curl -fsSL "https://pkgs.tailscale.com/stable/tailscale_1.102.5_${TS_ARCH}.tgz" \
        -o /tmp/ts.tgz \
    && tar -xzf /tmp/ts.tgz -C /tmp \
    && mv "/tmp/tailscale_1.102.5_${TS_ARCH}/tailscale" /usr/local/bin/tailscale \
    && rm -rf /tmp/ts.tgz "/tmp/tailscale_1.102.5_${TS_ARCH}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
