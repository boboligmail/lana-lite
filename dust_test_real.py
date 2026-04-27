"""DUST TEST - 1U margin x 5x = 5U notional on DOGEUSDT."""
import time
import binance_real as br

SYMBOL = "DOGEUSDT"
MARGIN_U = 1.0
LEV = 5
STOP_PCT = 0.10

def main():
    print("=" * 60)
    print(" BINANCE FUTURES DUST TEST (real money, ~5U notional)")
    print("=" * 60)
    print("")
    print("[1] balance (before)")
    bal0 = br.get_balance()
    b0 = bal0["balance"]
    a0 = bal0["available"]
    print("  balance=%.4fU  available=%.4fU" % (b0, a0))
    if a0 < 5:
        print("  [ABORT] available < 5U")
        return
    print("")
    print("[2] mark price " + SYMBOL)
    mark = br.get_mark_price(SYMBOL)
    print("  mark=%s" % mark)
    print("")
    print("[3] set leverage %dx" % LEV)
    r = br.set_leverage(SYMBOL, LEV)
    print("  -> %s" % r)
    print("")
    print("[4] set ISOLATED")
    r = br.set_isolated(SYMBOL)
    print("  -> %s" % r)
    notional = MARGIN_U * LEV
    qty_raw = notional / mark
    qty = br.round_qty(SYMBOL, qty_raw)
    f = br.symbol_filters(SYMBOL)
    print("")
    print("[5] qty calc: %.4f/%s=%.4f -> floor %s (step=%s)" % (notional, mark, qty_raw, qty, f["stepSize"]))
    print("  filters: minQty=%s minNotional=%s" % (f["minQty"], f["minNotional"]))
    if qty * mark < f["minNotional"]:
        qty = br.round_qty(SYMBOL, f["minNotional"] / mark + f["stepSize"])
        print("  bumped -> qty=%s (notional=%.4fU)" % (qty, qty*mark))
    print("")
    print("[6] MARKET BUY %s %s" % (qty, SYMBOL))
    o1 = br.place_market(SYMBOL, "BUY", qty)
    print("  -> orderId=%s status=%s avg=%s" % (o1.get("orderId"), o1.get("status"), o1.get("avgPrice")))
    time.sleep(1)
    print("")
    print("[7] position (after open)")
    pos = br.get_position(SYMBOL)
    print("  -> qty=%s side=%s entry=%s lev=%s margin=%s" % (pos["qty"], pos["side"], pos["entry_price"], pos["leverage"], pos["margin_type"]))
    if pos["side"] != "LONG" or pos["qty"] <= 0:
        print("  [FAIL] no LONG position")
        return
    sp_price = pos["entry_price"] * (1 - STOP_PCT)
    print("")
    print("[8] STOP_MARKET sell @ %.6f" % sp_price)
    try:
        o2 = br.place_stop_market(SYMBOL, "SELL", pos["qty"], sp_price)
        print("  -> orderId=%s type=%s stopPrice=%s" % (o2.get("orderId"), o2.get("type"), o2.get("stopPrice")))
    except Exception as e:
        print("  [WARN] stop_market failed: %s" % e)
    print("")
    print("[9] open orders")
    oo = br.list_open_orders(SYMBOL)
    print("  count=%d" % len(oo))
    for o in oo:
        print("    id=%s %s %s stop=%s qty=%s" % (o.get("orderId"), o.get("type"), o.get("side"), o.get("stopPrice"), o.get("origQty")))
    print("")
    print("[10] sleep 2s, CLOSE (reduceOnly market sell)")
    time.sleep(2)
    o3 = br.place_market(SYMBOL, "SELL", pos["qty"], reduce_only=True)
    print("  -> orderId=%s status=%s" % (o3.get("orderId"), o3.get("status")))
    time.sleep(1)
    print("")
    print("[11] cancel_all (clear leftover stop_market)")
    r = br.cancel_all(SYMBOL)
    print("  -> %s" % r)
    print("")
    print("[12] position (after close)")
    pos2 = br.get_position(SYMBOL)
    print("  -> qty=%s side=%s" % (pos2["qty"], pos2["side"]))
    print("")
    print("[13] balance (after)")
    bal1 = br.get_balance()
    b1 = bal1["balance"]
    delta = b1 - b0
    print("  balance=%.4fU  delta=%+.4fU" % (b1, delta))
    print("  expected: ~ -%.4fU (2 x taker fee)" % (notional*0.0004*2))
    print("")
    print("=" * 60)
    if pos2["side"] == "FLAT" and abs(delta) < 0.5:
        print(" [PASS] dust test OK")
    else:
        print(" [PARTIAL] review output above")
    print("=" * 60)

if __name__ == "__main__":
    main()
