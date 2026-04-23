#!/usr/bin/env python3
"""
Lana 信号胜率回测(基于 snapshot.oi_anomaly)
- entry = 推送当时的 spot_price(不是 K 线开/收盘价)
- 两种涨跌口径:
  A: 现价 vs 推荐价(按 aggregate 方向取正/反)
  B: 现价 vs 推荐后最高价(从峰值回落)
- 按 aggregate 标签分组算胜率
"""
import subprocess, json, requests, time, csv
from datetime import datetime
from collections import defaultdict

REPO = "/root/lana-lite"
FAPI = "https://fapi.binance.com/fapi/v1/klines"
TICKER = "https://fapi.binance.com/fapi/v1/ticker/price"
NOW_MS = int(time.time() * 1000)

def git_snapshots():
    cmd = ["git", "-C", REPO, "log", "--pretty=format:%H", "--", "latest_snapshot.json"]
    shas = subprocess.check_output(cmd, text=True).strip().split("\n")
    for sha in reversed(shas):
        try:
            c = subprocess.check_output(
                ["git", "-C", REPO, "show", f"{sha}:latest_snapshot.json"],
                text=True, stderr=subprocess.DEVNULL)
            yield json.loads(c)
        except Exception:
            continue
    try:
        with open(f"{REPO}/latest_snapshot.json") as f:
            yield json.load(f)
    except Exception:
        pass

def direction_of(agg: str) -> str:
    """从 aggregate 标签推方向"""
    if "做多" in agg: return "long"
    if "做空" in agg: return "short"
    return "neutral"

def fetch_range(symbol, start_ms, end_ms):
    out = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            r = requests.get(FAPI, params={
                "symbol": symbol, "interval": "1h",
                "startTime": cursor, "endTime": end_ms, "limit": 500
            }, timeout=15).json()
            if not isinstance(r, list) or not r: break
            for c in r:
                out.append((int(c[0]), float(c[2]), float(c[3]), float(c[4])))
            cursor = r[-1][0] + 3600000
            if len(r) < 500: break
        except Exception: break
        time.sleep(0.05)
    return out

def fetch_current(symbol):
    try:
        r = requests.get(TICKER, params={"symbol": symbol}, timeout=5).json()
        return float(r.get("price", 0)) or None
    except Exception:
        return None

def main():
    snaps = list(git_snapshots())
    print(f"Loaded {len(snaps)} snapshots\n")

    # 去重:同一 (symbol, 小时) 只算一次
    seen = set()
    signals = []
    for data in snaps:
        ts_str = data.get("timestamp")
        if not ts_str: continue
        try:
            ts_ms = int(datetime.fromisoformat(ts_str).timestamp() * 1000)
        except Exception: continue
        for a in data.get("oi_anomaly", []):
            sym = a.get("symbol")
            price = a.get("spot_price")
            agg = a.get("aggregate", "")
            if not sym or not price: continue
            key = (sym, ts_ms // 3600000)
            if key in seen: continue
            seen.add(key)
            signals.append({
                "ts_ms": ts_ms,
                "ts": ts_str[:16],
                "symbol": sym,
                "entry": float(price),
                "aggregate": agg,
                "direction": direction_of(agg),
                "score": a.get("score"),
                "chg24h": a.get("price_change_24h"),
                "listing_days": a.get("listing_days"),
            })

    if not signals:
        print("⚠️  没找到任何 oi_anomaly 推送 — 可能是历史 snapshot 里 oi_anomaly 都是空数组")
        return

    print(f"共找到 {len(signals)} 条独立推送信号\n")

    # 给每条算 A/B 口径
    results = []
    for s in signals:
        klines = fetch_range(s["symbol"], s["ts_ms"], NOW_MS)
        current = klines[-1][3] if klines else fetch_current(s["symbol"])
        if not current: continue

        # 推荐后最高价 / 最低价
        peak_high = max((k[1] for k in klines), default=current)
        peak_low = min((k[2] for k in klines), default=current)

        # A 口径:现价 vs 推荐价,按方向取符号
        raw_chg = (current - s["entry"]) / s["entry"] * 100
        ret_A = raw_chg if s["direction"] == "long" else (-raw_chg if s["direction"] == "short" else raw_chg)

        # B 口径:现价 vs 推荐后最高价(做多)或最低价(做空)
        if s["direction"] == "long":
            ret_B = (current - peak_high) / peak_high * 100
            peak_ret = (peak_high - s["entry"]) / s["entry"] * 100  # 期间最大利润
        elif s["direction"] == "short":
            ret_B = -(current - peak_low) / peak_low * 100
            peak_ret = -(peak_low - s["entry"]) / s["entry"] * 100
        else:
            ret_B = 0
            peak_ret = max(abs((peak_high - s["entry"])/s["entry"]*100),
                          abs((peak_low - s["entry"])/s["entry"]*100))

        # 判断方向预测是否"对"(涨跌方向与 aggregate 一致)
        correct = "N/A"
        if s["direction"] == "long":
            correct = "✅" if current > s["entry"] else "❌"
        elif s["direction"] == "short":
            correct = "✅" if current < s["entry"] else "❌"

        row = {
            "ts": s["ts"], "symbol": s["symbol"], "aggregate": s["aggregate"],
            "direction": s["direction"], "entry": s["entry"],
            "current": round(current, 6),
            "ret_A_%": round(ret_A, 2),
            "ret_B_%": round(ret_B, 2),
            "peak_ret_%": round(peak_ret, 2),
            "correct": correct,
            "score": s["score"], "chg24h": s["chg24h"],
            "listing_days": s["listing_days"],
        }
        results.append(row)
        print(f"{correct} {s['ts']} {s['symbol']:14s} {s['aggregate']:30s}  A口径{ret_A:+7.2f}%  B口径{ret_B:+7.2f}%  峰值{peak_ret:+7.2f}%")
        time.sleep(0.1)

    # ========== 汇总 ==========
    print(f"\n{'='*70}")
    print(f"总信号数: {len(results)}")

    # 按 aggregate 分组
    print(f"\n=== 按 aggregate 标签分组 ===")
    groups = defaultdict(list)
    for r in results:
        groups[r["aggregate"]].append(r)

    for agg, rs in sorted(groups.items(), key=lambda x: -len(x[1])):
        if not rs: continue
        correct_count = sum(1 for r in rs if r["correct"] == "✅")
        total_decided = sum(1 for r in rs if r["correct"] in ("✅", "❌"))
        win_rate = correct_count / total_decided * 100 if total_decided else 0
        mean_A = sum(r["ret_A_%"] for r in rs) / len(rs)
        mean_B = sum(r["ret_B_%"] for r in rs) / len(rs)
        mean_peak = sum(r["peak_ret_%"] for r in rs) / len(rs)
        print(f"  {agg}")
        print(f"    样本:{len(rs)}  方向正确率:{win_rate:5.1f}% ({correct_count}/{total_decided})")
        print(f"    A口径均值:{mean_A:+6.2f}%  B口径均值:{mean_B:+6.2f}%  峰值均值:{mean_peak:+6.2f}%")

    # 按方向大类分组
    print(f"\n=== 按方向大类分组 ===")
    for d in ["long", "short", "neutral"]:
        rs = [r for r in results if r["direction"] == d]
        if not rs: continue
        correct_count = sum(1 for r in rs if r["correct"] == "✅")
        total_decided = sum(1 for r in rs if r["correct"] in ("✅", "❌"))
        wr = correct_count / total_decided * 100 if total_decided else 0
        mean_A = sum(r["ret_A_%"] for r in rs) / len(rs)
        peak = sum(r["peak_ret_%"] for r in rs) / len(rs)
        label = {"long":"做多","short":"做空","neutral":"观望"}[d]
        print(f"  {label}({d}): {len(rs)}条 方向正确率{wr:.1f}% A均值{mean_A:+.2f}% 峰值均值{peak:+.2f}%")

    # 按 listing_days 分组
    print(f"\n=== 按 listing_days 分组(A口径) ===")
    buckets = {"新<7d": [], "中7-90d": [], "老>90d": []}
    for r in results:
        d = r["listing_days"] or 999
        k = "新<7d" if d<7 else ("中7-90d" if d<90 else "老>90d")
        buckets[k].append(r["ret_A_%"])
    for n, rs in buckets.items():
        if rs:
            w = sum(1 for x in rs if x > 0)
            print(f"  {n}: {len(rs)}样本  胜率{w/len(rs)*100:.1f}%  均值{sum(rs)/len(rs):+.2f}%")

    # 导 CSV
    if results:
        with open(f"{REPO}/backtest_signals.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            for r in results: w.writerow(r)
        print(f"\nCSV: {REPO}/backtest_signals.csv")

if __name__ == "__main__":
    main()
