"""Run the announce listener and the management UI in one process, on two ports."""

import asyncio
import contextlib
import logging

import httpx
import uvicorn

from . import announce_app, ui_app
from .config import ANNOUNCE_PORT, UI_PORT
from .db import DB

log = logging.getLogger("announce-proxy")


class _EmbeddedServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to the main one."""
    def install_signal_handlers(self) -> None:
        pass

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def _prune_loop(db: DB) -> None:
    while True:
        try:
            db.prune()
        except Exception:  # noqa: BLE001 -- housekeeping must never stop the proxy
            log.exception("prune failed")
        await asyncio.sleep(3600)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if ANNOUNCE_PORT == UI_PORT:
        raise SystemExit("ANNOUNCE_PORT and UI_PORT must differ: only the announce port is meant to be exposed")
    db = DB()
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as http:
        # No access log on either server: the two share uvicorn's global access logger,
        # and the announce URL carries the token.
        ann = _EmbeddedServer(uvicorn.Config(announce_app.make_app(db, http), host="0.0.0.0",
                                             port=ANNOUNCE_PORT, log_level="warning", access_log=False))
        ui = uvicorn.Server(uvicorn.Config(ui_app.make_app(db), host="0.0.0.0", port=UI_PORT, log_level="info", access_log=False))
        tasks = [asyncio.create_task(ann.serve()), asyncio.create_task(_prune_loop(db))]
        log.info("announce listener on :%s, management UI on :%s", ANNOUNCE_PORT, UI_PORT)
        try:
            await ui.serve()
        finally:
            ann.should_exit = True
            tasks[1].cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
