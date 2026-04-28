#!/usr/bin/env python3
"""algo_probe3.py - Phase C1d: 用官方正确参数 probe (algoType=CONDITIONAL, triggerPrice)"""
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
    print(f"body: {r.text[:700]}")

# Step 0: DOGEUSDT mark + filters
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
trigger_far = round(mark * 1.5, 5)
print(f"[0] mark={mark}, qty={qty}, notional={qty*mark:.4f}U, triggerPrice(far OTM)={trigger_far}")

# Test A: algoType=CONDITIONAL + STOP_MARKET + closePosition=true (推荐路径)
pA = {
    "algoType": "CONDITIONAL",
    "symbol": SYM,
    "side": "SELL",
    "type": "STOP_MARKET",
    "triggerPrice": trigger_far,
    "closePosition": "true",
    "workingType": "MARK_PRICE",
}
r = req("POST", "/fapi/v1/algoOrder", pA)
show("Test A: algoType=CONDITIONAL  STOP_MARKET  closePosition=true", r)
aidA = None
cidA = None
try:
    if r.status_code == 200:
        j = r.json()
        aidA = j.get("algoId")
        cidA = j.get("clientAlgoId")
        st_a = j.get("algoStatus")
        ot_a = j.get("orderType")
        print(f"    -> algoId={aidA}, clientAlgoId={cidA}, algoStatus={st_a}, type={ot_a}")
except Exception as e:
    print(f"    parse err: {e}")

# Test B: algoType=CONDITIONAL + STOP_MARKET + reduceOnly + quantity (备选路径)
pB = {
    "algoType": "CONDITIONAL",
    "symbol": SYM,
    "side": "SELL",
    "type": "STOP_MARKET",
    "triggerPrice": trigger_far,
    "quantity": qty,
    "reduceOnly": "true",
    "workingType": "MARK_PRICE",
}
r = req("POST", "/fapi/v1/algoOrder", pB)
show("Test B: algoType=CONDITIONAL  STOP_MARKET  reduceOnly=true  quantity", r)
aidB = None
try:
    if r.status_code == 200:
        j = r.json()
        aidB = j.get("algoId")
        st_b = j.get("algoStatus")
        ot_b = j.get("orderType")
        print(f"    -> algoId={aidB}, algoStatus={st_b}, type={ot_b}")
except Exception as e:
    print(f"    parse err: {e}")

# Z1: 查 openAlgoOrders 验证两单都挂上
time.sleep(1)
r = req("GET", "/fapi/v1/openAlgoOrders", {"symbol": SYM})
show("Z1: GET /fapi/v1/openAlgoOrders DOGEUSDT", r)

# Z2: 撤 Test A
if aidA:
    r = req("DELETE", "/fapi/v1/algoOrder", {"symbol": SYM, "algoId": aidA})
    show(f"Z2 cancel algoId={aidA} (Test A)", r)

# Z3: 撤 Test B
if aidB:
    r = req("DELETE", "/fapi/v1/algoOrder", {"symbol": SYM, "algoId": aidB})
    show(f"Z3 cancel algoId={aidB} (Test B)", r)

# Z4: 兜底全撤 (防止意外遗留)
r = req("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": SYM})
show("Z4 fallback: DELETE /fapi/v1/algoOpenOrders DOGEUSDT", r)

# Z5: 最终确认 0 遗留
r = req("GET", "/fapi/v1/openAlgoOrders", {"symbol": SYM})
show("Z5 final: GET /fapi/v1/openAlgoOrders DOGEUSDT (应为空)", r)

print("\n=== Phase C1d probe DONE ===")
