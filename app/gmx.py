"""
GMX / web.de account factory.

DEAD END (2026-10-11): web.de registration REQUIRES a mobile phone number.
----------------------------------------------------------------------------
The full flow was mapped end to end in camoufox and the last step is a hard
phone gate.  Do not spend time here again unless web.de changes the flow.

What was learned (all verified live from a German IP, Deutsche Telekom AS3320):

  * Signup URL is https://register.web.de/ — NOT https://web.de/registrieren/
    (that 404s).

  * register.web.de bounces to hilfe.web.de/kundenservice/flexreg.html unless
    the browser already holds web.de consent cookies.  Load https://web.de/
    first, click "Akzeptieren und weiter", THEN navigate to register.web.de.

  * The gate on step 1 is CaptchaFox (div#captchafox, response field
    textarea[name="cf-captcha-response"]) — NOT Cloudflare Turnstile, despite
    the "cf-" prefix.  The turnstile-solver sidecar does not apply.

  * patchright (even headful under Xvfb) never clears CaptchaFox: submitting
    gives "Bitte löse das Captcha."  camoufox DOES clear it, usually silently
    via `humanize` without ever populating the response field, but it is flaky
    — budget ~2 attempts, clicking the widget between tries.

  * Form is a multi-step React wizard.  Fields must be filled by clicking each
    one and typing (keyboard.type); .fill() does not fire React's onChange and
    Tab-order skips fields.  Steps:
        1. firstName, lastName, birthDay, birthMonth, birthYear   [CaptchaFox]
        2. gender (radio FEMALE/MALE/OTHER), postalCode, city, street
           (city autofills from postalCode)
        3. password, confirmPassword; the address itself is assigned by web.de
           and held in a hidden readonly input[name="username"]
           (e.g. "sophie.schneider1992@web.de")
        4+. mobilePhone + mobilePhonePrefix  <-- HARD REQUIREMENT, NOT SKIPPABLE
            Only back/next buttons and footer links exist on that step; an
            empty submit returns "Please fill in all required fields."

gmx.net is the same IONOS/1&1 backend and is assumed to gate identically.

IMAP (kept for whenever an account exists by other means):
  imap.web.de:993 / imap.gmx.net:993 (SSL)
  SMTP: mail.gmx.net:587 (STARTTLS) or :465 (SSL)

Usage
-----
from app.gmx import GmxFactory, gmx_find_activation

factory = GmxFactory(provider="web.de")      # or "gmx.net"
email, password = await factory.create()     # raises at the phone step

url = await gmx_find_activation(email, password, timeout=300)
"""

import asyncio
import imaplib
import os
import random
import re
import string
import time
import email as _email_module
from email.header import decode_header


class GmxError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _rand_login(n: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _rand_password(n: int = 14) -> str:
    chars = string.ascii_letters + string.digits + "!@#$"
    pw = "".join(random.choices(chars, k=n))
    # Ensure at least one upper, lower, digit, special
    pw = list(pw)
    pw[0] = random.choice(string.ascii_uppercase)
    pw[1] = random.choice(string.ascii_lowercase)
    pw[2] = random.choice(string.digits)
    pw[3] = random.choice("!@#$")
    random.shuffle(pw)
    return "".join(pw)


_FIRST_NAMES = ["Anna", "Marie", "Sophie", "Laura", "Julia", "Jan", "Tom", "Max", "Felix", "Paul"]
_LAST_NAMES  = ["Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker"]


# --------------------------------------------------------------------------- #
# Account factory
# --------------------------------------------------------------------------- #

class GmxFactory:
    """
    Creates GMX or web.de accounts via browser automation.

    Providers:
      "web.de"  → @web.de    (signup at https://web.de/registrieren/)
      "gmx.net" → @gmx.net   (signup at https://www.gmx.net/mail/registrierung/)

    Both providers share the same IONOS/1&1 backend; only the UI skin differs.

    Selector notes (verified 2026-10-11 from German IP):
      Both use a multi-step React SPA.  Step 1: firstName/lastName/birthDay/
      birthMonth/birthYear (text inputs), Next button (data-testid="next-button").
      Step 2+: email alias, password, terms.  Stable selectors are name= and
      data-testid= attrs; fill with keyboard.type() not fill() so React onChange fires.
    """

    _PROVIDERS = {
        "web.de": {
            "signup_url": "https://register.web.de/",
            "domain": "web.de",
            "imap_host": "imap.web.de",
        },
        "gmx.net": {
            "signup_url": "https://register.gmx.net/",
            "domain": "gmx.net",
            "imap_host": "imap.gmx.net",
        },
    }

    _LAUNCH_ARGS = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--enable-unsafe-swiftshader",
        "--use-gl=angle",
        "--use-angle=swiftshader",
        "--disable-dev-shm-usage",
    ]

    def __init__(self, provider: str = "web.de"):
        if provider not in self._PROVIDERS:
            raise GmxError(f"unknown provider {provider!r}; choose from {list(self._PROVIDERS)}")
        self._cfg = self._PROVIDERS[provider]

    async def create(self) -> tuple[str, str]:
        """Create a new account. Returns (full_email, password)."""
        login = _rand_login()
        password = _rand_password()
        email = await self._register_in_browser(login=login, password=password)
        return email, password

    async def _register_in_browser(self, login: str, password: str) -> str:
        from patchright.async_api import async_playwright
        from pyvirtualdisplay import Display

        display = Display(visible=0, size=(1920, 1080))
        display.start()
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=False, args=self._LAUNCH_ARGS)
                ctx = await browser.new_context(
                    viewport={"width": 1366, "height": 768},
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
                    ),
                    locale="de-DE",
                    timezone_id="Europe/Berlin",
                )
                page = await ctx.new_page()
                try:
                    return await self._fill_form(page, login=login, password=password)
                finally:
                    await browser.close()
        finally:
            display.stop()

    async def _fill_form(self, page, *, login: str, password: str) -> str:
        """Drive the multi-step registration form. Returns the created email address."""
        first = random.choice(_FIRST_NAMES)
        last = random.choice(_LAST_NAMES)
        dob_day = str(random.randint(1, 28))
        dob_month = str(random.randint(1, 12))
        dob_year = str(random.randint(1975, 2000))

        await page.goto(self._cfg["signup_url"], wait_until="domcontentloaded", timeout=30000)

        # --- Step 1: name + date of birth ---
        # DOB fields are <input type="text"> (not selects) — use React-compatible typing.
        await page.wait_for_selector('input[name="firstName"]', timeout=20000)
        await _react_type(page, 'input[name="firstName"]', first)
        await _react_type(page, 'input[name="lastName"]', last)
        await _react_type(page, 'input[name="birthDay"]', dob_day)
        await _react_type(page, 'input[name="birthMonth"]', dob_month)
        await _react_type(page, 'input[name="birthYear"]', dob_year)

        # Wait for validation to enable the Next button, then click it.
        await page.wait_for_selector(
            'button[data-testid="next-button"]:not([disabled])', timeout=10000
        )
        await page.click('button[data-testid="next-button"]')
        await page.wait_for_timeout(3000)

        # --- Step 2+: email address / password / terms ---
        # Fill email login (desired address prefix)
        await _react_type(page, 'input[name="emailAlias"]', login, required=False)
        await _react_type(page, 'input[name="login"]', login, required=False)

        # Password fields
        pw_els = await page.query_selector_all('input[type="password"]')
        for pw_el in pw_els[:2]:
            try:
                if await pw_el.is_visible():
                    await pw_el.click()
                    await pw_el.select_text()
                    await page.keyboard.type(password, delay=40)
            except Exception:
                pass

        # Accept terms checkbox if present
        for tos_sel in [
            'input[name="agb"]', 'input[name="terms"]',
            'input[type="checkbox"]', 'input[id*="agb"]',
        ]:
            try:
                cb = await page.query_selector(tos_sel)
                if cb and await cb.is_visible() and not await cb.is_checked():
                    await cb.click()
                    break
            except Exception:
                pass

        # Click next/submit if there's another step
        try:
            btn = await page.query_selector(
                'button[data-testid="next-button"]:not([disabled])'
            )
            if btn and await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(3000)
        except Exception:
            pass

        # --- Captcha (if any) ---
        captcha_text = await self._solve_captcha(page)
        if captcha_text:
            await _react_type(page, 'input[name="captcha"]', captcha_text, required=False)
            await _react_type(page, 'input[id*="captcha"]', captcha_text, required=False)

        # --- Final submit ---
        await _click_submit(page)
        await page.wait_for_timeout(5000)

        # --- Check outcome ---
        url = page.url
        err_text = await _get_error(page)

        if err_text and ("captcha" in err_text.lower() or "code" in err_text.lower()):
            captcha_text = await self._solve_captcha(page)
            if captcha_text:
                await _react_type(page, 'input[name="captcha"]', captcha_text, required=False)
                await _react_type(page, 'input[id*="captcha"]', captcha_text, required=False)
                await _click_submit(page)
                await page.wait_for_timeout(4000)
                url = page.url
                err_text = await _get_error(page)

        if err_text:
            raise GmxError(f"registration error: {err_text}")
        if "register" in url and "confirm" not in url and "success" not in url:
            raise GmxError(f"stuck on {url} after registration")

        return f"{login}@{self._cfg['domain']}"

    async def _solve_captcha(self, page) -> str | None:
        """Return the captcha text from the page, or None if no captcha found."""
        # Find a captcha image on the page
        for img_sel in [
            'img[src*="captcha"]', 'img[id*="captcha"]', 'img[alt*="aptcha"]',
            'img[src*="code"]',
        ]:
            try:
                img_el = await page.query_selector(img_sel)
                if not img_el:
                    continue
                # Screenshot just the captcha element
                img_bytes = await img_el.screenshot()
                if not img_bytes:
                    continue
                return await _solve_captcha_bytes(img_bytes)
            except Exception:
                continue
        return None


# --------------------------------------------------------------------------- #
# Captcha solver (same strategy as pornolab.py but imported standalone)
# --------------------------------------------------------------------------- #

async def _solve_captcha_bytes(image_bytes: bytes) -> str:
    """Solve an image captcha. Uses Claude Haiku if ANTHROPIC_API_KEY is set,
    falls back to ddddocr."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        try:
            return await asyncio.to_thread(_solve_claude, image_bytes, api_key)
        except Exception:
            pass
    return await asyncio.to_thread(_solve_ocr, image_bytes)


def _solve_claude(image_bytes: bytes, api_key: str) -> str:
    import base64
    import urllib.request, urllib.error
    import json

    b64 = base64.standard_b64encode(image_bytes).decode()
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 20,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": "Return ONLY the captcha text visible in this image, nothing else."},
            ],
        }],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return data["content"][0]["text"].strip()


def _solve_ocr(image_bytes: bytes) -> str:
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(show_ad=False)
        ocr_beta = ddddocr.DdddOcr(beta=True, show_ad=False)
        r1 = ocr.classification(image_bytes)
        r2 = ocr_beta.classification(image_bytes)
        return max(r1, r2, key=len)
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Form helpers
# --------------------------------------------------------------------------- #

async def _react_type(page, selector: str, value: str, required: bool = True) -> bool:
    """Type into a React-controlled input so onChange fires properly."""
    try:
        el = await page.query_selector(selector)
        if el and await el.is_visible():
            await el.click()
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            await page.keyboard.type(str(value), delay=40)
            return True
    except Exception:
        pass
    if required:
        raise GmxError(f"field not found: {selector!r}")
    return False


async def _fill_field(page, selectors: list[str], value: str, required: bool = True) -> None:
    for sel in selectors:
        if await _react_type(page, sel, value, required=False):
            return
    if required:
        raise GmxError(f"field not found: {selectors[:2]}")


async def _try_select(page, selectors: list[str], value: str) -> None:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.select_option(value=value)
                return
        except Exception:
            continue


async def _click_submit(page) -> None:
    for sel in [
        'button[type="submit"]',
        'button:has-text("Registrieren")',
        'button:has-text("Konto erstellen")',
        'button:has-text("Create account")',
        'input[type="submit"]',
    ]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                return
        except Exception:
            continue
    raise GmxError("submit button not found")


async def _get_error(page) -> str | None:
    for sel in [
        '.error-msg', '.error-message', '[class*="error"]',
        '[role="alert"]', '.form-error', '[data-testid*="error"]',
    ]:
        try:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# IMAP inbox reader — find PornoLab activation link
# --------------------------------------------------------------------------- #

_IMAP_MAP = {
    "web.de":  ("imap.web.de",  993),
    "gmx.net": ("imap.gmx.net", 993),
    "gmx.de":  ("imap.gmx.net", 993),
}


async def gmx_find_activation(
    email: str,
    password: str,
    timeout: int = 300,
    poll_interval: int = 10,
    sender_pattern: str = "pornolab",
) -> str | None:
    """Poll inbox until a PornoLab activation email arrives."""
    domain = email.split("@", 1)[-1].lower()
    host, port = _IMAP_MAP.get(domain, ("imap.gmx.net", 993))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = await asyncio.to_thread(_imap_find_link, host, port, email, password, sender_pattern)
        if url:
            return url
        remaining = deadline - time.monotonic()
        await asyncio.sleep(min(poll_interval, max(0, remaining)))
    return None


def _imap_find_link(host: str, port: int, email: str, password: str, sender_pattern: str) -> str | None:
    try:
        with imaplib.IMAP4_SSL(host, port) as imap:
            imap.login(email, password)
            imap.select("INBOX")
            _, data = imap.search(None, "ALL")
            for msg_id in reversed(data[0].split()[-20:]):
                _, msg_data = imap.fetch(msg_id, "(RFC822)")
                raw = msg_data[0][1]
                msg = _email_module.message_from_bytes(raw)
                from_hdr = msg.get("From", "").lower()
                subj = _decode_hdr(msg.get("Subject", "")).lower()
                if sender_pattern.lower() not in from_hdr and sender_pattern.lower() not in subj:
                    continue
                body = _body_text(msg)
                for url in re.findall(r"https?://[^\s\"'<>]+", body):
                    if "pornolab" in url and ("activate" in url or "profile" in url):
                        return url
    except Exception:
        pass
    return None


def _decode_hdr(hdr: str) -> str:
    parts = decode_header(hdr)
    out = []
    for b, enc in parts:
        out.append(b.decode(enc or "utf-8", errors="replace") if isinstance(b, bytes) else b)
    return "".join(out)


def _body_text(msg) -> str:
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                if payload:
                    parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            parts.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)
