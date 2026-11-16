# -*- coding: utf-8 -*-
"""
台灣空氣品質觀測台 (Streamlit)

資料來源：環境部環境資料開放平臺 aqx_p_432（空氣品質指標 AQI，每小時更新）

執行方式：
    pip install -r requirements.txt
    streamlit run air_quality_app.py

API 金鑰請擇一設定（不要寫進程式碼，也不要上傳到 GitHub）：
    1. .streamlit/secrets.toml          →  MOENV_API_KEY = "你的金鑰"
    2. 環境變數                          →  MOENV_API_KEY=你的金鑰
    （Streamlit Community Cloud：App settings → Secrets 貼上第 1 種格式）

選用：若要讓「時間選項」一開始就有多個時間點，可再設定環境部「空氣品質指標(AQI)歷史資料」
的資料集代碼（到 https://data.moenv.gov.tw/swagger/ 查詢）：
    MOENV_HISTORY_DATASET = "資料集代碼"
未設定時，程式會在伺服器運作期間自動累積每小時的最新資料，時間選項會隨時間增加。
"""

import os
import threading

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

# ─────────────────────────────── 基本設定 ───────────────────────────────
st.set_page_config(
    page_title="台灣空氣品質觀測台",
    page_icon="🌫️",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={"Get help": None, "Report a bug": None, "About": "資料來源：環境部環境資料開放平臺"},
)

API_BASE = "https://data.moenv.gov.tw/api/v2/"
LATEST_ID = "aqx_p_432"        # 即時 AQI（每小時更新）
CACHE_TTL = 600                # 快取 10 分鐘；資料每小時才更新，不需要更頻繁
MAX_SNAPSHOTS = 72             # 最多保留最近 72 個時間點
HISTORY_LIMIT = 3000

# ─────────────────────────────── 莫迪藍配色 ───────────────────────────────
INK = "#2B4257"       # 文字
BG = "#EDF1F4"        # 頁面底色
SIDEBAR_BG = "#DDE6EC"
LINE = "#CBD6DD"      # 格線 / 邊框
ACCENT = "#5B7B94"
FONT = "Noto Sans TC, PingFang TC, Microsoft JhengHei, sans-serif"

# 22 個縣市各自固定一個顏色（深淺交錯，相鄰切片較容易分辨）
COUNTY_ORDER = [
    "基隆市", "臺北市", "新北市", "桃園市", "新竹市", "新竹縣", "苗栗縣", "臺中市",
    "彰化縣", "南投縣", "雲林縣", "嘉義市", "嘉義縣", "臺南市", "高雄市", "屏東縣",
    "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣", "金門縣", "連江縣",
]
MORANDI_BLUES = [
    "#2F4B5F", "#A9BFCF", "#5B7B94", "#C9D5DC", "#46637A", "#9DB4C0", "#7C9CB4", "#8FA9A9",
    "#3F5A73", "#B5C7D3", "#6C8EA4", "#C9BFB3", "#4E6A82", "#AEC1CC", "#738FA3", "#9A93A8",
    "#5F7E8E", "#92ADB9", "#475F78", "#86A2B3", "#6A8799", "#7E9AAA",
]
STATUS_COLORS = {
    "良好": "#8FB3C4",
    "普通": "#C9D6DD",
    "對敏感族群不健康": "#D4B996",
    "對所有族群不健康": "#C58F8A",
    "非常不健康": "#A07C8F",
    "危害": "#7D6670",
    "未提供": "#B7BFC5",
}

METRICS = {
    "PM2.5 細懸浮微粒 (μg/m³)": "pm25",
    "O₃ 臭氧 (ppb)": "o3",
    "AQI 空氣品質指標": "aqi",
    "PM10 懸浮微粒 (μg/m³)": "pm10",
}
NUM_COLS = ["aqi", "pm2.5", "pm10", "o3"]


def county_colors(counties):
    """縣市 → 顏色（固定對應，切換時間或篩選後顏色不會跑掉）"""
    cmap = dict(zip(COUNTY_ORDER, MORANDI_BLUES))
    extra = [c for c in counties if c not in cmap]
    for i, c in enumerate(extra):
        cmap[c] = MORANDI_BLUES[i % len(MORANDI_BLUES)]
    return cmap


# ─────────────────────────────── 樣式 ───────────────────────────────
st.markdown(
    f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;700&display=swap');
    html, body, .stApp, [class*="css"] {{ font-family: {FONT}; }}
    .stApp {{ background: {BG}; color: {INK}; }}
    h1, h2, h3, p, label, span, div[data-testid="stMarkdownContainer"] {{ color: {INK}; }}
    section[data-testid="stSidebar"] {{ background: {SIDEBAR_BG}; }}
    h1 {{ font-weight: 700; letter-spacing: .02em; }}
    div[data-testid="stMetric"] {{
        background: #F8FAFB; border: 1px solid {LINE}; border-radius: 10px; padding: 14px 18px;
    }}
    div[data-testid="stMetricValue"] {{ color: {ACCENT}; font-weight: 700; }}
    button[data-baseweb="tab"] {{ font-weight: 500; }}
    /* 公開展示：隱藏右上角選單（含「Clear cache」）、頁尾與部署按鈕 */
    #MainMenu, footer, [data-testid="stToolbar"], [data-testid="stAppDeployButton"],
    .stDeployButton {{ visibility: hidden; display: none; }}
    </style>
    """,
    unsafe_allow_html=True,
)

# ─────────────────────────────── 資料取得 ───────────────────────────────


def get_api_key():
    key = None
    try:
        key = st.secrets.get("MOENV_API_KEY")
    except Exception:  # 沒有 secrets.toml 時
        pass
    key = key or os.environ.get("MOENV_API_KEY")
    return str(key).strip() if key else None


def get_history_id():
    hid = None
    try:
        hid = st.secrets.get("MOENV_HISTORY_DATASET")
    except Exception:
        pass
    hid = hid or os.environ.get("MOENV_HISTORY_DATASET")
    return str(hid).strip() if hid else ""


def _fetch(dataset_id, key, limit=1000):
    """呼叫環境部 API。錯誤訊息不含網址，避免金鑰外洩到公開頁面。"""
    params = {"api_key": key, "limit": limit, "offset": 0, "format": "json"}
    try:
        resp = requests.get(API_BASE + dataset_id, params=params, timeout=20)
    except requests.RequestException:
        raise RuntimeError("無法連線到環境部 API，請稍後再試") from None
    if resp.status_code != 200:
        raise RuntimeError(f"環境部 API 回應異常（HTTP {resp.status_code}）")
    # 金鑰錯誤時 API 仍回 200，但內容是純文字，必須先檢查
    if "json" not in resp.headers.get("Content-Type", "").lower():
        raise RuntimeError(f"環境部 API 沒有回傳 JSON：{resp.text[:60]}")
    data = resp.json()
    records = data.get("records") if isinstance(data, dict) else data
    if not records:
        raise RuntimeError("環境部 API 沒有回傳任何資料")
    return records


def _clean(records):
    df = pd.DataFrame(records).rename(columns={"datacreationdate": "publishtime"})
    missing = [c for c in ("publishtime", "county", "sitename", "status") if c not in df.columns]
    if missing:
        raise RuntimeError(f"API 欄位與預期不符，缺少：{missing}")
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else float("nan")
    df["publishtime"] = pd.to_datetime(df["publishtime"], errors="coerce")
    df["county"] = df["county"].astype(str).str.replace("台", "臺", regex=False)
    df["status"] = df["status"].fillna("").replace("", "未提供")
    df = df.dropna(subset=["publishtime"]).rename(columns={"pm2.5": "pm25"})
    return df[["publishtime", "county", "sitename", "status", "aqi", "pm25", "pm10", "o3"]]


@st.cache_resource
def _snapshot_store():
    """所有使用者共用的歷史快照（時間 → DataFrame）。"""
    return {"lock": threading.Lock(), "frames": {}}


@st.cache_data(ttl=CACHE_TTL, show_spinner="正在向環境部取得最新資料…")
def load_data(_key, _history_id):
    """
    只有在快取過期（每 10 分鐘）時才會真的呼叫 API。
    切換時間、縣市、指標都只是在記憶體內篩選，不會重新抓資料。
    """
    store = _snapshot_store()
    warn = ""
    try:
        latest = _clean(_fetch(LATEST_ID, _key))
        history = None
        if _history_id:
            try:
                history = _clean(_fetch(_history_id, _key, HISTORY_LIMIT))
            except RuntimeError as e:
                warn = f"歷史資料集讀取失敗（{e}），改用累積的最新資料。"
        with store["lock"]:
            if history is not None:
                for t, g in history.groupby("publishtime"):
                    store["frames"].setdefault(t, g)
            for t, g in latest.groupby("publishtime"):
                store["frames"][t] = g
            for t in sorted(store["frames"])[:-MAX_SNAPSHOTS]:
                del store["frames"][t]
    except RuntimeError as e:
        if not store["frames"]:
            raise
        warn = f"{e}；目前顯示先前取得的資料。"
    with store["lock"]:
        frames = [store["frames"][t] for t in sorted(store["frames"])]
    return pd.concat(frames, ignore_index=True), warn


# ─────────────────────────────── 圖表樣式 ───────────────────────────────


def style(fig, height=440):
    fig.update_layout(
        height=height,
        template="plotly_white",
        font=dict(family=FONT, color=INK, size=14),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(255,255,255,0.65)",
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(bgcolor="white", font=dict(family=FONT, color=INK)),
    )
    fig.update_xaxes(gridcolor=LINE, linecolor=LINE, zeroline=False)
    fig.update_yaxes(gridcolor=LINE, linecolor=LINE, zeroline=False)
    return fig


def show(fig):
    st.plotly_chart(fig, config={"displaylogo": False})


def fmt(v, digits=1):
    return "—" if pd.isna(v) else f"{v:.{digits}f}"


# ─────────────────────────────── 主畫面 ───────────────────────────────
st.title("台灣空氣品質觀測台")

api_key = get_api_key()
if not api_key:
    st.error("尚未設定環境部 API 金鑰。請在 .streamlit/secrets.toml 或環境變數設定 MOENV_API_KEY。")
    st.stop()

try:
    df, warn = load_data(api_key, get_history_id())
except RuntimeError as e:
    st.error(str(e))
    st.stop()

if warn:
    st.warning(warn)

all_times = pd.Series(df["publishtime"].unique()).sort_values(ascending=False).tolist()
all_counties = sorted(df["county"].unique(), key=lambda c: COUNTY_ORDER.index(c) if c in COUNTY_ORDER else 99)
cmap = county_colors(all_counties)

# ── 側邊欄（沒有「清除快取」選項）──
with st.sidebar:
    st.header("篩選條件")
    sel_time = st.selectbox(
        "資料時間",
        all_times,
        index=0,
        format_func=lambda t: t.strftime("%Y/%m/%d %H:%M"),
    )
    sel_counties = st.multiselect("縣市", all_counties, default=all_counties)
    metric_label = st.radio("圖表指標", list(METRICS.keys()))
    st.caption("資料每小時更新；切換條件不會重新呼叫 API，所以不會變慢。")

metric = METRICS[metric_label]
if not sel_counties:
    st.info("請在左側至少選擇一個縣市。")
    st.stop()

snap = df[(df["publishtime"] == sel_time) & (df["county"].isin(sel_counties))]
hist = df[df["county"].isin(sel_counties)]
st.caption(f"資料來源：環境部環境資料開放平臺　發布時間：{sel_time:%Y/%m/%d %H:%M}")

k1, k2, k3, k4 = st.columns(4)
k1.metric("測站數", len(snap))
k2.metric("平均 AQI", fmt(snap["aqi"].mean()))
k3.metric("平均 PM2.5", fmt(snap["pm25"].mean()))
k4.metric("平均 O₃", fmt(snap["o3"].mean()))

tab_line, tab_pie, tab_bar, tab_table = st.tabs(["折線圖", "圓餅圖", "長條圖", "資料表"])

# 各縣市平均（目前選取的時間）
agg = (
    snap.groupby("county", as_index=False)[metric].mean().dropna()
    .sort_values(metric, ascending=False)
)

with tab_line:
    trend = hist.groupby(["publishtime", "county"], as_index=False)[metric].mean()
    if trend["publishtime"].nunique() >= 2:
        fig = px.line(
            trend, x="publishtime", y=metric, color="county", markers=True,
            color_discrete_map=cmap, category_orders={"county": all_counties},
            labels={"publishtime": "時間", metric: metric_label, "county": "縣市"},
        )
        fig.update_traces(line_width=2.4, marker_size=7)
        fig.add_shape(
            type="line", x0=sel_time, x1=sel_time, y0=0, y1=1, yref="paper",
            line=dict(color=INK, width=1, dash="dot"),
        )
        st.caption("虛線為目前選取的資料時間。")
    else:
        # 只有一個時間點時，改畫各縣市比較折線，每個點用該縣市的顏色
        fig = go.Figure(
            go.Scatter(
                x=agg["county"], y=agg[metric], mode="lines+markers",
                line=dict(color=ACCENT, width=2.4),
                marker=dict(size=13, color=[cmap[c] for c in agg["county"]], line=dict(color="white", width=2)),
                hovertemplate="%{x}<br>%{y:.1f}<extra></extra>",
            )
        )
        fig.update_layout(xaxis_title="縣市", yaxis_title=metric_label)
        st.info("目前只有 1 個時間點，先顯示各縣市比較；之後累積多個時間點就會變成時間趨勢圖。")
    show(style(fig))

with tab_pie:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("各縣市測站數占比")
        cnt = snap.groupby("county").size().reset_index(name="測站數")
        fig = px.pie(cnt, names="county", values="測站數", color="county",
                     color_discrete_map=cmap, hole=0.4)
        fig.update_traces(textinfo="percent", marker=dict(line=dict(color=BG, width=2)),
                          hovertemplate="%{label}<br>%{value} 站（%{percent}）<extra></extra>")
        show(style(fig, 420))
    with c2:
        st.subheader("空氣品質狀態分布")
        stt = snap["status"].value_counts().reset_index()
        stt.columns = ["status", "測站數"]
        fig = px.pie(stt, names="status", values="測站數", color="status",
                     color_discrete_map=STATUS_COLORS, hole=0.4)
        fig.update_traces(textinfo="percent", marker=dict(line=dict(color=BG, width=2)),
                          hovertemplate="%{label}<br>%{value} 站（%{percent}）<extra></extra>")
        show(style(fig, 420))

with tab_bar:
    fig = px.bar(
        agg, x="county", y=metric, color="county", color_discrete_map=cmap,
        text_auto=".1f", labels={"county": "縣市", metric: metric_label},
    )
    fig.update_traces(marker_line_width=0, textposition="outside", cliponaxis=False)
    fig.update_layout(showlegend=False, xaxis_tickangle=-35)
    show(style(fig))

with tab_table:
    table = snap.rename(columns={
        "publishtime": "發布時間", "county": "縣市", "sitename": "測站", "status": "狀態",
        "aqi": "AQI", "pm25": "PM2.5", "pm10": "PM10", "o3": "O₃",
    }).sort_values(["縣市", "測站"])
    st.dataframe(table, hide_index=True)
    st.download_button(
        "下載目前資料（CSV）",
        table.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"air_quality_{sel_time:%Y%m%d_%H%M}.csv",
        mime="text/csv",
    )
