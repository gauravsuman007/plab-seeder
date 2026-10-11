"""
Tuta (tuta.com) account factory.

Verified live 2026-10-11 from the deployment's German IP: free @tutamail.com
signup needs only a username + password + two checkboxes — no phone number,
no captcha. This makes it the working alternative to mail.ru (phone-gated
via VK ID) and web.de/gmx.net (phone-gated at the final registration step —
see app/gmx.py's module docstring for that dead end).

The whole flow (signup AND inbox reading) runs through the turnstile-solver
sidecar's camoufox browser rather than patchright in-process, for two reasons:
  1. camoufox is already deployed and proven against this class of anti-bot
     surface (see CLAUDE.md's Cloudflare/Turnstile section).
  2. Free Tuta accounts have NO IMAP/POP — Tuta is API-only by design, part
     of its end-to-end-encryption model — so even just reading the PornoLab
     activation email has to drive the Tuta webmail UI in a browser. There is
     no imaplib shortcut here the way gmx_find_activation()/mailru_find_activation()
     use for other providers.

Usage
-----
from app.tuta import TutaFactory

factory = TutaFactory(sidecar_url="http://turnstile-solver:8000")
email, password = await factory.create()
# -> ("pl7x9k2m4q1r@tutamail.com", "Xk9!f3w2qmN5")

url = await factory.find_activation(email, password, timeout=120)
# -> "https://pornolab.net/forum/profile.php?mode=activate&..."
"""

import httpx


class TutaError(Exception):
    pass


class TutaFactory:
    def __init__(self, sidecar_url: str):
        self.sidecar_url = sidecar_url.rstrip("/")

    async def create(self) -> tuple[str, str]:
        """Create a new free @tutamail.com account. Returns (email, password)."""
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{self.sidecar_url}/tuta/create")
            r.raise_for_status()
            d = r.json()
        if not d.get("ok"):
            raise TutaError(d.get("error") or "tuta account creation failed")
        return d["email"], d["password"]

    async def find_activation(
        self,
        email: str,
        password: str,
        timeout: int = 120,
        sender_pattern: str = "pornolab",
    ) -> str | None:
        """Log into the Tuta webmail UI and return the activation link, or
        None if nothing matching arrived within `timeout` seconds."""
        async with httpx.AsyncClient(timeout=timeout + 30) as c:
            r = await c.post(
                f"{self.sidecar_url}/tuta/inbox",
                json={
                    "email": email,
                    "password": password,
                    "sender_pattern": sender_pattern,
                    "max_timeout": timeout,
                },
            )
            r.raise_for_status()
            d = r.json()
        if not d.get("ok"):
            return None
        return d.get("url") or None
