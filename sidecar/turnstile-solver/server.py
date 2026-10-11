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


# --------------------------------------------------------------------------- #
# Tuta (tuta.com) account factory
# --------------------------------------------------------------------------- #
#
# Not Turnstile-related, but lives here because this container already has
# camoufox + Xvfb running (see the pornolab-seeder CLAUDE.md reverse-engineering
# playbook: run the site's own code, don't hand-decode it). Verified live
# 2026-10-11: free tuta.com signup needs no phone number and no captcha --
# just a username/password/two checkboxes. Free accounts have NO IMAP/POP
# (Tuta is API-only by design for its end-to-end encryption model), so the
# activation-email lookup also has to go through the browser, not imaplib.

import random as _random
import string as _string


def _rand_login(n: int = 12) -> str:
    return "pl" + "".join(_random.choices(_string.ascii_lowercase + _string.digits, k=n))


def _rand_password(n: int = 16) -> str:
    """Letters + digits only. Symbols like @/#/$ sit at DIFFERENT physical
    keyboard positions across layouts (US vs. German etc.) -- camoufox's
    keyboard.type() simulates real keystrokes, so a password typed under one
    layout can come out as a different string than it looks like, producing
    "Invalid login credentials" on every later login even though the on-page
    input_value() showed the right characters the whole time (confirmed live
    2026-10-11: a password containing "@@" created an account that could
    never log back in). Alphanumeric-only sidesteps the whole class of bug.
    """
    chars = _string.ascii_letters + _string.digits
    pw = list(_random.choices(chars, k=n))
    pw[0] = _random.choice(_string.ascii_uppercase)
    pw[1] = _random.choice(_string.ascii_lowercase)
    pw[2] = _random.choice(_string.digits)
    pw[3] = _random.choice(_string.digits)
    _random.shuffle(pw)
    return "".join(pw)


class TutaCreateResponse(BaseModel):
    ok: bool
    email: str = ""
    password: str = ""
    error: str = ""


class TutaInboxRequest(BaseModel):
    email: str
    password: str
    sender_pattern: str = "pornolab"
    max_timeout: int = Field(default=60, ge=5, le=180)


class TutaInboxResponse(BaseModel):
    ok: bool
    url: str = ""
    error: str = ""


@app.post("/tuta/create", response_model=TutaCreateResponse)
async def tuta_create() -> TutaCreateResponse:
    """Create a fresh free @tutamail.com account. No phone, no captcha."""
    from camoufox.async_api import AsyncCamoufox

    login = _rand_login()
    password = _rand_password()

    async with _solver_lock:
        try:
            async with AsyncCamoufox(
                headless="virtual", geoip=False, humanize=True, exclude_addons=["UBO"],
            ) as b:
                page = await b.new_page()
                await page.goto(
                    "https://app.tuta.com/signup?websiteLang=en",
                    wait_until="load", timeout=30000,
                )
                await page.wait_for_timeout(4000)

                # Plan picker: click the "Free" plan tile, then Continue.
                free_box = page.locator("text=Free").first
                await free_box.click()
                await page.wait_for_timeout(500)
                await page.locator('button:has-text("Continue")').first.click()
                await page.wait_for_timeout(4000)

                # Account-details step: first non-password/checkbox input is
                # the username, both password inputs get the same value.
                inputs = await page.query_selector_all("input")
                user_el, pw_els = None, []
                for el in inputs:
                    t = await el.get_attribute("type")
                    if t == "password":
                        pw_els.append(el)
                    elif t not in ("checkbox", "radio"):
                        user_el = user_el or el
                if not user_el or len(pw_els) < 2:
                    html = await page.content()
                    return TutaCreateResponse(
                        ok=False, error=f"signup form fields not found (html len {len(html)})"
                    )

                await user_el.click()
                await page.keyboard.type(login, delay=50)
                for pw_el in pw_els:
                    await pw_el.click()
                    await page.keyboard.type(password, delay=40)

                for cb in await page.query_selector_all('input[type="checkbox"]'):
                    if not await cb.is_checked():
                        await cb.click()
                await page.wait_for_timeout(500)

                await page.locator('button:has-text("Create account")').first.click()
                await page.wait_for_timeout(6000)

                text = await page.evaluate("document.body.innerText")
                if "Recovery" not in text and "recovery" not in text:
                    return TutaCreateResponse(
                        ok=False, error=f"unexpected post-submit page: {text[:300]!r}"
                    )

                log.info("tuta account created: %s@tutamail.com", login)
                return TutaCreateResponse(ok=True, email=f"{login}@tutamail.com", password=password)
        except Exception as e:                                           # noqa: BLE001
            log.exception("tuta/create failed")
            return TutaCreateResponse(ok=False, error=f"{type(e).__name__}: {e}")


@app.post("/tuta/inbox", response_model=TutaInboxResponse)
async def tuta_inbox(req: TutaInboxRequest) -> TutaInboxResponse:
    """Log into the Tuta webmail UI and return the first matching email's
    activation link. No IMAP exists on the free tier, so this drives the
    browser instead of polling imaplib like the gmx/mail.ru factories do."""
    from camoufox.async_api import AsyncCamoufox

    async with _solver_lock:
        try:
            async with AsyncCamoufox(
                headless="virtual", geoip=False, humanize=True, exclude_addons=["UBO"],
            ) as b:
                page = await b.new_page()
                await page.goto("https://app.tuta.com/login", wait_until="load", timeout=30000)
                await page.wait_for_timeout(3000)

                inputs = await page.query_selector_all("input")
                user_el, pw_el = None, None
                for el in inputs:
                    t = await el.get_attribute("type")
                    if t == "password":
                        pw_el = pw_el or el
                    elif t not in ("checkbox", "radio"):
                        user_el = user_el or el
                if not user_el or not pw_el:
                    return TutaInboxResponse(ok=False, error="login form fields not found")

                await user_el.click()
                await page.keyboard.type(req.email, delay=40)
                await pw_el.click()
                await page.keyboard.type(req.password, delay=40)
                login_btn = page.locator('button:has-text("Log in")').first
                if await login_btn.count() == 0:
                    login_btn = page.locator('button:has-text("Login")').first
                await login_btn.click()
                await page.wait_for_timeout(7000)

                # Fail fast on bad credentials instead of silently polling an
                # empty inbox list until max_timeout (that masked a real bug
                # here on 2026-10-11 -- see _rand_password()'s docstring).
                # Check the body text unconditionally -- gating on page.url
                # containing "login" was unreliable (observed false negatives
                # live: the error text was present but this returned ok:true).
                body_text = await page.evaluate("document.body.innerText")
                log.info("tuta/inbox post-login state: url=%s body_head=%r",
                         page.url, body_text[:120])
                if "Invalid login" in body_text:
                    return TutaInboxResponse(ok=False, error="invalid Tuta login credentials")

                # Verified selectors (2026-10-11): rows are <li class="list-row">,
                # the open-email reading pane is <div class="mail-viewer">.
                deadline = time.monotonic() + req.max_timeout
                while time.monotonic() < deadline:
                    rows = await page.query_selector_all("li.list-row")
                    for row in rows:
                        row_text = (await row.inner_text()).lower()
                        if req.sender_pattern.lower() in row_text:
                            await row.click()
                            await page.wait_for_timeout(2000)
                            body = await page.evaluate(
                                "document.querySelector('.mail-viewer')?.innerText"
                                " || document.body.innerText"
                            )
                            import re as _re
                            for url in _re.findall(r"https?://[^\s\"'<>]+", body):
                                if "pornolab" in url and (
                                    "activate" in url or "profile" in url
                                ):
                                    return TutaInboxResponse(ok=True, url=url)
                    await page.wait_for_timeout(5000)
                    await page.reload(wait_until="load")
                    await page.wait_for_timeout(2000)

                return TutaInboxResponse(ok=False, error="no matching email within max_timeout")
        except Exception as e:                                           # noqa: BLE001
            log.exception("tuta/inbox failed")
            return TutaInboxResponse(ok=False, error=f"{type(e).__name__}: {e}")
