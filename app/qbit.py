"""qBittorrent WebUI API v2 client (the vpngate container).

Credentials are optional: the vpngate qBittorrent whitelists its docker subnet,
so from inside that network every call is accepted without a login. When a
username is set, a 403 triggers one login and a retry.
"""

import httpx


class QbitError(Exception):
    pass


class Qbit:
    def __init__(self, url: str, username: str = "", password: str = ""):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.client = httpx.AsyncClient(timeout=20, headers={"Referer": self.url})

    async def close(self) -> None:
        await self.client.aclose()

    async def _login(self) -> None:
        r = await self.client.post(f"{self.url}/api/v2/auth/login",
                                   data={"username": self.username, "password": self.password})
        if r.text.strip() != "Ok.":
            raise QbitError("qBittorrent rejected the username/password")

    async def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = await self.client.request(method, f"{self.url}/api/v2/{path}", **kw)
            if r.status_code == 403 and self.username:
                await self._login()
                r = await self.client.request(method, f"{self.url}/api/v2/{path}", **kw)
        except httpx.HTTPError as e:
            raise QbitError(f"cannot reach qBittorrent at {self.url}: {e}") from e
        if r.status_code == 403:
            raise QbitError("qBittorrent refused the request (403): whitelist this network "
                            "or set a username/password")
        if r.status_code >= 400:
            raise QbitError(f"qBittorrent {path}: HTTP {r.status_code} {r.text[:200]}")
        return r

    async def version(self) -> str:
        return (await self._call("GET", "app/version")).text.strip()

    async def transfer(self) -> dict:
        return (await self._call("GET", "transfer/info")).json()

    async def torrents(self, category: str) -> list[dict]:
        return (await self._call("GET", "torrents/info", params={"category": category})).json()

    async def trackers(self, infohash: str) -> list[dict]:
        return (await self._call("GET", "torrents/trackers", params={"hash": infohash})).json()

    async def ensure_category(self, name: str, save_path: str) -> None:
        cats = (await self._call("GET", "torrents/categories")).json()
        if name not in cats:
            await self._call("POST", "torrents/createCategory", data={"category": name, "savePath": save_path})
        elif cats[name].get("savePath") != save_path:
            await self._call("POST", "torrents/editCategory", data={"category": name, "savePath": save_path})

    async def add(self, torrent: bytes, filename: str, category: str, save_path: str) -> None:
        r = await self._call(
            "POST", "torrents/add",
            files={"torrents": (filename, torrent, "application/x-bittorrent")},
            data={"category": category, "savepath": save_path, "tags": "pornolab-seeder"},
        )
        # qBittorrent 5.x answers with JSON instead of "Ok."
        if r.text.strip() in ("Ok.", ""):
            return
        try:
            ok = r.json().get("success_count", 0) > 0
        except ValueError:
            ok = False
        if not ok:
            raise QbitError(f"qBittorrent did not add the torrent: {r.text[:200]}")

    async def _state_call(self, new: str, old: str, hashes: list[str]) -> None:
        # qBittorrent 5 renamed pause/resume to stop/start; older builds 404.
        data = {"hashes": "|".join(hashes)}
        try:
            await self._call("POST", f"torrents/{new}", data=data)
        except QbitError as e:
            if "404" not in str(e):
                raise
            await self._call("POST", f"torrents/{old}", data=data)

    async def stop(self, hashes: list[str]) -> None:
        await self._state_call("stop", "pause", hashes)

    async def start(self, hashes: list[str]) -> None:
        await self._state_call("start", "resume", hashes)

    async def delete(self, hashes: list[str], delete_files: bool = True) -> None:
        await self._call("POST", "torrents/delete",
                         data={"hashes": "|".join(hashes), "deleteFiles": str(delete_files).lower()})

    async def replace_tracker(self, infohash: str, old: str, new: str) -> None:
        await self._call("POST", "torrents/editTracker", data={"hash": infohash, "url": old, "origUrl": old, "newUrl": new})
