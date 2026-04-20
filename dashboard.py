import streamlit as st
import json
import pandas as pd
from datetime import datetime

cat > dashboard.py << 'EOF'
import streamlit as st
import json
import pandas as pd
from datetime import datetime

st.set_page_config(page_title="拉哪 Lite Dashboard", page_icon="🦞", layout="wide")
st.title("🦞 拉哪 Lite 监控面板")

try:
    with open("latest_snapshot.json", "r", encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    st.error("还没有 snapshot 数据")
    st.stop()

ts = data.get("timestamp", "")
ver = data.get("version", "")
st.caption(f"版本 {ver} | 更新时间 {ts}")

col1, col2 = st.columns(2)

with col1:
    st.subheader("🔥 热度榜 Top 20")
    heat = data.get("top_heat", [])
    if heat:
        st.dataframe(pd.DataFrame(heat), use_container_width=True, height=600)
    else:
        st.info("暂无数据")

with col2:
    st.subheader("⚡ OI 异动")
    anom = data.get("oi_anomaly", [])
    if anom:
        for a in anom:
            with st.expander(f"{a['symbol']} - {a.get('aggregate','')}"):
                st.json(a)
    else:
        st.info("本轮无异动")
