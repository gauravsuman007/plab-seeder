# Turnstile solver attempts — archive

Previous approaches tried to clear the Cloudflare Turnstile widget embedded in
the TorrentPier registration page. Current shipping code lives in
[`app/pornolab.py`](../../app/pornolab.py) (`_playwright_register` and
friends); this directory keeps older attempts around so we don't repeat them.

Order of attempts, oldest first:

- **00 — FlareSolverr.** Tried first because it was already running in the
  stack. Logs `Challenge not detected!` and returns before Turnstile's async
  JS has filled `cf-turnstile-response`: FlareSolverr only engages on
  Cloudflare's own full-page interstitial ("Just a moment..."), not on a
  Turnstile widget embedded in an otherwise-normal page. No code to archive —
  nothing beyond a POST to its `/v1` endpoint from a scratch script.

- **01 — Vendored undetected-chromedriver** (see
  [`01-undetected-chromedriver-vendor/`](01-undetected-chromedriver-vendor/)).
  A Selenium-based alternative to patchright was prepared but never wired in;
  the vendored tree is kept for reference. Same mechanism as patchright — hide
  the automation surface so Turnstile's fingerprint check doesn't fire — so
  the lean rewrite made it redundant.

- **02 — Patchright + pyvirtualdisplay + Xvfb (headful)** (see
  [`02-patchright-headful-xvfb.py`](02-patchright-headful-xvfb.py)). Worked,
  but heavy: ~15 s cold start per `register_form` call, needs Xvfb and a
  downloaded Chromium build. Replaced by the lean rewrite, which tries
  patchright *headless* first (patched Chromium alone is usually enough) and
  only escalates to headful-under-Xvfb if the token doesn't appear.

Rules for anything new in this directory (see also [`CLAUDE.md`](../../CLAUDE.md)):

- One subdirectory or file per attempt, numbered.
- Record what was tried, what the symptom was when it failed, and the date.
- Do not import from here in shipping code.
