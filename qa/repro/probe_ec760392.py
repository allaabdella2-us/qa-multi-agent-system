"""Throwaway minimisation probe for finding ec760392.

Question: what is the SHORTEST path that still makes DELETE answer 200 {}?
Cutting one variable at a time: auth required? role? any seeded data?
"""

import json
import urllib.error
import urllib.request

BASE = "http://localhost:8000"


def call(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def login(email):
    status, raw = call("POST", "/v1/auth/login", body={"email": email, "password": "password123"})
    assert status == 200, (status, raw)
    return json.loads(raw)["access_token"]


print("--- cut 1: is auth required at all? ---")
print(f"  DELETE 999999 with no token -> {call('DELETE', '/v1/orders/999999')[0]} (401 => auth needed)")

print("--- cut 2: does it need the admin role? ---")
for role in ("admin", "member", "viewer"):
    tok = login(f"{role}@northwind.test")
    status, body = call("DELETE", "/v1/orders/999999", tok)
    print(f"  DELETE 999999 as {role:<7} -> status={status} body={body!r}")

print("--- cut 3: does the 204 case need a FIXTURE order, or will a self-made one do? ---")
tok = login("viewer@northwind.test")
status, raw = call(
    "POST",
    "/v1/orders",
    tok,
    {"reference": "REPRO-EC76", "items": [{"sku": "X", "description": "d", "quantity": 1, "unit_price_cents": 1}]},
)
print(f"  POST /v1/orders as viewer -> {status}")
if status == 201:
    oid = json.loads(raw)["id"]
    dstatus, dbody = call("DELETE", f"/v1/orders/{oid}", tok)
    print(f"  DELETE own new order {oid} -> status={dstatus} body={dbody!r} (spec says 204, empty)")
    print(f"  GET {oid} after delete -> {call('GET', f'/v1/orders/{oid}', tok)[0]} (404 => really gone)")
