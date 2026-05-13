import http.cookiejar
import json
import ssl
import urllib.request

ctx = ssl._create_unverified_context()
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
urllib.request.install_opener(opener)

# Try SSO OAuth flow - the official programmatic auth
print("=== SSO OAuth Init ===")
try:
    r = urllib.request.urlopen("https://127.0.0.1:5000/v1/api/iserver/auth/ssodh/init", context=ctx, timeout=10)
    d = json.loads(r.read())
    print(json.dumps(d, indent=2))
except Exception as e:
    print(f"SSO Init error: {e}")

# Try with POST
print("\n=== SSO OAuth Init (POST) ===")
try:
    data = json.dumps({"publish": True, "competition": False}).encode()
    req = urllib.request.Request("https://127.0.0.1:5000/v1/api/iserver/auth/ssodh/init", data=data)
    req.add_header("Content-Type", "application/json")
    r2 = urllib.request.urlopen(req, context=ctx, timeout=10)
    print(json.dumps(json.loads(r2.read()), indent=2))
except Exception as e:
    print(f"SSO POST error: {e}")

# Try gateway status
print("\n=== Gateway Status ===")
try:
    r3 = urllib.request.urlopen("https://127.0.0.1:5000/v1/api/one/user", context=ctx, timeout=5)
    print(json.dumps(json.loads(r3.read()), indent=2))
except Exception as e:
    print(f"User: {e}")
