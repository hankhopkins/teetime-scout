"""Shared data model + base class for tee-sheet providers."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, date

import requests

try:
    from curl_cffi import requests as cffi_requests
    HAVE_CFFI = True
except Exception:  # noqa: BLE001
    HAVE_CFFI = False

log = logging.getLogger("teetime_scout")


def get_proxy(session_id: str | None = None) -> str | None:
    """Residential proxy URL from env, e.g.
    http://user:pass@gw.dataimpulse.com:823 . Empty/unset means no proxy.

    If session_id is given, pin a sticky residential IP by appending a
    DataImpulse-style session tag to the username, so every request in this
    course's flow (browser challenge + API calls) uses the SAME exit IP.
    Cloudflare clearance is IP-bound, so this is essential."""
    p = os.environ.get("RESI_PROXY", "").strip()
    if not p:
        return None
    if session_id:
        from urllib.parse import urlparse, urlunparse
        u = urlparse(p)
        if u.username and u.password:
            # DataImpulse sticky: username followed by ;session=<id>
            new_user = f"{u.username};session={session_id}"
            netloc = f"{new_user}:{u.password}@{u.hostname}"
            if u.port:
                netloc += f":{u.port}"
            p = urlunparse((u.scheme, netloc, u.path, u.params, u.query, u.fragment))
    return p


def make_session(impersonate: bool = False, use_proxy: bool = False,
                 session_id: str | None = None):
    """Return a requests-like session. impersonate=True mimics a real Chrome
    TLS fingerprint (via curl_cffi). use_proxy=True routes through RESI_PROXY
    if one is configured."""
    proxy = get_proxy(session_id) if use_proxy else None
    if impersonate and HAVE_CFFI:
        s = cffi_requests.Session(impersonate="chrome")
    else:
        s = requests.Session()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


@dataclass
class TeeTime:
    """One available tee time, normalized across providers."""
    when: datetime              # tz-aware, local course time
    open_spots: int | None = None
    price: float | None = None  # per-player green fee, dollars
    holes: int | None = None
    note: str = ""


@dataclass
class FetchResult:
    """Times found for one course on one date, or an error explaining why not."""
    course_name: str
    day: date
    times: list[TeeTime] = field(default_factory=list)
    error: str | None = None


class Provider:
    """Base class. Subclasses implement fetch_day()."""

    name = "base"

    impersonate = False   # subclasses set True to route via curl_cffi
    use_proxy = False     # subclasses set True to route via RESI_PROXY

    def __init__(self, course_cfg: dict, settings: dict):
        self.cfg = course_cfg
        self.settings = settings
        # one sticky proxy session per provider instance keeps Cloudflare
        # clearance and API calls on the same residential IP
        import uuid as _uuid
        self._proxy_session_id = _uuid.uuid4().hex[:12]
        self.session = make_session(self.impersonate, self.use_proxy,
                                    self._proxy_session_id)
        self.session.headers.update({"User-Agent": UA, "Accept": "application/json"})

    # -- interface ------------------------------------------------------------
    def fetch_day(self, day: date) -> FetchResult:  # pragma: no cover
        raise NotImplementedError

    # -- helpers --------------------------------------------------------------
    def _result(self, day: date, times=None, error=None) -> FetchResult:
        return FetchResult(self.cfg["name"], day, times or [], error)

    def _get_json(self, url: str, **kw):
        resp = self.session.get(url, timeout=25, **kw)
        resp.raise_for_status()
        return resp.json()
