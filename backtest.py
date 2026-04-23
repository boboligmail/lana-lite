#!/usr/bin/env python3
"""
Lana Lite 生命周期回测 v2
- 修复:startTime 早于合约上线时使用"最早可用 K 线"作为 entry
- 修复:遇到 None 返回时提供 debug 输出
"""
import subprocess, json, requests, time, csv
from datetime import datetime

REPO = "/root/lana-lite"
FAPI = "https://fapi.binance.com/fapi/v1/klines"
NOW_MS = int(time.time() * 1000)

def git_snapshots():
    cmd = ["git", "-C", REPO, "log", "--pretty=format:%H", "--", "latest_snapshot.json"]
    shas = subprocess.check_output(cmd, text=True).strip().split("\n")
    for sha in reversed(shas):
        try:
            content = subprocess.check_output(
                ["git", "-C", REPO, "show", f"{sha}:latest_snapshot.json"],
                text=True, stderr=subprocess.DEVNULL
            )
            yield json.loads(content)
        except Exception:
            continue
    try:
        with open(f"{REPO}/latest_snapshot.json") as f:
            yield json.load(f)
    except Exception:
        pass

def kline_close(symbol, ts_ms, window_h=6):
    """取 ts_ms 开始 window_h 小时内的第一根 K 线收盘价"""
    try:
        r = requests.get(FAPI, params={
            "symbol": symbol, "interval": "1h",
            "startTime": ts_ms,
            "endTime": ts_ms + window_h * 3600000,
            "limit": 1
        }, timeout=10)
        k = r.json()
        if isinstance(k, list) and k:
            return float(k[0][4])
    except Exception:
        pass
    return None

def kline_earliest(symbol):
    """取该合约最早可用 K 线收盘价(fallback 用于合约后上线的情况)"""
    try:
        r = requests.get(FAPI, params={
            "symbol": symbol, "interval": "1h",
            "startTime": 0, "limit": 1
        }, timeout=10)
        k = r.json()
        if isinstance(k, list) and k:
            return int(k[0][0]), float(k[0][4])
    except Exception:
        pass
    return None, None

def kline_range(symbol, start_ms, end_ms):
    out = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            r = requests.get(FAPI, params={
                "symbol": symbol, "interval": "1h",
                "startTime": cursor, "endTime": end_ms, "limit": 500
            }, timeout=15)
            k = r.json()
            if not isinstance(k, list) or not k:
                break
            for c in k:
                out.append((int(c[0]), float(c[2]), float(c[3]), float(c[4])))
            cursor = k[-1][0] + 3600000
            if len(k) < 500:
                break
        except Exception:
            break
        time.sleep(0.05)
    return out

def main():
    snaps = list(git_snapshots())
    print(f"Loaded {len(snaps)} snapshots\n")

    coin_history = {}
    for data in snaps:
        ts_str = data.get("timestamp")
        if not ts_str:
            continue
        try:
            ts_ms = int(datetime.fromisoformat(ts_str).timestamp() * 1000)
        except Exception:
            continue
        for pick in data.get("top_heat", []):
            sym = pick.get("symbol")
            if not sym:
                continue
            if sym not in coin_history:
                coin_history[sym] = {
                    "first_ts": ts_ms, "last_ts": ts_ms, "seen": 1,
                    "first_score": pick.get("score"),
                    "first_chg24h": pick.get("price_change_24h"),
                    "listing_days": pick.get("listing_days"),
                }
            else:
                coin_history[sym]["last_ts"] = max(coin_history[sym]["last_ts"], ts_ms)
                coin_history[sym]["first_ts"] = min(coin_history[sym]["first_ts"], ts_ms)
                coin_history[sym]["seen"] += 1

    print(f"共追踪 {len(coin_history)} 个独立币\n")

    results = []
    skipped = []
    for sym, h in sorted(coin_history.items(), key=lambda x: -x[1]["seen"]):
        entry_ts = h["first_ts"]
        entry = kline_close(sym, entry_ts, window_h=6)
        used_fallback = False

        # fallback:first_ts 早于合约上线,取最早可用
        if entry is None:
            earliest_ts, earliest_px = kline_earliest(sym)
            if earliest_px is not None:
                entry = earliest_px
                entry_ts = earliest_ts
                used_fallback = True

        if entry is None:
            skipped.append(sym)
            continue

        klines = kline_range(sym, entry_ts, NOW_MS)
        if not klines:
            skipped.append(sym)
            continue

        highs = [k[1] for k in klines]
        lows = [k[2] for k in klines]
        current_price = klines[-1][3]

        peak_ret = (max(highs) - entry) / entry * 100
        max_dd = (min(lows) - entry) / entry * 100
        current_ret = (current_price - entry) / entry * 100

        def ret_at(h_offset):
            target = entry_ts + h_offset * 3600000
            if target > NOW_MS:
                return None
            for ts, hi, lo, cl in klines:
                if ts >= target:
                    return round((cl - entry) / entry * 100, 2)
            return None

        tag = " [合约晚于入池]" if used_fallback else ""
        row = {
            "symbol": sym, "seen_count": h["seen"],
            "first_seen": datetime.fromtimestamp(h["first_ts"]/1000).strftime("%m-%d %H:%M"),
            "entry_ts": datetime.fromtimestamp(entry_ts/1000).strftime("%m-%d %H:%M"),
            "entry": entry, "current": round(current_price, 6),
            "current_ret_%": round(current_ret, 2),
            "peak_ret_%": round(peak_ret, 2),
            "max_drawdown_%": round(max_dd, 2),
            "ret_1h": ret_at(1), "ret_4h": ret_at(4),
            "ret_24h": ret_at(24), "ret_72h": ret_at(72),
            "listing_days": h["listing_days"],
            "first_score": h["first_score"],
            "first_chg24h": h["first_chg24h"],
            "fallback": used_fallback,
        }
        results.append(row)
        print(f"  {sym:14s} 入池{h['seen']:2d}次 | 峰值{peak_ret:+7.2f}% | 回撤{max_dd:+7.2f}% | 现在{current_ret:+7.2f}%{tag}")
        time.sleep(0.1)

    if skipped:
        print(f"\n⚠️  真正查不到数据的 symbol ({len(skipped)}个): {skipped}")
        print("  → 这些可能是 Binance 合约用了不同 ticker (如 1000XXX),或 CoinGecko 假币")

    print(f"\n{'='*60}")
    print(f"成功回测: {len(results)} 币 | 跳过: {len(skipped)} 币")

    for key, label in [("ret_1h","T+1h"),("ret_4h","T+4h"),("ret_24h","T+24h"),("ret_72h","T+72h"),("current_ret_%","至今")]:
        vals = [r[key] for r in results if r[key] is not None]
        if vals:
            wins = sum(1 for x in vals if x > 0)
            print(f"{label:6s}: {len(vals):3d}币 胜率{wins/len(vals)*100:5.1f}% 均值{sum(vals)/len(vals):+6.2f}% 中位{sorted(vals)[len(vals)//2]:+6.2f}%")

    peaks = [r["peak_ret_%"] for r in results]
    dds = [r["max_drawdown_%"] for r in results]
    if peaks:
        print(f"\n峰值收益: 均值{sum(peaks)/len(peaks):+.2f}% 最高{max(peaks):+.2f}%")
        print(f"最大回撤: 均值{sum(dds)/len(dds):+.2f}% 最深{min(dds):+.2f}%")

    print(f"\n=== 按推荐次数分组 ===")
    groups = {"只推1-3次": [], "推4-10次": [], "推11+次": []}
    for r in results:
        c = r["seen_count"]
        k = "只推1-3次" if c<=3 else ("推4-10次" if c<=10 else "推11+次")
        groups[k].append(r["current_ret_%"])
    for n, rs in groups.items():
        if rs:
            w = sum(1 for x in rs if x>0)
            print(f"  {n}: {len(rs)}币 至今胜率{w/len(rs)*100:.1f}% 均值{sum(rs)/len(rs):+.2f}%")

    if results:
        keys = list(results[0].keys())
        with open(f"{REPO}/backtest_results.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for r in results:
                w.writerow(r)
        print(f"\nCSV: {REPO}/backtest_results.csv")

        srt = sorted(results, key=lambda x: x["current_ret_%"], reverse=True)
        print(f"\n🏆 至今涨幅 Top 10:")
        for r in srt[:10]:
            print(f"  {r['symbol']:14s} {r['current_ret_%']:+8.2f}% (入池{r['seen_count']}次, 峰值{r['peak_ret_%']:+.2f}%)")
        print(f"\n💀 至今跌幅 Top 10:")
        for r in srt[-10:]:
            print(f"  {r['symbol']:14s} {r['current_ret_%']:+8.2f}% (入池{r['seen_count']}次, 回撤{r['max_drawdown_%']:+.2f}%)")

if __name__ == "__main__":
    main()
