#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拉哪 Lite — 历史推送/平仓数据胜率统计 v3
v3 改动:
- 字段名兼容 paper trail_stop (pnl_u/exit_price/qty_closed) vs paper orphan/real (realized_pnl_u/close_price/qty)
- 三类样本分离: real_trade (PnL≠0) / breakeven_trail (trail_stop PnL=0) / maint (清理)
- 胜率仅在 real_trade 子集计算
"""
import json, sys, pathlib
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

ROOT = pathlib.Path("/root/lana-lite")
PAPER_STATE = ROOT / "paper_state.json"
REAL_STATE  = ROOT / "real_state.json"
SIGNALS_LOG = ROOT / "signals_log.jsonl"
MIN_SAMPLES_FOR_WINRATE = 10
TZ_CN = timezone(timedelta(hours=8))
MAINT_REASONS = {"dedup_cleanup_v0.1.16d", "manual_close_v0.1.16_orphan", "phase7_dust"}

# ============ Helpers ============
def load_json(p):
    if not p.exists(): return {}
    try: return json.loads(p.read_text())
    except: return {}

def load_jsonl(p):
    if not p.exists(): return []
    out = []
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if not ln: continue
        try: out.append(json.loads(ln))
        except: pass
    return out

def parse_ts(s):
    if not s: return None
    s = str(s).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_CN)
        return dt
    except: return None

def hold_bucket(h):
    if h is None: return "?"
    if h < 1: return "<1h"
    if h < 4: return "1-4h"
    if h < 12: return "4-12h"
    if h < 24: return "12-24h"
    return ">24h"

def hour_bucket(h):
    if h is None: return "?"
    if 0 <= h < 6: return "00-06 夜"
    if 6 <= h < 12: return "06-12 早"
    if 12 <= h < 18: return "12-18 午"
    return "18-24 晚"

def fmt_u(u):
    if u == 0: return "+0.00U"
    if abs(u) < 0.01: return f"{u:+.4f}U"
    return f"{u:+.2f}U"

def fmt_pct(p): return f"{p*100:.1f}%"

# 字段名兼容
def get_pnl(c):
    v = c.get("realized_pnl_u")
    if v is None: v = c.get("pnl_u")
    return v if v is not None else 0

def get_exit_price(c):
    return c.get("close_price") or c.get("exit_price") or 0

def get_qty(c):
    return c.get("qty") or c.get("qty_closed") or 0

def get_reason(c):
    return c.get("reason") or c.get("close_reason") or "?"

def is_breakeven_trail(c):
    """trail_stop 但 PnL=0 视为保本退出(by design 行为,非真盈亏)"""
    return get_reason(c) == "trail_stop" and abs(get_pnl(c)) < 0.005

def classify(c):
    """三分类: maint / breakeven_trail / real_trade"""
    if get_reason(c) in MAINT_REASONS: return "maint"
    if is_breakeven_trail(c): return "breakeven_trail"
    return "real_trade"

# ============ 已平仓分析 ============
def analyze_closed(name, closed):
    res = {"name": name, "n": len(closed)}
    if not closed:
        res["msg"] = "无平仓样本"
        return res

    by_class = defaultdict(list)
    for c in closed:
        by_class[classify(c)].append(c)
    res["maint_n"]      = len(by_class["maint"])
    res["breakeven_n"]  = len(by_class["breakeven_trail"])
    res["real_n"]       = len(by_class["real_trade"])
    res["maint_breakdown"]    = dict(Counter(get_reason(c) for c in by_class["maint"]))
    res["breakeven_symbols"]  = dict(Counter(c.get("symbol", "?") for c in by_class["breakeven_trail"]))

    real_trades = by_class["real_trade"]
    if not real_trades:
        res["msg"] = (f"全部 {res['n']} 条非真实交易"
                      f" — maint:{res['maint_n']} / breakeven:{res['breakeven_n']}")
        return res

    pnls = [get_pnl(c) for c in real_trades]
    wins = [p for p in pnls if p > 0]
    pnl_sum = sum(pnls)
    res.update({
        "wins": len(wins),
        "losses": len(real_trades) - len(wins),
        "winrate": len(wins) / len(real_trades),
        "pnl_sum": pnl_sum,
        "pnl_avg": pnl_sum / len(real_trades),
        "pnl_max": max(pnls),
        "pnl_min": min(pnls),
        "ok_winrate": len(real_trades) >= MIN_SAMPLES_FOR_WINRATE,
    })

    def bucket(field_fn, items):
        d = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
        for c in items:
            k = field_fn(c)
            p = get_pnl(c)
            d[k]["n"] += 1
            d[k]["pnl"] += p
            if p > 0: d[k]["wins"] += 1
        return dict(d)

    res["by_reason"] = bucket(get_reason, real_trades)
    res["by_symbol"] = bucket(lambda c: c.get("symbol", "?"), real_trades)
    res["by_side"]   = bucket(lambda c: c.get("side", "?"), real_trades)
    res["by_hold"]   = bucket(
        lambda c: hold_bucket((parse_ts(c.get("close_ts")) - parse_ts(c.get("open_ts"))).total_seconds()/3600
                              if (parse_ts(c.get("open_ts")) and parse_ts(c.get("close_ts"))) else None),
        real_trades)
    res["by_hour"]   = bucket(
        lambda c: hour_bucket(parse_ts(c.get("open_ts")).hour if parse_ts(c.get("open_ts")) else None),
        real_trades)
    return res

# ============ 信号分析 ============
def analyze_signals(sigs):
    res = {"n": len(sigs)}
    if not sigs:
        res["msg"] = "无信号"
        return res

    with_h1 = [s for s in sigs if "h1" in s]
    no_h1   = [s for s in sigs if "h1" not in s]
    res["with_h1_n"] = len(with_h1)
    res["legacy_n"]  = len(no_h1)

    if with_h1:
        st = [s for s in with_h1 if s.get("h1", {}).get("should_trade")]
        res["should_trade_n"] = len(st)
        res["should_trade_rate"] = len(st) / len(with_h1)
        res["by_h1_level"] = dict(Counter(s.get("h1", {}).get("level", "?") for s in with_h1))
        if st:
            res["should_trade_by_symbol_top10"] = Counter(
                s.get("symbol", "?") for s in st).most_common(10)
            res["should_trade_by_level"] = dict(Counter(
                s.get("h1", {}).get("level", "?") for s in st))

    res["by_aggregate"] = dict(Counter(s.get("aggregate", "?") for s in sigs).most_common(10))

    by_r = defaultdict(int)
    for s in sigs:
        tf = s.get("tf", {}) or {}
        rs = [float(tf.get(k, {}).get("ratio", 0) or 0) for k in ("1h","4h","12h","1d")]
        r = max(rs) if rs else 0
        if r < 1.0: b = "<1.0"
        elif r < 1.5: b = "1.0-1.5"
        elif r < 2.0: b = "1.5-2.0"
        elif r < 3.0: b = "2.0-3.0"
        elif r < 5.0: b = "3.0-5.0"
        else: b = "≥5.0"
        by_r[b] += 1
    res["by_max_ratio"] = dict(by_r)

    by_hour = Counter()
    for s in sigs:
        t = parse_ts(s.get("timestamp"))
        by_hour[hour_bucket(t.hour if t else None)] += 1
    res["by_hour"] = dict(by_hour)
    res["by_symbol_top10"] = Counter(s.get("symbol", "?") for s in sigs).most_common(10)
    return res

# ============ 渲染 ============
def render_closed(r, quiet=False):
    print(f"\n{'='*60}")
    print(f"  {r['name']}  样本数: {r['n']}")
    print('='*60)
    if r['n'] == 0:
        print(f"  {r.get('msg','')}")
        return

    print(f"  真实交易: {r.get('real_n', 0)}  保本退出(trail breakeven): {r.get('breakeven_n', 0)}  维护清理: {r.get('maint_n', 0)}")
    if r.get("breakeven_symbols"):
        print(f"  保本退出明细: {r['breakeven_symbols']}")
    if r.get("maint_breakdown"):
        print(f"  维护清理明细: {r['maint_breakdown']}")

    if not r.get("real_n"):
        print(f"  ⚠️  {r.get('msg','无真实交易样本')}")
        return

    if not r.get("ok_winrate"):
        print(f"  ⚠️  真实交易 {r['real_n']} < {MIN_SAMPLES_FOR_WINRATE},胜率仅供参考")

    print(f"\n  胜: {r['wins']}  负: {r['losses']}  胜率: {fmt_pct(r['winrate'])}")
    print(f"  累计盈亏: {fmt_u(r['pnl_sum'])}  均盈亏: {fmt_u(r['pnl_avg'])}")
    print(f"  最大盈: {fmt_u(r['pnl_max'])}  最大亏: {fmt_u(r['pnl_min'])}")

    if quiet: return

    def render_bucket(title, d, order=None):
        if not d: return
        print(f"\n  [{title}]")
        if order:
            items = [(k, d[k]) for k in order if k in d]
        else:
            items = sorted(d.items(), key=lambda x: -x[1]["n"])
        for k, v in items:
            wr = v["wins"]/v["n"]*100 if v["n"] else 0
            print(f"    {str(k)[:25]:<25} n={v['n']:<3} pnl={fmt_u(v['pnl']):<10} wr={wr:.0f}%")

    render_bucket("按平仓原因", r.get("by_reason", {}))
    render_bucket("按币种",     r.get("by_symbol", {}))
    render_bucket("按方向",     r.get("by_side", {}))
    render_bucket("按持仓时长", r.get("by_hold", {}),
                  order=["<1h","1-4h","4-12h","12-24h",">24h","?"])
    render_bucket("按入场时段", r.get("by_hour", {}),
                  order=["00-06 夜","06-12 早","12-18 午","18-24 晚","?"])

def render_signals(sr, quiet=False):
    print(f"\n{'='*60}")
    print(f"  SIGNALS_LOG  累计: {sr['n']} 条  (新引擎 h1: {sr.get('with_h1_n',0)} / 旧记录: {sr.get('legacy_n',0)})")
    print('='*60)
    if sr['n'] == 0: return

    if sr.get("should_trade_n") is not None:
        print(f"\n  ⭐ h1.should_trade=true 强信号: {sr['should_trade_n']} 条 ({fmt_pct(sr['should_trade_rate'])} of h1 子集)")

    if sr.get("by_h1_level"):
        print(f"\n  [h1 level 分布]")
        for k, v in sorted(sr["by_h1_level"].items(), key=lambda x: -x[1]):
            print(f"    {k:<20} {v} 次")

    if sr.get("should_trade_by_symbol_top10") and not quiet:
        print(f"\n  [should_trade=true Top10 币种]")
        for sym, n in sr["should_trade_by_symbol_top10"]:
            print(f"    {sym:<15} {n} 次")

    if sr.get("by_aggregate"):
        print(f"\n  [aggregate 标签 Top10]")
        for k, v in sr["by_aggregate"].items():
            print(f"    {str(k)[:30]:<30} {v} 次")

    if sr.get("by_max_ratio"):
        print(f"\n  [信号 max(tf ratio) 分布]")
        for k in ["<1.0","1.0-1.5","1.5-2.0","2.0-3.0","3.0-5.0","≥5.0"]:
            if k in sr["by_max_ratio"]:
                print(f"    {k:<10} {sr['by_max_ratio'][k]} 次")

    if not quiet:
        if sr.get("by_hour"):
            print(f"\n  [时段分布]")
            for k in ["00-06 夜","06-12 早","12-18 午","18-24 晚","?"]:
                if k in sr["by_hour"]:
                    print(f"    {k:<12} {sr['by_hour'][k]} 次")
        if sr.get("by_symbol_top10"):
            print(f"\n  [全信号 Top10 币种]")
            for sym, n in sr["by_symbol_top10"]:
                print(f"    {sym:<15} {n} 次")

def render_conversion(paper_r, real_r, sig_r):
    print(f"\n{'='*60}")
    print(f"  信号 → 交易转化率")
    print('='*60)
    paper_state = load_json(PAPER_STATE)
    real_state  = load_json(REAL_STATE)
    paper_open = len(paper_state.get("positions", []))
    real_open  = len(real_state.get("positions", []))
    paper_total = paper_r["n"] + paper_open
    real_total  = real_r["n"] + real_open
    print(f"  累计信号:           {sig_r['n']} 条")
    if sig_r.get("should_trade_n"):
        st = sig_r["should_trade_n"]
        print(f"  其中 should_trade:  {st} 条 (应触发的强信号)")
        if st > 0:
            print(f"  Paper 累计开单:     {paper_total} 单 ({paper_total/st*100:.1f}% of strong)")
            print(f"  Real 累计开单:      {real_total} 单 ({real_total/st*100:.1f}% of strong)")
    print(f"  Paper 持仓: {paper_open}  已平: {paper_r['n']}")
    print(f"  Real 持仓:  {real_open}  已平: {real_r['n']}")

# ============ Main ============
def main():
    args = sys.argv[1:]
    json_mode = "--json" in args
    quiet = "--quiet" in args

    paper = load_json(PAPER_STATE)
    real  = load_json(REAL_STATE)
    sigs  = load_jsonl(SIGNALS_LOG)

    paper_r = analyze_closed("PAPER (模拟盘)", paper.get("closed", []))
    real_r  = analyze_closed("REAL (真盘)",   real.get("closed", []))
    sig_r   = analyze_signals(sigs)

    if json_mode:
        out = {
            "generated_at": datetime.now(TZ_CN).isoformat(),
            "paper": paper_r, "real": real_r, "signals": sig_r,
            "paper_open": len(paper.get("positions", [])),
            "real_open":  len(real.get("positions", [])),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return

    print(f"\n📊 拉哪 Lite 历史数据胜率快照 v3  @ {datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')}")
    render_closed(paper_r, quiet=quiet)
    render_closed(real_r,  quiet=quiet)
    render_signals(sig_r, quiet=quiet)
    render_conversion(paper_r, real_r, sig_r)
    print()

if __name__ == "__main__":
    main()
