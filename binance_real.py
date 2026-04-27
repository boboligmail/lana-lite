"""Binance USDT-M Futures real-money client. Reads .env."""
import os, time, hmac, hashlib, math
from urllib.parse import urlencode
import requests

BASE = "https://fapi.binance.com"
RECV_WINDOW = 5000

def _load_env():
    p = "/root/lana-lite/.env"
    if not os.path.exists(p): return
    for line in open(p):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

_load_env()
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
assert API_KEY and API_SECRET, "BINANCE_API_KEY / BINANCE_API_SECRET missing in .env"

def _sign(params):
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = RECV_WINDOW
    qs = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return qs + "&signature=" + sig

def _req(method, path, params=None, signed=True, timeout=10):
    params = dict(params or {})
    headers = {"X-MBX-APIKEY": API_KEY}
    url = BASE + path
    if signed:
        qs = _sign(params)
        if method in ("GET", "DELETE"):
            r = requests.request(method, url + "?" + qs, headers=headers, timeout=timeout)
        else:
            r = requests.request(method, url, headers=headers, data=qs, timeout=timeout)
    else:
        r = requests.request(method, url, headers=headers, params=params, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"Binance {method} {path} -> {r.status_code}: {r.text}")
    return r.json()

_xinfo = {"ts": 0, "data": None}

def exchange_info():
    now = time.time()
    if _xinfo["data"] and now - _xinfo["ts"] < 600:
        return _xinfo["data"]
    data = _req("GET", "/fapi/v1/exchangeInfo", signed=False)
    _xinfo["data"] = data
    _xinfo["ts"] = now
    return data

def symbol_filters(symbol):
    info = exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("LOT_SIZE", {})
            price = f.get("PRICE_FILTER", {})
            mn = f.get("MIN_NOTIONAL", {})
            return {"stepSize": float(lot.get("stepSize", "0.001")), "minQty": float(lot.get("minQty", "0.001")), "tickSize": float(price.get("tickSize", "0.01")), "minNotional": float(mn.get("notional", "5")), "qtyPrecision": int(s.get("quantityPrecision", 3)), "pricePrecision": int(s.get("pricePrecision", 2))}
    raise RuntimeError(f"symbol not found: {symbol}")

def floor_step(v, step):
    return v if step <= 0 else math.floor(v / step) * step

def round_qty(symbol, qty):
    f = symbol_filters(symbol)
    return round(floor_step(qty, f["stepSize"]), f["qtyPrecision"])

def round_price(symbol, price):
    f = symbol_filters(symbol)
    return round(floor_step(price, f["tickSize"]), f["pricePrecision"])

def get_mark_price(symbol):
    d = _req("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, signed=False)
    return float(d["markPrice"])

def get_balance():
    data = _req("GET", "/fapi/v2/balance")
    for b in data:
        if b["asset"] == "USDT":
            return {"balance": float(b["balance"]), "available": float(b["availableBalance"]), "cross_unpnl": float(b.get("crossUnPnl", 0))}
    return {"balance": 0, "available": 0, "cross_unpnl": 0}

def get_position(symbol):
    data = _req("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    for p in data:
        if p["symbol"] == symbol:
            amt = float(p["positionAmt"])
            side = "LONG" if amt > 0 else ("SHORT" if amt < 0 else "FLAT")
            return {"symbol": symbol, "qty": amt, "side": side, "entry_price": float(p["entryPrice"]), "mark_price": float(p["markPrice"]), "unrealized_pnl": float(p["unRealizedProfit"]), "leverage": int(p["leverage"]), "margin_type": p.get("marginType", "")}
    return {"symbol": symbol, "qty": 0, "side": "FLAT", "entry_price": 0, "mark_price": 0, "unrealized_pnl": 0, "leverage": 0, "margin_type": ""}

def set_leverage(symbol, leverage):
    return _req("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})

def set_isolated(symbol):
    try:
        return _req("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
    except RuntimeError as e:
        if "-4046" in str(e) or "No need to change" in str(e):
            return {"msg": "already isolated"}
        raise

def place_market(symbol, side, qty, reduce_only=False):
    qty = round_qty(symbol, abs(qty))
    if qty <= 0:
        raise ValueError(f"qty rounds to 0 for {symbol}")
    params = {"symbol": symbol, "side": side.upper(), "type": "MARKET", "quantity": qty}
    if reduce_only:
        params["reduceOnly"] = "true"
    return _req("POST", "/fapi/v1/order", params)

def place_stop_market(symbol, side, qty, stop_price, reduce_only=True):
    qty = round_qty(symbol, abs(qty))
    stop_price = round_price(symbol, stop_price)
    params = {"symbol": symbol, "side": side.upper(), "type": "STOP_MARKET", "quantity": qty, "stopPrice": stop_price, "workingType": "MARK_PRICE", "timeInForce": "GTC"}
    if reduce_only:
        params["reduceOnly"] = "true"
    return _req("POST", "/fapi/v1/order", params)

def cancel_all(symbol):
    try:
        return _req("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    except RuntimeError as e:
        if "-2011" in str(e):
            return {"msg": "no open orders"}
        raise

def list_open_orders(symbol):
    return _req("GET", "/fapi/v1/openOrders", {"symbol": symbol})


# ============ Algo Service (post-2025-12-09 migration) ============
# Replaces deprecated /fapi/v1/order STOP_MARKET (now -4120).
# place: POST /fapi/v1/algoOrder    cancel: DELETE /fapi/v1/algoOrder
# bulk : DELETE /fapi/v1/algoOpenOrders     list: GET /fapi/v1/openAlgoOrders

def place_algo_stop_close(symbol, side, trigger_price, working_type="MARK_PRICE"):
    """Conditional STOP_MARKET with closePosition=true. Returns dict with algoId."""
    trigger = round_price(symbol, trigger_price)
    params = {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": side.upper(),
        "type": "STOP_MARKET",
        "triggerPrice": trigger,
        "closePosition": "true",
        "workingType": working_type,
    }
    return _req("POST", "/fapi/v1/algoOrder", params)

def cancel_algo_order(symbol, algo_id):
    """Cancel single algo. -2011 (Unknown order) = already triggered/canceled, treat as success."""
    try:
        return _req("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})
    except RuntimeError as e:
        s = str(e)
        if "-2011" in s or "Unknown order" in s:
            return {"msg": "already_gone"}
        raise

def cancel_all_algo(symbol):
    """Bulk cancel all open algo orders for a symbol."""
    try:
        return _req("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})
    except RuntimeError as e:
        if "-2011" in str(e):
            return {"msg": "no open algo"}
        raise

def list_open_algo_orders(symbol=None):
    """List open algo orders (optionally filter by symbol)."""
    params = {}
    if symbol:
        params["symbol"] = symbol
    return _req("GET", "/fapi/v1/openAlgoOrders", params)


def assert_one_way_mode():
    """v0.1.18 P0: ensure account is in One-Way mode before any real_open call.
    GET /fapi/v1/positionSide/dual; if dualSidePosition=true, POST to switch; verify again.
    Raise on failure (caller MUST abort real_open)."""
    r = _req("GET", "/fapi/v1/positionSide/dual", {})
    dual = r.get("dualSidePosition")
    if dual is False:
        return True
    if dual is True:
        _req("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"})
        r2 = _req("GET", "/fapi/v1/positionSide/dual", {})
        if r2.get("dualSidePosition") is False:
            return True
        raise RuntimeError("assert_one_way_mode: switch failed, still " + str(r2))
    raise RuntimeError("assert_one_way_mode: unexpected response " + str(r))
