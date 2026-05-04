import urllib.request, ssl, json, http.cookiejar

ctx = ssl._create_unverified_context()

# Create a cookie jar and opener that persists cookies
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
urllib.request.install_opener(opener)

# Step 1: Hit the SSO page to get session cookie
print("Step 1: SSO page...")
r = urllib.request.urlopen('https://127.0.0.1:5000/sso/Login?forwardTo=22&RL=1', context=ctx)
print(f"SSO: {r.status}, cookies: {len(cj)}")

# Step 2: Hit reauthenticate
print("Step 2: Reauthenticate...")
req = urllib.request.Request('https://127.0.0.1:5000/v1/api/iserver/reauthenticate', data=b'{}')
req.add_header('Content-Type', 'application/json')
try:
    r2 = urllib.request.urlopen(req, context=ctx)
    print("Reauth:", json.dumps(json.loads(r2.read()), indent=2))
except Exception as e:
    print("Reauth error:", e)

# Step 3: Check auth
print("Step 3: Auth status...")
r3 = urllib.request.urlopen('https://127.0.0.1:5000/v1/api/iserver/auth/status', context=ctx)
print("Auth:", json.dumps(json.loads(r3.read()), indent=2))
