"""Binance real-money runner. Plan B strategy. Process-side stop monitoring."""
import os, json
import fcntl  # v0.1.22 cross-process lock
import time, threading, traceback, uuid
from datetime import datetime
import binance_real as br
import risk_gate as rg

DATA_DIR = "/root/lana-lite"
STATE_PATH = os.path.join(DATA_DIR, "real_state.json")
LOG_PATH = "/var/log/lana.log"

TRAIL_PCT = 0.20
TRAIL_ACT = 0.10
STOP_U = 3.0
MARGIN_U = 5.0
LEV_LONG = 5
LEV_SHORT = 3
POLL_SEC = 60
MAX_OPEN = 2  # v0.1.23 G: 1->2 解封 19/16h SKIP 拥堵 (handbook §11.7)
INIT_BAL = 100.0  # v0.1.23 W: 钱包升级 50->100
ALERT_BAL = 200.0  # v0.1.23 W: 翻倍报警同步 100->200

_LOCK = threading.Lock()
_alerted_bal = False

def _log(msg):
    line = "[" + datetime.now().isoformat() + "] [real] " + str(msg)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def _load_state():
    if not os.path.exists(STATE_PATH):
        return {"positions": [], "closed": []}
    with open(STATE_PATH, "r") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # v0.1.22
        try:
            return json.loads(f.read())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

def _save_state(s):
    # v0.1.22: cross-process EX lock + atomic tmp+rename + fsync
    lock_fd = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

def _tg_send(text):
    import requests
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    cid = os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not cid:
        return
    try:
        requests.post("https://api.telegram.org/bot" + tok + "/sendMessage", data={"chat_id": cid, "text": text}, timeout=5)
    except Exception:
        pass

def assert_one_way_mode():
    res = br._req("GET", "/fapi/v1/positionSide/dual", signed=True)
    if str(res.get("dualSidePosition")).lower() != "true":
        return
    _log("WARN: account in Hedge Mode, attempting auto-switch to One-Way")
    try:
        br._req("POST", "/fapi/v1/positionSide/dual",
                params={"dualSidePosition": "false"}, signed=True)
    except Exception as e:
        _log("FATAL: auto-switch failed: " + str(e))
        try: _tg_send("🚨 Hedge→One-Way 自愈失败: " + str(e))
        except Exception: pass
        raise RuntimeError("Auto-switch to One-Way failed: " + str(e))
    res2 = br._req("GET", "/fapi/v1/positionSide/dual", signed=True)
    if str(res2.get("dualSidePosition")).lower() == "true":
        _log("FATAL: still in Hedge Mode after switch attempt")
        try: _tg_send("🚨 自愈后仍为 Hedge 模式，请手动切换")
        except Exception: pass
        raise RuntimeError("Still in Hedge Mode after auto-switch")
    _log("OK: auto-switched to One-Way Mode")
    try: _tg_send("✅ 账户已自动切换到 One-Way 模式")
    except Exception: pass


def _replace_algo_stop(pos, new_trigger, side):
    """v0.1.20: cancel old algo + place new at new_trigger.
    Returns True/False; on 2nd-retry fail: HALT + tg_send + clear pos algo fields.
    """
    import binance_real as br
    import time as _t
    sym = pos["symbol"]
    old_aid = pos.get("algo_id")
    exit_side = "SELL" if side == "long" else "BUY"
    if old_aid:
        try:
            br.cancel_algo_order(sym, old_aid)
            _log(f"[TRAIL-SYNC] {sym} cancel old aid={old_aid} OK")
        except Exception as e:
            _log(f"[TRAIL-SYNC] {sym} cancel aid={old_aid} err (treat OK): {type(e).__name__}: {e}")
    for attempt in (1, 2):
        try:
            r = br.place_algo_stop_close(sym, exit_side, new_trigger, working_type="MARK_PRICE")
            if r and r.get("algoStatus") == "NEW":
                pos["algo_id"] = r.get("algoId")
                pos["algo_trigger"] = float(new_trigger)
                _log(f"[TRAIL-SYNC-OK] {sym} new_aid={r.get('algoId')} trigger={new_trigger} attempt={attempt}")
                return True
            _log(f"[TRAIL-SYNC] {sym} place attempt={attempt} non-NEW: {r}")
        except Exception as e:
            _log(f"[TRAIL-SYNC] {sym} place attempt={attempt} err: {type(e).__name__}: {e}")
        if attempt == 1:
            _t.sleep(2)
    try: Path("HALT").touch()
    except Exception: pass
    try:
        tg_send_real(f"🔴 TRAIL-SYNC FAILED {sym} target={new_trigger}\nNAKED POSITION (process soft-stop only)\nHALT 已置位, 请手工介入")
    except Exception: pass
    _log(f"[TRAIL-SYNC-FAIL] {sym} 2 attempts failed -> HALT + clear algo_id")
    pos["algo_id"] = None
    pos["algo_trigger"] = None
    return False

def real_open(symbol, side, margin=None, lev=None):
    """Open real position. side: LONG or SHORT. margin/lev args ignored (uses constants)."""
    try:
        br.assert_one_way_mode()  # v0.1.18 P0: One-Way tripwire
    except Exception as e:
        _log("[BLOCKED] assert_one_way_mode failed: " + str(e))
        return {"ok": False, "position": None, "err": "init", "reason": "assert_one_way_mode: " + str(e)[:200]}
    side = str(side).upper()
    if side not in ("LONG", "SHORT"):
        _log("[ERROR] real_open: invalid side " + repr(side))
        return {"ok": False, "position": None, "err": "validation", "reason": "invalid side: " + repr(side)}
    ok, why = rg.can_open()
    if not ok:
        _log("[BLOCKED] " + why)
        _tg_send("\u26d4 \u771f\u76d8\u5f00\u4ed3\u88ab\u98ce\u63a7\u963b\u6b62: " + why)
        return {"ok": False, "position": None, "err": "risk_gate", "reason": str(why)}
    st = _load_state()
    open_n = sum(1 for p in st.get("positions", []) if p.get("status") == "open")
    if open_n >= MAX_OPEN:
        _log("[SKIP] MAX_OPEN=" + str(MAX_OPEN) + " already reached, skip " + symbol)
        return {"ok": False, "position": None, "err": "max_open", "reason": "MAX_OPEN=" + str(MAX_OPEN) + " reached (" + str(open_n) + " open)"}
    use_lev = LEV_LONG if side == "LONG" else LEV_SHORT
    order_side = "BUY" if side == "LONG" else "SELL"
    try:
        br.set_leverage(symbol, use_lev)
        br.set_isolated(symbol)
    except Exception as e:
        _log("[ERROR] setup " + symbol + ": " + str(e))
        return {"ok": False, "position": None, "err": "setup", "reason": "set_leverage/isolated: " + str(e)[:200]}
    try:
        mark = br.get_mark_price(symbol)
    except Exception as e:
        _log("[ERROR] get_mark " + symbol + ": " + str(e))
        return {"ok": False, "position": None, "err": "setup", "reason": "get_mark_price: " + str(e)[:200]}
    notional = MARGIN_U * use_lev
    qty = br.round_qty(symbol, notional / mark)
    f = br.symbol_filters(symbol)
    if qty * mark < f["minNotional"]:
        qty = br.round_qty(symbol, f["minNotional"] / mark + f["stepSize"])
    if qty <= 0:
        _log("[ERROR] qty=0 for " + symbol + " mark=" + str(mark))
        return {"ok": False, "position": None, "err": "validation", "reason": "qty<=0 mark=" + str(mark)}
    try:
        order = br.place_market(symbol, order_side, qty)
    except Exception as e:
        _log("[ERROR] market open failed " + symbol + " " + order_side + " " + str(qty) + ": " + str(e))
        _tg_send("\u274c \u771f\u76d8\u5f00\u4ed3\u5931\u8d25 " + symbol + ": " + str(e))
        return {"ok": False, "position": None, "err": "market_order", "reason": "place_market: " + str(e)[:200]}
    time.sleep(1)
    pos_real = br.get_position(symbol)
    entry = pos_real["entry_price"] if pos_real["entry_price"] else mark
    actual_qty = abs(pos_real["qty"]) if pos_real["qty"] else qty
    pos = {"id": uuid.uuid4().hex[:8], "symbol": symbol, "side": side, "qty": actual_qty, "entry_price": entry, "leverage": use_lev, "margin_u": MARGIN_U, "stop_u": STOP_U, "open_ts": datetime.now().isoformat(), "peak_high": entry, "trail_active": False, "trail_stop_price": None, "status": "open", "open_order_id": order.get("orderId")}
    # v0.1.17 ALGO-STOP: place exchange-side hard stop (closePosition=true)
    # v0.1.21 B: ALGO-STOP retry queue (3 attempts, 2s/4s backoff, force_close on abort)
    algo_side = "SELL" if side == "LONG" else "BUY"
    if side == "LONG":
        stop_trigger = entry - STOP_U / actual_qty
    else:
        stop_trigger = entry + STOP_U / actual_qty
    stop_trigger = br.round_price(symbol, stop_trigger)
    algo_ok = False
    last_err = None
    for _attempt in range(1, 4):
        try:
            algo_res = br.place_algo_stop_close(symbol, algo_side, stop_trigger, working_type="MARK_PRICE")
            _status = (algo_res or {}).get("algoStatus") or (algo_res or {}).get("status")
            if _status == "NEW":
                pos["algo_id"] = algo_res.get("algoId")
                pos["algo_trigger"] = stop_trigger
                _log("[ALGO-STOP] " + symbol + " algoId=" + str(pos["algo_id"]) + " trigger=" + str(stop_trigger) + " attempt=" + str(_attempt))
                _tg_send("✅ " + symbol + " " + side + " hard stop: " + str(stop_trigger) + " algoId=" + str(pos["algo_id"]) + " (try " + str(_attempt) + ")")
                algo_ok = True
                break
            else:
                last_err = "algoStatus=" + str(_status) + " not NEW"
                _log("[ALGO-RETRY] " + symbol + " attempt=" + str(_attempt) + " " + last_err)
        except Exception as _ae:
            last_err = type(_ae).__name__ + ": " + str(_ae)[:120]
            _log("[ALGO-RETRY] " + symbol + " attempt=" + str(_attempt) + " " + last_err)
        if _attempt < 3:
            time.sleep(2 ** _attempt)  # 2s, 4s
    if not algo_ok:
        _log("[ALGO-ABORT] " + symbol + " 3 attempts failed: " + str(last_err))
        try:
            _close_side = "SELL" if side == "LONG" else "BUY"
            br.place_market(symbol, _close_side, actual_qty, reduce_only=True)
            _force_msg = "force_close OK"
            _log("[ALGO-ABORT] " + symbol + " " + _force_msg)
        except Exception as _ce:
            _force_msg = "force_close FAILED: " + type(_ce).__name__ + ": " + str(_ce)[:80]
            _log("[ALGO-ABORT] " + symbol + " " + _force_msg)
        _tg_send("🔴 OPEN_ABORTED_NO_HARD_STOP " + symbol + " " + side + " | 3 algo retries: " + str(last_err)[:100] + " | " + _force_msg + " | state NOT written")
        return {"ok": False, "position": None, "err": "algo_abort", "reason": "OPEN_ABORTED_NO_HARD_STOP: " + str(last_err)[:200]}  # v0.1.22
    with _LOCK:
        st = _load_state()
        st.setdefault("positions", []).append(pos)
        _save_state(st)
    msg = "[OPEN] " + symbol + " " + side + " qty=" + str(actual_qty) + " entry=" + str(entry) + " margin=" + str(MARGIN_U) + "U lev=" + str(use_lev) + "x"
    _log(msg)
    _tg_send("\U0001F7E2 \u771f\u76d8\u5f00\u4ed3 " + symbol + " " + side + " qty=" + str(actual_qty) + " entry=" + str(entry) + " margin=" + str(MARGIN_U) + "U lev=" + str(use_lev) + "x")
    try:
        from lana_lite import tg_send as _tg_main_open
        _tg_main_open("\U0001F7E2 \u771f\u76d8\u5f00\u4ed3 " + symbol + " " + side + " qty=" + str(actual_qty) + " entry=" + str(entry) + " margin=" + str(MARGIN_U) + "U lev=" + str(use_lev) + "x", channel="social")
    except Exception as _e_sm_o:
        _log("[social-mirror open] " + str(_e_sm_o))
    return {"ok": True, "position": pos, "err": None, "reason": "ok"}

def _close(pos, reason="manual"):
    symbol = pos["symbol"]
    exit_side = "SELL" if pos["side"] == "LONG" else "BUY"
    try:
        real_pos = br.get_position(symbol)
        actual_qty = abs(real_pos["qty"])
        mark_at_close = real_pos["mark_price"] if real_pos["mark_price"] else br.get_mark_price(symbol)
        # v0.1.17 ALGO-STOP cancel: avoid algo trigger after market sell
        algo_id = pos.get("algo_id")
        if algo_id:
            try:
                br.cancel_algo_order(symbol, algo_id)
                _log("[ALGO-CANCEL] " + symbol + " algoId=" + str(algo_id))
            except Exception as e:
                _log("[ALGO-CANCEL-WARN] " + symbol + " " + str(e))
        if actual_qty > 0:
            br.place_market(symbol, exit_side, actual_qty, reduce_only=True)
        else:
            _log("[CLOSE-WARN] " + symbol + " already FLAT on exchange")
        time.sleep(1)
        br.cancel_all(symbol)
        try:
            br.cancel_all_algo(symbol)
        except Exception:
            pass
    except Exception as e:
        _log("[ERROR] close failed " + symbol + ": " + str(e))
        _tg_send("\u26a0\ufe0f \u771f\u76d8\u5e73\u4ed3\u5931\u8d25 " + symbol + ": " + str(e))
        return None
    exit_price = mark_at_close
    if pos["side"] == "LONG":
        pnl = (exit_price - pos["entry_price"]) * pos["qty"]
    else:
        pnl = (pos["entry_price"] - exit_price) * pos["qty"]
    pnl = round(pnl, 4)
    with _LOCK:
        st = _load_state()
        remaining = []
        for p in st.get("positions", []):
            if p["id"] == pos["id"]:
                p["status"] = "closed"
                p["close_ts"] = datetime.now().isoformat()
                p["close_reason"] = reason
                p["exit_price"] = exit_price
                p["realized_pnl_u"] = pnl
                st.setdefault("closed", []).append(p)
            else:
                remaining.append(p)
        st["positions"] = remaining
        _save_state(st)
    rg.record_close(pnl)
    msg = "[CLOSE] " + symbol + " " + pos["side"] + " exit=" + str(exit_price) + " pnl=" + ("%+.4f" % pnl) + "U reason=" + reason
    _log(msg)
    icon = "\U0001F534" if pnl < 0 else "\U0001F7E2"
    _tg_send(icon + " \u771f\u76d8\u5e73\u4ed3 " + symbol + " " + pos["side"] + " exit=" + str(exit_price) + " pnl=" + ("%+.4f" % pnl) + "U reason=" + reason)
    try:
        from lana_lite import tg_send as _tg_main_close
        _tg_main_close(icon + " \u771f\u76d8\u5e73\u4ed3 " + symbol + " " + pos["side"] + " exit=" + str(exit_price) + " pnl=" + ("%+.4f" % pnl) + "U reason=" + reason, channel="social")
    except Exception as _e_sm_c:
        _log("[social-mirror close] " + str(_e_sm_c))
    return pnl

def _check_one(pos):
    symbol = pos["symbol"]
    try:
        mark = br.get_mark_price(symbol)
    except Exception as e:
        _log("[poll-warn] get_mark " + symbol + ": " + str(e))
        return False
    entry = pos["entry_price"]
    qty = pos["qty"]
    if pos["side"] == "LONG":
        cur_pnl = (mark - entry) * qty
    else:
        cur_pnl = (entry - mark) * qty
    if cur_pnl <= -STOP_U:
        _close(pos, reason="stop_loss")
        return True
    if pos["side"] == "LONG":
        pos["peak_high"] = max(pos.get("peak_high", entry), mark)
    else:
        pos["peak_high"] = min(pos.get("peak_high", entry), mark)
    if not pos.get("trail_active", False):
        if pos["side"] == "LONG":
            activated = pos["peak_high"] >= entry * (1 + TRAIL_ACT)
        else:
            activated = pos["peak_high"] <= entry * (1 - TRAIL_ACT)
        if activated:
            pos["trail_active"] = True
            if pos["side"] == "LONG":
                pos["trail_stop_price"] = max(entry, pos["peak_high"] * (1 - TRAIL_PCT))
            else:
                pos["trail_stop_price"] = min(entry, pos["peak_high"] * (1 + TRAIL_PCT))
            _log("[trail ACTIVATED] " + symbol + " " + pos["side"] + " peak=" + str(pos["peak_high"]) + " trail_stop=" + str(pos["trail_stop_price"]))
            _tg_send("\U0001F4C8 \u771f\u76d8\u8ffd\u8e2a\u6b62\u635f\u542f\u52a8 " + symbol + " " + pos["side"] + " peak=" + str(pos["peak_high"]) + " trail=" + str(pos["trail_stop_price"]))
    if pos.get("trail_active"):
        if pos["side"] == "LONG":
            new_stop = max(entry, pos["peak_high"] * (1 - TRAIL_PCT))
            pos["trail_stop_price"] = max(pos.get("trail_stop_price") or new_stop, new_stop)
            if mark <= pos["trail_stop_price"]:
                _close(pos, reason="trail_stop")
                return True
        else:
            new_stop = min(entry, pos["peak_high"] * (1 + TRAIL_PCT))
            pos["trail_stop_price"] = min(pos.get("trail_stop_price") or new_stop, new_stop)
            if mark >= pos["trail_stop_price"]:
                _close(pos, reason="trail_stop")
                return True
    with _LOCK:
        st = _load_state()
        for p in st.get("positions", []):
            if p["id"] == pos["id"]:
                p["peak_high"] = pos["peak_high"]
                p["trail_active"] = pos["trail_active"]
                p["trail_stop_price"] = pos.get("trail_stop_price")
        _save_state(st)
    return False

    # v0.1.20 trail-sync: 检测 trail_stop_price 与 algo_trigger 漂移, 同步交易所端 algo
    if pos.get("trail_active") and pos.get("trail_stop_price") is not None:
        cur_trig = pos.get("algo_trigger")
        new_trig = float(pos["trail_stop_price"])
        if cur_trig is None or abs(float(cur_trig) - new_trig) / max(abs(new_trig), 1e-9) > 0.0001:
            try:
                _replace_algo_stop(pos, new_trig, side)
            except Exception as _e:
                _log(f"[TRAIL-SYNC] outer err: {type(_e).__name__}: {_e}")

def _maybe_alert_bal():
    global _alerted_bal
    if _alerted_bal:
        return
    try:
        b = br.get_balance()["balance"]
        if b >= ALERT_BAL:
            _tg_send("\U0001F4B0 \u771f\u76d8\u4f59\u989d\u8fbe\u5230 " + ("%.2f" % b) + "U")
            _alerted_bal = True
    except Exception:
        pass

def real_check_all():
    _log("[real] check loop started, poll=" + str(POLL_SEC) + "s, strategy=trail_" + str(int(TRAIL_PCT*100)) + "%/act_" + str(int(TRAIL_ACT*100)) + "%, stop=" + str(STOP_U) + "U, margin=" + str(MARGIN_U) + "U")
    try:
        assert_one_way_mode()
    except Exception as e:
        _log("[FATAL] " + str(e))
        _tg_send("\u274c \u771f\u76d8 daemon \u542f\u52a8\u5931\u8d25: " + str(e))
        return
    while True:
        try:
            st = _load_state()
            for pos in list(st.get("positions", [])):
                if pos.get("status") == "open":
                    _check_one(pos)
            _maybe_alert_bal()
        except Exception as e:
            _log("[poll-error] " + str(e) + "\n" + traceback.format_exc())
        time.sleep(POLL_SEC)

# Compat shims so lana_lite.py can use this module as drop-in for binance_paper
def paper_open(symbol, side, margin, lev):
    """Compat shim matching binance_paper.paper_open interface."""
    pos = real_open(symbol, side, margin, lev)
    if pos is None:
        return {"ok": False, "err": "blocked or failed (see /var/log/lana.log)"}
    return {"ok": True, "position": pos}

paper_check_all = real_check_all


# ===== v0.1.19 boot_reconcile (added 2026-04-27) =====
def boot_reconcile():
    """VPS 重启对账: state vs ex positions vs open algos."""
    import json, sys, os
    from pathlib import Path
    from datetime import datetime
    STATE = Path("/root/lana-lite/real_state.json")
    HALT = Path("/root/lana-lite/HALT")
    DRY = os.environ.get("BOOT_RECONCILE_DRY_RUN") == "1"
    def _bl(m):
        print(f"[{datetime.now().isoformat()}] [boot_reconcile] {m}", flush=True)
    fatal = None
    ex_positions = []; ex_algos = []
    try:
        import binance_real as br
        raw = br._req("GET", "/fapi/v2/positionRisk", {})
        ex_positions = [p for p in raw if abs(float(p.get("positionAmt", 0))) > 0]
        ex_algos = br.list_open_algo_orders()
    except Exception as e:
        fatal = f"FATAL 拉交易所失败: {type(e).__name__}: {e}"
        _bl(fatal)
    if fatal:
        if DRY:
            return {"ok": False, "issues": [fatal], "dry_run": True}
        HALT.touch()
        try:
            from lana_lite import tg_send
            tg_send(f"⚠️ boot_reconcile {fatal[:200]}, 已 HALT")
        except Exception: pass
        sys.exit(1)
    st = json.loads(STATE.read_text()) if STATE.exists() else {"positions": [], "closed": []}
    ex_pos_map = {p["symbol"]: p for p in ex_positions}
    ex_algo_map = {}
    for a in ex_algos:
        ex_algo_map.setdefault(a["symbol"], []).append(a)
    state_pos_map = {p["symbol"]: p for p in st.get("positions", [])}
    all_syms = set(ex_pos_map) | set(state_pos_map)
    issues = []
    for sym in all_syms:
        in_st = sym in state_pos_map
        in_ex = sym in ex_pos_map
        algos = ex_algo_map.get(sym, [])
        if in_st and in_ex and algos:
            sp = state_pos_map[sym]
            sp_aid = str(sp.get("algo_id", ""))
            aid_set = {str(a.get("algoId", "")) for a in algos}
            if sp_aid and sp_aid not in aid_set:
                issues.append(f"ALGO_ID_MISMATCH {sym}: state.algo_id={sp_aid} not in {aid_set}")
            else:
                _bl(f"OK {sym}: state+ex+algo consistent (algo_id={sp_aid})")
            continue
        if in_st and in_ex and not algos:
            issues.append(f"MISSING_HARD_STOP {sym}: 持仓有但 open algo 无")
        elif in_st and not in_ex:
            issues.append(f"STATE_GHOST {sym}: state 有但交易所无持仓")
        elif not in_st and in_ex:
            amt = ex_pos_map[sym].get("positionAmt", "?")
            ent = ex_pos_map[sym].get("entryPrice", "?")
            issues.append(f"ORPHAN_POSITION {sym}: amt={amt} entry={ent} 但 state 无 (失控仓!)")
    if issues:
        rep = "; ".join(issues)
        _bl(f"FOUND ISSUES: {rep}")
        if DRY:
            return {"ok": False, "issues": issues, "ex_keys": list(ex_pos_map.keys()), "state_keys": list(state_pos_map.keys()), "algo_keys": list(ex_algo_map.keys()), "dry_run": True}
        HALT.touch()
        try:
            from lana_lite import tg_send
            tg_send(f"⚠️ boot_reconcile 不一致已 HALT: {rep[:3500]}")
        except Exception: pass
        sys.exit(1)
    _bl(f"OK: {len(state_pos_map)} state / {len(ex_pos_map)} ex / {sum(len(v) for v in ex_algo_map.values())} algos consistent")
    return {"ok": True, "issues": []}
