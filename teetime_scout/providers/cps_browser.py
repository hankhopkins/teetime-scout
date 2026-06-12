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

log = logging.getLogger("teetime_scout")

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except Exception:  # noqa: BLE001
    HAVE_PLAYWRIGHT = False

# cache one browser context per site for the lifetime of the run
_CACHE: dict[str, dict | None] = {}


def get_clearance(site: str, timeout_ms: int = 45000) -> dict | None:
    """Return {'cookies': {...}, 'token': str|None, 'user_agent': str} for a
    CPS site, or None if Playwright is unavailable or the challenge isn't
    cleared. Result is cached per site."""
    if site in _CACHE:
        return _CACHE[site]
    if not HAVE_PLAYWRIGHT:
        _CACHE[site] = None
        return None

    base = f"https://{site}.cps.golf"
    result = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ])
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/148.0.0.0 Safari/537.36"),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            captured = {"token": None}

            # sniff the Authorization header off the app's own API calls
            def on_request(req):
                auth = req.headers.get("authorization", "")
                if auth.lower().startswith("bearer ") and not captured["token"]:
                    captured["token"] = auth.split(" ", 1)[1]
            context.on("request", on_request)

            page = context.new_page()
            # 'domcontentloaded' not 'networkidle' — these Angular apps poll
            # forever and never reach network idle.
            page.goto(f"{base}/onlineresweb/search-teetime",
                      wait_until="domcontentloaded", timeout=timeout_ms)

            # poll up to ~30s for Cloudflare to clear AND a token to appear
            token_seen = False
            for _ in range(30):
                page.wait_for_timeout(1000)
                title = (page.title() or "").lower()
                if captured["token"]:
                    token_seen = True
                    # small grace so cf_clearance cookie is also set
                    page.wait_for_timeout(1500)
                    break
                if "just a moment" not in title:
                    # challenge cleared; keep waiting briefly for the token
                    continue
            _ = token_seen

            cookies = {c["name"]: c["value"]
                       for c in context.cookies()
                       if site in c.get("domain", "")}
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

    _CACHE[site] = result
    return result
