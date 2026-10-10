"""
GMX / web.de account factory.

Creates free @gmx.net / @web.de email accounts automatically using patchright.
These are legitimate German email providers (IONOS/1&1 group) with clean MX
servers not on any DNSBL, and whose domains are not on the disposable-email
blocklist.

Registration requires:
  - First name, last name
  - Desired login (username part of the address)
  - Password
  - Date of birth (day / month / year)
  - A simple image captcha (solved with ddddocr / Claude vision)
  - No phone number

Verified to work from a German IP (Deutsche Telekom AS3320).  From other
countries you may get a "bailout" redirect — try web.de first (it tends to be
more lenient than gmx.com).

IMAP access: imap.gmx.net:993 (SSL), also works for @web.de.
SMTP: mail.gmx.net:587 (STARTTLS) or :465 (SSL).

Usage
-----
from app.gmx import GmxFactory, gmx_find_activation

factory = GmxFactory(provider="web.de")      # or "gmx.net"
email, password = await factory.create()     # -> ("abc123@web.de", "Xk9f3w2q!mN5")

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

    Selector notes (verified 2025-10-11 from German IP):
      web.de and gmx.net both use a React/Angular SPA with dynamically generated
      class names.  The stable selectors are name= and data-testid= attributes.
    """

    _PROVIDERS = {
        "web.de": {
            "signup_url": "https://web.de/registrieren/",
            "domain": "web.de",
            "imap_host": "imap.web.de",
        },
        "gmx.net": {
            "signup_url": "https://www.gmx.net/mail/registrierung/",
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
        """Drive the registration form. Returns the created email address."""
        first = random.choice(_FIRST_NAMES)
        last = random.choice(_LAST_NAMES)
        dob_day = str(random.randint(1, 28))
        dob_month = str(random.randint(1, 12))
        dob_year = str(random.randint(1975, 2000))

        await page.goto(self._cfg["signup_url"], wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        # --- Dismiss cookie / consent dialog if present ---
        for consent_sel in [
            'button[data-testid="uc-accept-all-button"]',
            'button[data-action="accept"]',
            'button:has-text("Alle akzeptieren")',
            'button:has-text("Akzeptieren")',
            'button:has-text("Zustimmen")',
            'button:has-text("Agree")',
        ]:
            try:
                btn = await page.query_selector(consent_sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                pass

        # --- Wait for the registration form to appear ---
        # Both web.de and gmx.net load the form after the consent dialog
        await page.wait_for_timeout(2000)

        # --- Fill personal details ---
        await _fill_field(page, [
            'input[name="firstName"]', 'input[name="firstname"]',
            'input[id*="first"]', 'input[placeholder*="Vorname"]',
            'input[placeholder*="First"]',
        ], first)

        await _fill_field(page, [
            'input[name="lastName"]', 'input[name="lastname"]', 'input[name="surname"]',
            'input[id*="last"]', 'input[placeholder*="Nachname"]',
            'input[placeholder*="Last"]',
        ], last)

        # --- Desired email address ---
        await _fill_field(page, [
            'input[name="email"]', 'input[name="login"]', 'input[name="userName"]',
            'input[id*="email"]', 'input[placeholder*="@"]',
            'input[placeholder*="E-Mail"]', 'input[placeholder*="Wunsch"]',
        ], login)

        # --- Password ---
        pw_els = await page.query_selector_all('input[type="password"]')
        for pw_el in pw_els[:2]:
            try:
                await pw_el.triple_click()
                await pw_el.fill(password)
            except Exception:
                pass

        # --- Date of birth ---
        await _try_select(page, [
            'select[name="dayofbirth"]', 'select[name="day"]', 'select[id*="day"]',
        ], dob_day)

        await _try_select(page, [
            'select[name="monthofbirth"]', 'select[name="month"]', 'select[id*="month"]',
        ], dob_month)

        await _try_select(page, [
            'select[name="yearofbirth"]', 'select[name="year"]', 'select[id*="year"]',
        ], dob_year)

        # --- Accept terms (checkbox) ---
        for tos_sel in [
            'input[name="agb"]', 'input[name="terms"]', 'input[type="checkbox"]',
            'input[id*="agb"]', 'input[id*="terms"]',
        ]:
            try:
                cb = await page.query_selector(tos_sel)
                if cb and not await cb.is_checked():
                    await cb.click()
                    break
            except Exception:
                pass

        # --- CAPTCHA (image-based, solved by _solve_captcha) ---
        captcha_text = await self._solve_captcha(page)
        if captcha_text:
            await _fill_field(page, [
                'input[name="captcha"]', 'input[name="captchacode"]',
                'input[id*="captcha"]', 'input[placeholder*="aptcha"]',
            ], captcha_text, required=False)

        # --- Submit ---
        await _click_submit(page)
        await page.wait_for_timeout(4000)

        # --- Check outcome ---
        url = page.url
        if "inbox" in url or "mail" in url and "login" not in url:
            return f"{login}@{self._cfg['domain']}"

        # Look for captcha error → retry once with fresh captcha
        err_text = await _get_error(page)
        if err_text and ("captcha" in err_text.lower() or "code" in err_text.lower()):
            captcha_text = await self._solve_captcha(page)
            if captcha_text:
                await _fill_field(page, [
                    'input[name="captcha"]', 'input[name="captchacode"]',
                    'input[id*="captcha"]', 'input[placeholder*="aptcha"]',
                ], captcha_text, required=False)
                await _click_submit(page)
                await page.wait_for_timeout(4000)
                url = page.url
                err_text = await _get_error(page)

        # Final error check
        if err_text:
            raise GmxError(f"registration error: {err_text}")
        if "signup" in url or "registrier" in url or "error" in url:
            html_len = len(await page.content())
            raise GmxError(f"stuck on {url} after registration (page length {html_len})")

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

async def _fill_field(page, selectors: list[str], value: str, required: bool = True) -> None:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.triple_click()
                await el.fill(value)
                return
        except Exception:
            continue
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
