"""Playwright-backed Cloudflare clearance for CPS sites.

CPS booking sites sit behind Cloudflare's JS challenge, which plain HTTP can't
pass. This module drives a real headless Chromium to (a) clear the challenge
(yielding a cf_clearance cookie) and (b) let the Angular app mint its anonymous
bearer token, then captures both so the lightweight CPSProvider can make its
normal API calls.

Playwright is optional. If it isn't installed, get_clearance() returns None and
CPS courses fall back to check-manually links.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("teetime_scout")


def _proxy_for_playwright(session_id=None):
    """Convert RESI_PROXY into Playwright's {server, username, password}.
    With session_id, pin a sticky IP so the browser challenge and the later
    API calls share one residential exit IP (Cloudflare clearance is IP-bound)."""
    raw = os.environ.get("RESI_PROXY", "").strip()
    if not raw:
        return None
    from urllib.parse import urlparse
    u = urlparse(raw)
    server = f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"
    cfg = {"server": server}
    user = u.username or ""
    if session_id and user:
        user = f"{user}__cr.us;sessid.{session_id}"
    if user:
        cfg["username"] = user
    if u.password:
        cfg["password"] = u.password
    return cfg

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except Exception:  # noqa: BLE001
    HAVE_PLAYWRIGHT = False

# cache one browser context per site for the lifetime of the run
_CACHE: dict[str, dict | None] = {}


def get_clearance(site: str, timeout_ms: int = 60000,
                  proxy_session_id: str | None = None) -> dict | None:
    """Return {'cookies': {...}, 'token': str|None, 'user_agent': str} for a
    CPS site, or None if Playwright is unavailable or the challenge isn't
    cleared. Result is cached per site."""
    cache_key = f"{site}:{proxy_session_id}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    if not HAVE_PLAYWRIGHT:
        _CACHE[cache_key] = None
        return None

    base = f"https://{site}.cps.golf"
    result = None
    try:
        with sync_playwright() as pw:
            launch_kwargs = {"headless": True, "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1280,900",
            ]}
            _proxy = _proxy_for_playwright(proxy_session_id)
            if _proxy:
                launch_kwargs["proxy"] = _proxy
                log.info("Playwright: routing %s via residential proxy", site)
            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/148.0.0.0 Safari/537.36"),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                timezone_id="America/Chicago",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "sec-ch-ua": ('"Chromium";v="148", "Google Chrome";v="148", '
                                  '"Not/A)Brand";v="99"'),
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-ch-ua-mobile": "?0",
                },
            )
            # hide the most obvious automation tell before any page script runs
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome={runtime:{}};"
                "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
                "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});")
            captured = {"token": None}

            # sniff the Authorization header off the app's own API calls
            def on_request(req):
                auth = req.headers.get("authorization", "")
                if auth.lower().startswith("bearer ") and not captured["token"]:
                    captured["token"] = auth.split(" ", 1)[1]
            context.on("request", on_request)

            page = context.new_page()
            page.goto(f"{base}/onlineresweb/search-teetime",
                      wait_until="domcontentloaded", timeout=timeout_ms)

            # Phase 1: wait up to 40s for the Cloudflare challenge to clear.
            challenge_cleared = False
            for _ in range(40):
                page.wait_for_timeout(1000)
                if "just a moment" not in (page.title() or "").lower():
                    challenge_cleared = True
                    break
                if captured["token"]:
                    challenge_cleared = True
                    break

            # Phase 2: wait for the app to mint its anonymous token (up to 30s).
            # Some sites (e.g. Edinburgh) only mint on interaction, so we nudge
            # the page: a click, then a deep-link navigation that forces a
            # tee-time query. Reload as a last resort.
            def _nudge(step):
                try:
                    if step == 0:
                        page.mouse.click(640, 450)
                    elif step == 1:
                        page.goto(f"{base}/onlineresweb/search-teetime"
                                  f"?TeeOffTimeMin=0&TeeOffTimeMax=23",
                                  wait_until="domcontentloaded",
                                  timeout=timeout_ms)
                    else:
                        page.reload(wait_until="domcontentloaded",
                                    timeout=timeout_ms)
                except Exception:  # noqa: BLE001
                    pass

            for i in range(30):
                if captured["token"]:
                    break
                page.wait_for_timeout(1000)
                if i in (6, 14, 22) and not captured["token"]:
                    _nudge((i - 6) // 8)
            page.wait_for_timeout(1500)

            cookies = {c["name"]: c["value"]
                       for c in context.cookies()
                       if "cps.golf" in c.get("domain", "")}
            ua = page.evaluate("() => navigator.userAgent")
            challenged = "just a moment" in (page.title() or "").lower()
            browser.close()

            # success if we got a token OR at least the cf_clearance cookie
            if captured["token"] or "cf_clearance" in cookies:
                result = {"cookies": cookies, "token": captured["token"],
                          "user_agent": ua}
                log.info("Playwright: cleared %s (token=%s, cf_clearance=%s)",
                         site, bool(captured["token"]),
                         "cf_clearance" in cookies)
            else:
                log.warning("Playwright: %s — no token or clearance cookie "
                            "(challenged=%s)", site, challenged)
    except Exception as e:  # noqa: BLE001
        log.warning("Playwright clearance for %s failed: %s", site, e)

    _CACHE[cache_key] = result
    return result


def fetch_in_browser(site: str, search_params: dict, headers: dict,
                     timeout_ms: int = 45000,
                     proxy_session_id: str | None = None,
                     deadline_s: float = 150.0) -> dict | None:
    """Retrying entry point: attempt the in-browser register+search up to twice,
    using a fresh sticky proxy session on the retry. Cloudflare clearance is
    IP-bound, so a new exit IP often succeeds where a throttled one failed.

    A wall-clock `deadline_s` caps the TOTAL time spent across attempts so one
    stuck site can't hold up the whole run (the scheduled job runs 6x/day).
    """
    import time as _time
    import uuid as _uuid
    started = _time.monotonic()
    base_sess = proxy_session_id or _uuid.uuid4().hex[:12]
    attempts = [base_sess, _uuid.uuid4().hex[:12]]  # retry on a brand-new exit IP
    for i, sess in enumerate(attempts):
        if _time.monotonic() - started > deadline_s:
            log.warning("fetch_in_browser %s: deadline hit, giving up", site)
            break
        remaining = deadline_s - (_time.monotonic() - started)
        # don't start an attempt that can't plausibly finish its phases
        per_attempt_ms = int(min(timeout_ms, max(20000, remaining * 1000 / 2)))
        out = _fetch_in_browser_once(site, search_params, headers,
                                     timeout_ms=per_attempt_ms,
                                     proxy_session_id=sess)
        if out is not None:
            if i:
                log.info("fetch_in_browser %s: recovered on retry %d", site, i)
            return out
        log.info("fetch_in_browser %s: attempt %d/%d failed", site,
                 i + 1, len(attempts))
    return None


def _fetch_in_browser_once(site: str, search_params: dict, headers: dict,
                           timeout_ms: int = 60000,
                           proxy_session_id: str | None = None) -> dict | None:
    """For strict-tier CPS sites that re-challenge every API call: clear the
    challenge with a real browser, then make the RegisterTransactionId +
    TeeTimes calls FROM INSIDE that browser (via fetch in page context), so
    they inherit the live Cloudflare/JS environment. Returns parsed JSON
    (the TeeTimes response) or None.

    search_params: dict of query params for the TeeTimes call (we add the
    transactionId ourselves). headers: the x-* / authorization / client-id
    headers the API expects.
    """
    if not HAVE_PLAYWRIGHT:
        return None
    base = f"https://{site}.cps.golf"
    api = f"{base}/onlineres/onlineapi/api/v1/onlinereservation"
    result = None
    try:
        with sync_playwright() as pw:
            launch_kwargs = {"headless": True, "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--window-size=1280,900",
            ]}
            _proxy = _proxy_for_playwright(proxy_session_id)
            if _proxy:
                launch_kwargs["proxy"] = _proxy
            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/148.0.0.0 Safari/537.36"),
                viewport={"width": 1280, "height": 900},
                locale="en-US", timezone_id="America/Chicago")
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome={runtime:{}};")
            captured = {"token": None, "teetimes": None}

            def on_request(req):
                auth = req.headers.get("authorization", "")
                if auth.lower().startswith("bearer ") and not captured["token"]:
                    captured["token"] = auth.split(" ", 1)[1]

            def on_response(resp):
                # If the app itself successfully fetches TeeTimes while we're
                # driving it, grab that response body directly — it's the most
                # reliable path (no header/token reconstruction needed).
                try:
                    if "/TeeTimes" in resp.url and resp.status == 200 \
                            and captured["teetimes"] is None:
                        captured["teetimes"] = resp.text()
                except Exception:  # noqa: BLE001
                    pass
            context.on("request", on_request)
            context.on("response", on_response)

            # Phase 2: the app mints its anonymous bearer only after it issues
            # a real tee-time query. On several Minneapolis sites that doesn't
            # happen on load, so nudge the page (click, then a deep-link that
            # forces a search, then reload) until a token is sniffed — up to 30s.
            def _nudge(step):
                try:
                    if step == 0:
                        page.mouse.click(640, 450)
                    elif step == 1:
                        page.goto(f"{base}/onlineresweb/search-teetime"
                                  f"?TeeOffTimeMin=0&TeeOffTimeMax=23",
                                  wait_until="domcontentloaded",
                                  timeout=timeout_ms)
                    else:
                        page.reload(wait_until="domcontentloaded",
                                    timeout=timeout_ms)
                except Exception:  # noqa: BLE001
                    pass

            for i in range(30):
                if captured["token"] or captured["teetimes"]:
                    break
                page.wait_for_timeout(1000)
                if i in (5, 13, 21) and not captured["token"]:
                    _nudge((i - 5) // 8)
            page.wait_for_timeout(1000)

            token = captured["token"]
            challenged = "just a moment" in (page.title() or "").lower()

            # Best case: the app fetched its own TeeTimes while we drove it.
            if captured["teetimes"]:
                try:
                    import json as _json0
                    result = _json0.loads(captured["teetimes"])
                    log.info("fetch_in_browser %s: captured app's own TeeTimes "
                             "response directly", site)
                    browser.close()
                    return result
                except Exception:  # noqa: BLE001
                    pass

            if not token and challenged:
                # never got past Cloudflare at all — nothing we can do here
                log.warning("fetch_in_browser %s: challenge not cleared", site)
                browser.close()
                return None
            # If we cleared the challenge but never sniffed a bearer, still try
            # the API calls from inside the page: the cf_clearance cookie travels
            # automatically and some installs accept the guest call without an
            # explicit Authorization header.

            # build the two URLs
            import json as _json
            import uuid as _uuid
            tid = str(_uuid.uuid4())
            qs = dict(search_params)
            qs["transactionId"] = tid
            from urllib.parse import urlencode
            search_url = f"{api}/TeeTimes?{urlencode(qs)}"
            reg_url = f"{api}/RegisterTransactionId"

            # headers to attach inside the browser fetch
            hdr = {k: v for k, v in headers.items()
                   if k.lower() not in ("host", "content-length", "cookie",
                                        "authorization")}
            if token:
                hdr["authorization"] = f"Bearer {token}"
            hdr["content-type"] = "application/json"
            # CPS expects the register POST and the TeeTimes GET to carry the
            # SAME x-requestid, equal to the transactionId. The headers we were
            # handed came from the failed HTTP attempt and carry a stale id, so
            # overwrite it to match the tid we generate here.
            hdr["x-requestid"] = tid

            # run register then search from INSIDE the page context, so the
            # browser's Cloudflare cookies + JS environment apply automatically
            js = """
            async (args) => {
              const {regUrl, searchUrl, tid, headers} = args;
              try {
                const reg = await fetch(regUrl, {
                  method: 'POST', headers, credentials: 'include',
                  body: JSON.stringify({transactionId: tid})
                });
                const regText = await reg.text();
                const res = await fetch(searchUrl, {
                  method: 'GET', headers, credentials: 'include'
                });
                const text = await res.text();
                return {regStatus: reg.status, regText: regText.slice(0,200),
                        status: res.status, text};
              } catch (e) {
                return {error: String(e)};
              }
            }
            """
            out = page.evaluate(js, {"regUrl": reg_url, "searchUrl": search_url,
                                     "tid": tid, "headers": hdr})
            browser.close()

            if out and out.get("status") == 200:
                try:
                    result = _json.loads(out["text"])
                    log.info("fetch_in_browser %s: OK (reg=%s, token=%s)", site,
                             out.get("regStatus"), bool(token))
                except Exception:  # noqa: BLE001
                    log.warning("fetch_in_browser %s: non-JSON response", site)
            else:
                log.warning("fetch_in_browser %s: search HTTP %s (reg %s) %s",
                            site, out.get("status") if out else "?",
                            out.get("regStatus") if out else "?",
                            (out.get("text", "")[:120] if out
                             else (out.get("error", "") if out else "")))
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_in_browser %s failed: %s", site, e)
    return result
