"""PornoLab page parsers. Pure functions over HTML, so they are testable offline.

PornoLab is a TorrentPier forum served as windows-1251. The selectors for the
tracker listing are the ones Prowlarr's own `pornolab` definition uses; the
profile is parsed from its visible text, because the numbers we need are laid
out as prose and a table without stable ids.
"""

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
SIZE_RE = r"[0-9]+(?:[.,][0-9]+)?\s*(?:TB|GB|MB|KB|B)\b"


def parse_size(text: str) -> int:
    """'989.7 MB' -> bytes. PornoLab's units are binary (989.7 MB = 1037762867)."""
    m = re.match(r"\s*([0-9]+(?:[.,][0-9]+)?)\s*(TB|GB|MB|KB|B)\b", text or "", re.I)
    if not m:
        raise ValueError(f"not a size: {text!r}")
    return int(float(m.group(1).replace(",", ".")) * UNITS[m.group(2).upper()])


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(" ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #
@dataclass
class StatColumns:
    """One row of the profile's statistics table, in bytes."""
    last_update: int = 0
    today: int = 0
    yesterday: int = 0
    total: int = 0


@dataclass
class Profile:
    username: str | None = None
    rating: float | None = None
    newbie: bool = False                 # the "after 2 GB downloaded..." banner
    # Raw byte counts from the rating formula line (what the rating is computed on).
    uploaded: int = 0
    uploaded_own: int = 0
    uploaded_bonus: int = 0
    downloaded: int = 0
    # The per-day statistics table.
    down: StatColumns = field(default_factory=StatColumns)
    up: StatColumns = field(default_factory=StatColumns)
    own: StatColumns = field(default_factory=StatColumns)
    bonus: StatColumns = field(default_factory=StatColumns)

    @property
    def credited_upload(self) -> int:
        """Everything the rating formula counts as given."""
        return self.uploaded + self.uploaded_own + self.uploaded_bonus

    @property
    def live_downloaded(self) -> int:
        """Downloaded as of the last 5-10 minute stats update.

        The total ('Всего учтено') only moves once a day, between 00:00 and
        01:00 Moscow time; today's column is what has happened since. Inside
        that hour today's figure may already be in the total, so this can count
        it twice -- an overestimate, which is the safe direction for the guard.
        """
        return max(self.downloaded, self.down.total) + self.down.today

    @property
    def live_credited_upload(self) -> int:
        return (max(self.credited_upload, self.up.total + self.own.total + self.bonus.total)
                + self.up.today + self.own.today + self.bonus.today)


def _row(text: str, label: str) -> StatColumns:
    m = re.search(rf"{label}\s+({SIZE_RE})\s+({SIZE_RE})\s+({SIZE_RE})\s+({SIZE_RE})", text)
    if not m:
        return StatColumns()
    return StatColumns(*(parse_size(m.group(i)) for i in range(1, 5)))


def parse_profile(html: str) -> Profile:
    text = page_text(html)
    p = Profile()

    if m := re.search(r"Профиль пользователя:\s*(\S+)", text):
        p.username = m.group(1)
    if m := re.search(r"Рейтинг:\s*([0-9]+(?:[.,][0-9]+)?)", text):
        p.rating = float(m.group(1).replace(",", "."))
    p.newbie = bool(re.search(r"После 2\s*GB скачанного", text))

    # The formula is printed twice: once with units, once in raw bytes.
    if m := re.search(
        r"\(\s*Всего отдано\s+(\d+)\s*\+\s*на своих раздачах\s+(\d+)\s*\+\s*бонусных\s+(\d+)\s*\)"
        r"\s*/\s*Скачано\s+(\d+)\b(?!\s*(?:TB|GB|MB|KB|B)\b)",
        text,
    ):
        p.uploaded, p.uploaded_own, p.uploaded_bonus, p.downloaded = (int(m.group(i)) for i in range(1, 5))

    # The table starts at its header; searching from there keeps the formula
    # line's "Скачано" (which has no units) from matching.
    start = text.find("Посл. обновл.")
    if start >= 0:
        table = text[start:]
        p.down = _row(table, "Скачано")
        p.up = _row(table, "Отдано")
        p.own = _row(table, "На своих")
        p.bonus = _row(table, "Бонус")

    if not p.downloaded:
        p.downloaded = p.down.total
    return p


# --------------------------------------------------------------------------- #
# Tracker listing
# --------------------------------------------------------------------------- #
@dataclass
class Release:
    topic_id: int
    title: str
    forum: str = ""
    forum_id: int | None = None
    size: int = 0
    seeders: int = 0
    leechers: int = 0
    grabs: int = 0
    added: int | None = None             # unix time
    downloadable: bool = True            # has a dl.php link (closed topics don't)


def _int(text: str | None, default: int = 0) -> int:
    m = re.search(r"-?\d+", text or "")
    return int(m.group(0)) if m else default


def _query_int(href: str | None, key: str) -> int | None:
    m = re.search(rf"[?&]{key}=(\d+)", href or "")
    return int(m.group(1)) if m else None


def parse_tracker(html: str) -> list[Release]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select("table#tor-tbl > tbody > tr"):
        link = row.select_one("a.tLink")
        if not link:
            continue
        topic = _query_int(link.get("href"), "t")
        if topic is None:
            continue
        dl = row.select_one("a.tr-dl")
        forum = row.select_one("a.f")

        size = 0
        if (u := row.select_one("td:nth-child(6) u")) and u.get_text(strip=True).isdigit():
            size = int(u.get_text(strip=True))
        elif dl:
            try:
                size = parse_size(dl.get_text(" ", strip=True).replace("\xa0", " "))
            except ValueError:
                pass

        added = row.select_one("td:nth-child(11) u")
        seed = row.select_one("td.seedmed > b")
        leech = row.select_one("td.leechmed > b")
        grabs = row.select_one("td:nth-child(9)")

        out.append(Release(
            topic_id=topic,
            title=re.sub(r"\s+", " ", link.get_text(" ", strip=True)),
            forum=forum.get_text(" ", strip=True) if forum else "",
            forum_id=_query_int(forum.get("href"), "f") if forum else None,
            size=size,
            seeders=_int(seed.get_text() if seed else None),
            leechers=_int(leech.get_text() if leech else None),
            grabs=_int(grabs.get_text() if grabs else None),
            added=_int(added.get_text(), None) if added else None,
            downloadable=dl is not None,
        ))
    return out


# --------------------------------------------------------------------------- #
# Login and site messages
# --------------------------------------------------------------------------- #
@dataclass
class LoginForm:
    captcha_url: str | None = None
    cap_sid: str | None = None
    cap_field: str | None = None         # the per-session "cap_code_<x>" input name


def parse_login_form(html: str) -> LoginForm:
    soup = BeautifulSoup(html, "html.parser")
    form = LoginForm()
    if img := soup.select_one('img[src*="/captcha/"]'):
        src = img.get("src", "")
        form.captcha_url = "https:" + src if src.startswith("//") else src
    if sid := soup.select_one('input[name="cap_sid"]'):
        form.cap_sid = sid.get("value")
    if code := soup.select_one('input[name^="cap_code_"]'):
        form.cap_field = code.get("name")
    return form


def login_error(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    if el := soup.select_one("h4.warnColor1"):
        return el.get_text(" ", strip=True)
    return None


def is_logged_in(html: str) -> bool:
    return "logout" in html and "Вы зашли как" in html


def find_uid(html: str) -> int | None:
    m = re.search(r"profile\.php\?mode=viewprofile&(?:amp;)?u=(\d+)", html)
    return int(m.group(1)) if m else None


def site_message(html: str) -> str | None:
    """The text of a TorrentPier message box ('download limit reached', ...)."""
    soup = BeautifulSoup(html, "html.parser")
    if el := soup.select_one("table.message .mrg_16") or soup.select_one("table.message td"):
        return el.get_text(" ", strip=True)
    return None


# --------------------------------------------------------------------------- #
# Registration form
# --------------------------------------------------------------------------- #
@dataclass
class RegisterForm:
    captcha_url: str | None = None
    cap_sid: str | None = None
    cap_field: str | None = None          # per-session "cap_code_<x>" input name
    turnstile_token: str | None = None    # cf-turnstile-response, filled by the headless browser
    countries: list[tuple[str, str]] = field(default_factory=list)   # (value, label)
    timezones: list[tuple[str, str]] = field(default_factory=list)   # (value, label)
    # Cookies from the browser session that fetched the form — kept separate
    # from the owner's session so the register POST goes out as anonymous.
    browser_cookies: list[dict] = field(default_factory=list)


def parse_register_form(html: str) -> RegisterForm:
    soup = BeautifulSoup(html, "html.parser")
    form = RegisterForm()
    if img := soup.select_one('img[src*="/captcha/"]'):
        src = img.get("src", "")
        form.captcha_url = "https:" + src if src.startswith("//") else src
    if sid := soup.select_one('input[name="cap_sid"]'):
        form.cap_sid = sid.get("value")
    if code := soup.select_one('input[name^="cap_code_"]'):
        form.cap_field = code.get("name")
    if ts := soup.select_one('input[name="cf-turnstile-response"]'):
        v = ts.get("value", "")
        if v:
            form.turnstile_token = v
    if sel := soup.select_one('select[name="user_flag_id"]'):
        form.countries = [(o.get("value", ""), o.get_text(strip=True)) for o in sel.select("option")]
    if sel := soup.select_one('select[name="user_timezone_x2"]'):
        form.timezones = [(o.get("value", ""), o.get_text(strip=True)) for o in sel.select("option")]
    return form


def parse_register_error(html: str) -> str | None:
    """Return the site's registration error message, if any."""
    soup = BeautifulSoup(html, "html.parser")
    # div.msg is used for field-level validation errors (e.g. "wrong captcha").
    if el := soup.select_one("div.msg"):
        return el.get_text(" ", strip=True)
    # .warnColor1 can carry errors, but skip legend elements — they are TOS
    # section headers on the registration form, not error indicators.
    for sel in ("h4.warnColor1", "span.warnColor1", "div.warnColor1", "p.warnColor1"):
        if el := soup.select_one(sel):
            return el.get_text(" ", strip=True)
    for el in soup.select(".warnColor1"):
        if el.name != "legend":
            return el.get_text(" ", strip=True)
    # Generic site message box (download-limit page, etc.)
    if msg := site_message(html):
        return msg
    return None


def register_success(html: str) -> bool:
    """True if the registration success page is shown."""
    return "Письмо с инструкцией по активации" in html or "activation" in html.lower()
