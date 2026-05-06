import http.cookiejar
import json
import ssl
import urllib.request

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
print(f"SSO Login: {resp.status}, cookies in response: {resp.getheader('Set-Cookie')}")
conn.close()

# Step 2: Try SSO validate endpoint
req = urllib.request.Request('https://127.0.0.1:5000/sso/Validate?forwardTo=22&RL=1')
try:
    r = urllib.request.urlopen(req, context=ctx)
    print(f"SSO Validate: {r.status}")
except Exception as e:
    print(f"SSO Validate: {e}")

# Step 3: Reauthenticate
data = b'{}'
req2 = urllib.request.Request('https://127.0.0.1:5000/v1/api/iserver/reauthenticate', data=data)
req2.add_header('Content-Type', 'application/json')
try:
    r2 = urllib.request.urlopen(req2, context=ctx)
    print(f"Reauth: {json.dumps(json.loads(r2.read()), indent=2)}")
except Exception as e:
    print(f"Reauth: {e}")

# Step 4: Tickle (keepalive)
try:
    r3 = urllib.request.urlopen('https://127.0.0.1:5000/v1/api/tickle', context=ctx)
    print(f"Tickle: {json.dumps(json.loads(r3.read()), indent=2)}")
except Exception as e:
    print(f"Tickle: {e}")
