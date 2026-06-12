"""debug.py — prints exactly what the CPS identity server and Brookview's
tee sheet say at each step, so the providers can be fixed with evidence
instead of guesses.

  python debug.py            # checks chaska + brookview
  python debug.py edinburghusa
"""
from __future__ import annotations

import re
import sys
import uuid

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0 Safari/537.36")


def check_cps(site: str):
    base = f"https://{site}.cps.golf"
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    print(f"\n══ CPS token diagnostics: {site} ══")

    # 0. is the site reachable at all from a script?
    r = s.get(f"{base}/onlineresweb/", timeout=25, headers={"Accept": "text/html"})
    cf = "cf-mitigated" in str(r.headers).lower() or "challenge" in r.text.lower()[:3000]
    print(f"[0] app shell: HTTP {r.status_code}, {len(r.text)} bytes, "
          f"cloudflare-challenge-suspected={cf}")

    # 1. OIDC discovery
    auth_ep = f"{base}/identityapi/connect/authorize"
    token_ep = f"{base}/identityapi/connect/token"
    r = s.get(f"{base}/identityapi/.well-known/openid-configuration", timeout=25)
    print(f"[1] oidc discovery: HTTP {r.status_code}")
    if r.status_code == 200:
        try:
            d = r.json()
            auth_ep = d.get("authorization_endpoint", auth_ep)
            token_ep = d.get("token_endpoint", token_ep)
            print(f"    authorize: {auth_ep}")
            print(f"    token:     {token_ep}")
            print(f"    grant_types: {d.get('grant_types_supported')}")
            print(f"    response_types: {d.get('response_types_supported')}")
        except ValueError:
            print(f"    (non-JSON: {r.text[:150]!r})")
    else:
        print(f"    body: {r.text[:200]!r}")

    # 2. client_credentials attempt
    r = s.post(token_ep, timeout=25, data={
        "grant_type": "client_credentials",
        "client_id": "onlinereswebshortlived",
        "scope": "onlinereservation references"})
    print(f"[2] client_credentials: HTTP {r.status_code} → {r.text[:200]!r}")

    # 3. implicit-flow attempts
    candidates = [
        f"{base}/onlineresweb/",
        f"{base}/onlineresweb/index.html",
        f"{base}/onlineresweb/assets/oidc-login-redirect.html",
        f"{base}/onlineresweb/silent-renew.html",
        f"{base}/onlineresweb/assets/silent-refresh.html",
        f"{base}/onlineresweb/auth-callback",
    ]
    for uri in candidates:
        r = s.get(auth_ep, timeout=25, allow_redirects=False, params={
            "client_id": "onlinereswebshortlived",
            "response_type": "token",
            "scope": "onlinereservation references",
            "redirect_uri": uri,
            "state": uuid.uuid4().hex,
            "nonce": uuid.uuid4().hex})
        loc = r.headers.get("Location", "")
        tail = uri.rsplit("/", 1)[-1] or "(root)"
        if "access_token=" in loc:
            print(f"[3] {tail}: HTTP {r.status_code} ✓ TOKEN IN REDIRECT")
            print(f"    {loc[:120]}...")
            return
        print(f"[3] {tail}: HTTP {r.status_code} loc={loc[:110]!r} "
              f"body={r.text[:110]!r}")


def check_brookview():
    print("\n══ Brookview (prophet v3) diagnostics ══")
    s = requests.Session()
    s.headers.update({"User-Agent": UA,
                      "Accept": "text/html,application/xhtml+xml"})
    base = "https://secure.east.prophetservices.com/BrookviewGCv3"
    params = {"CourseId": "1", "Date": "2026-6-14", "Time": "AnyTime",
              "Player": "99", "Hole": "18"}
    first = s.get(f"{base}/Home/nIndex", params=params, timeout=35)
    print(f"[1] first request: HTTP {first.status_code}, final url: {first.url[:110]}")
    session_url = first.url.split("?")[0]
    r = s.get(session_url, params=params, timeout=35)
    html = r.text
    print(f"[2] session request: HTTP {r.status_code}, {len(html)} bytes")
    print(f"    'no tee times' phrase present: {'no tee times' in html.lower()}")
    matches = list(re.finditer(r"\b\d{1,2}:\d{2}\s*[AP]M\b", html, re.I))
    print(f"    clock-time matches in HTML: {len(matches)}")
    if matches:
        i = matches[0].start()
        print(f"    context around first match:\n    {html[max(0,i-150):i+150]!r}")
    else:
        # show what the page mostly is
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        print(f"    page text sample: {text[:400]!r}")


if __name__ == "__main__":
    site = sys.argv[1] if len(sys.argv) > 1 else "chaska"
    check_cps(site)
    check_brookview()
