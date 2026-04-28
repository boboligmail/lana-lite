#!/usr/bin/env python3
"""algo_probe.py - Phase C1: 探测 STOP_MARKET 端点签名 (DOGEUSDT 远 OTM, 不会真触发)"""
import os, time, hmac, hashlib, urllib.parse, requests, math
from dotenv import load_dotenv
load_dotenv()

K = os.environ.get("BINANCE_API_KEY")
S = os.environ.get("BINANCE_API_SECRET")
BASE = "https://fapi.binance.com"
if not K or not S:
    raise SystemExit("FATAL: missing BINANCE_API_KEY / SECRET")

def sign(p):
    q = urllib.parse.urlencode(p)
    g = hmac.new(S.encode(), q.encode(), hashlib.sha256).hexdigest()
    return q + "&signature=" + g

def req(method, path, params=None, signed=True):
    h = {"X-MBX-APIKEY": K}
    p = dict(params or {})
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 5000
        url = BASE + path + "?" + sign(p)
        return requests.request(method, url, headers=h, timeout=10)
    return requests.request(method, BASE + path, headers=h, params=p, timeout=10)

def show(label, r):
    print(f"\n===== {label} =====")
    print(f"status={r.status_code}")
    print(f"body: {r.text[:500]}")

# ----- Step 0: DOGEUSDT mark + filters (账户无该币持仓) -----
SYM = "DOGEUSDT"
r = req("GET", "/fapi/v1/premiumIndex", {"symbol": SYM}, signed=False)
mark = float(r.json()["markPrice"])
r = req("GET", "/fapi/v1/exchangeInfo", {}, signed=False)
info = next(s for s in r.json()["symbols"] if s["symbol"] == SYM)
flt = {f["filterType"]: f for f in info["filters"]}
step = float(flt["LOT_SIZE"]["stepSize"])
min_qty = float(flt["LOT_SIZE"]["minQty"])
min_not = float(flt.get("MIN_NOTIONAL", {}).get("notional", "5"))
qty = max(min_qty, math.ceil((min_not / mark) / step) * step)
qty = round(qty, 8)
stop_far = round(mark * 1.5, 5)
print(f"[0] mark={mark}, qty={qty}, notional={qty*mark:.4f}U, stopPrice(far OTM)={stop_far}")

# ----- Test 1: classic /fapi/v1/order + closePosition=true -----
p1 = {"symbol":SYM,"side":"SELL","type":"STOP_MARKET","stopPrice":stop_far,"closePosition":"true","workingType":"MARK_PRICE"}
r = req("POST", "/fapi/v1/order", p1)
show("Test 1: /fapi/v1/order  closePosition=true", r)
try: oid1 = r.json().get("orderId") if r.status_code == 200 else None
except: oid1 = None

# ----- Test 2: classic /fapi/v1/order + reduceOnly + quantity -----
p2 = {"symbol":SYM,"side":"SELL","type":"STOP_MARKET","stopPrice":stop_far,"quantity":qty,"reduceOnly":"true","workingType":"MARK_PRICE"}
r = req("POST", "/fapi/v1/order", p2)
show("Test 2: /fapi/v1/order  reduceOnly=true", r)
try: oid2 = r.json().get("orderId") if r.status_code == 200 else None
except: oid2 = None

# ----- Test 3: /fapi/v1/algo/futures/newOrder (-4120 推荐) -----
p3 = {"symbol":SYM,"side":"SELL","type":"STOP_MARKET","stopPrice":stop_far,"quantity":qty,"reduceOnly":"true","workingType":"MARK_PRICE"}
r = req("POST", "/fapi/v1/algo/futures/newOrder", p3)
show("Test 3: /fapi/v1/algo/futures/newOrder", r)
try: aid3 = r.json().get("algoId") if r.status_code == 200 else None
except: aid3 = None

# ----- Z1: 查询 open orders (DOGEUSDT) -----
r = req("GET", "/fapi/v1/openOrders", {"symbol": SYM})
show("Z1: GET /fapi/v1/openOrders DOGEUSDT", r)

# ----- Z2: 撤销 Test 1/2 的订单 -----
for oid in [oid1, oid2]:
    if oid:
        r = req("DELETE", "/fapi/v1/order", {"symbol": SYM, "orderId": oid})
        show(f"Z2 cancel orderId={oid}", r)

# ----- Z3: 撤销 algo 订单 -----
if aid3:
    r = req("DELETE", "/fapi/v1/algo/futures/order", {"algoId": aid3})
    show(f"Z3 cancel algoId={aid3}", r)

print("\n=== Phase C1 probe DONE ===")
