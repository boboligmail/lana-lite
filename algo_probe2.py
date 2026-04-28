#!/usr/bin/env python3
"""algo_probe2.py - Phase C1b: 探测 /fapi/v1/algoOrder (官方迁移后的真路径)"""
import os, time, hmac, hashlib, urllib.parse, requests, math
from dotenv import load_dotenv
load_dotenv()

K = os.environ.get("BINANCE_API_KEY")
S = os.environ.get("BINANCE_API_SECRET")
BASE = "https://fapi.binance.com"
if not K or not S:
    raise SystemExit("FATAL: missing API key")

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
    print(f"body: {r.text[:600]}")

# ----- Step 0: DOGEUSDT mark + filters (账户无该币持仓, 远 OTM 不触发) -----
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

# ----- Test A: POST /fapi/v1/algoOrder + closePosition=true -----
pA = {"symbol":SYM,"side":"SELL","type":"STOP_MARKET","stopPrice":stop_far,"closePosition":"true","workingType":"MARK_PRICE"}
r = req("POST", "/fapi/v1/algoOrder", pA)
show("Test A: /fapi/v1/algoOrder  closePosition=true", r)
aidA = None
try:
    if r.status_code == 200:
        j = r.json()
        aidA = j.get("algoId") or j.get("orderId")
        print(f"    -> response keys: {list(j.keys())}")
except Exception as e:
    print(f"    parse err: {e}")

# ----- Test B: POST /fapi/v1/algoOrder + reduceOnly + quantity -----
pB = {"symbol":SYM,"side":"SELL","type":"STOP_MARKET","stopPrice":stop_far,"quantity":qty,"reduceOnly":"true","workingType":"MARK_PRICE"}
r = req("POST", "/fapi/v1/algoOrder", pB)
show("Test B: /fapi/v1/algoOrder  reduceOnly=true", r)
aidB = None
try:
    if r.status_code == 200:
        j = r.json()
        aidB = j.get("algoId") or j.get("orderId")
        print(f"    -> response keys: {list(j.keys())}")
except Exception as e:
    print(f"    parse err: {e}")

# ----- Z1: GET /fapi/v1/openAlgoOrders -----
r = req("GET", "/fapi/v1/openAlgoOrders", {"symbol": SYM})
show("Z1: GET /fapi/v1/openAlgoOrders DOGEUSDT", r)

# ----- Z2: 撤销 Test A / B 的 algoId -----
for aid in [aidA, aidB]:
    if aid:
        r = req("DELETE", "/fapi/v1/algoOrder", {"symbol": SYM, "algoId": aid})
        show(f"Z2 cancel algoId={aid}", r)

# ----- Z3: 兜底 — 撤所有 DOGEUSDT 遗留 algo -----
r = req("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": SYM})
show("Z3: DELETE /fapi/v1/algoOpenOrders DOGEUSDT (兜底全撤)", r)

print("\n=== Phase C1b probe DONE ===")
