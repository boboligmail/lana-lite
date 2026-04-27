#!/bin/bash
# 周日 20:00 自动推送胜率快照到 TG
cd /root/lana-lite
TOKEN=$(grep ^TELEGRAM_BOT_TOKEN .env | cut -d= -f2)
CHAT=$(grep ^TELEGRAM_CHAT_ID .env | cut -d= -f2)
TEXT=$(python3 paper_stats.py --quiet 2>&1)
# Telegram 消息 4096 字符上限,quiet 模式输出短
curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${CHAT}" \
    --data-urlencode "text=📊 拉哪 Lite 周报${NL}${NL}${TEXT}" \
    > /dev/null
