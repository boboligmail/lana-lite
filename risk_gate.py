"""Risk gate: daily/3-day cumulative loss limits + HALT flag."""
import os, json
from datetime import datetime, timedelta

DATA_DIR = "/root/lana-lite"
STATE_PATH = os.path.join(DATA_DIR, "risk_state.json")
HALT_PATH = os.path.join(DATA_DIR, "HALT")

DAILY_LOSS_LIMIT = -10.0
THREE_DAY_LIMIT = -15.0

def _today():
    return datetime.now().strftime("%Y-%m-%d")

def _load():
    if not os.path.exists(STATE_PATH):
        return {"day": _today(), "daily_loss_u": 0.0, "halt_3day_until": None, "history": []}
    with open(STATE_PATH) as f:
        return json.load(f)

def _save(s):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2, default=str)
    os.replace(tmp, STATE_PATH)

def _roll_day(s):
    today = _today()
    if s.get("day") == today:
        return s
    if s.get("day"):
        h = s.setdefault("history", [])
        h.append({"day": s["day"], "loss_u": s.get("daily_loss_u", 0.0)})
        s["history"] = h[-30:]
    s["day"] = today
    s["daily_loss_u"] = 0.0
    return s

def can_open():
    if os.path.exists(HALT_PATH):
        return False, "HALT flag file exists"
    s = _load()
    s = _roll_day(s)
    _save(s)
    until = s.get("halt_3day_until")
    if until and datetime.now().isoformat() < until:
        return False, "3-day halt active until " + str(until)
    if s.get("daily_loss_u", 0.0) <= DAILY_LOSS_LIMIT:
        return False, "daily loss " + str(s["daily_loss_u"]) + "U <= limit " + str(DAILY_LOSS_LIMIT) + "U"
    return True, "ok"

def record_close(pnl_u):
    s = _load()
    s = _roll_day(s)
    s["daily_loss_u"] = round(s.get("daily_loss_u", 0.0) + pnl_u, 4)
    today = datetime.now()
    cutoff = (today - timedelta(days=3)).strftime("%Y-%m-%d")
    cumul = s["daily_loss_u"]
    for h in s.get("history", []):
        if h["day"] >= cutoff:
            cumul += h.get("loss_u", 0.0)
    if cumul <= THREE_DAY_LIMIT:
        until = (today + timedelta(days=3)).isoformat()
        s["halt_3day_until"] = until
    _save(s)
    return s

def status():
    return _load()
