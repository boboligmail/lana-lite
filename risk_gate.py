"""Risk gate: daily/3-day cumulative loss limits + HALT flag."""
import os, json, fcntl  # v0.1.22
from datetime import datetime, timedelta

DATA_DIR = "/root/lana-lite"
STATE_PATH = os.path.join(DATA_DIR, "risk_state.json")
HALT_PATH = os.path.join(DATA_DIR, "HALT")

DAILY_LOSS_LIMIT = -10.0
THREE_DAY_LIMIT = -25.0  # v0.1.22: upgraded for 100U/2-unit per handbook §11.7

def _today():
    return datetime.now().strftime("%Y-%m-%d")

def _load():
    if not os.path.exists(STATE_PATH):
        return {"day": _today(), "daily_loss_u": 0.0, "halt_3day_until": None, "history": []}
    with open(STATE_PATH) as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # v0.1.22
        try:
            return json.load(f)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

def _save(s):
    # v0.1.22: cross-process EX lock + fsync + atomic rename
    lock_fd = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

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
