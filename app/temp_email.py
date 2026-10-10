"""Disposable email providers: generate an address, poll the inbox, read messages.

Provider ladder (cheapest/most-reliable first):
  1. mail.tm        — full REST/OpenAPI, JWT, ~10 rotating domains
  2. mail.gw        — identical API to mail.tm, different domain pool
  3. guerrillamail  — stable JSON API since 2013, no auth
  4. maildrop       — public GraphQL, no auth
  5. dispostable    — JSON on Accept header, no auth
  6. inboxkitten    — MIT open-source REST, no auth
  7. trashmail.at   — JSON endpoint, no auth

A provider is tried in order; if it raises ProviderError it falls through to the
next one.  The caller gets a TempAddress containing the address, credentials
needed to poll, and a reference to the provider so inbox() can be called.
"""

import asyncio
import random
import secrets
import string
from dataclasses import dataclass, field
from typing import Any

import httpx

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
_TIMEOUT = 20


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


class ProviderError(Exception):
    pass


@dataclass
class Message:
    id: str
    from_addr: str
    subject: str
    body_text: str       # plain-text body (may be empty if only HTML)
    body_html: str       # HTML body (may be empty)
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class TempAddress:
    address: str
    provider: str
    # Provider-specific state needed for polling:
    _state: dict = field(default_factory=dict, repr=False)
    # Back-reference so callers can call inbox()/message() without caring about provider:
    _impl: Any = field(default=None, repr=False)

    async def inbox(self) -> list[Message]:
        return await self._impl.inbox(self)

    async def message(self, msg_id: str) -> Message:
        return await self._impl.message(self, msg_id)


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

class _MailTM:
    """mail.tm and mail.gw — identical REST API, full OpenAPI spec."""

    def __init__(self, base: str, name: str):
        self.base = base.rstrip("/")
        self.name = name

    async def domains(self) -> list[str]:
        async with _client() as c:
            r = await c.get(f"{self.base}/domains")
            if r.status_code != 200:
                raise ProviderError(f"{self.name}: domains → {r.status_code}")
            data = r.json()
            return [d["domain"] for d in (data.get("hydra:member") or data) if d.get("isActive")]

    async def create(self) -> TempAddress:
        domains = await self.domains()
        if not domains:
            raise ProviderError(f"{self.name}: no active domains")
        domain = random.choice(domains)
        local = "".join(random.choices(string.ascii_lowercase, k=8)) + str(random.randint(10, 99))
        address = f"{local}@{domain}"
        password = secrets.token_urlsafe(16)
        async with _client() as c:
            r = await c.post(f"{self.base}/accounts",
                             json={"address": address, "password": password})
            if r.status_code not in (200, 201):
                raise ProviderError(f"{self.name}: create account → {r.status_code}: {r.text[:200]}")
            tok_r = await c.post(f"{self.base}/token",
                                 json={"address": address, "password": password})
            if tok_r.status_code != 200:
                raise ProviderError(f"{self.name}: get token → {tok_r.status_code}")
            token = tok_r.json().get("token", "")
        return TempAddress(
            address=address, provider=self.name,
            _state={"token": token, "base": self.base},
            _impl=self,
        )

    async def inbox(self, addr: TempAddress) -> list[Message]:
        token = addr._state["token"]
        base = addr._state["base"]
        async with _client() as c:
            r = await c.get(f"{base}/messages",
                            headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            raise ProviderError(f"{self.name}: inbox → {r.status_code}")
        data = r.json()
        items = data.get("hydra:member") or data
        return [
            Message(
                id=m["id"],
                from_addr=m.get("from", {}).get("address", ""),
                subject=m.get("subject", ""),
                body_text=m.get("text", ""),
                body_html=m.get("html", [""])[0] if isinstance(m.get("html"), list) else m.get("html", ""),
                raw=m,
            )
            for m in items
        ]

    async def message(self, addr: TempAddress, msg_id: str) -> Message:
        token = addr._state["token"]
        base = addr._state["base"]
        async with _client() as c:
            r = await c.get(f"{base}/messages/{msg_id}",
                            headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            raise ProviderError(f"{self.name}: message → {r.status_code}")
        m = r.json()
        return Message(
            id=m["id"],
            from_addr=m.get("from", {}).get("address", ""),
            subject=m.get("subject", ""),
            body_text=m.get("text", ""),
            body_html=m.get("html", [""])[0] if isinstance(m.get("html"), list) else m.get("html", ""),
            raw=m,
        )


class _GuerrillaMailProvider:
    name = "guerrillamail"
    _BASE = "https://api.guerrillamail.com/ajax.php"
    # The API lets you pick from these domains via set_email_user
    DOMAINS = [
        "guerrillamail.com", "guerrillamail.net", "guerrillamail.org",
        "guerrillamail.biz", "guerrillamail.de", "guerrillamail.info",
        "grr.la", "sharklasers.com", "spam4.me",
    ]

    async def create(self) -> TempAddress:
        domain = random.choice(self.DOMAINS)
        local = "".join(random.choices(string.ascii_lowercase, k=8)) + str(random.randint(10, 99))
        async with _client() as c:
            # get_email_address assigns a random address and returns a sid_token
            r = await c.get(self._BASE, params={"f": "get_email_address", "lang": "en"})
            if r.status_code != 200:
                raise ProviderError(f"guerrillamail: get address → {r.status_code}")
            init = r.json()
            sid = init.get("sid_token", "")
            # set_email_user lets us choose the local part + domain
            r2 = await c.get(self._BASE, params={
                "f": "set_email_user", "email_user": local,
                "email_domain": domain, "lang": "en", "sid_token": sid,
            })
            if r2.status_code != 200:
                raise ProviderError(f"guerrillamail: set user → {r2.status_code}")
            data = r2.json()
            sid = data.get("sid_token", sid)
            address = data.get("email_addr") or f"{local}@{domain}"
        return TempAddress(
            address=address, provider=self.name,
            _state={"sid": sid, "seq": 0},
            _impl=self,
        )

    async def inbox(self, addr: TempAddress) -> list[Message]:
        seq = addr._state.get("seq", 0)
        async with _client() as c:
            r = await c.get(self._BASE, params={
                "f": "check_email", "seq": seq,
                "sid_token": addr._state["sid"],
            })
        if r.status_code != 200:
            raise ProviderError(f"guerrillamail: check → {r.status_code}")
        data = r.json()
        msgs = data.get("list") or []
        if msgs:
            addr._state["seq"] = max(int(m.get("mail_id", 0)) for m in msgs)
        return [
            Message(
                id=str(m.get("mail_id", "")),
                from_addr=m.get("mail_from", ""),
                subject=m.get("mail_subject", ""),
                body_text=m.get("mail_excerpt", ""),
                body_html="",
                raw=m,
            )
            for m in msgs
        ]

    async def message(self, addr: TempAddress, msg_id: str) -> Message:
        async with _client() as c:
            r = await c.get(self._BASE, params={
                "f": "fetch_email", "email_id": msg_id,
                "sid_token": addr._state["sid"],
            })
        if r.status_code != 200:
            raise ProviderError(f"guerrillamail: fetch → {r.status_code}")
        m = r.json()
        html_body = m.get("mail_body", "")
        text_body = ""
        if html_body:
            # strip tags for a rough plain-text version
            import re
            text_body = re.sub(r"<[^>]+>", " ", html_body).strip()
        return Message(
            id=msg_id,
            from_addr=m.get("mail_from", ""),
            subject=m.get("mail_subject", ""),
            body_text=text_body,
            body_html=html_body,
            raw=m,
        )


class _MaildropProvider:
    """Maildrop — public GraphQL endpoint, no auth."""
    name = "maildrop"
    _GQL = "https://api.maildrop.cc/graphql"
    DOMAIN = "maildrop.cc"

    async def create(self) -> TempAddress:
        local = "".join(random.choices(string.ascii_lowercase, k=10))
        address = f"{local}@{self.DOMAIN}"
        return TempAddress(
            address=address, provider=self.name,
            _state={"local": local},
            _impl=self,
        )

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        async with _client() as c:
            r = await c.post(self._GQL,
                             json={"query": query, "variables": variables or {}},
                             headers={"Content-Type": "application/json"})
        if r.status_code != 200:
            raise ProviderError(f"maildrop: GraphQL → {r.status_code}")
        return r.json()

    async def inbox(self, addr: TempAddress) -> list[Message]:
        q = """
        query Inbox($mailbox: String!) {
          inbox(mailbox: $mailbox) { id headerfrom subject date }
        }
        """
        data = await self._gql(q, {"mailbox": addr._state["local"]})
        items = (data.get("data") or {}).get("inbox") or []
        return [
            Message(
                id=m["id"],
                from_addr=m.get("headerfrom", ""),
                subject=m.get("subject", ""),
                body_text="",
                body_html="",
                raw=m,
            )
            for m in items
        ]

    async def message(self, addr: TempAddress, msg_id: str) -> Message:
        q = """
        query Message($mailbox: String!, $id: String!) {
          message(mailbox: $mailbox, id: $id) {
            id headerfrom subject date html
          }
        }
        """
        data = await self._gql(q, {"mailbox": addr._state["local"], "id": msg_id})
        m = (data.get("data") or {}).get("message") or {}
        html = m.get("html", "")
        import re
        text = re.sub(r"<[^>]+>", " ", html).strip() if html else ""
        return Message(
            id=msg_id,
            from_addr=m.get("headerfrom", ""),
            subject=m.get("subject", ""),
            body_text=text,
            body_html=html,
            raw=m,
        )


class _DispostableProvider:
    """dispostable.com — JSON on Accept: application/json."""
    name = "dispostable"
    DOMAIN = "dispostable.com"

    async def create(self) -> TempAddress:
        local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        return TempAddress(
            address=f"{local}@{self.DOMAIN}", provider=self.name,
            _state={"local": local},
            _impl=self,
        )

    async def inbox(self, addr: TempAddress) -> list[Message]:
        url = f"https://www.dispostable.com/inbox/{addr._state['local']}/"
        async with _client() as c:
            r = await c.get(url, headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise ProviderError(f"dispostable: inbox → {r.status_code}")
        data = r.json()
        msgs = data.get("messages") or []
        return [
            Message(
                id=str(m.get("id", "")),
                from_addr=m.get("sender", ""),
                subject=m.get("subject", ""),
                body_text=m.get("body_text", ""),
                body_html=m.get("body_html", ""),
                raw=m,
            )
            for m in msgs
        ]

    async def message(self, addr: TempAddress, msg_id: str) -> Message:
        # Fetch the full message page
        url = f"https://www.dispostable.com/inbox/{addr._state['local']}/{msg_id}/"
        async with _client() as c:
            r = await c.get(url, headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise ProviderError(f"dispostable: message → {r.status_code}")
        m = r.json().get("message") or r.json()
        return Message(
            id=msg_id,
            from_addr=m.get("sender", ""),
            subject=m.get("subject", ""),
            body_text=m.get("body_text", ""),
            body_html=m.get("body_html", ""),
            raw=m,
        )


class _InboxKittenProvider:
    """inboxkitten.com — MIT open-source REST, no auth."""
    name = "inboxkitten"
    DOMAIN = "inboxkitten.com"
    _BASE = "https://inboxkitten.com/api/v1"

    async def create(self) -> TempAddress:
        local = "".join(random.choices(string.ascii_lowercase, k=10))
        return TempAddress(
            address=f"{local}@{self.DOMAIN}", provider=self.name,
            _state={"local": local},
            _impl=self,
        )

    async def inbox(self, addr: TempAddress) -> list[Message]:
        async with _client() as c:
            r = await c.get(f"{self._BASE}/list", params={
                "emailUsername": addr._state["local"],
                "emailDomain": self.DOMAIN,
            })
        if r.status_code != 200:
            raise ProviderError(f"inboxkitten: inbox → {r.status_code}")
        msgs = r.json() or []
        return [
            Message(
                id=str(m.get("key", "")),
                from_addr=m.get("sender", ""),
                subject=m.get("subject", ""),
                body_text="",
                body_html="",
                raw=m,
            )
            for m in msgs
        ]

    async def message(self, addr: TempAddress, msg_id: str) -> Message:
        async with _client() as c:
            r = await c.get(f"{self._BASE}/message", params={
                "emailUsername": addr._state["local"],
                "emailDomain": self.DOMAIN,
                "key": msg_id,
            })
        if r.status_code != 200:
            raise ProviderError(f"inboxkitten: message → {r.status_code}")
        m = r.json() or {}
        return Message(
            id=msg_id,
            from_addr=m.get("sender", ""),
            subject=m.get("subject", ""),
            body_text=m.get("body", ""),
            body_html="",
            raw=m,
        )


class _TrashMailProvider:
    """trashmail.at — ?cmd=get_messages JSON endpoint."""
    name = "trashmail"
    DOMAINS = ["trashmail.at", "trashmail.me", "trashmail.io", "trashmail.net"]

    async def create(self) -> TempAddress:
        domain = random.choice(self.DOMAINS)
        local = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        return TempAddress(
            address=f"{local}@{domain}", provider=self.name,
            _state={"address": f"{local}@{domain}"},
            _impl=self,
        )

    async def inbox(self, addr: TempAddress) -> list[Message]:
        async with _client() as c:
            r = await c.get("https://trashmail.at/",
                            params={"cmd": "get_messages", "email": addr._state["address"]})
        if r.status_code != 200:
            raise ProviderError(f"trashmail: inbox → {r.status_code}")
        try:
            msgs = r.json()
        except Exception:
            raise ProviderError("trashmail: inbox response not JSON")
        if not isinstance(msgs, list):
            msgs = msgs.get("messages") or []
        return [
            Message(
                id=str(m.get("id", "")),
                from_addr=m.get("from", ""),
                subject=m.get("subject", ""),
                body_text=m.get("body", ""),
                body_html="",
                raw=m,
            )
            for m in msgs
        ]

    async def message(self, addr: TempAddress, msg_id: str) -> Message:
        async with _client() as c:
            r = await c.get("https://trashmail.at/",
                            params={"cmd": "get_message", "id": msg_id,
                                    "email": addr._state["address"]})
        if r.status_code != 200:
            raise ProviderError(f"trashmail: message → {r.status_code}")
        m = r.json() or {}
        return Message(
            id=msg_id,
            from_addr=m.get("from", ""),
            subject=m.get("subject", ""),
            body_text=m.get("body", ""),
            body_html="",
            raw=m,
        )


class _MailsacProvider:
    """mailsac.com — public catch-all inbox, no auth, simple REST API."""
    name = "mailsac"
    DOMAIN = "mailsac.com"
    _BASE = "https://mailsac.com/api"

    async def create(self) -> TempAddress:
        local = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        return TempAddress(
            address=f"{local}@{self.DOMAIN}", provider=self.name,
            _state={"local": local},
            _impl=self,
        )

    async def inbox(self, addr: TempAddress) -> list[Message]:
        local = addr._state["local"]
        async with _client() as c:
            r = await c.get(f"{self._BASE}/addresses/{local}@{self.DOMAIN}/messages")
        if r.status_code != 200:
            raise ProviderError(f"mailsac: inbox → {r.status_code}")
        msgs = r.json() or []
        return [
            Message(
                id=str(m.get("_id", "")),
                from_addr=(m.get("from") or [{}])[0].get("address", "") if m.get("from") else "",
                subject=m.get("subject", ""),
                body_text="",
                body_html="",
                raw=m,
            )
            for m in msgs
        ]

    async def message(self, addr: TempAddress, msg_id: str) -> Message:
        local = addr._state["local"]
        async with _client() as c:
            r = await c.get(f"{self._BASE}/text/{local}@{self.DOMAIN}/{msg_id}")
        if r.status_code != 200:
            raise ProviderError(f"mailsac: message → {r.status_code}")
        text = r.text
        return Message(
            id=msg_id,
            from_addr="",
            subject="",
            body_text=text,
            body_html="",
            raw={},
        )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

_PROVIDERS = [
    _MailTM("https://api.mail.tm", "mail.tm"),
    _MailTM("https://api.mail.gw", "mail.gw"),
    _MailsacProvider(),
    _GuerrillaMailProvider(),
    _MaildropProvider(),
    _DispostableProvider(),
    _InboxKittenProvider(),
    _TrashMailProvider(),
]


async def new_address(provider: str | None = None) -> TempAddress:
    """Create a new disposable email address.

    If `provider` is given, try that provider only (raise ProviderError on
    failure).  Otherwise try the ladder in order until one succeeds.
    """
    if provider:
        impl = next((p for p in _PROVIDERS
                     if getattr(p, "name", None) == provider), None)
        if not impl:
            raise ProviderError(f"unknown provider: {provider!r}")
        return await impl.create()

    last_err: Exception | None = None
    for impl in _PROVIDERS:
        try:
            return await impl.create()
        except Exception as e:
            last_err = e
            continue
    raise ProviderError(f"all providers failed; last error: {last_err}")


def provider_names() -> list[str]:
    return [getattr(p, "name", type(p).__name__) for p in _PROVIDERS]
