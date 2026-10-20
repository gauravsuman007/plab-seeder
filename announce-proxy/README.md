# announce-proxy

A standalone announce proxy for the PornoLab seeding tools. Torrent clients announce to it
instead of the tracker; it hides downloaded bytes (`downloaded=0`), can add a steady, irregular
amount of upload per account ("fake upload credit"), and relays the announce to the real tracker.
Its dashboard shows who is using it and trends per client over time.

It serves every tool that adds torrents for you:

| Client | How it talks to the proxy |
|---|---|
| the main seeder (`app/main.py`) | *Settings → Announce proxy* (URL + key) |
| the lean fake-quota app (`app/standalone.py`) | *Settings → Announce proxy*, or `ANNOUNCE_PROXY_URL` / `ANNOUNCE_PROXY_KEY` |
| the Prowlarr indexer sidecar (`plab-prowlarr`) | `ANNOUNCE_PROXY_URL` / `ANNOUNCE_PROXY_KEY` env on the Prowlarr container |

## Ports

| Port (default) | What | Expose it? |
|---|---|---|
| `ANNOUNCE_PORT` (8101) | `/ann` (what torrent clients hit) and the key-gated `/v1/*` API | this is the one to point a reverse proxy at |
| `UI_PORT` (8102) | dashboard and admin API | keep private (`UI_PASSWORD` adds HTTP Basic) |

They must differ; the container refuses to start otherwise. The announce port serves nothing
else — no dashboard, no docs, no access log (the token is in the URL).

## How a client uses it

1. In the dashboard's **Clients** tab, create a client (name + kind). You get a key **once**.
2. Put the proxy's **announce-port** URL and that key in the client's settings.
3. The client registers each tracker announce URL with `POST /v1/register` and puts the returned
   `…/ann?t=<token>` into the `.torrent` instead. The real URL (and the passkey in it) never
   leaves the proxy. Registering the same URL again returns the same token.

The URL handed back is built from the address the client used to reach the proxy, so each client
automatically gets one that works from where it is. A client's row in the **Clients** tab can
override that (for example with a public `https://` address for a debrid service).

Each client can have several **accounts** (the label it registers under, normally the PornoLab
username). Fake upload credit is a per-account setting in the proxy:

- the rate is per account, not per torrent — torrents of one account share it;
- the reported counter is the client's real one plus a running offset that only ever grows, so the
  tracker never sees it go backwards (even if you switch it off);
- it is held back until the account has some download history, as the seeder's bootstrap
  torrent provides.

Clients can register as **passthrough** (the lean fake-quota app does): the proxy then relays its
counters untouched and just lists it on the dashboard.

## Safety

- Registration is key-gated; a bad key gets a bare 404, and 20 bad attempts from one address in
  10 minutes locks that address out for a while.
- A request that arrives through a reverse proxy (it carries `X-Forwarded-For`) is refused on
  `/v1/*` unless *Settings → accept the registration API through a reverse proxy* is on. So even
  with the announce port published, registration stays on your private network by default.
- Tokens only resolve to URLs registered with a valid key. Registered URLs may not point at
  private/loopback addresses (the proxy fetches them on every announce) unless you allow it, and
  you can restrict tracker hosts to an allow-list.

## Networks

A client reaches the proxy over a docker network, so attach the proxy to each client's network
(each with its own address) in a `docker-compose.override.yml`:

```yaml
services:
  announce-proxy:
    networks:
      default: {}
      seeder_net:
        ipv4_address: 192.168.176.20
networks:
  seeder_net:
    name: vpngate-torrent_default
    external: true
```

and give that client `http://192.168.176.20:8101` as its proxy URL. Clients on the host's LAN can
use the published port (`http://<host>:8101`).

## Development

```bash
pip install -r announce-proxy/requirements.txt pytest
pytest announce-proxy/tests
```
