import json
import ssl
import urllib.request

ctx = ssl._create_unverified_context()

# Check auth status
r = urllib.request.urlopen("https://127.0.0.1:5000/v1/api/iserver/auth/status", context=ctx)
d = json.loads(r.read())
print("AUTH:", json.dumps(d, indent=2))

# Check if we can access account
try:
    r2 = urllib.request.urlopen("https://127.0.0.1:5000/v1/api/portfolio/accounts", context=ctx)
    print("ACCOUNTS:", json.dumps(json.loads(r2.read()), indent=2))
except Exception as e:
    print("ACCOUNTS ERROR:", e)

# Check orders
try:
    r3 = urllib.request.urlopen("https://127.0.0.1:5000/v1/api/iserver/account/orders", context=ctx)
    print("ORDERS:", json.dumps(json.loads(r3.read()), indent=2))
except Exception as e:
    print("ORDERS ERROR:", e)

# Try sso/validate
try:
    r4 = urllib.request.urlopen("https://127.0.0.1:5000/sso/Login?forwardTo=22&RL=1", context=ctx)
    print("SSO page returned:", r4.status)
except Exception as e:
    print("SSO ERROR:", e)
