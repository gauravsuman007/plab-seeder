"""Attempt 02 (last committed before the lean rewrite): patchright + headful +
Xvfb, lifted verbatim from app/pornolab.py as of commit 65e0b4a.

Why we moved on: the heavy stack (patchright + pyvirtualdisplay + Xvfb) worked
but was slow (~15 s cold start on every register_form call) and dragged in two
system packages (xvfb) plus a downloaded Chromium build. The lean rewrite in
app/pornolab.py starts with patchright *headless* -- patched Chromium is
usually enough on its own -- and only falls back to Xvfb-headful when the
token doesn't appear.

The vendored undetected_chromedriver dir in 01-undetected-chromedriver-vendor/
was prep for a never-shipped Selenium-based alternative; archived because the
patched-Playwright route makes it redundant (same mechanism: hide the
automation surface so Turnstile's fingerprint check doesn't fire)."""

async def _playwright_register(self) -> tuple[str, list[dict]]:
    """Drive a real browser through the terms-agree step so Cloudflare Turnstile
    actually gets to run.

    FlareSolverr only waits/solves when it recognises Cloudflare's own full-page
    challenge wrapper ("Just a moment..."). The registration form is a normal 200
    TorrentPier page that just happens to embed a Turnstile widget, so FlareSolverr
    logs "Challenge not detected!" and returns before the widget's async JS has a
    chance to populate the cf-turnstile-response hidden input.

    A plain headless Playwright/Chromium browser doesn't work either: Turnstile
    fingerprints headless rendering (missing GPU, automation-only signals) and the
    widget just sits there unsolved. Patchright strips the usual CDP/automation
    leaks, and running it headful under a virtual framebuffer (Xvfb) avoids the
    headless rendering fingerprint entirely.
    """
    from patchright.async_api import async_playwright
    from pyvirtualdisplay import Display

    reg_url = f"{self.base}/forum/profile.php?mode=register"
    display = Display(visible=0, size=(1920, 1080))
    display.start()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False, args=["--no-sandbox"])
            page = await browser.new_page()
            try:
                await page.goto(reg_url, wait_until="load")
                # the "I agree" link submits a hidden form (id="go-to-reg") via
                # jQuery; match on the onclick handler, not the (Russian) link text
                await page.click('a[onclick*="go-to-reg"]')
                # "networkidle" never fires once Turnstile is on the page — its own
                # background requests keep the network busy, so wait for "load" only
                await page.wait_for_load_state("load")
                # Turnstile resolves asynchronously after the form page loads — poll
                # the hidden input it fills in rather than trusting the first snapshot
                for _ in range(25):
                    field = await page.query_selector('input[name="cf-turnstile-response"]')
                    value = await field.input_value() if field else None
                    if value:
                        break
                    await page.wait_for_timeout(1000)
                html = await page.content()
                cookies = await page.context.cookies()
            finally:
                await browser.close()
    finally:
        display.stop()
    return html, cookies
