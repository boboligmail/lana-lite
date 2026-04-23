#!/usr/bin/env python3
"""Lana Lite 生命周期回测 v3 (2026-04-23)
修复 v2 的价格计算 bug:
- 删掉 kline_close / kline_earliest (fapi 在 startTime < 上线时静默返首根 → 虚高 10x)
- entry 按优先级多源取:
    1) jsonl 的 spot_price / price / last_price > 0
    2) snapshot top_heat 的 price (v0.1.11+)
    3) fapi kline at first_ts,强制校验 openTime gap ≤ 2h,否则踢出汇总
- current 用 fapi /ticker/price (不用 klines 末尾)
- timestamp 统一按 UTC 解析,修 VPS 本地时区漂移
- 每个币输出 entry_src,按来源分组统计让数据质量一目了然
"""
import subprocess, json, requests, time, csv, os
from datetime import datetime, timezone

REPO = "/root/lana-lite"
JSONL = f"{REPO}/signals_log.jsonl"
FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
FAPI_TICKER = "https://fapi.binance.com/fapi/v1/ticker/price"
NOW_MS = int(time.time() * 1000)
KLINE_TOLERANCE_MS = 2 * 3600 * 1000

PRICE_FIELDS = ("spot_price", "price", "last_price", "perp_price", "current_price")

def parse_ts_utc(ts_str):
    if not ts_str:
        return None
    try:
        s = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        # naive → 按 VPS 本地 tz 解析(lana 用 datetime.now().isoformat())
        # aware  → 尊重显式 tz
        return int(dt.timestamp() * 1000)
    except Exception:
        return None

def pick_price(d):
    for k in PRICE_FIELDS:
        v = d.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v), k
    return None, None

def read_jsonl():
    if not os.path.exists(JSONL):
        return
    with open(JSONL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = parse_ts_utc(d.get("timestamp"))
            sym = d.get("symbol")
            if ts and sym:
                yield ts, sym, d

def read_git_snapshots():
    try:
        shas = subprocess.check_output(
            ["git","-C",REPO,"log","--pretty=format:%H","--","latest_snapshot.json"],
            text=True).strip().split("\n")
    except Exception:
        shas = []
    for sha in reversed(shas):
        try:
            c = subprocess.check_output(
                ["git","-C",REPO,"show",f"{sha}:latest_snapshot.json"],
                text=True, stderr=subprocess.DEVNULL)
            d = json.loads(c)
            ts = parse_ts_utc(d.get("timestamp"))
            if ts:
                yield ts, d
        except Exception:
            continue
    try:
        d = json.load(open(f"{REPO}/latest_snapshot.json"))
        ts = parse_ts_utc(d.get("timestamp"))
        if ts:
            yield ts, d
    except Exception:
        pass

def fetch_ticker(sym):
    try:
        r = requests.get(FAPI_TICKER, params={"symbol": sym}, timeout=10)
        d = r.json()
        if isinstance(d, dict) and d.get("price"):
            return float(d["price"])
    except Exception:
        pass
    return None

def fetch_klines(sym, start_ms, end_ms):
    out, cursor = [], start_ms
    while cursor < end_ms:
        try:
            r = requests.get(FAPI_KLINES, params={
                "symbol": sym, "interval":"1h",
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
    coin = {}

    def record(ts, sym, d, tag):
        price, pf = pick_price(d)
        if sym not in coin:
            coin[sym] = {
                "first_ts": ts, "last_ts": ts, "seen": 1,
                "first_price": price,
                "price_src": f"{tag}.{pf}" if price else None,
                "score": d.get("score"), "chg24h": d.get("price_change_24h"),
                "listing_days": d.get("listing_days"),
            }
        else:
            c = coin[sym]
            if ts < c["first_ts"]:
                c["first_ts"] = ts
                c["first_price"] = price
                c["price_src"] = f"{tag}.{pf}" if price else None
                c["score"] = d.get("score") or c["score"]
                c["chg24h"] = d.get("price_change_24h") or c["chg24h"]
                c["listing_days"] = d.get("listing_days") or c["listing_days"]
            if ts > c["last_ts"]:
                c["last_ts"] = ts
            c["seen"] += 1

    j_cnt = s_cnt = 0
    for ts, sym, d in read_jsonl():
        record(ts, sym, d, "jsonl"); j_cnt += 1
    for ts, snap in read_git_snapshots():
        for p in snap.get("top_heat", []):
            if p.get("symbol"):
                record(ts, p["symbol"], p, "top_heat")
        s_cnt += 1
    print(f"吃了 {j_cnt} 条 jsonl + {s_cnt} 条 snapshot,追踪 {len(coin)} 币\n")

    results, skipped = [], []
    for sym, h in sorted(coin.items(), key=lambda x: -x[1]["seen"]):
        entry_ts, entry, src = h["first_ts"], h["first_price"], h["price_src"]

        if entry is None:
            try:
                r = requests.get(FAPI_KLINES, params={
                    "symbol": sym, "interval":"1h",
                    "startTime": entry_ts,
                    "endTime": entry_ts + 6*3600000, "limit": 1
                }, timeout=10)
                k = r.json()
                if isinstance(k, list) and k:
                    gap = abs(int(k[0][0]) - entry_ts)
                    if gap <= KLINE_TOLERANCE_MS:
                        entry = float(k[0][4]); src = "fapi_kline"
                    else:
                        skipped.append((sym, f"fapi K 线晚于入池 {gap/3600000:.1f}h"))
                        continue
                else:
                    skipped.append((sym, "fapi kline 无数据")); continue
            except Exception as e:
                skipped.append((sym, f"kline 异常:{e}")); continue

        current = fetch_ticker(sym)
        if current is None:
            skipped.append((sym, "ticker 无数据")); continue

        ks = fetch_klines(sym, entry_ts, NOW_MS)
        if ks:
            peak, low = max(k[1] for k in ks), min(k[2] for k in ks)
            gap_h = (ks[0][0] - entry_ts) / 3600000
        else:
            peak, low, gap_h = current, current, None

        def ret_at(h_off):
            if not ks: return None
            target = entry_ts + h_off*3600000
            if target > NOW_MS: return None
            for ts_, hi, lo, cl in ks:
                if ts_ >= target:
                    return round((cl - entry)/entry*100, 2)
            return None

        row = {
            "symbol": sym, "seen": h["seen"],
            "first_seen_utc": datetime.fromtimestamp(entry_ts/1000, tz=timezone.utc).strftime("%m-%d %H:%MZ"),
            "entry": round(entry, 6), "entry_src": src,
            "current": round(current, 6),
            "current_ret%": round((current-entry)/entry*100, 2),
            "peak_ret%": round((peak-entry)/entry*100, 2),
            "max_dd%": round((low-entry)/entry*100, 2),
            "ret_1h": ret_at(1), "ret_4h": ret_at(4),
            "ret_24h": ret_at(24), "ret_72h": ret_at(72),
            "listing_days": h["listing_days"], "score": h["score"],
            "chg24h": h["chg24h"],
            "fapi_gap_h": round(gap_h, 2) if gap_h is not None else None,
        }
        results.append(row)
        gap_tag = f" [fapi晚{gap_h:.1f}h]" if gap_h and gap_h > 2 else ""
        print(f"  {sym:14s} 见{h['seen']:2d}次 | {entry:.5f}→{current:.5f} | 峰{row['peak_ret%']:+6.1f}% 撤{row['max_dd%']:+6.1f}% 今{row['current_ret%']:+6.1f}% [{src}]{gap_tag}")
        time.sleep(0.08)

    print(f"\n{'='*70}\n成功 {len(results)} | 跳过 {len(skipped)}")
    if skipped:
        for s, reason in skipped[:15]:
            print(f"  ✗ {s:14s} {reason}")
        if len(skipped) > 15:
            print(f"  ... 共 {len(skipped)} 个见 CSV")

    for key, lbl in [("ret_1h","T+1h"),("ret_4h","T+4h"),("ret_24h","T+24h"),("ret_72h","T+72h"),("current_ret%","至今")]:
        v = [r[key] for r in results if r[key] is not None]
        if v:
            w = sum(1 for x in v if x>0)
            print(f"{lbl:6s}: {len(v):3d}币 胜率{w/len(v)*100:5.1f}% 均{sum(v)/len(v):+6.2f}% 中{sorted(v)[len(v)//2]:+6.2f}%")

    if results:
        peaks = [r["peak_ret%"] for r in results]
        dds = [r["max_dd%"] for r in results]
        print(f"\n峰值: 均{sum(peaks)/len(peaks):+.2f}% 最高{max(peaks):+.2f}%")
        print(f"回撤: 均{sum(dds)/len(dds):+.2f}% 最深{min(dds):+.2f}%")

        print(f"\n=== 按见面次数分组 ===")
        groups = {"1-3次": [], "4-10次": [], "11+次": []}
        for r in results:
            c = r["seen"]
            k = "1-3次" if c<=3 else ("4-10次" if c<=10 else "11+次")
            groups[k].append(r["current_ret%"])
        for n, rs in groups.items():
            if rs:
                w = sum(1 for x in rs if x>0)
                print(f"  见{n}: {len(rs)}币 至今胜率{w/len(rs)*100:.1f}% 均{sum(rs)/len(rs):+.2f}%")

        print(f"\n=== 按 entry 来源分组(数据质量)===")
        src_groups = {}
        for r in results:
            s = (r["entry_src"] or "unknown").split(".")[0]
            src_groups.setdefault(s, []).append(r["current_ret%"])
        for s, rs in sorted(src_groups.items(), key=lambda x:-len(x[1])):
            w = sum(1 for x in rs if x>0)
            print(f"  {s:14s}: {len(rs)}币 胜率{w/len(rs)*100:.1f}% 均{sum(rs)/len(rs):+.2f}%")

        keys = list(results[0].keys())
        with open(f"{REPO}/backtest_results.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys); w.writeheader()
            for r in results: w.writerow(r)
        print(f"\nCSV: {REPO}/backtest_results.csv")

        if skipped:
            with open(f"{REPO}/backtest_skipped.csv", "w", newline="") as f:
                w = csv.writer(f); w.writerow(["symbol","reason"]); w.writerows(skipped)
            print(f"跳过 CSV: {REPO}/backtest_skipped.csv")

        srt = sorted(results, key=lambda x: x["current_ret%"], reverse=True)
        print(f"\n🏆 至今涨幅 Top 10:")
        for r in srt[:10]:
            g = f" [晚{r['fapi_gap_h']}h]" if r.get("fapi_gap_h") and r["fapi_gap_h"]>2 else ""
            print(f"  {r['symbol']:14s} {r['current_ret%']:+8.2f}% 见{r['seen']}次 峰{r['peak_ret%']:+.1f}% [{r['entry_src']}]{g}")
        print(f"\n💀 至今跌幅 Top 10:")
        for r in srt[-10:]:
            g = f" [晚{r['fapi_gap_h']}h]" if r.get("fapi_gap_h") and r["fapi_gap_h"]>2 else ""
            print(f"  {r['symbol']:14s} {r['current_ret%']:+8.2f}% 见{r['seen']}次 撤{r['max_dd%']:+.1f}% [{r['entry_src']}]{g}")

if __name__ == "__main__":
    main()
