"""从 git 历史提取 oi_anomaly 灌进 signals_log.jsonl,按 timestamp+symbol 去重"""
import subprocess, json, os

OUT = "signals_log.jsonl"
seen = set()
if os.path.exists(OUT):
    with open(OUT) as f:
        for line in f:
            try:
                d = json.loads(line)
                seen.add((d.get("timestamp"), d.get("symbol")))
            except Exception:
                pass

commits = subprocess.check_output(
    ["git", "log", "--format=%H", "--reverse", "--", "latest_snapshot.json"]
).decode().splitlines()

added = 0
with open(OUT, "a", encoding="utf-8") as out:
    for c in commits:
        try:
            blob = subprocess.check_output(
                ["git", "show", f"{c}:latest_snapshot.json"],
                stderr=subprocess.DEVNULL
            ).decode()
            d = json.loads(blob)
        except Exception:
            continue
        ts = d.get("timestamp", "")
        for a in d.get("oi_anomaly", []):
            key = (ts, a.get("symbol"))
            if key in seen:
                continue
            seen.add(key)
            out.write(json.dumps({"timestamp": ts, **a}, ensure_ascii=False) + "\n")
            added += 1
print(f"迁移完成,新增 {added} 条")
