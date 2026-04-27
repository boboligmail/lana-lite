"""v0.1.18 funding rate 采集 - P0 from 2026-04-27 庄家雷达启发
独立模块 + 60s batch cache,零侵入主进程。
单源:GET /fapi/v1/premiumIndex (无 symbol 参数 = 全合约一次返回)。
失败静默,不影响主流程。
"""
import time
import requests

FAPI = "https://fapi.binance.com"
_cache = {"ts": 0, "data": {}}
_TTL = 60

def get_all_funding_rates():
    now = time.time()
    if now - _cache["ts"] < _TTL and _cache["data"]:
        return _cache["data"]
    try:
        r = requests.get(FAPI + "/fapi/v1/premiumIndex", timeout=10)
        r.raise_for_status()
        items = r.json()
        if not isinstance(items, list):
            return _cache["data"]
        d = {}
        for it in items:
            sym = it.get("symbol")
            fr = it.get("lastFundingRate")
            nft = it.get("nextFundingTime")
            mp = it.get("markPrice")
            if sym and fr is not None:
                try:
                    d[sym] = {
                        "lastFundingRate": float(fr),
                        "nextFundingTime": int(nft) if nft else None,
                        "markPrice": float(mp) if mp else None,
                    }
                except (ValueError, TypeError):
                    continue
        if d:
            _cache["ts"] = now
            _cache["data"] = d
        return d
    except Exception:
        return _cache["data"]

def get_funding_for(symbol):
    return get_all_funding_rates().get(symbol)

if __name__ == "__main__":
    d = get_all_funding_rates()
    print("Total symbols:", len(d))
    for s in ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "LDOUSDT", "ETHWUSDT", "CROSSUSDT", "KATUSDT", "STABLEUSDT"]:
        v = d.get(s)
        if v:
            print(f"  {s}: funding={v['lastFundingRate']*100:.4f}%  mark={v['markPrice']}")
        else:
            print(f"  {s}: (not listed)")
