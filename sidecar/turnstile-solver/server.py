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
import string
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

def _rand_login(n: int = 12) -> str:
    return "pl" + "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


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
    chars = string.ascii_letters + string.digits
    pw = list(random.choices(chars, k=n))
    pw[0] = random.choice(string.ascii_uppercase)
    pw[1] = random.choice(string.ascii_lowercase)
    pw[2] = random.choice(string.digits)
    pw[3] = random.choice(string.digits)
    random.shuffle(pw)
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


class TutaListRequest(BaseModel):
    email: str
    password: str


class TutaListResponse(BaseModel):
    ok: bool
    messages: list[dict[str, str]] = []
    error: str = ""


@app.post("/tuta/list", response_model=TutaListResponse)
async def tuta_list(req: TutaListRequest) -> TutaListResponse:
    """One-shot inbox listing (subject + sender) for the owner to eyeball
    from the app's UI -- unlike /tuta/inbox this doesn't poll or wait, it
    just logs in once and reports whatever's there right now."""
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
                    return TutaListResponse(ok=False, error="login form fields not found")

                await user_el.click()
                await page.keyboard.type(req.email, delay=40)
                await pw_el.click()
                await page.keyboard.type(req.password, delay=40)
                login_btn = page.locator('button:has-text("Log in")').first
                if await login_btn.count() == 0:
                    login_btn = page.locator('button:has-text("Login")').first
                await login_btn.click()
                await page.wait_for_timeout(6000)

                body_text = await page.evaluate("document.body.innerText")
                if "Invalid login" in body_text:
                    return TutaListResponse(ok=False, error="invalid Tuta login credentials")

                rows = await page.query_selector_all("li.list-row")
                messages = []
                for row in rows[:30]:
                    text = (await row.inner_text()).strip()
                    lines = [ln for ln in text.split("\n") if ln.strip()]
                    sender = lines[0] if lines else ""
                    subject = lines[-1] if len(lines) > 1 else ""
                    messages.append({"from": sender, "subject": subject})
                return TutaListResponse(ok=True, messages=messages)
        except Exception as e:                                           # noqa: BLE001
            log.exception("tuta/list failed")
            return TutaListResponse(ok=False, error=f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# atomicmail.io account factory -- interactive hCaptcha relay
# --------------------------------------------------------------------------- #
#
# atomicmail.io's own signup flow (name -> username -> password -> seed
# phrase) is simple and phone-free, confirmed live 2026-10-11, but the final
# step is gated by hCaptcha in "invisible" mode -- unlike PornoLab's/Tuta's
# Cloudflare Turnstile, camoufox's humanize behaviour does NOT auto-clear it
# (confirmed: reCAPTCHA v2 checkboxes elsewhere on this host auto-pass with a
# single mouse click, this does not). hCaptcha also varies its challenge
# type per round (3x3 grid-click, "click the odd one out" on a single image,
# drag-a-shape) so there's no single click-pattern to automate; the only
# approach that generalises is handing the actual challenge screenshot to a
# human and relaying their click back into the live browser session.
#
# This keeps a camoufox browser alive in memory across HTTP calls (start,
# then one or more solve calls) -- deliberately NOT under the shared
# _solver_lock used by /solve and /tuta/*, since a human may take minutes to
# look at and click the image and that must not block the one-shot
# Turnstile/Tuta endpoints other parts of the app depend on.

import base64 as _b64
import secrets as _secrets

_atomicmail_lock = asyncio.Lock()           # at most one interactive session at a time
_atomicmail_sessions: dict[str, dict] = {}  # session_id -> state
_ATOMICMAIL_SESSION_TIMEOUT = 600            # 10 min idle -> cleaned up


def _rand_am_login(n: int = 12) -> str:
    return "pl" + "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _rand_am_password(n: int = 16) -> str:
    chars = string.ascii_letters + string.digits
    pw = list(random.choices(chars, k=n))
    pw[0] = random.choice(string.ascii_uppercase)
    pw[1] = random.choice(string.ascii_lowercase)
    pw[2] = random.choice(string.digits)
    pw[3] = "!"
    random.shuffle(pw)
    return "".join(pw) + "!"


async def _close_am_session(session_id: str) -> None:
    sess = _atomicmail_sessions.pop(session_id, None)
    if sess:
        try:
            await sess["browser"].close()
        except Exception:                                                # noqa: BLE001
            pass


async def _am_cleanup_stale() -> None:
    now = time.monotonic()
    stale = [sid for sid, s in _atomicmail_sessions.items()
             if now - s["last_used"] > _ATOMICMAIL_SESSION_TIMEOUT]
    for sid in stale:
        await _close_am_session(sid)


async def _am_capture_challenge(page) -> dict | None:
    """Find the hCaptcha challenge iframe, screenshot it, return
    {image_b64, prompt, box} or None if no challenge is currently showing."""
    iframe_el = None
    for el in await page.query_selector_all("iframe"):
        src = await el.get_attribute("src") or ""
        if "hcaptcha" in src and "frame=challenge" in src:
            iframe_el = el
            break
    if not iframe_el:
        return None
    box = await iframe_el.bounding_box()
    if not box or box["width"] <= 0:
        return None
    png = await page.screenshot(clip={"x": box["x"], "y": box["y"],
                                      "width": box["width"], "height": box["height"]})
    prompt = ""
    for f in page.frames:
        if "hcaptcha" in f.url and "frame=challenge" in f.url:
            try:
                prompt = (await f.evaluate(
                    "document.querySelector('[class*=\"prompt\"],.challenge-prompt-text')?.innerText || ''"
                )) or ""
            except Exception:                                            # noqa: BLE001
                pass
            break
    return {"image_b64": _b64.standard_b64encode(png).decode(), "prompt": prompt.strip(), "box": box}


class AtomicmailStartResponse(BaseModel):
    ok: bool
    session_id: str = ""
    image: str = ""
    prompt: str = ""
    email: str = ""
    password: str = ""
    solved_without_challenge: bool = False
    error: str = ""


@app.post("/atomicmail/signup/start", response_model=AtomicmailStartResponse)
async def atomicmail_signup_start() -> AtomicmailStartResponse:
    await _am_cleanup_stale()
    if _atomicmail_lock.locked():
        return AtomicmailStartResponse(ok=False, error="another interactive signup is already in progress")

    await _atomicmail_lock.acquire()
    login = _rand_am_login()
    password = _rand_am_password()
    try:
        from camoufox.async_api import AsyncCamoufox
        camoufox = AsyncCamoufox(headless="virtual", geoip=False, humanize=True, exclude_addons=["UBO"])
        browser = await camoufox.__aenter__()
        page = await browser.new_page()

        await page.goto("https://atomicmail.io/app/auth/sign-up", wait_until="load", timeout=30000)
        await page.wait_for_timeout(3000)
        await page.locator('input[name="firstName"]').first.click()
        await page.keyboard.type("Anna", delay=50)
        await page.wait_for_timeout(300)
        await page.locator('button:has-text("Submit")').first.click(force=True)
        await page.wait_for_timeout(3000)
        await page.locator('input[name="username"]').first.click()
        await page.keyboard.type(login, delay=50)
        await page.wait_for_timeout(500)
        await page.locator('button:has-text("Submit")').first.click(force=True)
        await page.wait_for_timeout(3000)
        await page.locator('input[name="password"]').first.click()
        await page.keyboard.type(password, delay=40)
        await page.locator('input[name="confirmPassword"]').first.click()
        await page.keyboard.type(password, delay=40)
        await page.wait_for_timeout(500)
        await page.locator('button:has-text("Submit")').first.click(force=True)
        await page.wait_for_timeout(4000)
        await page.locator('button:has-text("Download & Proceed")').first.click(force=True)
        await page.wait_for_timeout(5000)

        text = await page.evaluate("document.body.innerText")
        if "Save your Seed Phrase" not in text:
            # Unexpectedly resolved without a challenge (or failed outright)
            await browser.__aexit__(None, None, None)
            _atomicmail_lock.release()
            ok = "atomicmail.io" in page.url or "success" in text.lower()
            return AtomicmailStartResponse(ok=ok, email=f"{login}@atomicmail.io", password=password,
                                           solved_without_challenge=True,
                                           error="" if ok else "unexpected state after signup")

        challenge = await _am_capture_challenge(page)
        if not challenge:
            await browser.__aexit__(None, None, None)
            _atomicmail_lock.release()
            return AtomicmailStartResponse(ok=False, error="no hCaptcha challenge found to solve")

        session_id = _secrets.token_urlsafe(16)
        _atomicmail_sessions[session_id] = {
            "camoufox": camoufox, "browser": browser, "page": page,
            "login": login, "password": password, "box": challenge["box"],
            "last_used": time.monotonic(), "rounds": 0,
        }
        _atomicmail_lock.release()
        return AtomicmailStartResponse(
            ok=True, session_id=session_id, image=challenge["image_b64"],
            prompt=challenge["prompt"], email=f"{login}@atomicmail.io", password=password,
        )
    except Exception as e:                                               # noqa: BLE001
        log.exception("atomicmail/signup/start failed")
        try:
            _atomicmail_lock.release()
        except RuntimeError:
            pass
        return AtomicmailStartResponse(ok=False, error=f"{type(e).__name__}: {e}")


class AtomicmailClickRequest(BaseModel):
    session_id: str
    x: float = Field(..., ge=0.0, le=1.0, description="relative x within the challenge image, 0-1")
    y: float = Field(..., ge=0.0, le=1.0, description="relative y within the challenge image, 0-1")


class AtomicmailClickResponse(BaseModel):
    ok: bool
    error: str = ""


@app.post("/atomicmail/signup/click", response_model=AtomicmailClickResponse)
async def atomicmail_signup_click(req: AtomicmailClickRequest) -> AtomicmailClickResponse:
    """Register one click at a relative position within the challenge image
    the owner is looking at -- for multi-click challenges (3x3 grids), call
    this once per cell, then call /verify."""
    sess = _atomicmail_sessions.get(req.session_id)
    if not sess:
        return AtomicmailClickResponse(ok=False, error="unknown or expired session")
    sess["last_used"] = time.monotonic()
    page = sess["page"]
    box = sess["box"]
    x = box["x"] + req.x * box["width"]
    y = box["y"] + req.y * box["height"]
    try:
        await page.mouse.click(x, y, delay=90)
        await page.wait_for_timeout(400)
        return AtomicmailClickResponse(ok=True)
    except Exception as e:                                               # noqa: BLE001
        return AtomicmailClickResponse(ok=False, error=f"{type(e).__name__}: {e}")


class AtomicmailVerifyRequest(BaseModel):
    session_id: str


class AtomicmailVerifyResponse(BaseModel):
    ok: bool
    done: bool = False          # true once fully resolved (success or permanent failure)
    solved: bool = False        # true if signup completed successfully
    image: str = ""             # a new challenge, if another round is needed
    prompt: str = ""
    email: str = ""
    password: str = ""
    error: str = ""


@app.post("/atomicmail/signup/verify", response_model=AtomicmailVerifyResponse)
async def atomicmail_signup_verify(req: AtomicmailVerifyRequest) -> AtomicmailVerifyResponse:
    """After one or more /click calls select the right spot(s), call this to
    submit hCaptcha's own verify control and see what happened: solved
    (signup completes and the session closes), another round (a fresh image
    comes back, call /click again), or still unsolved (try again)."""
    sess = _atomicmail_sessions.get(req.session_id)
    if not sess:
        return AtomicmailVerifyResponse(ok=False, error="unknown or expired session")
    sess["last_used"] = time.monotonic()
    page = sess["page"]
    try:
        # hCaptcha's own submit control inside the challenge frame -- try a
        # few likely selectors, falling back to no-op (some challenge types
        # auto-advance once enough correct cells are clicked).
        for f in page.frames:
            if "hcaptcha" in f.url and "frame=challenge" in f.url:
                for sel in ['.button-submit', '[class*="submit"]', 'button[type="submit"]']:
                    try:
                        btn = f.locator(sel).first
                        if await btn.count() and await btn.is_visible():
                            await btn.click(force=True)
                            break
                    except Exception:                                     # noqa: BLE001
                        pass
                break
        await page.wait_for_timeout(2500)

        text = await page.evaluate("document.body.innerText")
        if "Save your Seed Phrase" not in text:
            # Moved past the seed-phrase screen -- signup is done.
            email = f"{sess['login']}@atomicmail.io"
            password = sess["password"]
            await _close_am_session(req.session_id)
            return AtomicmailVerifyResponse(ok=True, done=True, solved=True, email=email, password=password)

        sess["rounds"] += 1
        if sess["rounds"] >= 8:
            await _close_am_session(req.session_id)
            return AtomicmailVerifyResponse(ok=False, done=True, error="too many captcha rounds, giving up")

        challenge = await _am_capture_challenge(page)
        if not challenge:
            # Challenge widget vanished without the seed screen clearing --
            # ambiguous; report back without closing so the owner can retry.
            return AtomicmailVerifyResponse(ok=True, done=False,
                                            error="no visible challenge right now -- try /verify again shortly")
        sess["box"] = challenge["box"]
        return AtomicmailVerifyResponse(ok=True, done=False, image=challenge["image_b64"],
                                        prompt=challenge["prompt"])
    except Exception as e:                                               # noqa: BLE001
        log.exception("atomicmail/signup/verify failed")
        await _close_am_session(req.session_id)
        return AtomicmailVerifyResponse(ok=False, done=True, error=f"{type(e).__name__}: {e}")


class AtomicmailCancelRequest(BaseModel):
    session_id: str


@app.post("/atomicmail/signup/cancel")
async def atomicmail_signup_cancel(req: AtomicmailCancelRequest) -> dict:
    await _close_am_session(req.session_id)
    return {"ok": True}
