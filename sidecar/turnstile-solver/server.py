"""Thin HTTP sidecar that clears a Cloudflare Turnstile widget embedded in a
normal page and returns the token.

Why this exists: FlareSolverr and byparr only engage on Cloudflare's own
full-page interstitial. For an embedded widget on an otherwise-200 page
(the TorrentPier register flow is the canonical case) they return
"Challenge not detected!" and nothing in `turnstile_token`. Camoufox
(patched Firefox, arm64-native) clears the same widget in ~1 s once the
click lands on the right target -- see the parent repo's CLAUDE.md and
the comments in app/pornolab.py for the recipe. This sidecar packages
exactly that recipe behind a small HTTP API.

API:

    POST /solve
    {
        "url":              "<page that renders the widget>",
        "pre_post":         {"url": "...", "data": "..."},     # optional
        "agree_click":      "<CSS selector to click after loading url>",  # optional
        "widget_selector":  ".cf-turnstile",                   # default
        "max_timeout":      90,                                # seconds
    }
      ->
    {
        "ok":      true,
        "token":   "<cf-turnstile-response>",
        "cookies": [...],
        "html":    "<final page>",
        "elapsed": 1.3,
    }

The caller does the submit; this sidecar only extracts the token. Keeping it
single-purpose means no site-specific logic bleeds in."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("turnstile-solver")

app = FastAPI(title="Turnstile Solver", version="1.0.0")

# One request at a time -- camoufox starts a Firefox per `AsyncCamoufox()`
# context and running two concurrently inside one container just thrashes.
_solver_lock = asyncio.Lock()


class PrePost(BaseModel):
    url: str = Field(..., description="URL to POST before navigating to `url`")
    data: str = Field(..., description="Form-encoded body for the pre-POST")


class SolveRequest(BaseModel):
    url: str = Field(..., description="Page that renders the widget")
    pre_post: PrePost | None = None
    agree_click: str | None = Field(
        default=None, description="CSS selector to click after loading `url`"
    )
    widget_selector: str = Field(default=".cf-turnstile")
    max_timeout: int = Field(default=90, ge=10, le=180)


class SolveResponse(BaseModel):
    ok: bool
    token: str = ""
    cookies: list[dict[str, Any]] = []
    html: str = ""
    elapsed: float = 0.0
    error: str = ""


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/solve", response_model=SolveResponse)
async def solve(req: SolveRequest) -> SolveResponse:
    from camoufox.async_api import AsyncCamoufox

    t0 = time.monotonic()
    async with _solver_lock:
        try:
            async with AsyncCamoufox(
                headless="virtual",
                geoip=False,
                humanize=True,
                exclude_addons=["UBO"],  # UBO blocks challenges.cloudflare.com
            ) as b:
                page = await b.new_page()

                # Optional pre-flight POST (e.g. the "I agree to the terms"
                # form that TorrentPier gates its register page behind).
                if req.pre_post is not None:
                    # Use the browser's own request API so cookies set here
                    # persist to the follow-up navigation.
                    await page.request.post(
                        req.pre_post.url,
                        form={k: v for k, v in _parse_form(req.pre_post.data)},
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )

                await page.goto(req.url, wait_until="load", timeout=req.max_timeout * 1000)

                if req.agree_click:
                    try:
                        await page.click(req.agree_click, timeout=10_000)
                        await page.wait_for_load_state("load", timeout=20_000)
                    except Exception as e:                               # noqa: BLE001
                        log.warning("agree_click %r failed: %s", req.agree_click, e)

                # Give the widget's async JS a few seconds to inject its iframe
                # inside the .cf-turnstile div.
                await page.wait_for_timeout(5000)

                # Click the widget div itself -- the iframe inside it has no
                # src attribute so an iframe-src selector misses it, but the
                # div's getBoundingClientRect is always reliable.
                rect = await page.evaluate(
                    f"""() => {{
                        const d = document.querySelector({req.widget_selector!r});
                        if (!d) return null;
                        const r = d.getBoundingClientRect();
                        return {{x: r.x, y: r.y, w: r.width, h: r.height}};
                    }}"""
                )
                if rect and rect["w"] > 0 and rect["h"] > 0:
                    x = rect["x"] + 30 + random.uniform(-2, 2)
                    y = rect["y"] + rect["h"] / 2 + random.uniform(-2, 2)
                    await page.mouse.move(x - 60, y - 20, steps=10)
                    await page.mouse.move(x, y, steps=6)
                    await page.mouse.click(x, y, delay=120)

                # Poll for the token. The widget populates the hidden input
                # asynchronously some seconds after it clears, not when its
                # iframe loads.
                deadline = time.monotonic() + req.max_timeout
                token = ""
                while time.monotonic() < deadline:
                    token = await page.evaluate(
                        "document.querySelector('input[name=\"cf-turnstile-response\"]')?.value || ''"
                    )
                    if token:
                        break
                    await page.wait_for_timeout(500)

                html = await page.content()
                cookies = await page.context.cookies()
                elapsed = time.monotonic() - t0
                if token:
                    log.info("solved in %.1fs (token len %d)", elapsed, len(token))
                    return SolveResponse(ok=True, token=token, cookies=cookies, html=html, elapsed=elapsed)
                log.warning("widget never populated after %.1fs", elapsed)
                return SolveResponse(ok=False, cookies=cookies, html=html, elapsed=elapsed,
                                     error="Turnstile widget never populated cf-turnstile-response")
        except Exception as e:                                           # noqa: BLE001
            log.exception("solve failed")
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e


def _parse_form(data: str) -> list[tuple[str, str]]:
    """urllib.parse.parse_qsl, but keeps everything strings and preserves order."""
    from urllib.parse import parse_qsl
    return parse_qsl(data, keep_blank_values=True)
