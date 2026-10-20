# pornolab-seeder

A small seeding helper for one PornoLab account: it logs in, reads the tracker
listing and your profile, scores releases by expected upload-per-byte (see
[app/strategy.py](app/strategy.py)), and hands the best matches to
qBittorrent — all while staying inside the site's download-limit rules (see
[app/limits.py](app/limits.py)). A web UI shows login, live profile stats, the
candidate ranking, what's seeding, and history.

## Running it

```bash
pip install -r requirements.txt
DATA_DIR=./data QBIT_URL=http://localhost:8080 uvicorn app.main:app --port 8000
```

Then open `http://localhost:8000`.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `/data` | Where `settings.json`, `seeder.db` and the session cookie are kept. |
| `QBIT_URL` | `http://gateway:8080` | qBittorrent WebUI base URL. |
| `GATEWAY_URL` | `http://gateway:8081` | Optional VPN-gateway status panel (informational only). |
| `APP_PASSWORD` | unset | If set, HTTP Basic auth is required for the whole UI (any username). `/proxy/ann` is exempt (it is gated by its random token). |
| `PUBLIC_ANNOUNCE_ENABLED` | `false` | Start the announce-only listener (also togglable in Settings). |
| `PUBLIC_ANNOUNCE_PORT` | `8001` | Compose only: host *and* container port of that listener. |
| `PUBLIC_ANNOUNCE_BIND` | `0.0.0.0` | Compose only: host interface it is published on (`127.0.0.1` if the reverse proxy is on the same host). |
| `PUBLIC_ANNOUNCE_URL` | unset | The address your reverse proxy serves, e.g. `https://ann.example.com`. |

Everything else (PornoLab URL, selection rules, the download guard, pruning)
is configured from the Settings tab in the UI and stored in
`DATA_DIR/settings.json`.

## Using the UI

- **Dashboard** — log in to PornoLab (captcha is shown inline if the site asks
  for one), see your live profile (rating, downloaded/uploaded breakdown,
  today vs. yesterday), the current tier and download guard headroom, the
  daily `.torrent` fetch budget, and a history chart.
- **Candidates** — scan the tracker and see every release ranked by score,
  with size/cost, seeders/leechers/grabs, and why each one is or isn't
  addable right now; add any that fit with one click.
- **Torrents** — what's seeding in qBittorrent, with stop/start/remove.
- **Settings** — qBittorrent connection, scan/automation cadence, selection
  rules, the download guard, and pruning.
- **Events** — a log of logins, scans, additions, guard interventions, and
  errors.

Turning on **Auto-seed** (top bar) lets the engine add the best-fitting
candidates on its own, within the daily budget and the download guard,
instead of only adding what you click.

See [AGENTS.md](AGENTS.md) for the policy an AI agent working on this repo
follows (credentials, scraping etiquette, scope).

### Making the announce proxy public

The dashboard stays private; only `/proxy/ann` is exposed, on its own port.

1. Enable it in Settings -> *Public announce endpoint* (or `PUBLIC_ANNOUNCE_ENABLED=true`).
2. Point your reverse proxy at `http://<host>:8001` (or your `PUBLIC_ANNOUNCE_PORT`) for a
   hostname of your choice, and enter that address as the *Public URL*.
3. Click *Test public URL*: it should report that the announce listener answered.

The listener serves nothing but `/proxy/ann`, has no access log, and torrents carry
random tokens rather than your passkey. Torrents added by this app still use
`SELF_URL` (the internal address qBittorrent reaches); the public URL is for clients
outside your network.

## Standalone stack (seeder + qBittorrent)

For hosts without the vpngate gateway, `docker-compose.standalone.yml` runs the
seeder next to a regular qBittorrent that stays usable for normal downloads:

```bash
docker compose -f docker-compose.standalone.yml up -d --build
```

Seeder UI on `:8010`, qBittorrent on `:8080`. On first start an init script
whitelists private subnets on qBittorrent's WebUI so the seeder needs no login;
if you expose `:8080` beyond your LAN, set a WebUI password instead and enter it
in the seeder's Settings. Optional env: `DOWNLOADS_DIR`, `QBIT_WEBUI_PORT`,
`QBIT_PEER_PORT`, `SEEDER_PORT`, `APP_PASSWORD`, `PUID`, `PGID`, `TZ`.
