"""CPS Golf (Club Prophet "onlineresweb") provider — Edinburgh USA,
Highland National, Victory Links, Gross National.

Real API, captured from the live Edinburgh site (June 2026):
  GET https://{site}.cps.golf/onlineres/onlineapi/api/v1/onlinereservation/TeeTimes
      ?searchDate=Fri Jun 12 2026&holes=18&numberOfPlayer=0&courseIds=2,1
      &searchTimeType=0&teeOffTimeMin=0&teeOffTimeMax=23&isChangeTeeOffTime=true
      &teeSheetSearchView=5&classCode=GP&defaultOnlineRate=N
      &isUseCapacityPricing=false&memberStoreId=1&searchType=1
  headers: x-apikey {guid}, client-id onlineresweb, x-componentid 1,
           x-productid 1, x-moduleid 7, x-siteid 1, x-terminalid 3, ...

The x-apikey is per-site. Pin it in config as cps: { api_key: ... }. If it is
not pinned, the provider tries to bootstrap it from the app's unauthenticated
configuration endpoints. The request is sent WITHOUT a user login token; if a
site insists on one, the error will say so.
"""
from __future__ import annotations

import json
import re
import uuid as uuidlib
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .base import HAVE_CFFI, Provider, TeeTime, log
from .cps_browser import get_clearance

GUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class CPSProvider(Provider):
    name = "cps"
    impersonate = True   # CPS sits behind Cloudflare; mimic a real Chrome

    def __init__(self, course_cfg, settings):
        super().__init__(course_cfg, settings)
        c = course_cfg["cps"]
        self.site = c["site"]
        self.api_key = c.get("api_key")
        self.website_id = c.get("website_id")
        self.course_ids = str(c.get("course_ids", "1"))
        self.class_code = c.get("class_code", "R")
        self.member_store_id = str(c.get("member_store_id", 1))
        self.site_id = str(c.get("site_id", 1))
        self.tz = ZoneInfo(settings["timezone"])
        self._bootstrapped = False
        self._token = None
        self._token_tried = False
        self._cleared = False

    # -- bootstrap: find the site's api key without a browser -----------------
    def _bootstrap(self):
        if self._bootstrapped or self.api_key:
            return
        self._bootstrapped = True
        base = f"https://{self.site}.cps.golf"
        candidates = [
            f"{base}/onlineres/onlineapi/api/v1/onlinereservation/Configuration",
            f"{base}/onlineres/onlineapi/api/v1/Configuration",
            f"{base}/onlineresweb/api/v1/Configuration",
            f"{base}/onlineresweb/assets/config/config.json",
        ]
        for url in candidates:
            try:
                resp = self.session.get(url, timeout=20,
                                        headers={"Accept": "application/json",
                                                 "client-id": "onlineresweb"})
                if resp.status_code >= 400:
                    continue
                text = resp.text
            except Exception:  # noqa: BLE001
                continue
            m = re.search(r'(?:apiKey|api_key)["\']?\s*[:=]\s*["\']('
                          + GUID_RE.pattern + r')', text, re.I)
            if m:
                self.api_key = m.group(1)
                log.info("CPS %s: bootstrapped api key from %s", self.site,
                         url.rsplit("/", 2)[-1])
            m = re.search(r'websiteId["\']?\s*[:=]\s*["\']('
                          + GUID_RE.pattern + r')', text, re.I)
            if m:
                self.website_id = m.group(1)
            if self.api_key:
                return

    def _get_anonymous_token(self) -> str | None:
        """CPS requires a Bearer even for guests. The web app obtains a
        short-lived anonymous token via the OAuth *implicit flow*: it hits
        /identityapi/connect/authorize with client 'onlinereswebshortlived'
        and receives the access token in the redirect URL fragment. We
        replicate that without a browser by reading the Location header."""
        if self._token_tried:
            return self._token
        self._token_tried = True
        base = f"https://{self.site}.cps.golf"

        # discover endpoints (also tells us the server is reachable)
        auth_ep = f"{base}/identityapi/connect/authorize"
        token_ep = f"{base}/identityapi/connect/token"
        try:
            disco = self._get_json(
                f"{base}/identityapi/.well-known/openid-configuration")
            auth_ep = disco.get("authorization_endpoint", auth_ep)
            token_ep = disco.get("token_endpoint", token_ep)
        except Exception as e:  # noqa: BLE001
            log.debug("CPS %s: oidc discovery failed: %s", self.site, e)

        # attempt 1: client_credentials (some installs allow it)
        for payload in (
            {"grant_type": "client_credentials",
             "client_id": "onlinereswebshortlived",
             "scope": "onlinereservation references"},
        ):
            try:
                resp = self.session.post(token_ep, data=payload, timeout=20,
                                         headers={"Accept": "application/json"})
                if resp.status_code == 200:
                    tok = resp.json().get("access_token")
                    if tok:
                        self._token = tok
                        log.info("CPS %s: token via client_credentials", self.site)
                        return tok
            except Exception as e:  # noqa: BLE001
                log.debug("CPS %s client_credentials failed: %s", self.site, e)

        # attempt 2: implicit flow — token rides in the redirect fragment
        redirect_candidates = [
            f"{base}/onlineresweb/",
            f"{base}/onlineresweb/index.html",
            f"{base}/onlineresweb/assets/oidc-login-redirect.html",
            f"{base}/onlineresweb/silent-renew.html",
            f"{base}/onlineresweb/assets/silent-refresh.html",
            f"{base}/onlineresweb/auth-callback",
        ]
        for redirect_uri in redirect_candidates:
            params = {
                "client_id": "onlinereswebshortlived",
                "response_type": "token",
                "scope": "onlinereservation references",
                "redirect_uri": redirect_uri,
                "state": uuidlib.uuid4().hex,
                "nonce": uuidlib.uuid4().hex,
            }
            try:
                resp = self.session.get(auth_ep, params=params, timeout=20,
                                        allow_redirects=False)
            except Exception as e:  # noqa: BLE001
                log.debug("CPS %s authorize failed: %s", self.site, e)
                continue
            location = resp.headers.get("Location", "")
            m = re.search(r"[#&]access_token=([^&]+)", location)
            if m:
                self._token = m.group(1)
                log.info("CPS %s: token via implicit flow (%s)",
                         self.site, redirect_uri.rsplit("/", 1)[-1] or "root")
                return self._token
            log.debug("CPS %s authorize %s -> %s %s", self.site,
                      redirect_uri, resp.status_code, location[:120])
        log.warning("CPS %s: could not obtain anonymous token "
                    "(curl_cffi=%s)", self.site, HAVE_CFFI)
        return None

    def _apply_clearance(self):
        """When Cloudflare blocks us, drive a real browser once to obtain the
        cf_clearance cookie + anonymous token, then reuse them over HTTP."""
        if self._cleared:
            return
        self._cleared = True
        clear = get_clearance(self.site)
        if not clear:
            return
        for k, v in clear["cookies"].items():
            try:
                self.session.cookies.set(k, v, domain=f"{self.site}.cps.golf")
            except Exception:  # noqa: BLE001
                pass
        if clear.get("user_agent"):
            self.session.headers["User-Agent"] = clear["user_agent"]
        if clear.get("token"):
            self._token = clear["token"]
            self._token_tried = True   # skip the (blocked) HTTP token dance
            log.info("CPS %s: using browser-captured token", self.site)

    def _headers(self):
        h = {
            "Accept": "application/json, text/plain, */*",
            "client-id": "onlineresweb",
            "x-componentid": "1",
            "x-productid": "1",
            "x-moduleid": "7",
            "x-siteid": self.site_id,
            "x-terminalid": "3",
            "x-ismobile": "false",
            "x-timezoneid": str(self.tz),
            "x-requestid": str(uuidlib.uuid4()),
            "Referer": f"https://{self.site}.cps.golf/onlineresweb/search-teetime",
        }
        if self.api_key:
            h["x-apikey"] = self.api_key
        if self.website_id:
            h["x-websiteid"] = self.website_id
        token = self._get_anonymous_token()
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    # -- parsing ---------------------------------------------------------------
    @staticmethod
    def _extract_slots(data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("teeTimes", "teetimes", "data", "result", "items",
                        "availableTeeTimes"):
                v = data.get(key)
                if isinstance(v, list):
                    return v
        return []

    def _parse_slots(self, slots, day: date):
        times = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            raw = (slot.get("startTime") or slot.get("teeTime")
                   or slot.get("time") or slot.get("startDateTime")
                   or slot.get("teeTimeDisplay"))
            if not raw:
                continue
            when = None
            s = str(raw)
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                when = (dt.replace(tzinfo=self.tz) if dt.tzinfo is None
                        else dt.astimezone(self.tz))
            except ValueError:
                for fmt in ("%H:%M", "%I:%M %p"):
                    try:
                        when = datetime.combine(
                            day, datetime.strptime(s.strip(), fmt).time(),
                            tzinfo=self.tz)
                        break
                    except ValueError:
                        continue
            if when is None or when.date() != day:
                continue
            spots = (slot.get("availableParticipantNo")
                     or slot.get("avaliableParticipantNo")   # CPS typo, seen in the wild
                     or slot.get("availableSpots") or slot.get("maxPlayer"))
            if isinstance(spots, list):
                spots = max(spots) if spots else None
            price = (slot.get("shItemPrice") or slot.get("price")
                     or slot.get("greenFee") or slot.get("greenFee18")
                     or slot.get("cartRate"))
            times.append(TeeTime(
                when=when,
                open_spots=int(spots) if spots is not None else None,
                price=float(price) if price is not None else None,
                holes=self.settings.get("holes", 18),
            ))
        return times

    # -- fetch -------------------------------------------------------------
    def _register_transaction(self, tid: str) -> bool:
        """Register a specific transaction GUID so the subsequent search will
        accept it. The endpoint returns `true` on success."""
        base = (f"https://{self.site}.cps.golf/onlineres/onlineapi/api/v1/"
                f"onlinereservation/RegisterTransactionId")
        hdrs = self._headers()
        for kind, params, body in (
            ("params", {"transactionId": tid}, {}),
            ("json", None, {"transactionId": tid}),
            ("json", None, tid),
        ):
            try:
                if kind == "params":
                    resp = self.session.post(base, params=params, json=body,
                                             timeout=20, headers=hdrs)
                else:
                    resp = self.session.post(base, json=body,
                                             timeout=20, headers=hdrs)
            except Exception as e:  # noqa: BLE001
                log.debug("CPS %s register err: %s", self.site, e)
                continue
            if resp.status_code < 400 and (
                    "true" in resp.text.strip().lower() or resp.status_code == 200):
                log.info("CPS %s: registered transaction %s", self.site, tid)
                return True
            log.debug("CPS %s register (%s): HTTP %s %s", self.site, kind,
                      resp.status_code, resp.text[:120])
        log.warning("CPS %s: could not register transaction id", self.site)
        return False

    def fetch_day(self, day: date):
        self._apply_clearance()   # browser-clear Cloudflare if available
        self._bootstrap()         # best-effort api key

        txn_id = str(uuidlib.uuid4())
        self._register_transaction(txn_id)

        params = {
            "searchDate": day.strftime("%a %b %d %Y"),
            "holes": str(self.settings.get("holes", 18)),
            "numberOfPlayer": "0",
            "courseIds": self.course_ids,
            "searchTimeType": "0",
            "transactionId": txn_id,
            "teeOffTimeMin": "0",
            "teeOffTimeMax": "23",
            "isChangeTeeOffTime": "true",
            "teeSheetSearchView": "5",
            "classCode": self.class_code,
            "defaultOnlineRate": "N",
            "isUseCapacityPricing": "false",
            "memberStoreId": self.member_store_id,
            "searchType": "1",
        }
        url = (f"https://{self.site}.cps.golf/onlineres/onlineapi/api/v1/"
               f"onlinereservation/TeeTimes")
        try:
            resp = self.session.get(url, params=params, timeout=30,
                                    headers=self._headers())
            if resp.status_code == 403 and not self._cleared:
                self._apply_clearance()
                resp = self.session.get(url, params=params, timeout=30,
                                        headers=self._headers())
        except Exception as e:  # noqa: BLE001
            return self._result(day, error=f"CPS request failed: {e}")

        if resp.status_code == 403:
            return self._result(day, error=(
                f"CPS '{self.site}': blocked by Cloudflare (403). "
                f"{'Playwright cleared but the API still refused' if self._cleared else 'Playwright unavailable or could not clear the challenge'} "
                f"— this course shows a check-manually link."))

        if resp.status_code == 401:
            return self._result(day, error=(
                f"CPS '{self.site}' returned 401 — token flow blocked, likely "
                f"by Cloudflare. curl_cffi impersonation didn't clear it; this "
                f"site needs the headless-browser path."))
        if resp.status_code >= 400:
            return self._result(day, error=(
                f"CPS HTTP {resp.status_code}: {resp.text[:200]}".replace("\n", " ")))

        try:
            data = resp.json()
        except ValueError:
            return self._result(day, error=(
                f"CPS returned non-JSON: {resp.text[:150]}"))

        slots = self._extract_slots(data)
        times = self._parse_slots(slots, day)
        if slots and not times:
            return self._result(day, error=(
                f"CPS returned {len(slots)} slots but none parsed — first slot: "
                f"{json.dumps(slots[0])[:250]}"))
        return self._result(day, times=times)
