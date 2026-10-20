"""Client for the announce-proxy container (see announce-proxy/).

The proxy keeps the real tracker URLs (and so the passkey); this app only ever
holds the short proxy URL it gets back. Registration is idempotent per URL."""

import httpx


class ProxyError(Exception):
    pass


class AnnounceProxy:
    def __init__(self, base_url: str, key: str):
        self.base = base_url.rstrip("/")
        self._headers = {"X-Api-Key": key}

    async def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.request(method, self.base + path, headers=self._headers, **kw)
        except httpx.HTTPError as e:
            raise ProxyError(f"announce proxy unreachable: {type(e).__name__}") from e
        if r.status_code in (404, 429):
            raise ProxyError("announce proxy refused the request (check the URL and API key)")
        if r.status_code >= 400:
            try:
                detail = r.json().get("error", r.text)
            except ValueError:
                detail = r.text
            raise ProxyError(f"announce proxy: {detail}")
        return r

    async def register(self, url: str, label: str, *, first_download_at: float | None = None,
                       bootstrap: bool = False, passthrough: bool = False) -> str:
        body = {"url": url, "label": label, "bootstrap": bootstrap, "passthrough": passthrough}
        if first_download_at:
            body["first_download_at"] = first_download_at
        return (await self._call("POST", "/v1/register", json=body)).json()["announce_url"]

    async def set_ip(self, label: str, ip: str) -> None:
        await self._call("POST", "/v1/identity", json={"label": label, "ip": ip})

    async def whoami(self) -> dict:
        return (await self._call("GET", "/v1/whoami")).json()

    async def identity(self, label: str) -> dict:
        return (await self._call("GET", "/v1/identity", params={"label": label})).json()
