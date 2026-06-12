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


def get_clearance(site: str, timeout_ms: int = 60000) -> dict | None:
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

            # Phase 2: once past the wall, wait up to 25s for the app to mint
            # its anonymous token. Nudge with a reload at the halfway mark if
            # nothing has shown up yet (slower runners sometimes need it).
            if challenge_cleared:
                for i in range(25):
                    if captured["token"]:
                        break
                    page.wait_for_timeout(1000)
                    if i == 12 and not captured["token"]:
                        try:
                            page.reload(wait_until="domcontentloaded",
                                        timeout=timeout_ms)
                        except Exception:  # noqa: BLE001
                            pass
                # small grace for cf_clearance cookie to settle
                page.wait_for_timeout(1500)

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
