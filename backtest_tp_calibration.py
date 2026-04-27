#!/usr/bin/env python3
"""backtest_tp_calibration.py v2 - 去除30min窗口 + trail激活门槛 + 诊断"""
import csv, json, os, urllib.request, urllib.parse, time, statistics
from datetime import datetime, timedelta
from itertools import product

CSV          = "/root/lana-lite/backtest_h1_result.csv"
KLINES_DIR   = "/root/lana-lite/cache_klines"
OUT_CSV      = "/root/lana-lite/calibration_results.csv"
FAPI         = "https://fapi.binance.com"
SH_OFF_H     = 8
MARGIN, LEV  = 10.0, 5.0
STOP_U       = 10.0

os.makedirs(KLINES_DIR, exist_ok=True)

def to_bool(v): return str(v).strip().lower() in ("true","1","yes")
def to_float(v, d=0.0):
    try: return float(v)
    except: return d

with open(CSV, newline="") as f: rows = list(csv.DictReader(f))
hit = [r for r in rows if to_bool(r.get("h1_match",""))]
print(f"[init] CSV 总行: {len(rows)} / H1 命中: {len(hit)}")

def fetch(symbol, t0_str):
    safe = symbol + "_" + t0_str.replace(":","").replace("-","").replace("T","_")[:18]
    cache = os.path.join(KLINES_DIR, safe + ".json")
    if os.path.exists(cache):
        with open(cache) as f: return json.load(f)
    t0 = datetime.fromisoformat(t0_str)
    start_ms = int((t0 - timedelta(hours=SH_OFF_H)).timestamp() * 1000)
    end_ms   = start_ms + 24 * 3600 * 1000
    url = f"{FAPI}/fapi/v1/klines?" + urllib.parse.urlencode({
        "symbol": symbol, "interval":"5m", "startTime":start_ms, "endTime":end_ms, "limit":500})
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
        with open(cache,"w") as f: json.dump(data, f)
        return data
    except Exception as e:
        print(f"  [err] {symbol}@{t0_str}: {e}"); return None

print("\n[Phase 0] 拉取 K 线 + 诊断")
samples = []
for i, r in enumerate(hit, 1):
    sym = r["symbol"]; t0 = r["t0"]
    kl = fetch(sym, t0)
    if not kl or len(kl) < 12: continue
    samples.append((sym, t0, kl, r))
    time.sleep(0.05)
print(f"[Phase 0] 有效样本: {len(samples)}")

print("\n[诊断] 抽样验证 K 线窗口对齐 (前5)")
print("  样本                 entry        实测_peak%   实测_end%    CSV_peak%   CSV_end%")
for sym, t0, kl, r in samples[:5]:
    e = float(kl[0][4])
    ph = max(float(b[2]) for b in kl)
    lc = float(kl[-1][4])
    real_pk = (ph/e - 1) * 100
    real_ed = (lc/e - 1) * 100
    csv_pk  = to_float(r["peak_pct"])
    csv_ed  = to_float(r["end_pct"])
    mark = "OK" if abs(real_pk - csv_pk) < 5 else "!!"
    print(f"  {mark} {sym:12s} {t0[:16]} e={e:>10.6g}  {real_pk:+8.2f}%  {real_ed:+8.2f}%   {csv_pk:+8.2f}%  {csv_ed:+8.2f}%")

# ---- Phase 1: 分布 ----
def qsum(label, vs):
    if not vs: return
    vs = sorted(vs); n=len(vs); q=lambda p: vs[min(int(p*n),n-1)]
    print(f"  {label:9s}: med={statistics.median(vs):+7.2f}% p25={q(.25):+7.2f}% p75={q(.75):+7.2f}% min={min(vs):+7.2f}% max={max(vs):+7.2f}%")

print("\n[Phase 1] H1 命中 24h 分布")
peaks = [to_float(r["peak_pct"]) for r in hit]
draws = [to_float(r["draw_pct"]) for r in hit]
ends  = [to_float(r["end_pct"]) for r in hit]
qsum("peak", peaks); qsum("draw", draws); qsum("end", ends)
print("\n  峰值阈值频率:")
for thr in [3,5,8,10,12,15,20,30,50]:
    n=sum(1 for p in peaks if p>=thr); print(f"    peak>=+{thr:3d}%: {n:2d}/{len(peaks)}={n/len(peaks):>5.0%}")

# ---- 模拟器 v2 ----
def simulate(klines, tp1, frac, trail, trail_act, stop_u=STOP_U):
    if not klines or len(klines) < 2: return 0.0, "no_data"
    entry = float(klines[0][4])
    qty = (MARGIN * LEV) / entry
    qty_left = qty
    pnl = 0.0
    tp1_done = (tp1 == 0)
    trail_active = False
    trail_price = None
    last_close = entry
    for k in klines[1:]:
        high=float(k[2]); low=float(k[3]); close=float(k[4])
        last_close = close
        # 1. 硬止损
        if qty_left * (low - entry) <= -stop_u:
            pnl += -stop_u
            return pnl, ("stop_loss_post_tp1" if tp1_done and tp1 > 0 else "stop_loss")
        # 2. TP1 触发 (无时间窗)
        if not tp1_done and tp1 > 0 and frac > 0 and high >= entry * (1 + tp1):
            cq = qty * frac
            pnl += cq * (entry * tp1)
            qty_left -= cq
            tp1_done = True
            if frac >= 0.999:
                return pnl, "tp1_full"
            if trail > 0:
                trail_active = True
                trail_price = max(entry, high * (1 - trail))
        # 3. 纯 trail 激活 (tp1=0 且 trail>0, 等 high 突破激活门槛)
        if not trail_active and tp1 == 0 and trail > 0:
            if high >= entry * (1 + trail_act):
                trail_active = True
                trail_price = max(entry, high * (1 - trail))
        # 4. trail 棘轮 + 触发
        if trail_active:
            trail_price = max(trail_price, high * (1 - trail))
            if low <= trail_price:
                pnl += qty_left * (trail_price - entry)
                qty_left = 0
                return pnl, "trail_stop"
    if qty_left > 0:
        pnl += qty_left * (last_close - entry)
        return pnl, "end_24h"
    return pnl, "closed"

# ---- 候选生成 ----
combos = []
combos.append((0.0, 0.0, 0.0, 0.0, "bare_hold"))
for trail in [0.05, 0.07, 0.10, 0.15, 0.20]:
    for trail_act in [0.0, 0.05, 0.10, 0.15, 0.20]:
        combos.append((0.0, 0.0, trail, trail_act, "pure_trail"))
for tp1 in [0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.50]:
    for frac in [0.30, 0.50, 0.70, 1.00]:
        combos.append((tp1, frac, 0.0, 0.0, "tp1_only"))
for tp1 in [0.05, 0.08, 0.10, 0.15, 0.20]:
    for frac in [0.30, 0.50, 0.70]:
        for trail in [0.05, 0.10, 0.15]:
            combos.append((tp1, frac, trail, 0.0, "tp1_trail"))
print(f"\n[Phase 2] 候选: {len(combos)}")

results = []
for tp1, frac, trail, trail_act, mode in combos:
    pnls, reasons = [], []
    for sym, t0, kl, _ in samples:
        p, r = simulate(kl, tp1, frac, trail, trail_act)
        pnls.append(p); reasons.append(r)
    n = len(pnls)
    sp = sorted(pnls)
    results.append({
        "mode":mode, "tp1":tp1, "frac":frac, "trail":trail, "act":trail_act,
        "E":round(sum(pnls)/n,2), "med":round(sp[n//2],2),
        "win%":round(sum(p>0 for p in pnls)/n*100,0),
        "worst":round(min(pnls),2), "best":round(max(pnls),2),
        "total":round(sum(pnls),2),
        "stop":sum(r.startswith("stop") for r in reasons),
        "trail_n":sum(r=="trail_stop" for r in reasons),
        "tp1_n":sum(r in("tp1_full",) for r in reasons),
        "end":sum(r=="end_24h" for r in reasons),})

results.sort(key=lambda x: x["E"], reverse=True)

def fmt(r):
    return (f"  {r['mode']:10s} tp1={r['tp1']*100:>5.1f}% frac={r['frac']*100:>4.0f}% trail={r['trail']*100:>4.1f}% act={r['act']*100:>4.1f}%"
        f" | E={r['E']:+7.2f} med={r['med']:+6.2f} win={r['win%']:>3.0f}%"
        f" | worst={r['worst']:+7.2f} best={r['best']:+7.2f} total={r['total']:+8.2f}"
        f" | stop={r['stop']} trail={r['trail_n']} tp1={r['tp1_n']} end={r['end']}")

print("\n=== TOP 25 (按 E 降序) ===")
for r in results[:25]: print(fmt(r))

def find(mode, **kw):
    for r in results:
        if r["mode"]==mode and all(abs(r[k]-v)<1e-6 for k,v in kw.items()): return r

print("\n=== 关键参考点 ===")
bh = find("bare_hold")
cur = find("tp1_only", tp1=0.05, frac=0.50)
print("  [基线] " + fmt(bh)[2:] if bh else "")
print("  [当前] " + fmt(cur)[2:] if cur else "")

keys = list(results[0].keys())
with open(OUT_CSV,"w",newline="") as f:
    w=csv.DictWriter(f,fieldnames=keys); w.writeheader()
    for r in results: w.writerow(r)
print(f"\n[done] {OUT_CSV} ({len(results)} 行)")
