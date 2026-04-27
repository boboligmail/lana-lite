#!/usr/bin/env python3
"""
backtest_h1.py — 验证 H1 假设
"""
import json, time, csv, statistics, urllib.request, urllib.parse
from datetime import datetime, timedelta
from collections import defaultdict

JSONL = "/root/lana-lite/signals_log.jsonl"
OUT_CSV = "/root/lana-lite/backtest_h1_result.csv"
FAPI = "https://fapi.binance.com"
DEDUP_WINDOW_MIN = 60
WIN_THRESHOLD = 3.0
SHANGHAI_OFFSET_H = 8
V012_CUTOFF = "2026-04-23T17:26:00"

def load_and_dedup():
    rows = []
    with open(JSONL) as f:
        for line in f:
            try: rows.append(json.loads(line))
            except: continue
    rows.sort(key=lambda r: r["timestamp"])
    last_seen = {}
    keep = []
    for r in rows:
        sym = r.get("symbol")
        if not sym: continue
        try: t0 = datetime.fromisoformat(r["timestamp"])
        except: continue
        if sym in last_seen and (t0 - last_seen[sym]).total_seconds() < DEDUP_WINDOW_MIN * 60:
            continue
        last_seen[sym] = t0
        r["_t0_dt"] = t0
        keep.append(r)
    return keep

def h1_judge(r):
    tf = r.get("tf", {})
    try:
        per = {k: tf[k] for k in ("1h","4h","12h","1d")}
    except KeyError:
        return None
    c1 = all(p["oi_pct"] > 0 and p["price_pct"] > 0 for p in per.values())
    c2 = all(p["ratio"] >= 1.3 for p in per.values())
    c3 = per["1d"]["ratio"] <= 2 * per["4h"]["ratio"]
    return {"c1": c1, "c2": c2, "c3": c3, "h1": c1 and c2 and c3,
            "r4h": per["4h"]["ratio"], "r1d": per["1d"]["ratio"]}

def to_utc_ms(dt_naive):
    return int((dt_naive - timedelta(hours=SHANGHAI_OFFSET_H)).timestamp() * 1000)

def fetch_klines(symbol, t0_dt):
    start_ms = to_utc_ms(t0_dt)
    end_ms = start_ms + 24 * 3600 * 1000
    url = f"{FAPI}/fapi/v1/klines?" + urllib.parse.urlencode(
        {"symbol": symbol, "interval": "5m", "startTime": start_ms,
         "endTime": end_ms, "limit": 500})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None

def compute_perf(klines):
    if not klines or len(klines) < 2: return None
    entry = float(klines[0][4])
    closes = [float(k[4]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    return {"entry": entry,
            "peak_pct": (max(highs) - entry) / entry * 100,
            "draw_pct": (min(lows)  - entry) / entry * 100,
            "end_pct":  (closes[-1] - entry) / entry * 100}

def main():
    rows = load_and_dedup()
    print(f"[load] 去重后 {len(rows)} 条独立信号")
    results = []
    for i, r in enumerate(rows, 1):
        sym = r["symbol"]; t0 = r["_t0_dt"]
        version = "v0.1.12" if r["timestamp"] >= V012_CUTOFF else "pre-v0.1.12"
        h1 = h1_judge(r)
        if h1 is None: continue
        kl = fetch_klines(sym, t0)
        if kl is None:
            print(f"  [{i}/{len(rows)}] {sym} 无合约,跳过"); continue
        perf = compute_perf(kl)
        if perf is None: continue
        results.append({
            "version": version, "symbol": sym, "t0": r["timestamp"],
            "aggregate": r.get("aggregate",""),
            "listing_days": r.get("listing_days"),
            "c1": h1["c1"], "c2": h1["c2"], "c3": h1["c3"],
            "h1_match": h1["h1"], "r4h": round(h1["r4h"],2), "r1d": round(h1["r1d"],2),
            "peak_pct": round(perf["peak_pct"],2),
            "draw_pct": round(perf["draw_pct"],2),
            "end_pct":  round(perf["end_pct"],2),
            "win": perf["peak_pct"] >= WIN_THRESHOLD,
        })
        time.sleep(0.12)
        if i % 20 == 0: print(f"  ...{i}/{len(rows)}")

    if not results:
        print("[err] 没有有效结果"); return
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader(); w.writerows(results)
    print(f"\n[csv] {OUT_CSV} 共 {len(results)} 行\n")

    def stats(group, label):
        if not group:
            print(f"  {label:32s}: N=0"); return
        peaks = [r["peak_pct"] for r in group]
        ends  = [r["end_pct"] for r in group]
        draws = [r["draw_pct"] for r in group]
        wins = sum(r["win"] for r in group)
        print(f"  {label:32s}: N={len(group):3d}  "
              f"峰均={statistics.mean(peaks):+6.2f}%  "
              f"末均={statistics.mean(ends):+6.2f}%  "
              f"撤均={statistics.mean(draws):+6.2f}%  "
              f"胜率={wins/len(group)*100:5.1f}%")

    print("=" * 90)
    print("【全样本 vs H1 命中】")
    stats(results, "全样本")
    stats([r for r in results if r["h1_match"]], "H1 命中")
    stats([r for r in results if not r["h1_match"]], "H1 未命中")
    print("\n【按版本切分】")
    for v in ("pre-v0.1.12", "v0.1.12"):
        sub = [r for r in results if r["version"] == v]
        print(f"-- {v}")
        stats(sub, "  全部")
        stats([r for r in sub if r["h1_match"]], "  H1 命中")
        stats([r for r in sub if not r["h1_match"]], "  H1 未命中")
    print("\n【单条件消融】")
    stats([r for r in results if r["c1"] and r["c2"] and r["c3"]], "C1+C2+C3 全满足")
    stats([r for r in results if r["c1"] and r["c2"] and not r["c3"]], "C1+C2 但 C3 失败")
    stats([r for r in results if r["c1"] and not r["c2"]], "C1 但 C2 失败")
    stats([r for r in results if not r["c1"]], "C1 不满足")
    print("\n【新币过滤 H3】")
    stats([r for r in results if (r["listing_days"] or 0) < 14 or r["listing_days"] == 9999], "新币(<14d 或 9999)")
    stats([r for r in results if r["listing_days"] and 14 <= r["listing_days"] < 9999], "成熟币(≥14d)")

if __name__ == "__main__":
    main()
