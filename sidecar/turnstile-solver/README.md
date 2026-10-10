# turnstile-solver

Tiny HTTP sidecar that clears a Cloudflare Turnstile widget embedded in an
otherwise-normal page and returns the token. Built because FlareSolverr and
byparr only engage on Cloudflare's own full-page interstitial; an embedded
widget on a 200 OK page is invisible to them.

Uses camoufox (patched Firefox, arm64-native). See the parent repo's
[`CLAUDE.md`](../../CLAUDE.md) and the comments in
[`app/pornolab.py`](../../app/pornolab.py) for the recipe this packages.

## API

```http
POST /solve
Content-Type: application/json

{
  "url":              "https://pornolab.net/forum/profile.php?mode=register",
  "agree_click":      "a[onclick*=\"go-to-reg\"]",
  "widget_selector":  ".cf-turnstile",
  "max_timeout":      90
}
```

Response:

```json
{
  "ok":      true,
  "token":   "0.<base64>...",
  "cookies": [{"name":"...", "value":"...", "domain":"...", ...}, ...],
  "html":    "<the final page HTML>",
  "elapsed": 1.3
}
```

The caller does the subsequent form submit; this sidecar only extracts the
token.

## Image

Published to GHCR via CI (linux/amd64 + linux/arm64):

```bash
docker pull ghcr.io/gauravsuman007/camoufox-turnstile-solver:latest
```

Or build locally from the repo root:

```bash
docker build -t camoufox-turnstile-solver sidecar/turnstile-solver
```

## Run

```bash
docker run -d --name turnstile-solver --restart unless-stopped \
  -p 8193:8000 ghcr.io/gauravsuman007/camoufox-turnstile-solver:latest
```

Health check: `curl http://localhost:8193/healthz` -> `{"status":"ok"}`.

## Notes

- One Firefox process per `/solve` call, serialised through an in-process lock.
  Don't horizontally stack calls -- scale by replicas instead.
- `exclude_addons=["UBO"]` is critical: camoufox ships uBlock Origin, which
  blocks `challenges.cloudflare.com` by default.
- The reliable click target is the `.cf-turnstile` **div**, not its iframe --
  Turnstile injects the iframe with no `src` attribute, so an iframe-src
  selector misses it, but the div's `getBoundingClientRect` is always valid.
  Click 30 px from its left edge, vertical centre.
