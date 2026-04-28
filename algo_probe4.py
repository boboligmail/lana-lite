#!/usr/bin/env python3
"""algo_probe4.py - Phase C1e: 终极验证 - CROSSUSDT 实仓 closePosition probe"""
import os, time, hmac, hashlib, urllib.parse, requests
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

SYM = "CROSSUSDT"

# 0a. 确认 CROSSUSDT 实仓还在
r = req("GET", "/fapi/v2/positionRisk", {"symbol": SYM})
pos = r.json()
print(f"\n[0a] positionRisk: {pos[:1]}")
amt = float(pos[0]["positionAmt"]) if pos else 0
print(f"    positionAmt={amt}")
if amt <= 0:
    raise SystemExit("FATAL: CROSSUSDT 没有 LONG 持仓, 取消测试")

# 0b. 取当前 mark
r = req("GET", "/fapi/v1/premiumIndex", {"symbol": SYM}, signed=False)
mark = float(r.json()["markPrice"])
trigger_far = round(mark * 0.5, 6)
print(f"[0b] mark={mark}, triggerPrice (mark x 0.5, 永远不会触发) = {trigger_far}")

# Test C: closePosition=true, SELL stop, trigger 远低于 mark (LONG 跌到那里才触发,实际不会)
pC = {
    "algoType": "CONDITIONAL",
    "symbol": SYM,
    "side": "SELL",
    "type": "STOP_MARKET",
    "triggerPrice": trigger_far,
    "closePosition": "true",
    "workingType": "MARK_PRICE",
}
r = req("POST", "/fapi/v1/algoOrder", pC)
show("Test C: CROSSUSDT  closePosition=true  trigger 远 OTM", r)
aidC = None
try:
    if r.status_code == 200:
        j = r.json()
        aidC = j.get("algoId")
        cidC = j.get("clientAlgoId")
        stC = j.get("algoStatus")
        otC = j.get("orderType")
        cpC = j.get("closePosition")
        tpC = j.get("triggerPrice")
        print(f"    -> algoId={aidC}, clientAlgoId={cidC}, algoStatus={stC}, orderType={otC}, closePosition={cpC}, triggerPrice={tpC}")
except Exception as e:
    print(f"    parse err: {e}")

# Z1: 查 openAlgoOrders 验证挂上
time.sleep(1)
r = req("GET", "/fapi/v1/openAlgoOrders", {"symbol": SYM})
show("Z1: openAlgoOrders CROSSUSDT (应有 1 单 algoId={})".format(aidC), r)

# Z2: 撤销
if aidC:
    r = req("DELETE", "/fapi/v1/algoOrder", {"symbol": SYM, "algoId": aidC})
    show(f"Z2 cancel algoId={aidC}", r)
else:
    r = req("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": SYM})
    show("Z2 fallback: cancel all CROSSUSDT (因 aidC=None)", r)

# Z3: 最终确认 0 遗留
r = req("GET", "/fapi/v1/openAlgoOrders", {"symbol": SYM})
show("Z3 final: openAlgoOrders CROSSUSDT (应为空)", r)

print("\n=== Phase C1e probe DONE ===")
