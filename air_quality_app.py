# -*- coding: utf-8 -*-
"""
台灣空氣品質觀測台 (Streamlit)

資料來源：環境部環境資料開放平臺
    即時            aqx_p_432  空氣品質指標(AQI)與各污染物（每小時）
    空氣品質 期間平均 aqx_p_434  日空氣品質指標(AQI)（以 year_month 取得歷史月份）
    污染物   期間平均 aqx_p_08   空氣品質監測月值（每站每月各污染物平均）

執行方式：
    pip install -r requirements.txt
    streamlit run air_quality_app.py

API 金鑰請擇一設定（不要寫進程式碼，也不要上傳到 GitHub）：
    1. .streamlit/secrets.toml  →  MOENV_API_KEY = "你的金鑰"
    2. 環境變數                  →  MOENV_API_KEY=你的金鑰
    （Streamlit Community Cloud：App settings → Secrets 貼上第 1 種格式）
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import plotly.express as px
import plotly.io as pio
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
LATEST_ID = "aqx_p_432"      # 即時（每小時）
AQI_DAILY_ID = "aqx_p_434"   # 日 AQI（歷史，依 year_month）
MONTHLY_ID = "aqx_p_08"      # 月值（各污染物，每站每月平均）

CACHE_TTL = 600              # 即時資料快取 10 分鐘
TTL_PAST_MONTH = 7 * 86400   # 已結束月份的資料不會再變
TTL_CURRENT_MONTH = 6 * 3600
TTL_MONTHLY_ALL = 24 * 3600
FIG_TTL = 1800               # 已畫好的圖表快取 30 分鐘
LIVE_COL_WIDTH = 64          # 資料表（即時）每欄寬度（像素）；9 欄×64 + 狀態 128 ≈ 704 像素
LIVE_STATUS_WIDTH = 128      # 資料表（即時）「狀態」欄寬度；想要 10 欄完全等寬就設成和上面一樣
PERIOD_COL_WIDTH = 160       # 資料表（期間平均）最後兩欄寬度（像素）
PAGE = 1000
MAX_PAGES = 60               # 單一月份最多讀 6 萬筆
MAX_PAGES_ALL = 300          # 月值全部歷史最多讀 30 萬筆
MAX_WORKERS = 8

# 資料時間選項 → 往回幾天（0 = 即時）；污染物用月值，天數換算成月數
PERIODS = {"即時": 0, "近三個月平均": 90, "近半年平均": 180, "近一年平均": 365, "近三年平均": 1095}
PERIOD_MONTHS = {90: 3, 180: 6, 365: 12, 1095: 36}

# ─────────────────────────────── 莫迪藍配色 ───────────────────────────────
INK = "#2B4257"
BG = "#EDF1F4"
SIDEBAR_BG = "#DDE6EC"
LINE = "#CBD6DD"
ACCENT = "#5B7B94"
FONT = "Noto Sans TC, PingFang TC, Microsoft JhengHei, sans-serif"

# 縣市依區域分組：同一區域用同一色系，區內再用不同線型，折線圖才分得清楚
REGIONS = {
    "北部": ["基隆市", "臺北市", "新北市", "桃園市", "新竹市", "新竹縣", "苗栗縣"],
    "中部": ["臺中市", "彰化縣", "南投縣", "雲林縣"],
    "南部": ["嘉義市", "嘉義縣", "臺南市", "高雄市", "屏東縣"],
    "東部": ["宜蘭縣", "花蓮縣", "臺東縣"],
    "離島": ["澎湖縣", "金門縣", "連江縣"],
}
REGION_COLORS = {
    "北部": ["#1F3A5F", "#5887AE", "#8DB6D6", "#2E6F8E", "#6D8CB0", "#A8C5D8", "#3C5A78"],  # 莫迪藍
    "中部": ["#4E7F6D", "#8DB39F", "#2F5A4C", "#B7CDB8"],                                    # 鼠尾草綠
    "南部": ["#B26B62", "#D9A877", "#C98C98", "#8E534F", "#E2CB93"],                          # 赭紅、沙色
    "東部": ["#7A6591", "#A99BBE", "#54446C"],                                               # 灰紫
    "離島": ["#7F8387", "#BBAA98", "#4E5054"],                                               # 灰、駝
}
DASHES = ["solid", "dash", "dot", "dashdot", "longdash", "longdashdot", "solid"]
COUNTY_ORDER = [c for cs in REGIONS.values() for c in cs]
REGION_OF = {c: r for r, cs in REGIONS.items() for c in cs}
COUNTY_COLOR = {c: REGION_COLORS[r][i] for r, cs in REGIONS.items() for i, c in enumerate(cs)}
COUNTY_DASH = {c: DASHES[i % len(DASHES)] for r, cs in REGIONS.items() for i, c in enumerate(cs)}
STATUS_ORDER = ["良好", "普通", "對敏感族群不健康", "對所有族群不健康", "非常不健康", "危害"]
STATUS_COLORS = {
    "良好": "#8FB3C4", "普通": "#C9D6DD", "對敏感族群不健康": "#D4B996",
    "對所有族群不健康": "#C58F8A", "非常不健康": "#A07C8F", "危害": "#7D6670", "未提供": "#B7BFC5",
}

# 指標類別：空氣品質(AQI 綜合指標) / 污染物(六種各別濃度)
CAT_AQI = "空氣品質（AQI）"
CAT_POLL = "污染物"
POLLUTANTS = {  # 顯示名稱 → (欄位, 單位)
    "PM2.5 細懸浮微粒": ("pm25", "μg/m³"),
    "PM10 懸浮微粒": ("pm10", "μg/m³"),
    "O₃ 臭氧": ("o3", "ppb"),
    "CO 一氧化碳": ("co", "ppm"),
    "SO₂ 二氧化硫": ("so2", "ppb"),
    "NO₂ 二氧化氮": ("no2", "ppb"),
}
POLL_CODES = {"PM2.5": "pm25", "PM10": "pm10", "O3": "o3", "CO": "co", "SO2": "so2", "NO2": "no2"}
POLL_CN = {"細懸浮微粒": "pm25", "懸浮微粒": "pm10", "臭氧": "o3",
           "一氧化碳": "co", "二氧化硫": "so2", "二氧化氮": "no2"}
LIVE_NUM = {"aqi": "aqi", "pm2.5": "pm25", "pm10": "pm10", "o3": "o3", "co": "co", "so2": "so2", "no2": "no2"}


def norm_name(s):
    """統一「台／臺」寫法，避免不同資料集的測站、縣市對不上。"""
    return s.astype(str).str.replace("台", "臺", regex=False).str.strip()


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

# ─────────────────────────────── 金鑰與 API ───────────────────────────────


def get_api_key():
    key = None
    try:
        key = st.secrets.get("MOENV_API_KEY")
    except Exception:  # 沒有 secrets.toml 時
        pass
    key = key or os.environ.get("MOENV_API_KEY")
    return str(key).strip() if key else None


class SchemaError(RuntimeError):
    """資料欄位與預期不符（會直接顯示給使用者，不會被略過）。"""


def _request(dataset_id, key, limit, offset, year_month=None):
    """呼叫一次 API，回傳 records(list)。錯誤訊息不含網址，避免金鑰外洩。"""
    params = {"api_key": key, "limit": limit, "offset": offset, "format": "json"}
    if year_month:
        params["year_month"] = year_month
    last = "無法連線到環境部 API，請稍後再試"
    for attempt in range(3):
        try:
            resp = requests.get(API_BASE + dataset_id, params=params, timeout=20)
        except requests.RequestException:
            time.sleep(0.5 * (attempt + 1))
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            last = f"環境部 API 回應異常（HTTP {resp.status_code}）"
            time.sleep(0.5 * (attempt + 1))
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"環境部 API 回應異常（HTTP {resp.status_code}）")
        if "json" not in resp.headers.get("Content-Type", "").lower():  # 金鑰錯誤時 API 仍回 200，內容是純文字
            raise RuntimeError(f"環境部 API 沒有回傳 JSON：{resp.text[:60]}")
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError("環境部 API 回傳的 JSON 格式無法解析") from None
        records = data.get("records") if isinstance(data, dict) else data
        return records or []
    raise RuntimeError(last)


def _fetch_pages(dataset_id, key, year_month=None):
    rows = []
    for page in range(MAX_PAGES):
        part = _request(dataset_id, key, PAGE, page * PAGE, year_month)
        rows += part
        if len(part) < PAGE:
            break
    return rows


# ─────────────────────────────── 即時資料 ───────────────────────────────


def _clean_realtime(records):
    df = pd.DataFrame(records)
    missing = [c for c in ("publishtime", "county", "sitename", "status") if c not in df.columns]
    if missing:
        raise RuntimeError(f"API 欄位與預期不符，缺少：{missing}")
    for raw, name in LIVE_NUM.items():
        df[name] = pd.to_numeric(df[raw], errors="coerce") if raw in df.columns else float("nan")
    df["publishtime"] = pd.to_datetime(df["publishtime"], errors="coerce")
    df["county"] = norm_name(df["county"])
    df["sitename"] = norm_name(df["sitename"])
    df["status"] = df["status"].fillna("").replace("", "未提供")
    df = df.dropna(subset=["publishtime"])
    return df[["publishtime", "county", "sitename", "status", *LIVE_NUM.values()]]


@st.cache_resource
def _realtime_backup():
    return {"df": None}


@st.cache_data(ttl=CACHE_TTL, show_spinner="工作中：正在向環境部取得最新資料…")
def load_realtime(_key):
    backup = _realtime_backup()
    try:
        records = _request(LATEST_ID, _key, 1000, 0)
        if not records:
            raise RuntimeError("環境部 API 沒有回傳任何資料")
        df = _clean_realtime(records)
        backup["df"] = df
        return df, ""
    except RuntimeError as e:
        if backup["df"] is None:
            raise
        return backup["df"], f"{e}；目前顯示先前取得的資料。"


# ─────────────────────────────── 歷史資料：解析 ───────────────────────────────
EMPTY_DAILY = pd.DataFrame({"monitordate": pd.Series(dtype="datetime64[ns]"),
                            "sitename": pd.Series(dtype="object"), "value": pd.Series(dtype="float64")})
EMPTY_MONTHLY = pd.DataFrame({"month": pd.Series(dtype="datetime64[ns]"), "sitename": pd.Series(dtype="object"),
                              "pollutant": pd.Series(dtype="object"), "value": pd.Series(dtype="float64")})


def _lower_cols(df):
    df.columns = [str(c).lower() for c in df.columns]
    return df


def _parse_daily(rows):
    """日 AQI → monitordate, sitename, value"""
    df = _lower_cols(pd.DataFrame(rows))
    cols = list(df.columns)
    for need in ("monitordate", "sitename"):
        if need not in cols:
            raise SchemaError(f"日 AQI 欄位與預期不符，缺少 {need}；實際欄位：{cols[:12]}")
    value_col = "aqi" if "aqi" in cols else next(
        (c for c in cols if "aqi" in c and "sub" not in c and "副" not in c), None)
    if value_col is None:
        raise SchemaError(f"日 AQI 找不到 AQI 欄位；實際欄位：{cols[:12]}")
    out = pd.DataFrame({
        "monitordate": pd.to_datetime(df["monitordate"], errors="coerce").dt.normalize(),
        "sitename": norm_name(df["sitename"]),
        "value": pd.to_numeric(df[value_col], errors="coerce"),
    })
    return out.dropna(subset=["monitordate", "sitename"])


def _parse_month(series):
    """監測月份可能是 2026-08、2026/08 或 202608，統一成該月 1 號。"""
    s = series.astype(str).str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    for fmt in ("%Y-%m", "%Y/%m", "%Y%m", "%Y-%m-%d", "%Y/%m/%d"):
        todo = out.isna()
        if not todo.any():
            break
        out = out.fillna(pd.to_datetime(s.where(todo), format=fmt, errors="coerce"))
    return out.dt.to_period("M").dt.to_timestamp()


def _poll_key(eng, cn):
    e = str(eng).upper().replace(" ", "")
    return POLL_CODES.get(e) or POLL_CN.get(str(cn).strip())


def _parse_monthly(rows):
    """月值 → month, sitename, pollutant(六種), value；沒有六種污染物則回傳空表。"""
    df = _lower_cols(pd.DataFrame(rows))
    cols = list(df.columns)
    for need in ("sitename", "monitormonth", "concentration"):
        if need not in cols:
            raise SchemaError(f"月值欄位與預期不符，缺少 {need}；實際欄位：{cols[:12]}")
    eng = df["itemengname"] if "itemengname" in cols else [""] * len(df)
    cn = df["itemname"] if "itemname" in cols else [""] * len(df)
    poll = pd.Series([_poll_key(e, c) for e, c in zip(eng, cn)], index=df.index)
    df = df[poll.notna()]
    if df.empty:
        return EMPTY_MONTHLY.copy()
    out = pd.DataFrame({
        "month": _parse_month(df["monitormonth"]),
        "sitename": norm_name(df["sitename"]),
        "pollutant": poll[df.index],
        "value": pd.to_numeric(df["concentration"], errors="coerce"),
    })
    return out.dropna(subset=["month", "sitename"])


# ─────────────────────────────── 歷史資料：共用暫存與「工作中」提示 ───────────────────────────────


@st.cache_resource
def _store():
    """所有使用者共用的歷史資料暫存（已解析成小型 DataFrame）。"""
    return {"lock": threading.Lock(), "items": {}, "last_error": ""}


def _fresh(store, k, now):
    with store["lock"]:
        hit = store["items"].get(k)
    return hit if (hit and now < hit[0]) else None


def _put(store, k, value, ttl):
    with store["lock"]:
        store["items"][k] = (time.time() + ttl, value)


def _get_cached(store, ds, ym, key, current_ym):
    """取得某資料集某月份（ym=None 表示「最新資料」）；成功與失敗都會暫存。"""
    k = (ds, ym)
    hit = _fresh(store, k, time.time())
    if hit:
        return hit[1]
    empty = EMPTY_DAILY if ds == AQI_DAILY_ID else EMPTY_MONTHLY
    try:
        rows = _fetch_pages(ds, key, ym)
        df = (_parse_daily(rows) if ds == AQI_DAILY_ID else _parse_monthly(rows)) if rows else empty
        ttl = TTL_CURRENT_MONTH if (ym is None or ym == current_ym) else TTL_PAST_MONTH
    except SchemaError:
        raise
    except RuntimeError as e:
        store["last_error"] = str(e)
        df, ttl = empty, 1800  # 失敗的月份 30 分鐘後再試，避免每次切換都重試
    _put(store, k, df, ttl)
    return df


def run_tasks(tasks, key, current_ym, label):
    """平行取得多個（資料集, 月份）；有需要實際下載時顯示「工作中」與進度條。"""
    store = _store()
    now = time.time()
    pending = [t for t in tasks if not _fresh(store, t, now)]
    if pending:
        with st.status(f"工作中：{label}", expanded=True) as status:
            bar = st.progress(0.0, text=f"準備中…（共 {len(pending)} 項）")
            done = 0
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futs = [ex.submit(_get_cached, store, ds, ym, key, current_ym) for ds, ym in pending]
                for f in as_completed(futs):
                    f.result()
                    done += 1
                    bar.progress(done / len(pending), text=f"已完成 {done}／{len(pending)} 項")
            status.update(label="載入完成", state="complete", expanded=False)
    return [_get_cached(store, ds, ym, key, current_ym) for ds, ym in tasks]


def _fetch_monthly_all(key, on_progress):
    """不帶 year_month，把月值資料集整份分頁讀完（平行）。"""
    parts, total, samples, page, done = [], 0, set(), 0, False
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        while not done and page < MAX_PAGES_ALL:
            offsets = [(page + i) * PAGE for i in range(MAX_WORKERS)]
            batch = list(ex.map(lambda o: _request(MONTHLY_ID, key, PAGE, o), offsets))
            for rows in batch:
                if rows:
                    parts.append(_parse_monthly(rows))
                    total += len(rows)
                    if len(samples) < 8:
                        samples |= {f"{r.get('itemengname', r.get('ItemEngName', ''))}/"
                                    f"{r.get('itemname', r.get('ItemName', ''))}" for r in rows[:50]}
                if len(rows) < PAGE:
                    done = True
            page += MAX_WORKERS
            on_progress(total)
    parts = [p for p in parts if not p.empty]
    if total and not parts:
        raise SchemaError(f"月值資料找不到六種污染物測項；測項範例：{sorted(samples)[:8]}")
    df = pd.concat(parts, ignore_index=True) if parts else EMPTY_MONTHLY.copy()
    return df, (not done)


def get_monthly_all(key):
    store = _store()
    hit = _fresh(store, (MONTHLY_ID, "ALL"), time.time())
    if hit:
        return hit[1]
    try:
        with st.status("工作中：正在讀取歷史月平均資料（首次約需數十秒）…", expanded=True) as status:
            note = st.empty()
            df, truncated = _fetch_monthly_all(key, lambda n: note.write(f"已讀取 {n:,} 筆…"))
            status.update(label="載入完成", state="complete", expanded=False)
        result, ttl = (df, truncated), TTL_MONTHLY_ALL
    except SchemaError:
        raise
    except RuntimeError as e:
        store["last_error"] = str(e)
        result, ttl = (EMPTY_MONTHLY.copy(), False), 1800
    _put(store, (MONTHLY_ID, "ALL"), result, ttl)
    return result


# ─────────────────────────────── 期間平均：兩種資料 ───────────────────────────────


def load_aqi_period(days, end_date, key, site_county):
    """空氣品質（AQI）：日資料 → (date, county, sitename, value), 預期月份數"""
    end = pd.Timestamp(end_date)
    start = end - pd.Timedelta(days=days)
    months = pd.period_range(start=start.to_period("M"), end=end.to_period("M"), freq="M")
    cur = f"{end.year}_{end.month:02d}"
    tasks = [(AQI_DAILY_ID, f"{p.year}_{p.month:02d}") for p in months] + [(AQI_DAILY_ID, None)]
    results = run_tasks(tasks, key, cur, f"正在載入近 {days} 天的日 AQI（共 {len(months)} 個月）")
    parts = [r for r in results if not r.empty]
    if not parts:
        raise RuntimeError(f"沒有取得任何歷史資料：{_store()['last_error'] or '資料集沒有回傳資料'}")
    ts = pd.concat(parts, ignore_index=True).drop_duplicates(["monitordate", "sitename"])
    ts["county"] = ts["sitename"].map(site_county)
    ts = ts.dropna(subset=["county"])
    ts = ts[(ts["monitordate"] > start) & (ts["monitordate"] <= end)]
    ts = ts.rename(columns={"monitordate": "date"})
    return ts[["date", "county", "sitename", "value"]].reset_index(drop=True), len(months)


def load_pollutant_period(k_months, pollutant, end_date, key, site_county):
    """污染物：月值 → (date, county, sitename, value), 預期月份數"""
    full, truncated = get_monthly_all(key)
    last_pub = pd.Timestamp(end_date).to_period("M") - 1  # 月值每月更新，最新月份通常是上個月
    need = [last_pub - i for i in range(k_months)]
    have = set(full["month"].dt.to_period("M"))
    missing = [p for p in need if p not in have]
    extras = []
    if missing:  # 整份資料沒帶到的月份，改用 year_month 逐月補
        tasks = [(MONTHLY_ID, f"{p.year}_{p.month:02d}") for p in missing]
        extras = run_tasks(tasks, key, None, f"正在補抓 {len(missing)} 個月份的月平均資料")
    parts = [p for p in [full, *extras] if not p.empty]
    if not parts:
        raise RuntimeError(f"沒有取得任何月平均資料：{_store()['last_error'] or '資料集沒有回傳資料'}")
    mdf = pd.concat(parts, ignore_index=True).drop_duplicates(["month", "sitename", "pollutant"])
    mdf = mdf[mdf["pollutant"] == pollutant]
    anchor = mdf["month"].max().to_period("M") if not mdf.empty else last_pub
    first = anchor - (k_months - 1)
    mdf = mdf[(mdf["month"].dt.to_period("M") >= first) & (mdf["month"].dt.to_period("M") <= anchor)]
    mdf["county"] = mdf["sitename"].map(site_county)
    mdf = mdf.dropna(subset=["county"]).rename(columns={"month": "date"})
    return mdf[["date", "county", "sitename", "value"]].reset_index(drop=True), k_months, truncated


# ─────────────────────────────── 圖表樣式與圖表快取 ───────────────────────────────
if "morandi" not in pio.templates:  # 樣式只註冊一次，之後每張圖不必再逐項設定
    _t = go.layout.Template(pio.templates["plotly_white"])
    _t.layout.update(
        font=dict(family=FONT, color=INK, size=14),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(255,255,255,0.65)",
        margin=dict(l=10, r=10, t=30, b=10), legend=dict(bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(bgcolor="white", font=dict(family=FONT, color=INK)),
        xaxis=dict(gridcolor=LINE, linecolor=LINE, zeroline=False),
        yaxis=dict(gridcolor=LINE, linecolor=LINE, zeroline=False),
    )
    pio.templates["morandi"] = _t


def show(fig):
    st.plotly_chart(fig, config={"displaylogo": False})


def fmt(v, digits=1):
    return "—" if pd.isna(v) else f"{v:.{digits}f}"


def aqi_status(aqi):
    s = pd.cut(aqi, [-float("inf"), 50, 100, 150, 200, 300, float("inf")], labels=STATUS_ORDER)
    return s.astype(object).fillna("未提供")


# 下面的圖表函式都有快取：同樣的條件（sig）再選一次，直接取用現成的圖，不重畫。
# 參數名稱以底線開頭的是資料表，不參與快取比對，條件都已包含在 sig 裡。
_fig_cache = st.cache_data(ttl=FIG_TTL, max_entries=64, show_spinner=False)


@_fig_cache
def fig_line_live(sig, _agg, label):
    fig = go.Figure(go.Scatter(
        x=_agg["county"], y=_agg["value"], mode="lines+markers",
        line=dict(color=ACCENT, width=2.4),
        marker=dict(size=13, color=[COUNTY_COLOR.get(c, ACCENT) for c in _agg["county"]],
                    line=dict(color="white", width=2)),
        hovertemplate="%{x}<br>%{y:.1f}<extra></extra>",
    ))
    fig.update_layout(template="morandi", height=520, xaxis_title="縣市", yaxis_title=label)
    return fig


@_fig_cache
def fig_line_period(sig, _ts, label, freq, region):
    ts = _ts if region == "全部" else _ts[_ts["county"].isin(REGIONS[region])]
    trend = ts.groupby(["county", pd.Grouper(key="date", freq=freq)])["value"].mean().reset_index().dropna()
    order = [c for c in COUNTY_ORDER if c in set(trend["county"])]
    n_pts = trend.groupby("county").size().max() if len(trend) else 0
    fig = px.line(
        trend, x="date", y="value", color="county", markers=bool(n_pts <= 40),
        color_discrete_map=COUNTY_COLOR, category_orders={"county": order}, template="morandi",
        labels={"date": "日期", "value": label, "county": "縣市"},
    )
    fig.for_each_trace(lambda t: t.update(
        line=dict(color=COUNTY_COLOR.get(t.name), dash=COUNTY_DASH.get(t.name, "solid"), width=2.6),
        marker=dict(size=6), legendgroup=REGION_OF.get(t.name, "其他"),
        legendgrouptitle_text=REGION_OF.get(t.name, "其他")))
    fig.update_layout(height=540, hovermode="closest", legend=dict(itemwidth=50))
    fig.update_xaxes(tickformat="%Y/%m/%d" if freq == "W" else "%Y/%m")
    return fig


@_fig_cache
def fig_bar(sig, _agg, label):
    fig = px.bar(_agg, x="county", y="value", color="county", color_discrete_map=COUNTY_COLOR,
                 text_auto=".1f", labels={"county": "縣市", "value": label}, template="morandi")
    fig.update_traces(marker_line_width=0, textposition="outside", cliponaxis=False)
    fig.update_layout(height=440, showlegend=False, xaxis_tickangle=-40)
    return fig


def _pie(df, names, values, colors):
    fig = px.pie(df, names=names, values=values, color=names, color_discrete_map=colors,
                 hole=0.4, template="morandi")
    fig.update_traces(textinfo="percent", marker=dict(line=dict(color=BG, width=2)))
    fig.update_layout(height=440)
    return fig


@_fig_cache
def fig_pie_status(sig, _counts):
    fig = _pie(_counts, "status", "測站數", STATUS_COLORS)
    fig.update_traces(hovertemplate="%{label}<br>%{value} 站（%{percent}）<extra></extra>")
    # 只有 6 個等級，圖例放在下方置中，圓餅才會在版面正中央（縣市圖例有 22 項，維持放右側）
    fig.update_layout(legend=dict(orientation="h", xanchor="center", x=0.5, yanchor="top", y=-0.02))
    return fig


@_fig_cache
def fig_pie_share(sig, _agg):
    fig = _pie(_agg, "county", "value", COUNTY_COLOR)
    fig.update_traces(hovertemplate="%{label}<br>平均 %{value:.1f}（%{percent}）<extra></extra>")
    return fig


@_fig_cache
def fig_pie_stations(sig, _cnt):
    fig = _pie(_cnt, "county", "測站數", COUNTY_COLOR)
    fig.update_layout(height=520)
    fig.update_traces(hovertemplate="%{label}<br>%{value} 站（%{percent}）<extra></extra>")
    return fig


# ─────────────────────────────── 主畫面 ───────────────────────────────
st.title("台灣空氣品質觀測台")

api_key = get_api_key()
if not api_key:
    st.error("尚未設定環境部 API 金鑰。請在 .streamlit/secrets.toml 或環境變數設定 MOENV_API_KEY。")
    st.stop()

try:
    live_df, warn = load_realtime(api_key)
except RuntimeError as e:
    st.error(str(e))
    st.stop()
if warn:
    st.warning(warn)

latest_time = live_df["publishtime"].max()
live = live_df[live_df["publishtime"] == latest_time]
site_county = live.drop_duplicates("sitename").set_index("sitename")["county"].to_dict()
all_counties = sorted(live["county"].unique(),
                      key=lambda c: COUNTY_ORDER.index(c) if c in COUNTY_ORDER else 99)

# ── 側邊欄：只有「資料時間」「縣市」「空氣品質／污染物」；沒有清除快取選項 ──
with st.sidebar:
    st.header("篩選條件")
    period = st.selectbox("資料時間", list(PERIODS.keys()), index=0)
    days = PERIODS[period]
    is_live = days == 0
    sel_counties = st.multiselect("縣市", all_counties, default=all_counties)
    category = st.radio("指標", [CAT_AQI, CAT_POLL])
    if category == CAT_POLL:
        pol_label = st.selectbox("污染物", list(POLLUTANTS.keys()))
        metric, unit = POLLUTANTS[pol_label]
        metric_label = f"{pol_label}（{unit}）"
        st.caption("污染物：各污染物的實際濃度。")
    else:
        metric, unit = "aqi", ""
        metric_label = "AQI 空氣品質指標"
        st.caption("空氣品質（AQI）：由 PM2.5、PM10、O₃、CO、SO₂、NO₂ 六種污染物換算出的綜合指標。數值越高，污染越嚴重。")
    if is_live:
        st.caption("即時資料每小時更新；切換條件不會重新呼叫 API。")
    elif category == CAT_POLL:
        st.caption("污染物的期間平均使用月平均資料，最新月份通常是上個月。")
    else:
        st.caption("空氣品質的期間平均使用日 AQI，每個月份只會向環境部抓取一次。")

if not sel_counties:
    st.info("請在左側至少選擇一個縣市。")
    st.stop()

# ── 準備資料：station（每站一個值）、ts（時間序列，即時為 None）──
ts = None
if is_live:
    win = live[live["county"].isin(sel_counties)]
    station = win[["county", "sitename", "status", metric]].rename(columns={metric: "value"})
    station = station.dropna(subset=["value"])
    st.caption(f"資料來源：環境部環境資料開放平臺　即時資料發布時間：{latest_time:%Y/%m/%d %H:%M}")
else:
    try:
        if category == CAT_AQI:
            ts, expected = load_aqi_period(days, f"{latest_time:%Y-%m-%d}", api_key, site_county)
            truncated, unit_txt = False, "日資料"
        else:
            ts, expected, truncated = load_pollutant_period(
                PERIOD_MONTHS[days], metric, f"{latest_time:%Y-%m-%d}", api_key, site_county)
            unit_txt = "月平均資料"
    except SchemaError as e:
        st.error(f"{e}\n\n（歷史資料集欄位與程式預期不同，請把這段訊息回報給開發者。）")
        st.stop()
    except RuntimeError as e:
        st.error(str(e))
        st.stop()
    ts = ts[ts["county"].isin(sel_counties)].dropna(subset=["value"])
    if ts.empty:
        st.info("所選縣市在這段期間沒有資料。")
        st.stop()
    covered = ts["date"].dt.to_period("M").nunique()
    st.caption(
        f"資料來源：環境部環境資料開放平臺　期間：{ts['date'].min():%Y/%m} – {ts['date'].max():%Y/%m}"
        f"（{unit_txt}，涵蓋 {covered}／{expected} 個月）"
    )
    if covered < expected:
        st.warning(f"歷史資料只取得 {covered}／{expected} 個月份，平均值可能不完整。")
    if truncated:
        st.warning("月平均資料量超過讀取上限，較舊的月份可能沒有讀到。")
    station = ts.groupby(["county", "sitename"], as_index=False).agg(value=("value", "mean"), 資料筆數=("value", "size"))

agg = station.groupby("county", as_index=False)["value"].mean().sort_values("value", ascending=False)

# ── 指標卡 ──
k1, k2, k3, k4 = st.columns(4)
k1.metric("測站數", len(station))
k2.metric(f"平均{'AQI' if category == CAT_AQI else pol_label.split(' ')[0]}", fmt(station["value"].mean()))
if len(agg):
    hi, lo = agg.iloc[0], agg.iloc[-1]
    k3.metric("最高縣市", f"{hi['county']} {hi['value']:.1f}")
    k4.metric("最低縣市", f"{lo['county']} {lo['value']:.1f}")

# ── 頁籤：只畫目前開啟的那一頁（其餘頁籤不計算，切換條件才不會卡） ──
TAB_NAMES = ["折線圖", "長條圖與圓餅圖", "測站數", "資料表"]
try:
    tabs = st.tabs(TAB_NAMES, on_change="rerun", key="main_tabs")
    active = next((n for n, t in zip(TAB_NAMES, tabs) if t.open), TAB_NAMES[0])
    lazy = True
except TypeError:  # 舊版 Streamlit 不支援，退回一次畫完全部
    tabs, active, lazy = st.tabs(TAB_NAMES), None, False


def want(name):
    return (not lazy) or active == name


sig = (f"{latest_time:%Y%m%d%H}", days, category, metric, tuple(sel_counties))

if want("折線圖"):
    with tabs[0]:
        if is_live:
            show(fig_line_live(sig, agg, metric_label))
            st.caption("即時資料只有一個時間點，所以比較各縣市；想看時間趨勢請切換為「近三個月平均」等選項。")
        else:
            if category == CAT_AQI:
                freq, note = ("W", "每週平均") if days <= 180 else ("MS", "每月平均")
            else:
                freq, note = "MS", "每月平均"
            region = st.radio("顯示區域", ["全部", *REGIONS.keys()], horizontal=True, key="line_region")
            if region != "全部" and not ts["county"].isin(REGIONS[region]).any():
                st.info(f"目前選取的縣市沒有{region}的縣市。")
            else:
                show(fig_line_period(sig + (region,), ts, metric_label, freq, region))
                st.caption(f"趨勢線為各縣市{note}。縣市依區域分色，同區內以不同線型區分；"
                           "點圖例可隱藏或顯示單一縣市，雙擊圖例可只看該縣市。")

if want("長條圖與圓餅圖"):
    with tabs[1]:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader(f"各縣市平均{'AQI' if category == CAT_AQI else pol_label.split(' ')[0]}")
            show(fig_bar(sig, agg, metric_label))
        with c2:
            if category == CAT_AQI:
                st.subheader("空氣品質狀態分布" + ("" if is_live else "（依期間平均 AQI）"))
                status_series = station["status"] if is_live else aqi_status(station["value"])
                counts = status_series.value_counts().reset_index()
                counts.columns = ["status", "測站數"]
                show(fig_pie_status(sig, counts))
            else:
                st.subheader(f"各縣市 {pol_label.split(' ')[0]} 濃度占比")
                show(fig_pie_share(sig, agg))
                st.caption("以各縣市平均濃度相加為 100%，用來看相對高低。")

if want("測站數"):
    with tabs[2]:
        st.subheader("各縣市測站數占比")
        cnt = station.groupby("county").size().reset_index(name="測站數")
        show(fig_pie_stations(sig, cnt))

if want("資料表"):
    with tabs[3]:
        if is_live:
            cols = {"county": "縣市", "sitename": "測站", "status": "狀態", "aqi": "AQI", "pm25": "PM2.5",
                    "pm10": "PM10", "o3": "O₃", "co": "CO", "so2": "SO₂", "no2": "NO₂"}
            table = win[list(cols)].rename(columns=cols)
            fname = f"air_quality_live_{latest_time:%Y%m%d_%H%M}.csv"
        else:
            table = station.rename(columns={"county": "縣市", "sitename": "測站",
                                            "value": f"{metric_label} 平均"}).round(2)
            fname = f"air_quality_{metric}_{days}d_avg.csv"
        table = table.sort_values(["縣市", "測站"])
        if is_live:   # 即時：10 欄縮窄，筆電不用左右捲動就能一次看完
            widths = {c: LIVE_COL_WIDTH for c in table.columns}
            widths["狀態"] = LIVE_STATUS_WIDTH  # 「對敏感族群不健康」字數多，這一欄稍寬才不會被截掉
        else:         # 期間平均：最後兩欄等寬
            widths = {c: PERIOD_COL_WIDTH for c in table.columns[-2:]}
        try:
            cfg = {c: st.column_config.Column(c, width=w) for c, w in widths.items()}
            st.dataframe(table, hide_index=True, column_config=cfg)
        except Exception:  # 舊版 Streamlit 不支援像素寬度時，改用內建寬度
            cfg = {c: st.column_config.Column(c, width="small" if w <= 100 else "medium") for c, w in widths.items()}
            st.dataframe(table, hide_index=True, column_config=cfg)
        st.download_button("下載目前資料（CSV）", table.to_csv(index=False).encode("utf-8-sig"),
                           file_name=fname, mime="text/csv")
