import urllib.request, ssl, json, http.cookiejar

ctx = ssl._create_unverified_context()
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
urllib.request.install_opener(opener)

# Step 1: Login via SSO
import http.client
conn = http.client.HTTPSConnection('127.0.0.1', 5000, context=ctx)
body = 'username=apexpredatoribkr&password=Rogue199478%21&hasSecondFactor=false'
conn.request('POST', '/sso/Login', body=body,
             headers={'Content-Type': 'application/x-www-form-urlencoded'})
resp = conn.getresponse()
print(f"Login: {resp.status}")
# Grab cookies from SSO response
for hdr in resp.getheaders():
    if hdr[0].lower() == 'set-cookie':
        c = hdr[1].split(';')[0]
        name, val = c.split('=', 1)
        cj.set_cookie(http.cookiejar.Cookie(0, name, val, None, False, '127.0.0.1', True, False, '/', False, False, None, False, None, None, {}))
conn.close()

print(f"Cookies: {len(cj)}")

# Step 2: Check auth
r = urllib.request.urlopen('https://127.0.0.1:5000/v1/api/iserver/auth/status', context=ctx)
auth = json.loads(r.read())
print(f"Auth: {json.dumps(auth, indent=2)}")

# Step 3: Get accounts
if auth.get('authenticated'):
    r2 = urllib.request.urlopen('https://127.0.0.1:5000/v1/api/portfolio/accounts', context=ctx)
    print(f"Accounts: {json.dumps(json.loads(r2.read()), indent=2)}")
    
    # Step 4: Get orders
    r3 = urllib.request.urlopen('https://127.0.0.1:5000/v1/api/iserver/account/orders', context=ctx)
    print(f"Orders: {json.dumps(json.loads(r3.read()), indent=2)}")
