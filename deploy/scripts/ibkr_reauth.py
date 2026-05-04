import urllib.request, ssl, json

ctx = ssl._create_unverified_context()

print("Step 1: Reauthenticate...")
r = urllib.request.urlopen("https://127.0.0.1:5000/v1/api/iserver/reauthenticate", data=b"{}", context=ctx)
print(json.dumps(json.loads(r.read()), indent=2))

print("Step 2: Tickle...")
r2 = urllib.request.urlopen("https://127.0.0.1:5000/v1/api/tickle", data=b"{}", context=ctx)
print("tickle ok")

print("Step 3: Verify auth...")
r3 = urllib.request.urlopen("https://127.0.0.1:5000/v1/api/iserver/auth/status", context=ctx)
d = json.loads(r3.read())
print(f"authenticated={d.get('authenticated')}, connected={d.get('connected')}")

if d.get("authenticated"):
    print("Step 4: Check orders...")
    r4 = urllib.request.urlopen("https://127.0.0.1:5000/v1/api/iserver/account/orders", context=ctx)
    orders = json.loads(r4.read()).get("orders", [])
    print(f"Live orders: {len(orders)}")
else:
    print("Still not authenticated — needs browser login on VPS")
