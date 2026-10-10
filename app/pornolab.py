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
    #
    # Clearing the embedded Turnstile widget on the TorrentPier register page is
    # the whole reason this module reaches for a browser. The ladder below is
    # the CLAUDE.md playbook with a hard-won twist: on arm64 Debian (our host),
    # patched Chromium alone (patchright) is NOT enough -- Cloudflare's
    # fingerprint-assessment endpoint answers 401 Unauthorized before the
    # challenge even runs, because there's no Google Chrome binary for arm64
    # and Chromium's trust surface is downranked. Camoufox (patched Firefox,
    # arm64-native) clears the same widget in ~1 s. See
    # archive/turnstile-attempts/ for everything we ruled out.
    #
    # Order (first that returns a token wins):
    #   1. sidecar HTTP call                  (if TURNSTILE_SIDECAR_URL is set;
    #                                          ghcr.io/gauravsuman007/camoufox-turnstile-solver
    #                                          — see sidecar/turnstile-solver/)
    #   2. patchright headful under Xvfb      (safety-net; does NOT clear the
    #                                          current ruleset on arm64, kept
    #                                          in case the sidecar is unavailable)
    #
    # The caller only ever gets the HTML + cookies of a successful attempt. We
    # stop as soon as `cf-turnstile-response` is populated, so a stage that
    # works short-circuits every heavier one.
    async def _playwright_register(self) -> tuple[str, list[dict]]:
        import logging, os
        log = logging.getLogger("seeder")

        stages: list = []
        if os.environ.get("TURNSTILE_SIDECAR_URL"):
            stages.append(self._register_stage_sidecar)
        stages += [self._register_stage_xvfb]

        last_err: Exception | None = None
        for stage in stages:
            name = stage.__name__.removeprefix("_register_stage_")
            try:
                log.info("Turnstile: trying stage %s", name)
                html, cookies, token = await stage()
            except Exception as e:                               # noqa: BLE001
                last_err = e
                log.warning("Turnstile stage %s raised %s: %s", name, type(e).__name__, e)
                continue
            if token:
                log.info("Turnstile: stage %s cleared the widget", name)
                return html, cookies
            log.warning("Turnstile: stage %s loaded the form but never got a token", name)
        # No stage got a token — hand back the LAST successful HTML/cookies if
        # any, so the caller can still show the image captcha, and let the
        # submit fail with the server's own error. parse_register_form flags
        # the missing token.
        if last_err:
            raise PornolabError(f"could not reach the registration form: {last_err}")
        raise PornolabError("Turnstile widget never populated cf-turnstile-response")

    # Launch flags shared by every stage. --enable-unsafe-swiftshader turns on
    # software WebGL (without it Chromium reports "No available adapters", which
    # is a hard Cloudflare fingerprint fail -- the /cdn-cgi/.../pat endpoint
    # answers 401 for a client with no GPU adapter at all). The rest hides the
    # automation surface.
    _LAUNCH_ARGS = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--enable-unsafe-swiftshader",
        "--use-gl=angle",
        "--use-angle=swiftshader",
        "--disable-dev-shm-usage",
    ]
    # Match a real desktop Linux Chrome, not the Chromium default that Turnstile
    # downranks on sight. The viewport is a common one (not Playwright's 1280x720).
    _CTX_UA = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    )
    _CTX_VIEWPORT = {"width": 1366, "height": 768}

    async def _new_context(self, browser):
        """A browser context with the viewport/UA/locale real browsers have."""
        return await browser.new_context(
            viewport=self._CTX_VIEWPORT,
            user_agent=self._CTX_UA,
            locale="en-US",
            timezone_id="Europe/Berlin",
        )

    async def _run_patchright_page(self, launch, is_persistent=False):
        """Shared body: open the register page, click agree, poll for the token.

        `launch` returns either a Playwright `Browser` (we build a fresh
        context ourselves) or a `BrowserContext` (persistent-context mode,
        when `is_persistent=True`).
        """
        reg_url = f"{self.base}/forum/profile.php?mode=register"
        obj = await launch()
        close_target = obj
        try:
            if is_persistent:
                page = await obj.new_page()
            else:
                ctx = await self._new_context(obj)
                page = await ctx.new_page()
            # Block the page's third-party ad/promo hosts: they load mixed
            # content and expired/wrong-CN certs, which pollutes the browser's
            # error surface and (per diagnosis 2026-10-10) nudges Turnstile
            # off its invisible path onto a managed challenge.
            async def _block(route):
                host = route.request.url.split("/", 3)[2].lower()
                if any(bad in host for bad in ("vpipi.com", "dynspt.com", "doubleclick", "googlesyndication")):
                    await route.abort()
                else:
                    await route.continue_()
            await page.route("**/*", _block)

            await page.goto(reg_url, wait_until="load")
            # "I agree" is a jQuery-submitted hidden form; match the onclick
            # handler, not the (Russian) link text.
            await page.click('a[onclick*="go-to-reg"]')
            # "networkidle" never fires once Turnstile is on the page -- its
            # own long-poll keeps the network busy. Wait on "load" only.
            await page.wait_for_load_state("load")

            # If Turnstile shifts from invisible mode onto a managed/interactive
            # challenge, nothing happens until a real-looking click lands inside
            # the widget's iframe (the "I'm not a robot" checkbox). We dispatch
            # one blind click a few seconds in; patched Chromium's mouse looks
            # convincing enough for Turnstile to accept it. If the widget had
            # already auto-passed, the click is harmless.
            async def _try_click_widget():
                await page.wait_for_timeout(4000)
                iframe = await page.query_selector('iframe[src*="challenges.cloudflare.com"]')
                if not iframe:
                    return
                box = await iframe.bounding_box()
                if not box:
                    return
                # Turnstile puts its checkbox ~30 px from the iframe's left edge
                # and vertically centred; aim for that spot plus a small jitter.
                import random
                x = box["x"] + 30 + random.uniform(-2, 2)
                y = box["y"] + box["height"] / 2 + random.uniform(-2, 2)
                await page.mouse.move(x - 50, y - 10, steps=10)
                await page.mouse.move(x, y, steps=8)
                await page.mouse.click(x, y, delay=90)
            try:
                await _try_click_widget()
            except Exception:
                pass  # the click is a nice-to-have; the invisible path may still carry

            # The widget populates `cf-turnstile-response` asynchronously --
            # some seconds after it clears, not when its iframe loads. Poll it.
            token = None
            for _ in range(45):
                field = await page.query_selector('input[name="cf-turnstile-response"]')
                token = await field.input_value() if field else None
                if token:
                    break
                await page.wait_for_timeout(1000)
            html = await page.content()
            cookies = await page.context.cookies()
            return html, cookies, token
        finally:
            await close_target.close()

    async def _register_stage_sidecar(self):
        """Call the camoufox-based turnstile-solver sidecar over HTTP.

        Scheduled when TURNSTILE_SIDECAR_URL points at the sidecar container
        (see sidecar/turnstile-solver/). Keeping the Firefox install out of
        the seeder's own image saves ~1.5 GB and lets the sidecar be reused
        and scaled independently.
        """
        import os
        base = os.environ["TURNSTILE_SIDECAR_URL"].rstrip("/")
        payload = {
            "url": f"{self.base}/forum/profile.php?mode=register",
            "agree_click": 'a[onclick*="go-to-reg"]',
            "widget_selector": ".cf-turnstile",
            "max_timeout": 90,
        }
        # Separate client: the sidecar is local and should NOT inherit the
        # tracker-session cookies or UA; it mints a fresh browser profile.
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{base}/solve", json=payload)
            r.raise_for_status()
            d = r.json()
        token = d.get("token", "")
        html = d.get("html", "")
        cookies = d.get("cookies", [])
        return html, cookies, token

    async def _register_stage_xvfb(self):
        """Fallback: patchright + headful + Xvfb. On arm64 this does NOT clear
        the current Turnstile ruleset (confirmed 2026-10-10: /pat/ -> 401), but
        kept as a safety net in case the sidecar is unavailable."""
        from patchright.async_api import async_playwright
        from pyvirtualdisplay import Display
        display = Display(visible=0, size=(1920, 1080))
        display.start()
        try:
            async with async_playwright() as pw:
                async def launch():
                    return await pw.chromium.launch(headless=False, args=self._LAUNCH_ARGS)
                return await self._run_patchright_page(launch)
        finally:
            display.stop()

    async def register_form(self) -> parsing.RegisterForm:
        """Fetch the registration page and return the form metadata (captcha, lists,
        Turnstile token) via a real headless browser — see _playwright_register.
        """
        html, reg_cookies = await self._playwright_register()
        # Keep the browser cookies separate from the owner's session: the
        # register POST must go out as an anonymous request, not as the
        # logged-in owner. Store them on the form object so register() can
        # use them without touching self.client.
        form = parsing.parse_register_form(html)
        form.browser_cookies = reg_cookies          # stash for register()
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
        # Use a fresh cookieless client so the owner's session cookies are NOT
        # sent — the tracker redirects to the homepage for already-logged-in users.
        reg_client = httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"},
            timeout=30,
            follow_redirects=True,
        )
        for c in getattr(form, "browser_cookies", []):
            reg_client.cookies.set(c["name"], c["value"], domain=c.get("domain", "").lstrip("."))
        async with reg_client:
            r = await reg_client.post(
                f"{self.base}/forum/profile.php",
                content=body.encode("cp1251"),
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "Referer": f"{self.base}/forum/profile.php?mode=register"},
            )
        html = self._text(r)
        if parsing.register_success(html):
            return {"ok": True}
        err = parsing.parse_register_error(html)
        # Always include a snippet of the raw page so the UI can show the
        # tracker's actual message even when the parser misses it.
        return {"ok": False, "error": err or "Registration failed – unknown error",
                "raw_html": html}

    async def close(self) -> None:
        await self.client.aclose()


def _cp1251_quote(value: str) -> str:
    """Form values are posted in the site's own encoding (windows-1251)."""
    from urllib.parse import quote_plus
    return quote_plus(value, encoding="cp1251", errors="replace")
