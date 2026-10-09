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
| `APP_PASSWORD` | unset | If set, HTTP Basic auth is required for the whole UI (any username). |

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
