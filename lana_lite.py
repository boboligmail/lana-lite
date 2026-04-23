"""
拉哪 Lite - 舆情热度 + 多时间框架 OI 监控
版本: v0.1.7 (CoinGecko API Key + GitHub Actions 部署就绪)
新增:
  - CoinGecko demo key 支持 (避免限流)
  - 实时价格 (fetch_spot_price)
  - 上线天数 (via exchangeInfo)
  - 多时间框架 OI (1h/4h/12h/1d)
  - 综合方向判定 (aggregate_signal)
  - 动态死币识别 (24h>85% 且 OI 1h<5% 自动跳过)
  - 两阶段扫描 (先 1h 筛选,通过后再补多 TF 省 API)
"""

import os
import time
import json
import requests
import schedule
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ========== 配置 ==========
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

STABLECOINS = {"USDT","USDC","BUSD","FDUSD","DAI","TUSD","USDP","USDD","PYUSD"}
PERMANENT_BLACKLIST = {"RAVE"}  # 已归零/退市/诈骗确认的币

OI_RATIO_THRESHOLD   = 3.0
OI_CHANGE_THRESHOLD  = 0.08
HEAT_TOP_N           = 40

FAPI = "https://fapi.binance.com"
CG   = "https://api.coingecko.com/api/v3"

_exchange_info_cache = {}
CG_API_KEY = os.getenv("COINGECKO_API_KEY", "")  # 可选 demo key


# ========== 工具函数 ==========
def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print("[WARN] Telegram 未配置,仅打印:\n", text)
        return
    url = "https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        print("[TG ERROR]", e)


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ========== 数据源 ==========
def fetch_coingecko_trending() -> set:
    try:
        headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
        r = requests.get(CG + "/search/trending", headers=headers, timeout=10).json()
        return {c["item"]["symbol"].upper() for c in r.get("coins", [])} - STABLECOINS
    except Exception as e:
        log(f"CoinGecko 失败: {e}")
        return set()


def fetch_binance_gainers(top_n: int = 40) -> list:
    try:
        r = requests.get(FAPI + "/fapi/v1/ticker/24hr", timeout=10).json()
        pairs = [x for x in r
                 if x["symbol"].endswith("USDT")
                 and x["symbol"].replace("USDT","") not in STABLECOINS
                 and x["symbol"].replace("USDT","") not in PERMANENT_BLACKLIST]
        pairs.sort(key=lambda x: abs(float(x["priceChangePercent"])), reverse=True)
        return pairs[:top_n]
    except Exception as e:
        log(f"币安涨跌榜失败: {e}")
        return []


def refresh_exchange_info():
    """拉所有合约的上线时间,每 24h 刷一次"""
    global _exchange_info_cache
    try:
        r = requests.get(FAPI + "/fapi/v1/exchangeInfo", timeout=10).json()
        _exchange_info_cache = {s["symbol"]: s.get("onboardDate", 0)
                                for s in r.get("symbols", [])}
        log(f"exchangeInfo 已缓存 {len(_exchange_info_cache)} 个合约")
    except Exception as e:
        log(f"exchangeInfo 失败: {e}")


def get_listing_age_days(symbol: str) -> int:
    ts_ms = _exchange_info_cache.get(symbol, 0)
    if not ts_ms:
        return 9999
    return int((time.time() * 1000 - ts_ms) / 86400000)


def fetch_spot_price(symbol: str) -> float:
    try:
        r = requests.get(FAPI + "/fapi/v1/ticker/price",
                         params={"symbol": symbol}, timeout=5).json()
        return float(r.get("price", 0))
    except Exception as e:
        log(f"{symbol} 价格失败: {e}")
        return 0


def fetch_oi_at_tf(symbol: str, period: str, limit: int):
    """拉单个时间框架的 OI 数据"""
    try:
        r = requests.get(FAPI + "/futures/data/openInterestHist",
                         params={"symbol": symbol, "period": period, "limit": limit},
                         timeout=10).json()
        if not isinstance(r, list) or len(r) < 2:
            return None
        old, new = r[0], r[-1]
        oi_old = float(old["sumOpenInterest"])
        oi_new = float(new["sumOpenInterest"])
        if oi_old == 0: return None
        p_old = float(old["sumOpenInterestValue"]) / oi_old
        p_new = float(new["sumOpenInterestValue"]) / oi_new
        if p_old == 0: return None
        oi_pct    = (oi_new - oi_old) / oi_old * 100
        price_pct = (p_new - p_old) / p_old * 100
        ratio = abs(oi_pct) / abs(price_pct) if price_pct != 0 else 999
        return {
            "oi_pct": round(oi_pct, 2),
            "price_pct": round(price_pct, 2),
            "ratio": round(ratio, 2),
        }
    except Exception as e:
        log(f"{symbol} OI({period}) 失败: {e}")
        return None


def fetch_oi_multi(symbol: str) -> dict:
    """拉 4h/12h/1d 三个时间框架(1h 已在主筛选拉过)"""
    tfs = {"4h":  ("15m", 17),
           "12h": ("1h",  13),
           "1d":  ("2h",  13)}
    out = {}
    for tf, (period, limit) in tfs.items():
        d = fetch_oi_at_tf(symbol, period, limit)
        if d:
            out[tf] = d
        time.sleep(0.2)
    return out


# ========== 判定规则 ==========
def is_dead_coin(pct_24h: float, oi_1h_pct: float) -> bool:
    """动态死币识别:24h 变化 >85% 且 OI 1h 变化 <5% → 崩完了,跳过"""
    return abs(pct_24h) > 85 and abs(oi_1h_pct) < 5


def classify_direction(oi_p: float, pr_p: float, ra: float) -> str:
    if oi_p >= 15 and pr_p >= 3 and ra >= 5:    return "🟢 做多强(A)"
    if oi_p >= 15 and pr_p <= -3 and ra >= 5:   return "🔴 做空强(B)"
    if oi_p <= -15 and pr_p <= -5:              return "⚪ 多头撤退(C)"
    if oi_p <= -15 and pr_p >= 5:               return "⚪ 空头回补(D)"
    return "⚪ 观望"


def aggregate_signal(tags: dict) -> str:
    """把 4 个时间框架方向合成综合信号"""
    longs  = sum(1 for t in tags.values() if "做多强" in t)
    shorts = sum(1 for t in tags.values() if "做空强" in t)
    if longs >= 3:   return "🟢🟢 多时间框架共振-做多"
    if shorts >= 3:  return "🔴🔴 多时间框架共振-做空"
    if longs == 2:   return "🟢 短中期做多"
    if shorts == 2:  return "🔴 短中期做空"
    if longs >= 1 and shorts == 0: return "🟢 弱做多"
    if shorts >= 1 and longs == 0: return "🔴 弱做空"
    return "⚪ 信号冲突-观望"


# ========== 核心逻辑 ==========
def build_heat_board() -> list:
    cg   = fetch_coingecko_trending()
    rows = fetch_binance_gainers(HEAT_TOP_N)
    scored = []
    for g in rows:
        sym  = g["symbol"]
        base = sym.replace("USDT","")
        if base in STABLECOINS or base in PERMANENT_BLACKLIST:
            continue
        score = 3
        if base in cg: score += 2
        if abs(float(g["priceChangePercent"])) > 15: score += 1
        scored.append({
            "symbol": sym,
            "base": base,
            "price_change_24h": round(float(g["priceChangePercent"]), 2),
            "score": score,
            "listing_days": get_listing_age_days(sym),
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def scan_anomalies(heat_board: list) -> list:
    """两阶段扫描:1h 快速筛选 → 通过后补多 TF + 价格"""
    anomalies = []
    for item in heat_board[:HEAT_TOP_N]:
        oi_1h = fetch_oi_at_tf(item["symbol"], "5m", 13)
        time.sleep(0.2)
        if not oi_1h:
            continue
        if is_dead_coin(item["price_change_24h"], oi_1h["oi_pct"]):
            log(f"跳过死币 {item['symbol']}")
            continue
        if abs(oi_1h["oi_pct"]) > 90:
            continue
        if abs(oi_1h["oi_pct"]) < OI_CHANGE_THRESHOLD * 100:
            continue
        if oi_1h["ratio"] < OI_RATIO_THRESHOLD:
            continue
        # 通过1h筛选,补多 TF + 实时价
        multi  = fetch_oi_multi(item["symbol"])
        price  = fetch_spot_price(item["symbol"])
        tf_all = {"1h": oi_1h, **multi}
        tags   = {tf: classify_direction(d["oi_pct"], d["price_pct"], d["ratio"])
                  for tf, d in tf_all.items()}
        anomalies.append({
            **item,
            "spot_price": price,
            "tf": tf_all,
            "tags": tags,
            "aggregate": aggregate_signal(tags),
        })
    return anomalies


def save_snapshot(heat, anomalies):
    ts = datetime.now().isoformat()
    with open("latest_snapshot.json", "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": ts,
            "version": "v0.1.10",
            "top_heat": heat[:20],
            "oi_anomaly": anomalies,
        }, f, ensure_ascii=False, indent=2)
    if anomalies:
        with open("signals_log.jsonl", "a", encoding="utf-8") as f:
            for a in anomalies:
                f.write(json.dumps({"timestamp": ts, **a}, ensure_ascii=False) + "\n")


def run_once():
    log("开始扫描...")
    heat = build_heat_board()
    log(f"热度榜 Top5: {[x['symbol'] for x in heat[:5]]}")
    anomalies = scan_anomalies(heat)
    save_snapshot(heat, anomalies)
    if not anomalies:
        log("无异动")
        return
    lines = ["🔥 *OI 异动提醒 v0.1.6*", ""]
    for a in anomalies[:5]:
        lines.append(f"*{a['symbol']}*  `${a['spot_price']}`")
        lines.append(f"上线 {a['listing_days']}天 | 24h {a['price_change_24h']:+.1f}% | 热度 {a['score']}")
        for tf in ["1h","4h","12h","1d"]:
            d = a["tf"].get(tf)
            if not d: continue
            emoji = a["tags"][tf].split(" ")[0]
            lines.append(f"`{tf:>3s}` OI {d['oi_pct']:+.1f}% | P {d['price_pct']:+.1f}% | r {d['ratio']} {emoji}")
        lines.append(f"*综合:{a['aggregate']}*")
        lines.append("━━━━━━━━━━")
    lines.append(f"_{datetime.now().strftime('%m-%d %H:%M')}_")
    lines.append("⚠️ 仅信号,需人工判断")
    tg_send("\n".join(lines))
    log(f"推送 {len(anomalies)} 条")


if __name__ == "__main__":
    log("拉哪 Lite v0.1.7 启动")
    refresh_exchange_info()
    tg_send("✅ 拉哪 Lite v0.1.7 已启动\n新功能: CoinGecko Key + GitHub Actions 部署就绪")
    run_once()
    schedule.every(5).minutes.do(run_once)
    schedule.every(24).hours.do(refresh_exchange_info)
    while True:
        schedule.run_pending()
        time.sleep(10)
