
#!/usr/bin/env python3
"""Binance Square 'Highest searched (6h)' + Fear&Greed.

Access: Playwright headless Chromium (AWS WAF blocks pure requests).
"""
import json, re, sys
from pathlib import Path
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

URL = "https://www.binance.com/zh-CN/square"
OUT_SQUARE = Path("/root/lana-lite/square_log.jsonl")
OUT_FG = Path("/root/lana-lite/fear_greed_log.jsonl")

APP_DATA_RE = re.compile(
    r'<script\s+id="__APP_DATA"[^>]*>(.*?)</script>',
    re.DOTALL,
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

def fetch_html() -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_function(
            "!!document.querySelector('script#__APP_DATA')",
            timeout=30000,
        )
        html = page.content()
        browser.close()
    return html

def extract_fg_data(html):
    m = APP_DATA_RE.search(html)
    if not m:
        raise RuntimeError("__APP_DATA missing after WAF wait")
    data = json.loads(m.group(1))
    fg = (data.get("pageData", {})
              .get("redux", {})
              .get("ui", {})
              .get("sidebarData", {})
              .get("fearGreedData"))
    if not fg:
        raise RuntimeError("fearGreedData missing - schema changed")
    return fg

def main():
    html = fetch_html()
    fg = extract_fg_data(html)
    now = datetime.now(timezone.utc).isoformat()

    fg_core = fg.get("fearGreed") or {}
    fg_row = {
        "fetched_at": now,
        "source": "binance_square_fear_greed",
        "current_value": fg_core.get("currentValue"),
        "yesterday_value": fg_core.get("yesterdayValue"),
        "last_week_value": fg_core.get("lastWeekValue"),
        "bullish_value": fg_core.get("bullishValue"),
        "bearish_value": fg_core.get("bearishValue"),
    }
    with OUT_FG.open("a") as f:
        f.write(json.dumps(fg_row, ensure_ascii=False) + "\n")

    coins = fg.get("highestSearchedCoinPairList") or []
    if not coins:
        raise RuntimeError("highestSearchedCoinPairList empty")
    with OUT_SQUARE.open("a") as f:
        for rank, item in enumerate(coins, 1):
            kline = item.get("klineChartDataPointList") or []
            last = kline[-1] if kline else {}
            row = {
                "fetched_at": now,
                "source": "binance_square_highest_searched_6h",
                "symbol": item.get("symbol"),
                "code": item.get("code"),
                "bridge": item.get("bridge"),
                "rank": rank,
                "is_rapid": bool(item.get("isRapid", False)),
                "price": last.get("value"),
                "price_ts": last.get("dateTime"),
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"[{datetime.now().isoformat()}] wrote 1 fg row + {len(coins)} coin rows")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] ERROR: {e}", file=sys.stderr)
        sys.exit(1)

