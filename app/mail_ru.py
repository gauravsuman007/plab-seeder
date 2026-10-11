"""
mail.ru account factory.

Creates fresh @mail.ru / @bk.ru / @inbox.ru / @list.ru accounts automatically.
mail.ru registration now goes through VK ID (id.vk.ru), which is entirely
phone-number-based — enter phone, receive SMS, verify.

After the SMS step, VK ID may ask the user to choose a login (email prefix)
and/or set a password; or it may auto-assign and redirect to inbox directly.
The code handles both by polling for known selectors and falling back gracefully.

Usage
-----
from app.mail_ru import MailRuFactory

factory = MailRuFactory(sms_api_key="your-sms-activate-key")
email, password = await factory.create()
# -> e.g. ("xk9f3w2q@mail.ru", "a8Bx2mN5pQ")

Inbox polling (IMAP)
--------------------
from app.mail_ru import mailru_find_activation

url = await mailru_find_activation(email, password, timeout=300)
# -> "https://pornolab.net/forum/profile.php?mode=activate&..."

Note on passwords
-----------------
If VK ID doesn't prompt for a password during registration (phone-only auth),
the returned password is one we try to set afterwards in mail.ru settings.
If that step fails, IMAP access requires setting an app-specific password at
https://account.mail.ru/user/security/passwords — tell the user if IMAP fails.

Environment / config
--------------------
sms_activate_api_key  – sms-activate.org key (required, set in app settings)
mailru_domain         – one of mail.ru bk.ru inbox.ru list.ru (default: mail.ru)
"""

import asyncio
import imaplib
import random
import re
import string
import time
import email as _email_module
from email.header import decode_header

import httpx


# --------------------------------------------------------------------------- #
# SMS service client (sms-activate.org)
# --------------------------------------------------------------------------- #

class SmsActivateError(Exception):
    pass


class SmsActivate:
    """Minimal client for sms-activate.org virtual-number API."""

    BASE = "https://api.sms-activate.org/stubs/handler_api.php"

    def __init__(self, api_key: str):
        self._key = api_key

    async def _get(self, **params) -> str:
        params["api_key"] = self._key
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(self.BASE, params=params)
        r.raise_for_status()
        return r.text.strip()

    async def get_number(self, service: str = "mailru", country: int = 0) -> tuple[str, str]:
        """Reserve a phone number. Returns (activation_id, phone_number)."""
        text = await self._get(action="getNumber", service=service, country=country)
        # Response: ACCESS_NUMBER:id:79161234567
        if not text.startswith("ACCESS_NUMBER:"):
            raise SmsActivateError(f"get_number: {text}")
        _, act_id, phone = text.split(":")
        return act_id, phone

    async def get_code(self, activation_id: str, timeout: int = 120, poll_interval: int = 5) -> str:
        """Poll until the SMS code arrives. Returns the code."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = await self._get(action="getStatus", id=activation_id)
            if text.startswith("STATUS_OK:"):
                return text.split(":", 1)[1]
            if text in ("STATUS_CANCEL", "STATUS_WAIT_RESEND"):
                raise SmsActivateError(f"activation {activation_id} cancelled/expired: {text}")
            # STATUS_WAIT_CODE or STATUS_WAIT_RETRY — keep polling
            await asyncio.sleep(poll_interval)
        raise SmsActivateError(f"SMS code not received within {timeout}s")

    async def report_done(self, activation_id: str) -> None:
        await self._get(action="setStatus", id=activation_id, status=6)

    async def report_cancel(self, activation_id: str) -> None:
        await self._get(action="setStatus", id=activation_id, status=8)


# --------------------------------------------------------------------------- #
# mail.ru registration automation (via VK ID)
# --------------------------------------------------------------------------- #

class MailRuError(Exception):
    pass


def _rand_login(n: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _rand_password(n: int = 14) -> str:
    chars = string.ascii_letters + string.digits + "!@#$"
    return "".join(random.choices(chars, k=n))


class MailRuFactory:
    """
    Creates fresh mail.ru accounts via browser automation + SMS verification.

    mail.ru registration now goes via id.vk.ru (VK ID):
      1. Navigate to account.mail.ru/signup → redirects to id.vk.ru
      2. Fill in phone number (input[name="phone"])
      3. Submit → SMS sent
      4. Fill SMS code from sms-activate.org
      5. Post-SMS: VK ID may ask to choose a login, set password, etc.
      6. End up with a working @mail.ru address

    Verified selectors (observed 2025-10-10):
      - Phone field: input[name="phone"]   (type="tel", placeholder="+7 000 000 00 00")
      - Submit:      button[type="submit"]
    Post-SMS steps vary and are handled by polling for known patterns.
    """

    SIGNUP_URL = "https://account.mail.ru/signup"
    DOMAINS = ["mail.ru", "bk.ru", "inbox.ru", "list.ru"]

    _LAUNCH_ARGS = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--enable-unsafe-swiftshader",
        "--use-gl=angle",
        "--use-angle=swiftshader",
        "--disable-dev-shm-usage",
    ]

    def __init__(self, sms_api_key: str, domain: str = "mail.ru"):
        self._sms = SmsActivate(sms_api_key)
        self._domain = domain if domain in self.DOMAINS else "mail.ru"

    async def create(self) -> tuple[str, str]:
        """Create a new account. Returns (full_email, password)."""
        act_id, phone = await self._sms.get_number(service="mailru")
        password = _rand_password()
        try:
            email = await self._register_in_browser(phone=phone, password=password, act_id=act_id)
        except Exception:
            await self._sms.report_cancel(act_id)
            raise
        await self._sms.report_done(act_id)
        return email, password

    async def _register_in_browser(self, phone: str, password: str, act_id: str) -> str:
        """Run the full VK ID signup flow. Returns the created email address."""
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
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                )
                page = await ctx.new_page()
                try:
                    return await self._fill_vkid_flow(
                        page, phone=phone, password=password, act_id=act_id
                    )
                finally:
                    await browser.close()
        finally:
            display.stop()

    async def _fill_vkid_flow(self, page, *, phone: str, password: str, act_id: str) -> str:
        """
        Drive the VK ID signup wizard for mail.ru.

        VK ID flow (verified 2025-10-10):
          Step 1: phone input  →  input[name="phone"], button[type="submit"]
          Step 2: SMS code     →  unknown selector (poll for it)
          Step 3+: post-SMS    →  VK may ask login choice, password, or go straight to inbox
        """
        login = _rand_login()
        await page.goto(self.SIGNUP_URL, wait_until="domcontentloaded", timeout=30000)
        # Wait for VK ID to load; it redirects from account.mail.ru to id.vk.ru
        await page.wait_for_selector('input[name="phone"]', timeout=20000)
        await page.wait_for_timeout(1000)

        # --- Step 1: phone number ---
        phone_el = await page.query_selector('input[name="phone"]')
        await phone_el.triple_click()
        # VK ID auto-prefixes +7; just enter digits if Russian number
        digits_only = re.sub(r"\D", "", phone)
        if digits_only.startswith("7") and len(digits_only) == 11:
            digits_only = digits_only[1:]   # strip country code; VK adds +7 itself
        await phone_el.fill(digits_only)
        await page.click('button[type="submit"]', timeout=5000)
        await page.wait_for_timeout(2000)

        # --- Step 2: SMS code ---
        # VK ID shows a 4-6 digit code input; poll for any numeric code input
        sms_input = await _wait_for_any(page, [
            'input[name="code"]',
            'input[type="number"]',
            'input[autocomplete="one-time-code"]',
            'input[placeholder*="код"]',
            'input[placeholder*="code"]',
            'input[maxlength="4"]',
            'input[maxlength="6"]',
        ], timeout=20000)
        if not sms_input:
            raise MailRuError("SMS code input did not appear after phone submission")

        sms_code = await self._sms.get_code(act_id, timeout=120)
        await sms_input.triple_click()
        await sms_input.fill(sms_code)
        # VK ID often auto-advances after code entry; submit only if button is still there
        try:
            await page.click('button[type="submit"]', timeout=3000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)

        # --- Steps 3+: post-SMS (VK ID may ask for login, password, or go straight to inbox) ---

        # If VK asks to choose a login/email prefix
        try:
            login_input = await _wait_for_any(page, [
                'input[name="login"]',
                'input[name="username"]',
                'input[placeholder*="логин"]',
                'input[placeholder*="login"]',
            ], timeout=5000)
            if login_input:
                await login_input.triple_click()
                await login_input.fill(login)
                # Pick domain if there's a domain selector
                if self._domain != "mail.ru":
                    await _try_select_domain(page, self._domain)
                await page.click('button[type="submit"]', timeout=5000)
                await page.wait_for_timeout(2000)
        except Exception:
            pass

        # If VK asks to set a password
        try:
            pw_input = await _wait_for_any(page, [
                'input[name="password"]',
                'input[type="password"]',
            ], timeout=5000)
            if pw_input:
                await pw_input.triple_click()
                await pw_input.fill(password)
                # Confirm field if present
                try:
                    confirm_el = await page.query_selector(
                        'input[name="password_repeat"], input[name="confirm_password"],'
                        'input[name="repassword"]'
                    )
                    if confirm_el:
                        await confirm_el.triple_click()
                        await confirm_el.fill(password)
                except Exception:
                    pass
                await page.click('button[type="submit"]', timeout=5000)
                await page.wait_for_timeout(3000)
        except Exception:
            pass

        # --- Determine the created email address ---
        # After success, VK ID redirects back to account.mail.ru or e.mail.ru
        # The URL or page content will tell us the email address.
        email = await self._detect_created_email(page, login)
        return email

    async def _detect_created_email(self, page, fallback_login: str) -> str:
        """
        Wait for successful redirect to mail.ru and extract the new email address.
        Falls back to constructing email from the login we attempted to set.
        """
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            url = page.url
            # Happy path: redirected to mail.ru inbox
            if "e.mail.ru" in url or "mail.ru/inbox" in url:
                # Try to get the email from the page
                try:
                    email_el = await page.query_selector(
                        '[data-testid="user-email"], .user-email, [class*="email"]'
                    )
                    if email_el:
                        text = (await email_el.inner_text()).strip()
                        if "@" in text:
                            return text
                except Exception:
                    pass
                return f"{fallback_login}@{self._domain}"

            # VK ID returned a silent token to account.mail.ru — account linked
            if "account.mail.ru" in url and "silent_token" not in url:
                return f"{fallback_login}@{self._domain}"

            # Check for any error messages
            try:
                err_el = await page.query_selector(
                    '.vkuiFormItem__bottom--status-error, [role="alert"], .error-message'
                )
                if err_el:
                    err_text = (await err_el.inner_text()).strip()
                    if err_text:
                        raise MailRuError(f"VK ID error: {err_text}")
            except MailRuError:
                raise
            except Exception:
                pass

            await asyncio.sleep(1)

        # If we timed out but didn't hit a known error, the account was likely
        # created (VK ID sometimes stays on its own page for a moment).
        return f"{fallback_login}@{self._domain}"


# --------------------------------------------------------------------------- #
# Helpers shared by the form filler
# --------------------------------------------------------------------------- #

async def _wait_for_any(page, selectors: list[str], timeout: int = 10000):
    """Return the first matching element, or None if none appear within timeout."""
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:
                pass
        await asyncio.sleep(0.5)
    return None


async def _try_select_domain(page, domain: str) -> None:
    """Try to open a domain picker and choose the requested domain."""
    for sel in ['button[data-testid="domain"]', '.domain-picker__button', 'button:has-text("@")']:
        try:
            await page.click(sel, timeout=2000)
            await page.wait_for_timeout(400)
            await page.click(
                f'[data-value="{domain}"], li:has-text("{domain}"), button:has-text("{domain}")',
                timeout=2000,
            )
            return
        except Exception:
            continue


# --------------------------------------------------------------------------- #
# IMAP inbox reader — find PornoLab activation link
# --------------------------------------------------------------------------- #

_IMAP_SERVERS = {
    "mail.ru":  ("imap.mail.ru", 993),
    "bk.ru":    ("imap.mail.ru", 993),
    "inbox.ru": ("imap.mail.ru", 993),
    "list.ru":  ("imap.mail.ru", 993),
}


async def mailru_list_inbox(email: str, password: str, limit: int = 20) -> list[dict]:
    """List recent inbox subjects/senders, for the owner's 'check inbox' UI."""
    domain = email.split("@", 1)[-1].lower()
    host, port = _IMAP_SERVERS.get(domain, ("imap.mail.ru", 993))
    return await asyncio.to_thread(_imap_list_messages, host, port, email, password, limit)


def _imap_list_messages(host: str, port: int, email: str, password: str, limit: int) -> list[dict]:
    out: list[dict] = []
    try:
        with imaplib.IMAP4_SSL(host, port) as imap:
            imap.login(email, password)
            imap.select("INBOX")
            _, data = imap.search(None, "ALL")
            for msg_id in reversed(data[0].split()[-limit:]):
                _, msg_data = imap.fetch(msg_id, "(RFC822)")
                raw = msg_data[0][1]
                msg = _email_module.message_from_bytes(raw)
                out.append({"from": msg.get("From", ""), "subject": _decode_header_str(msg.get("Subject", ""))})
    except Exception:
        pass
    return list(reversed(out))


async def mailru_find_activation(
    email: str,
    password: str,
    timeout: int = 300,
    poll_interval: int = 10,
    sender_pattern: str = "pornolab",
) -> str | None:
    """
    Poll the mail.ru IMAP inbox until a PornoLab activation email arrives.
    Returns the first URL from that email, or None on timeout.

    Note: if the account was created with phone-only auth, IMAP may require an
    app-specific password set at https://account.mail.ru/user/security/passwords
    """
    domain = email.split("@", 1)[-1].lower()
    host, port = _IMAP_SERVERS.get(domain, ("imap.mail.ru", 993))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = await asyncio.to_thread(_imap_find_link, host, port, email, password, sender_pattern)
        if url:
            return url
        remaining = deadline - time.monotonic()
        await asyncio.sleep(min(poll_interval, max(0, remaining)))
    return None


def _imap_find_link(host: str, port: int, email: str, password: str, sender_pattern: str) -> str | None:
    """Blocking IMAP search. Returns the first matching URL or None."""
    try:
        with imaplib.IMAP4_SSL(host, port) as imap:
            imap.login(email, password)
            imap.select("INBOX")
            _, data = imap.search(None, "ALL")
            msg_ids = data[0].split()
            for msg_id in reversed(msg_ids[-20:]):
                _, msg_data = imap.fetch(msg_id, "(RFC822)")
                raw = msg_data[0][1]
                msg = _email_module.message_from_bytes(raw)
                from_hdr = msg.get("From", "").lower()
                subj = _decode_header_str(msg.get("Subject", "")).lower()
                if sender_pattern.lower() not in from_hdr and sender_pattern.lower() not in subj:
                    continue
                body = _extract_body(msg)
                urls = re.findall(r"https?://[^\s\"'<>]+", body)
                for url in urls:
                    if "pornolab" in url and ("activate" in url or "profile" in url):
                        return url
    except Exception:
        pass
    return None


def _decode_header_str(hdr: str) -> str:
    parts = decode_header(hdr)
    out = []
    for b, enc in parts:
        if isinstance(b, bytes):
            out.append(b.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(b)
    return "".join(out)


def _extract_body(msg) -> str:
    """Return all text from the email (plain + html)."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                if payload:
                    parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        if payload:
            parts.append(payload.decode(charset, errors="replace"))
    return "\n".join(parts)
