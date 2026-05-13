import os
import ssl
import urllib.request
from urllib.parse import urlencode

ctx = ssl._create_unverified_context()

# Test if ANY API endpoint works without auth
endpoints = [
    "/v1/api/portfolio/DUQ319869/positions/0",
    "/v1/api/iserver/accounts",
    "/v1/api/one/user",
    "/v1/api/tickle",
]

print("=== Testing endpoints ===")
for ep in endpoints:
    try:
        r = urllib.request.urlopen(f"https://127.0.0.1:5000{ep}", context=ctx, timeout=5)
        print(f"{ep}: {r.status}")
    except Exception as e:
        print(f"{ep}: {e}")

# Try auth with form data to SSO
print("\n=== Trying SSO auth ===")
import http.client

try:
    username = os.environ.get("IBKR_USERNAME", "")
    password = os.environ.get("IBKR_PASSWORD", "")
    if not username or not password:
        raise SystemExit("Set IBKR_USERNAME and IBKR_PASSWORD before running this helper.")

    conn = http.client.HTTPSConnection("127.0.0.1", 5000, context=ctx)
    body = urlencode({"username": username, "password": password, "hasSecondFactor": "false"})
    conn.request("POST", "/sso/Login", body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = conn.getresponse()
    print(f"SSO Login: {resp.status}")
    print(f"Headers: {dict(resp.getheaders())}")
    conn.close()
except Exception as e:
    print(f"SSO error: {e}")
