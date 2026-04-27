#!/bin/bash
N=$(python3 << 'PYEOF'
import json
ps = json.loads(open('/root/lana-lite/paper_state.json').read())
MAINT = {'dedup_cleanup_v0.1.16d', 'manual_close_v0.1.16_orphan', 'phase7_dust'}
real = [c for c in ps.get('closed', [])
        if c.get('reason') not in MAINT
        and not (c.get('reason') == 'trail_stop' and abs(c.get('pnl_u', 0) or 0) < 0.005)]
print(len(real))
PYEOF
)
echo "$(date '+%F %T') paper real_trade count: $N / 30"
if [ "$N" -ge 30 ]; then
    TOKEN=$(grep ^TELEGRAM_BOT_TOKEN /root/lana-lite/.env | cut -d= -f2)
    CHAT=$(grep ^TELEGRAM_CHAT_ID /root/lana-lite/.env | cut -d= -f2)
    MSG="🎯 P1 门槛达成: paper real_trade=${N} ≥ 30 单
可启动下一阶段优化:
- TP1+TP2 锁利设计
- r 阈值回测扫描
- ATR-based trail
- 持仓时长上限"
    curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${CHAT}" \
        --data-urlencode "text=${MSG}" > /dev/null
    # 通知后改 cron 每天 1 次,避免重复推送
fi
