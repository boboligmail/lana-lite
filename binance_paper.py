"""
binance_paper.py - 拉哪 Lite v0.1.14 纸面交易模拟器
策略: 纯 trail 20% + 激活门槛 10% (校准 backtest_tp_calibration.py)

- Binance fapi 公开 ticker 拉实时价(无 key)
- 本地 paper_state.json 跟持仓 + 余额 + 已平仓历史
- 入场后仅受 -10U 硬止损保护
- LONG 价格触 entry×1.10 (SHORT 触 entry×0.90) 后启动 trail
- 启动后 trail_price = peak × 0.80 (LONG) / peak × 1.20 (SHORT), 单调棘轮
- 现价触 trail_price 即整笔平仓 (reason=trail_stop)
- 同时持仓上限 1 (硬约束)
- 余额达 500U 时 TG 提醒重评估策略

对外接口 (与 v0.1.13 兼容):
    get_balance()                            -> float
    get_positions()                          -> list[dict]
    paper_open(symbol, side, margin_u, lev)  -> dict
    paper_check_all()                        -> None (阻塞循环, 60s 轮询)
    paper_pnl_today()                        -> float
"""

import os, json, time, uuid, threading, requests
from datetime import datetime

FAPI       = "https://fapi.binance.com"
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_state.json")
INIT_BAL   = 100.0    # 100U 起始本金
STOP_U     = 10.0     # 单笔硬止损 -10U
TRAIL_PCT  = 0.20     # trail 缓冲 20%
TRAIL_ACT  = 0.10     # 激活门槛 +10% / -10%
POLL_SEC   = 60       # check loop 轮询间隔
MAX_OPEN   = 999   # v0.1.16: paper 不限并发 (同 symbol 去重在下面单独做)
ALERT_BAL  = 500.0    # 余额到此值 TG 提醒

_LOCK = threading.Lock()
_alerted_500 = False  # 进程内 latch

# --------------- TG 推送 (轻量) ---------------
def _tg_send(text):
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "")
    cid = os.getenv("TELEGRAM_CHAT_ID", "")
    if not tok or not cid: return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": cid, "text": text}, timeout=5)
    except Exception as e:
        print(f"[paper] tg fail: {e}")

# --------------- state I/O ---------------
def _now_iso():
    return datetime.now().isoformat()

def _load_state():
    if not os.path.exists(STATE_PATH):
        st = {"balance": INIT_BAL, "positions": [], "closed": []}
        _save_state(st)
        return st
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

# --------------- price ---------------
def _fetch_price(symbol):
    try:
        r = requests.get(FAPI + "/fapi/v1/ticker/price",
                         params={"symbol": symbol}, timeout=5).json()
        return float(r.get("price", 0))
    except Exception as e:
        print(f"[paper] price fetch fail {symbol}: {e}")
        return 0.0

# --------------- public API ---------------
def get_balance():
    with _LOCK:
        return float(_load_state().get("balance", INIT_BAL))

def get_positions():
    with _LOCK:
        return list(_load_state().get("positions", []))

def paper_pnl_today():
    today = datetime.now().date().isoformat()
    with _LOCK:
        st = _load_state()
        return round(sum(float(c.get("pnl_u", 0)) for c in st.get("closed", [])
                         if str(c.get("close_ts", "")).startswith(today)), 4)

def paper_open(symbol, side, margin_u, leverage):
    side = str(side).upper()
    if side not in ("LONG", "SHORT"):
        return {"ok": False, "err": f"bad side {side}"}
    if margin_u <= 0 or leverage <= 0:
        return {"ok": False, "err": "margin_u/leverage must > 0"}
    price = _fetch_price(symbol)
    if price <= 0:
        return {"ok": False, "err": "no price", "symbol": symbol}
    with _LOCK:
        st = _load_state()
        # v0.1.16: same-symbol dedup (paper 不限制总数,但禁止同 symbol 重复 open)
        for _p in st.get("positions", []):
            if _p.get("symbol") == symbol:
                return {"ok": False, "err": "already open: " + symbol, "symbol": symbol}
        if len(st.get("positions", [])) >= MAX_OPEN:
            return {"ok": False, "err": f"max {MAX_OPEN} open position(s)", "symbol": symbol}
        qty = (float(margin_u) * float(leverage)) / price
        pos = {
            "id":              uuid.uuid4().hex[:8],
            "symbol":          symbol,
            "side":            side,
            "entry_price":     price,
            "margin_u":        float(margin_u),
            "leverage":        float(leverage),
            "qty":             qty,
            "stop_loss_u":     STOP_U,
            "peak_high":       price,    # 实时跟踪极值 (LONG=最高, SHORT=最低)
            "trail_active":    False,    # 是否已突破激活门槛
            "trail_stop_price":None,     # 激活后的移动止损价
            "open_ts":         _now_iso(),
        }
        st["positions"].append(pos)
        _save_state(st)
    print(f"[paper] OPEN {side} {symbol} @ {price} margin={margin_u}U lev={leverage}x qty={qty:.6f}")
    return {"ok": True, "position": pos}

# --------------- internal close helpers ---------------
def _pnl_u(pos, price):
    if pos["side"] == "LONG":
        return (price - pos["entry_price"]) * pos["qty"]
    return (pos["entry_price"] - price) * pos["qty"]

def _close_position(st, pos, price, qty_close, reason):
    """平 qty_close 数量; 若 == pos[qty] 则整笔平, 否则部分平仓."""
    frac = qty_close / pos["qty"] if pos["qty"] else 1.0
    if pos["side"] == "LONG":
        pnl = (price - pos["entry_price"]) * qty_close
    else:
        pnl = (pos["entry_price"] - price) * qty_close
    st["balance"] = float(st.get("balance", INIT_BAL)) + pnl
    st.setdefault("closed", []).append({
        "id":          pos["id"],
        "symbol":      pos["symbol"],
        "side":        pos["side"],
        "entry_price": pos["entry_price"],
        "exit_price":  price,
        "qty_closed":  qty_close,
        "frac":        round(frac, 4),
        "pnl_u":       round(pnl, 4),
        "reason":      reason,
        "open_ts":     pos["open_ts"],
        "close_ts":    _now_iso(),
    })
    pos["qty"] = pos["qty"] - qty_close
    pos["margin_u"] = pos["margin_u"] * (1 - frac)
    sym, sd = pos["symbol"], pos["side"]
    print(f"[paper] CLOSE {reason} {sd} {sym} qty={qty_close:.6f} @ {price} pnl={pnl:+.2f}U bal={st['balance']:.2f}U")
    return pnl

def _check_one(pos, st):
    """返回 True 表示该 pos 已被整笔平仓, 需从 positions 列表移除."""
    price = _fetch_price(pos["symbol"])
    if price <= 0:
        return False
    entry = pos["entry_price"]
    side  = pos["side"]
    pnl_u = _pnl_u(pos, price)

    # 1) 硬止损 -10U (始终生效)
    if pnl_u <= -float(pos.get("stop_loss_u", STOP_U)):
        _close_position(st, pos, price, pos["qty"], "stop_loss")
        return True

    # 2) 维护 peak_high (LONG=最高价, SHORT=最低价)
    peak = float(pos.get("peak_high", entry))
    if side == "LONG":
        peak = max(peak, price)
    else:
        peak = min(peak, price)
    pos["peak_high"] = peak

    # 3) trail 激活检查
    if not pos.get("trail_active", False):
        if side == "LONG":
            activated = peak >= entry * (1 + TRAIL_ACT)
        else:
            activated = peak <= entry * (1 - TRAIL_ACT)
        if activated:
            pos["trail_active"] = True
            if side == "LONG":
                pos["trail_stop_price"] = max(entry, peak * (1 - TRAIL_PCT))
            else:
                pos["trail_stop_price"] = min(entry, peak * (1 + TRAIL_PCT))
            print(f"[paper] trail ACTIVATED {pos['symbol']} {side}: peak={peak} trail={pos['trail_stop_price']}")

    # 4) trail 棘轮 + 触发
    if pos.get("trail_active"):
        tsp = float(pos.get("trail_stop_price") or entry)
        if side == "LONG":
            new_tsp = peak * (1 - TRAIL_PCT)
            if new_tsp > tsp:
                tsp = new_tsp
            pos["trail_stop_price"] = tsp
            if price <= tsp:
                _close_position(st, pos, tsp, pos["qty"], "trail_stop")
                return True
        else:  # SHORT
            new_tsp = peak * (1 + TRAIL_PCT)
            if new_tsp < tsp:
                tsp = new_tsp
            pos["trail_stop_price"] = tsp
            if price >= tsp:
                _close_position(st, pos, tsp, pos["qty"], "trail_stop")
                return True
    return False

def _maybe_alert_500(balance):
    global _alerted_500
    if _alerted_500: return
    if balance >= ALERT_BAL:
        _alerted_500 = True
        _tg_send(f"\u26a0\ufe0f 拉哪 Lite paper 余额已达 {balance:.2f}U (>= {ALERT_BAL}U)\n请评估升级止盈止损策略 (扩大样本重跑 backtest_tp_calibration.py)")
        print(f"[paper] *** balance hit {balance:.2f}U, alert sent, 请评估升级策略 ***")

def paper_check_all():
    """阻塞循环, 每 POLL_SEC 秒轮询所有持仓的止损/止盈."""
    print(f"[paper] check loop started, poll={POLL_SEC}s, strategy=trail_{int(TRAIL_PCT*100)}%/act_{int(TRAIL_ACT*100)}%")
    while True:
        try:
            with _LOCK:
                st = _load_state()
                still_open = []
                for pos in list(st.get("positions", [])):
                    closed = _check_one(pos, st)
                    if not closed and pos.get("qty", 0) > 0:
                        still_open.append(pos)
                st["positions"] = still_open
                _save_state(st)
                _maybe_alert_500(float(st.get("balance", INIT_BAL)))
        except Exception as e:
            print(f"[paper] check loop error: {e}")
        time.sleep(POLL_SEC)

# --------------- CLI smoke test ---------------
if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print("balance      :", get_balance())
        print("positions    :", json.dumps(get_positions(), indent=2, ensure_ascii=False))
        print("pnl_today_u  :", paper_pnl_today())
    elif cmd == "open":
        # python3 binance_paper.py open BTCUSDT LONG 10 5
        sym, side, m, lev = sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5])
        print(json.dumps(paper_open(sym, side, m, lev), indent=2, ensure_ascii=False))
    elif cmd == "loop":
        paper_check_all()
    else:
        print("usage: status | open SYM SIDE MARGIN LEV | loop")
