"""The PornoLab website: login, profile, tracker listing, .torrent download.

All requests are serialised and spaced out (MIN_GAP seconds): this is one
account browsing the site, not a crawler. The session cookie is persisted so a
restart does not mean a fresh login (and a fresh captcha).
"""

import asyncio
import base64
import json
import time

import httpx

from . import parsing
from .store import DATA_DIR

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
MIN_GAP = 3.0
COOKIES = DATA_DIR / "pornolab_cookies.json"

_ocr = None


def _solve_captcha(img_bytes: bytes) -> str:
    """Solve a TorrentPier image captcha using ddddocr.

    The captcha has a 'pornolab.net' watermark strip at the bottom ~28% of the
    image that confuses the model, so we crop it before classifying.
    """
    global _ocr
    if _ocr is None:
        import ddddocr as _ddddocr
        _ocr = _ddddocr.DdddOcr(show_ad=False)
    from PIL import Image, ImageEnhance
    import io
    img = Image.open(io.BytesIO(img_bytes))
    w, h = img.size
    cropped = img.crop((0, 0, w, int(h * 0.72)))
    cropped = cropped.resize((w * 2, int(h * 0.72) * 2), Image.LANCZOS)
    cropped = cropped.convert("L")
    cropped = ImageEnhance.Contrast(cropped).enhance(3)
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return _ocr.classification(buf.getvalue())


class PornolabError(Exception):
    pass


class NotLoggedIn(PornolabError):
    pass


class LimitReached(PornolabError):
    """dl.php answered with the site's 'daily limit' page instead of a .torrent."""


class CaptchaRequired(PornolabError):
    def __init__(self, image_data_url: str, form: parsing.LoginForm):
        super().__init__("captcha required")
        self.image = image_data_url
        self.form = form


class Pornolab:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"},
            timeout=30,
            follow_redirects=True,
        )
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.uid: int | None = None
        self.pending_captcha: parsing.LoginForm | None = None
        self._load_cookies()

    # -- plumbing ----------------------------------------------------------- #
    def _load_cookies(self) -> None:
        try:
            data = json.loads(COOKIES.read_text())
        except (OSError, ValueError):
            return
        for c in data.get("cookies", []):
            self.client.cookies.set(c["name"], c["value"], domain=c.get("domain") or "")
        self.uid = data.get("uid")

    def _save_cookies(self) -> None:
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain} for c in self.client.cookies.jar]
        COOKIES.parent.mkdir(parents=True, exist_ok=True)
        COOKIES.write_text(json.dumps({"cookies": cookies, "uid": self.uid}))
        COOKIES.chmod(0o600)

    def clear_session(self) -> None:
        self.client.cookies.clear()
        self.uid = None
        COOKIES.unlink(missing_ok=True)

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        async with self._lock:
            wait = MIN_GAP - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self.client.request(method, f"{self.base}/{path.lstrip('/')}", **kw)
            finally:
                self._last = time.monotonic()

    @staticmethod
    def _text(r: httpx.Response) -> str:
        return r.content.decode("cp1251", "replace")

    # -- session ------------------------------------------------------------ #
    async def logged_in(self) -> bool:
        r = await self._request("GET", "forum/index.php")
        html = self._text(r)
        ok = parsing.is_logged_in(html)
        if ok and not self.uid:
            self.uid = parsing.find_uid(html)
            self._save_cookies()
        return ok

    async def login(self, username: str, password: str, captcha_code: str | None = None) -> None:
        """Log in, or raise CaptchaRequired with the image to show the user."""
        data = {"login_username": username, "login_password": password, "login": "Вход"}
        form = self.pending_captcha
        if captcha_code and form and form.cap_field:
            data["cap_sid"] = form.cap_sid or ""
            data[form.cap_field] = captcha_code

        body = "&".join(f"{k}={_cp1251_quote(v)}" for k, v in data.items())
        r = await self._request(
            "POST", "forum/login.php", content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        html = self._text(r)

        if parsing.is_logged_in(html):
            self.pending_captcha = None
            self.uid = parsing.find_uid(html)
            self._save_cookies()
            return

        form = parsing.parse_login_form(html)
        if form.captcha_url:
            self.pending_captcha = form
            # The image lives on static.pornolab.net; same cookies, same client.
            # A Referer is required -- without it the host hotlink-blocks the
            # request and answers 200 with an HTML page instead of the image.
            img = await self.client.get(form.captcha_url, headers={"Referer": f"{self.base}/forum/login.php"})
            mime = img.headers.get("content-type", "image/png").split(";")[0]
            if not mime.startswith("image/"):
                raise PornolabError(f"captcha image fetch returned {mime!r}, not an image")
            raise CaptchaRequired(f"data:{mime};base64,{base64.b64encode(img.content).decode()}", form)

        raise PornolabError(parsing.login_error(html) or "Login failed: wrong username or password?")

    # -- data --------------------------------------------------------------- #
    async def profile(self) -> parsing.Profile:
        if not self.uid and not await self.logged_in():
            raise NotLoggedIn("not logged in")
        r = await self._request("GET", f"forum/profile.php?mode=viewprofile&u={self.uid}")
        html = self._text(r)
        if not parsing.is_logged_in(html):
            raise NotLoggedIn("session expired")
        return parsing.parse_profile(html)

    async def tracker_page(self, page: int, order: int, forums: list[int] | None = None) -> list[parsing.Release]:
        params = [("f[]", str(f)) for f in (forums or [-1])]
        params += [("o", str(order)), ("s", "2"), ("start", str(page * 50))]
        r = await self._request("GET", "forum/tracker.php", params=params)
        html = self._text(r)
        if not parsing.is_logged_in(html):
            raise NotLoggedIn("session expired")
        return parsing.parse_tracker(html)

    async def download(self, topic_id: int) -> bytes:
        """The .torrent for a topic. Counts against the account's daily quota."""
        r = await self._request(
            "GET", f"forum/dl.php?t={topic_id}",
            headers={"Referer": f"{self.base}/forum/viewtopic.php?t={topic_id}"},
        )
        if r.content[:1] == b"d" and b"4:info" in r.content[:4096]:
            return r.content
        html = self._text(r)
        if not parsing.is_logged_in(html):
            raise NotLoggedIn("session expired")
        raise LimitReached(parsing.site_message(html) or "PornoLab returned a page instead of a .torrent")

    # -- registration ------------------------------------------------------- #
    async def _playwright_register(self) -> tuple[str, list[dict]]:
        """Drive a real headless Chromium through the terms-agree step so Cloudflare
        Turnstile actually gets to run.

        FlareSolverr only waits/solves when it recognises Cloudflare's own full-page
        challenge wrapper ("Just a moment..."). The registration form is a normal 200
        TorrentPier page that just happens to embed a Turnstile widget, so FlareSolverr
        logs "Challenge not detected!" and returns before the widget's async JS has a
        chance to populate the cf-turnstile-response hidden input. A real browser with
        an actual wait loop is needed instead.
        """
        from playwright.async_api import async_playwright

        reg_url = f"{self.base}/forum/profile.php?mode=register"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            page = await browser.new_page(user_agent=UA)
            try:
                await page.goto(reg_url, wait_until="networkidle")
                # the "I agree" link submits a hidden form (id="go-to-reg") via jQuery;
                # match on the onclick handler rather than the (Russian) link text
                await page.click('a[onclick*="go-to-reg"]')
                await page.wait_for_load_state("networkidle")
                # Turnstile resolves asynchronously after the form page loads — poll
                # the hidden input it fills in rather than trusting the first snapshot
                for _ in range(20):
                    value = await page.eval_on_selector(
                        'input[name="cf-turnstile-response"]', "el => el.value"
                    )
                    if value:
                        break
                    await page.wait_for_timeout(1000)
                html = await page.content()
                cookies = await page.context.cookies()
            finally:
                await browser.close()
        return html, cookies

    async def register_form(self) -> parsing.RegisterForm:
        """Fetch the registration page and return the form metadata (captcha, lists,
        Turnstile token) via a real headless browser — see _playwright_register.
        """
        html, cookies = await self._playwright_register()
        for c in cookies:
            self.client.cookies.set(c["name"], c["value"], domain=c.get("domain", "").lstrip("."))
        form = parsing.parse_register_form(html)
        if not form.turnstile_token:
            import logging
            logging.getLogger("seeder").warning(
                "headless browser did not produce a Turnstile token; submission may fail"
            )
        return form

    async def register(
        self,
        username: str,
        password: str,
        email: str,
        captcha_code: str,
        form: parsing.RegisterForm,
        country: str = "0",
        timezone: str = "100",
    ) -> dict:
        """Submit the registration form. Returns {"ok": True} or {"error": "..."}."""
        if not form.cap_field or not form.cap_sid:
            raise PornolabError("registration form not initialised – fetch the form first")
        data: dict[str, str] = {
            "mode": "register",
            "reg_agreed": "1",
            "username": username,
            "new_pass": password,
            "cfm_pass": password,
            "user_email": email,
            "cap_sid": form.cap_sid,
            form.cap_field: captcha_code,
            "user_flag_id": country,
            "user_timezone_x2": timezone,
            "submit": "Зарегистрироваться",
        }
        if form.turnstile_token:
            data["cf-turnstile-response"] = form.turnstile_token
        body = "&".join(f"{k}={_cp1251_quote(v)}" for k, v in data.items())
        r = await self._request(
            "POST", "forum/profile.php",
            content=body.encode("cp1251"),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Referer": f"{self.base}/forum/profile.php?mode=register"},
        )
        html = self._text(r)
        if parsing.register_success(html):
            return {"ok": True}
        err = parsing.parse_register_error(html)
        return {"ok": False, "error": err or "Registration failed – unknown error"}

    async def close(self) -> None:
        await self.client.aclose()


def _cp1251_quote(value: str) -> str:
    """Form values are posted in the site's own encoding (windows-1251)."""
    from urllib.parse import quote_plus
    return quote_plus(value, encoding="cp1251", errors="replace")
