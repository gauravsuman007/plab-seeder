# Temporary Email Services — Scrapability Assessment

Research date: 2026-10-10. Goal: 20 services with diverse domains that can be
driven from this app (Python/httpx/asyncio) without a paid API key.

Each entry lists: domains offered, access method, auth, scrapability notes.

---

## Tier 1 — Clean public REST/JSON APIs, no auth

### 1. Guerrilla Mail
- **Domains**: `guerrillamail.com`, `guerrillamail.net`, `guerrillamail.org`,
  `guerrillamail.biz`, `guerrillamail.de`, `guerrillamail.info`,
  `grr.la`, `sharklasers.com`, `spam4.me`, `yopmail.fr` (via same backend)
- **API base**: `https://api.guerrillamail.com/ajax.php`
- **Key endpoints**:
  - `?f=get_email_address` → JSON with `email_addr`, `sid_token`
  - `?f=check_email&seq=0&sid_token=<tok>` → array of mail objects
  - `?f=fetch_email&email_id=<id>&sid_token=<tok>` → full message HTML
- **Auth**: none; carry `sid_token` from first call
- **Scrapability**: ✅ Straightforward JSON, stable since ~2013, no Cloudflare
- **Rate limit**: not published; ~1 req/s is safe

### 2. mail.tm
- **Domains**: rotates; query `GET https://api.mail.tm/domains` for live list
  (currently ~10 domains such as `mail.tm`, `dpptd.com`, etc.)
- **API base**: `https://api.mail.tm`
- **Key endpoints**:
  - `POST /accounts` body `{"address":"x@domain","password":"p"}` → create
  - `POST /token` body `{"address":"...","password":"..."}` → JWT
  - `GET /messages` header `Authorization: Bearer <jwt>` → inbox list
  - `GET /messages/<id>` → full message
- **Auth**: JWT per account (obtained with password you set at creation)
- **Scrapability**: ✅ Full OpenAPI spec published; 8 req/s cap documented
- **Note**: accounts expire after inactivity; create fresh per registration

### 3. Maildrop (Heluna)
- **Domains**: `maildrop.cc`
- **API base**: `https://api.maildrop.cc` (GraphQL)
- **Key query**: `{ inbox(mailbox:"<name>") { id, from, subject, date, body } }`
  endpoint: `POST https://api.maildrop.cc/graphql`
- **Auth**: none; mailbox name is the only selector
- **Scrapability**: ✅ Public GraphQL, open-source backend on GitHub
- **Note**: mailbox name is public; anyone who knows it can read it

### 4. Mailinator (public tier)
- **Domains**: `mailinator.com`, `trashmail.com` (and ~50 more public inboxes)
- **API base**: `https://www.mailinator.com/api/v2`  (free tier, no key)
- **Key endpoints** (no-key public inboxes only):
  - `GET /domains/public/inboxes/<inbox>` → message list
  - `GET /domains/public/inboxes/<inbox>/messages/<msgid>` → body
- **Auth**: none for public inboxes; private domains need paid key
- **Scrapability**: ✅ JSON REST; the public endpoint has been stable for years
- **Rate limit**: ~100 req/min undocumented; works fine at human pace

### 5. FakeMailGenerator / Fake Mail Generator
- **Domains**: `abyssmail.com`, `cust.in`, `dispostable.com`, `hailmail.net`,
  `jetable.fr.nf`, `kurzepost.de`, `objectmail.com`, `obobbo.com`,
  `proxymail.eu`, `rcpt.at`, `spamgourmet.com`, `tempalias.com`, `uggsrock.com`
- **Web scrape**: `https://www.fakemailgenerator.com/` (HTML form, no public API)
- **Scrapability**: ⚠️ Screen-scrape only; parse `#email-display` for address,
  poll `GET /inbox/<domain>/<user>/` for messages as JSON fragment
- **Note**: No official API; HTML structure has been stable; Cloudflare present
  — use `curl_cffi` with `impersonate="chrome"` if 403 on plain httpx

### 6. Temp Mail (temp-mail.org) via unofficial JSON
- **Domains**: many rotating; `href.li`, `disinbox.com`, etc.
- **Unofficial API** (reverse-engineered from browser XHR):
  - `GET https://api.internal.temp-mail.io/api/v3/email/new` → `{email, token}`
  - `GET https://api.internal.temp-mail.io/api/v3/email/<addr>/messages` → list
- **Auth**: bearer `token` from creation call
- **Scrapability**: ⚠️ Unofficial; subject to change; Cloudflare on main site
  but the `api.internal` subdomain has been accessible via plain httpx as of 2025
- **Note**: Verify the endpoint still responds before relying on it; document
  the date in `app/temp_email.py` if/when it breaks

---

## Tier 2 — Scrapable with modest effort (HTML scrape or unofficial JSON)

### 7. YOPmail
- **Domains**: `yopmail.com`, `yopmail.fr`, `cool.fr.nf`, `jetable.fr.nf`,
  `nospam.ze.tc`, `nomail.xl.cx`, `mega.zik.dj`, `speed.1s.fr`,
  `courriel.fr.nf`, `moncourrier.fr.nf`
- **Scrape URL**: `https://yopmail.com/en/wm` (POST form with `login=<name>`)
- **Inbox**: `https://yopmail.com/inbox` → HTML iframe; parse `<iframe>` src
- **Scrapability**: ⚠️ HTML scrape; no Cloudflare; straightforward BeautifulSoup
- **Note**: YOPTOKEN cookie required — obtain from initial GET, carry forward

### 8. Dispostable
- **Domains**: `dispostable.com`
- **API**: `GET https://www.dispostable.com/inbox/<name>/` returns JSON with
  `{ "messages": [...] }` when `Accept: application/json` header is set
- **Auth**: none
- **Scrapability**: ✅ JSON on demand via Accept header; very simple

### 9. ThrowAM (throwam.com)
- **Domains**: `throwam.com`
- **API**: undocumented JSON at `GET https://throwam.com/<address>/`
- **Scrapability**: ⚠️ HTML scrape; light, no Cloudflare historically

### 10. Trashmail.at
- **Domains**: `trashmail.at`, `trashmail.com`, `trashmail.me`, `trashmail.io`,
  `trashmail.net`, `spamgourmet.com`, `dispostable.com`
- **API base**: `https://trashmail.at`
- **Key endpoint**: `GET https://trashmail.at/?cmd=get_messages&email=<addr>`
  returns JSON array
- **Auth**: none
- **Scrapability**: ✅ JSON endpoint, stable; no Cloudflare

### 11. Spamgourmet
- **Domains**: `spamgourmet.com`, `spamgourmet.net`, `spamgourmet.org`
- **Access**: IMAP or web scrape at `https://www.spamgourmet.com/`
- **Scrapability**: ⚠️ Requires creating an account (permanent, not ephemeral);
  after that, sub-addresses are auto-created. HTML scrape for inbox.
- **Note**: Better as a "owned domain relay" than pure throwaway

### 12. Dropmail.me
- **Domains**: changes per session (e.g. `dropmail.me`)
- **API** (reverse-engineered GraphQL):
  - `POST https://dropmail.me/api/graphql/<random-token>`
  - mutation `introduceSession` → session token + address
  - query `session(token)` → inbox
- **Auth**: session token from first mutation
- **Scrapability**: ⚠️ GraphQL, undocumented; has been used by open-source
  projects (see `github.com/tempmail-lol`) as of 2024

### 13. inboxkitten.com
- **Domains**: `inboxkitten.com`
- **API**: `GET https://inboxkitten.com/api/v1/list?emailUsername=<name>&emailDomain=inboxkitten.com`
- **Auth**: none
- **Scrapability**: ✅ Clean JSON, open source (MIT on GitHub), simple

### 14. Mailnesia
- **Domains**: `mailnesia.com`
- **Scrape**: `GET https://mailnesia.com/mailbox/<name>` → HTML table of messages
- **Auth**: none; mailbox is public
- **Scrapability**: ✅ Stable HTML, very light server, no JS required

### 15. 10MinuteMail
- **Domains**: `10minutemail.com`, `10minutemail.net`
- **API**: `GET https://10minutemail.com/session/address` → JSON `{"address":...}`
  (uses cookies); `GET https://10minutemail.com/messages/since/0` → message list
- **Auth**: session cookie; obtain with a GET to `/` first, carry `JSESSIONID`
- **Scrapability**: ⚠️ Cookie-based session; stable undocumented JSON

### 16. Mohmal
- **Domains**: `mohmal.com` (Arabic-focused service, multiple TLDs)
- **API**: `GET https://www.mohmal.com/en/inbox/<name>` → JSON when
  `X-Requested-With: XMLHttpRequest` header set
- **Scrapability**: ⚠️ XHR JSON endpoint; no Cloudflare

### 17. AnonAddy (self-hosted / managed)
- **Domains**: `anonaddy.me`, or your own domain (self-hosted)
- **API**: full REST with personal API token
- **Auth**: API token (free tier available)
- **Scrapability**: ✅ Documented REST, free tier; best for persistent aliases
  that forward to a real mailbox — not for ephemeral read-once inboxes

### 18. SimpleLogin (by Proton)
- **Domains**: `simplelogin.com`, `aleeas.com`, `slmail.me`, etc.
- **API**: full REST, `Authorization: Bearer <api_key>`
- **Auth**: free account API key
- **Scrapability**: ✅ Documented; free tier gives 10 aliases; forwards to
  real inbox so inbox polling is your own mailbox, not theirs

### 19. Emailondeck
- **Domains**: `emailondeck.com` + rotating partner domains
- **API**: `GET https://www.emailondeck.com/api/get_email_address?&opt=<opt>&site=<site>`
  (key `api_code` required; free tier available at emailondeck.com/api)
- **Auth**: free API code from signup
- **Scrapability**: ⚠️ Key required but free; JSON responses

### 20. mail.gw (sister service to mail.tm)
- **Domains**: `mail.gw` + 5–10 rotating domains; query `GET https://api.mail.gw/domains`
- **API base**: `https://api.mail.gw` (identical schema to mail.tm)
- **Key endpoints**: same as mail.tm above (replace hostname)
- **Auth**: JWT per account
- **Scrapability**: ✅ Same codebase as mail.tm; if mail.tm is working, this is
  a drop-in fallback with different domains

---

## Integration notes for this app

- **Best picks for registration automation** (ephemeral, no auth, diverse domains):
  #1 Guerrilla Mail, #2 mail.tm, #3 Maildrop, #8 Dispostable, #13 inboxkitten,
  #20 mail.gw — all clean JSON with no signup.

- **TLS / fingerprint**: Guerrilla Mail and mail.tm respond fine to plain
  `httpx`. If any service 403s on plain httpx but works in a browser, try
  `curl_cffi` with `impersonate="chrome"` per the CLAUDE.md playbook.

- **Parallel accounts**: mail.tm and mail.gw support creating N accounts quickly;
  good when one address per registration is needed in bulk.

- **Avoid**: services that require a permanent account (Spamgourmet, SimpleLogin,
  AnonAddy) unless alias-forwarding fits the use case.

---

## Sources
- [publicapis.io — Free Temporary Email APIs](https://publicapis.io/blog/free-temporary-email-api-lrvt)
- [publicapis.io — Guerrilla Mail API](https://publicapis.io/guerrilla-mail-api)
- [mailmomy.com — 45 Best Free Temp Mail Services 2026](https://mailmomy.com/blog/best-free-temp-mail-services)
- [saasodds.com — 7 Best Temp Mail Services 2026](https://www.saasodds.com/blog/best-temp-mail-services)
- [dev.to — Best Disposable Email Services 2025 (Dev's Perspective)](https://dev.to/aksoonesec/best-disposable-email-services-in-2025-a-devs-perspective-3239)
- [public-api.org — mail.tm](https://public-api.org/api/446/mail-tm)
