import streamlit as st
import json
from datetime import datetime
from pathlib import Path

st.set_page_config(page_title="拉哪 Lite Dashboard", page_icon="🦞", layout="wide")

# 读取脚本生成的 snapshot
snap_path = Path("latest_snapshot.json")
if not snap_path.exists():
    st.warning("还没有 snapshot 数据，等脚本跑一轮后刷新")
    st.stop()

snap = json.loads(snap_path.read_text(encoding="utf-8"))

# ===== 顶部状态 =====
st.title("🦞 拉哪 Lite Dashboard")
col1, col2, col3, col4 = st.columns(4)
col1.metric("本金", "100 U")
col2.metric("今日盈亏", "+0 U", "0%")
col3.metric("当前持仓", "0")
col4.metric("最近扫描", snap["timestamp"][:19].replace("T", " "))

# ===== 热度榜 =====
st.subheader("🔥 Top 20 热度榜")
st.dataframe(snap["top_heat"], use_container_width=True)

# ===== OI 异动 =====
st.subheader("⚡ OI 异动池")
if snap["oi_anomaly"]:
    st.dataframe(snap["oi_anomaly"], use_container_width=True)
else:
    st.info("当前无异动")

# 自动刷新
st.button("🔄 手动刷新")