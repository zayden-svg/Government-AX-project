import re
from datetime import datetime, timedelta
from html import escape, unescape

import pandas as pd
import streamlit as st

from ai_utils import (
    is_gemini_ready, generate_summary, recommend_keywords,
    generate_news_digest, score_news_relevance,
)
from db2 import get_engine
from news_utils import fetch_naver_news, fetch_google_news_rss, fetch_boannews, is_naver_ready

TABLE_NAME = "postings"

st.set_page_config(page_title="정부 IT 사업 AI 분석 대시보드", page_icon="📋", layout="wide")

COL_KEY = "uniq_key"
COL_SOURCE = "source"
COL_AGENCY = "agency"
COL_GUBUN = "gubun"
COL_POST_TYPE = "post_type"
COL_TITLE = "title"
COL_DEPT = "dept"
COL_MANAGER = "manager"
COL_REG_DATE = "reg_date"
COL_DUE_DATE = "due_date"
COL_BUDGET = "budget"
COL_ATTACH = "attach"
COL_VIEWS = "views"
COL_URL = "url"
COL_GRADE = "grade"
COL_CATEGORY = "category"
COL_KEYWORDS = "matched_keywords"
COL_SOLUTION = "recommended_solution"
COL_STATUS = "status"
COL_CREATED_AT = "created_at"
COL_UPDATED_AT = "updated_at"
COL_CONTENT = "content"
COL_TRACK = "track"
COL_TRACK_REASON = "track_reason"
COL_AI_SCORE = "ai_priority_score"
COL_AI_REASON = "ai_priority_reason"

TRACK_RND = "🔬 R&D 과제"
TRACK_BIZ = "💼 사업부 과제"
TRACK_MAP = {"RND": TRACK_RND, "BIZ": TRACK_BIZ}


def get_track_label(raw):
    return TRACK_MAP.get(str(raw).strip().upper(), "미분류")


def get_score_band(score):
    if score is None or score < 0:
        return {"label": "분석대기", "emoji": "⚪", "bg": "#f5f5f5", "text": "#757575", "border": "#bdbdbd"}
    if score >= 80:
        return {"label": "매우높음", "emoji": "🔥", "bg": "#ffebee", "text": "#b71c1c", "border": "#e53935"}
    if score >= 60:
        return {"label": "높음", "emoji": "🟠", "bg": "#fff3e0", "text": "#e65100", "border": "#fb8c00"}
    if score >= 40:
        return {"label": "보통", "emoji": "🟡", "bg": "#fffde7", "text": "#f9a825", "border": "#fdd835"}
    return {"label": "낮음", "emoji": "⚪", "bg": "#eceff1", "text": "#546e7a", "border": "#90a4ae"}


def score_badge_html(score):
    band = get_score_band(score)
    score_text = "분석대기" if (score is None or score < 0) else f"{int(score)}점"
    return (
        f'<span style="background:{band["bg"]};color:{band["text"]};'
        f'border:1px solid {band["border"]};border-radius:8px;padding:2px 10px;'
        f'font-weight:700;font-size:13px;white-space:nowrap;">'
        f'{band["emoji"]} {band["label"]} · {score_text}</span>'
    )


def render_meta_line(agency, due, status, score):
    badge = score_badge_html(score)
    return (
        f'<span style="color:#6b6b6b;font-size:0.85em;">{agency} · 마감 {due} · {status}</span>'
        f'&nbsp;&nbsp;{badge}'
    )


@st.cache_data(ttl=300)
def load_data():
    engine = get_engine()
    df = pd.read_sql_query(f"SELECT * FROM {TABLE_NAME}", engine)
    return df


df = load_data()
if df.empty:
    st.warning("데이터가 없습니다. python main.py를 먼저 실행해주세요.")
    st.stop()

for c in [COL_GRADE, COL_CATEGORY, COL_STATUS, COL_AGENCY]:
    if c in df.columns:
        df[c] = df[c].fillna("미분류").replace("", "미분류")

for c in [COL_TRACK, COL_TRACK_REASON, COL_AI_REASON]:
    if c not in df.columns:
        df[c] = ""
    df[c] = df[c].fillna("")

df["_reg_date_parsed"] = pd.to_datetime(df[COL_REG_DATE], errors="coerce")
df["_due_date_parsed"] = pd.to_datetime(df[COL_DUE_DATE], errors="coerce")
df["_track"] = df[COL_TRACK].map(get_track_label)

if COL_AI_SCORE not in df.columns:
    df[COL_AI_SCORE] = None
df[COL_AI_SCORE] = pd.to_numeric(df[COL_AI_SCORE], errors="coerce").fillna(-1).astype(int)

today = pd.Timestamp(datetime.now().date())
last_updated = None
if COL_UPDATED_AT in df.columns:
    parsed = pd.to_datetime(df[COL_UPDATED_AT], errors="coerce")
    if parsed.notna().any():
        last_updated = parsed.max()

if "ai_summary_cache" not in st.session_state:
    st.session_state.ai_summary_cache = {}
if "track_filter" not in st.session_state:
    st.session_state.track_filter = None
if "quick_filter" not in st.session_state:
    st.session_state.quick_filter = None
if "news_selected_keywords" not in st.session_state:
    st.session_state.news_selected_keywords = ["AI", "사이버보안"]
if "recommended_keywords" not in st.session_state:
    st.session_state.recommended_keywords = None

# [] 고정 모니터링 키워드 - 경쟁사 제품명은 여기서 제외 (별도 관리)
DEFAULT_FIXED_KEYWORDS = [
    "AI", "예약시스템", "먹통", "접속량", "폭주", "서버다운", "API",
    "트래픽", "매크로", "암표", "서버장애", "비대면", "에스티씨랩", "넷퍼넬", "NetFUNNEL",
]
if "fixed_keywords" not in st.session_state:
    st.session_state.fixed_keywords = list(DEFAULT_FIXED_KEYWORDS)

# [경쟁사] 동향 모니터링 키워드 -  연관도 스코어링 풀에는 절대 포함하지 않음
DEFAULT_COMPETITOR_KEYWORDS = ["DynaPath", "EverSafe"]
if "competitor_keywords" not in st.session_state:
    st.session_state.competitor_keywords = list(DEFAULT_COMPETITOR_KEYWORDS)


def build_info_block(row):
    lines = []

    def add(label, value):
        if value is not None and str(value).strip() not in ("", "nan", "None", "미분류"):
            lines.append(f"- {label}: {value}")

    add("공고 제목", row.get(COL_TITLE))
    add("주관부처 / 수행기관", f"{row.get(COL_DEPT, '')} / {row.get(COL_AGENCY, '')}")
    add("공고 유형", row.get(COL_GUBUN))
    add("사업 구분(AI 판단)", row.get("_track"))
    add("등급", row.get(COL_GRADE))
    add("매칭 키워드", row.get(COL_KEYWORDS))
    add("추천 솔루션", row.get(COL_SOLUTION))
    add("접수 시작일", row.get(COL_REG_DATE))
    add("접수 마감일", row.get(COL_DUE_DATE))
    add("예산", row.get(COL_BUDGET))
    if COL_CONTENT in row and str(row.get(COL_CONTENT, "")).strip():
        add("공고 본문 일부", str(row.get(COL_CONTENT))[:1500])

    return "\n".join(lines) if lines else "(제공된 정보가 거의 없습니다)"


def generate_ai_summary(row):
    key = row.get(COL_KEY) or row.get(COL_TITLE)
    if key in st.session_state.ai_summary_cache:
        return st.session_state.ai_summary_cache[key]
    if not is_gemini_ready():
        return None
    text_val, error = generate_summary(build_info_block(row))
    if error:
        return f"__ERROR__:{error}"
    st.session_state.ai_summary_cache[key] = text_val
    return text_val


def render_toggle_card(label, count, key, active, colors, on_click=None, args=None, height=100, font_size=24):
    use_key = True
    try:
        box = st.container(key=key)
    except TypeError:
        box = st.container()
        use_key = False

    if use_key:
        st.markdown(
            f"""
            <style>
            .st-key-{key} button {{
                height: {height}px;
                width: 100%;
                border-radius: 14px;
                border: none;
                white-space: pre-line;
                text-align: left;
                padding: 14px 18px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.08);
                transition: transform 0.15s ease, box-shadow 0.15s ease;
                line-height: 1.3;
            }}
            .st-key-{key} button:hover {{
                transform: translateY(-3px);
                box-shadow: 0 8px 18px rgba(0,0,0,0.16);
            }}
            .st-key-{key} button::first-line {{
                font-size: {font_size}px;
                font-weight: 800;
            }}
            .st-key-{key} button[kind="secondary"] {{
                background: linear-gradient(135deg, {colors['light_bg']} 0%, #ffffff 100%) !important;
                color: {colors['light_text']} !important;
                border-left: 6px solid {colors['border']} !important;
            }}
            .st-key-{key} button[kind="primary"] {{
                background: {colors['active_bg']} !important;
                color: {colors['active_text']} !important;
                border-left: 6px solid {colors['active_bg']} !important;
            }}
            </style>
            """,
            unsafe_allow_html=True,
        )

    with box:
        st.button(
            f"{count}건\n{label}",
            key=f"{key}_btn",
            use_container_width=True,
            type="primary" if active else "secondary",
            on_click=on_click,
            args=args or (),
        )


PALETTE_TOTAL = dict(light_bg="#eceff1", light_text="#37474f", border="#607d8b", active_bg="#37474f", active_text="#ffffff")
PALETTE_RND = dict(light_bg="#e3f2fd", light_text="#0d47a1", border="#2196f3", active_bg="#0d47a1", active_text="#ffffff")
PALETTE_BIZ = dict(light_bg="#e8f5e9", light_text="#1b5e20", border="#43a047", active_bg="#1b5e20", active_text="#ffffff")
PALETTE_PROGRESS = dict(light_bg="#e3f2fd", light_text="#0d47a1", border="#2196f3", active_bg="#0d47a1", active_text="#ffffff")
PALETTE_HIGH_GRADE = dict(light_bg="#ffebee", light_text="#b71c1c", border="#ef5350", active_bg="#b71c1c", active_text="#ffffff")
PALETTE_DUE_SOON = dict(light_bg="#fff8e1", light_text="#e65100", border="#ffb300", active_bg="#e65100", active_text="#ffffff")
PALETTE_FILTER_TOTAL = dict(light_bg="#ede7f6", light_text="#4527a0", border="#7e57c2", active_bg="#4527a0", active_text="#ffffff")


def get_unique_keywords(frame, col):
    kw_set = set()
    for val in frame[col].dropna():
        for kw in str(val).split(","):
            kw = kw.strip()
            if kw:
                kw_set.add(kw)
    return sorted(kw_set)


def popover_multiselect(label, options, state_key):
    if not options:
        st.sidebar.caption(f"{label}: 데이터 없음")
        return []

    for opt in options:
        ck = f"{state_key}__{opt}"
        if ck not in st.session_state:
            st.session_state[ck] = True

    selected = [opt for opt in options if st.session_state.get(f"{state_key}__{opt}", True)]

    if len(selected) == len(options):
        button_label = f"{label} ▾  전체"
    elif len(selected) == 0:
        button_label = f"{label} ▾  선택 없음"
    else:
        preview = ", ".join(selected[:2])
        more = f" 외 {len(selected) - 2}개" if len(selected) > 2 else ""
        button_label = f"{label} ▾  {preview}{more}"

    container_fn = st.sidebar.popover if hasattr(st.sidebar, "popover") else st.sidebar.expander

    with container_fn(button_label):
        st.caption(f"{label} 선택")
        bc1, bc2 = st.columns(2)
        if bc1.button("전체 선택", key=f"{state_key}_all_btn", use_container_width=True):
            for opt in options:
                st.session_state[f"{state_key}__{opt}"] = True
            st.rerun()
        if bc2.button("전체 해제", key=f"{state_key}_none_btn", use_container_width=True):
            for opt in options:
                st.session_state[f"{state_key}__{opt}"] = False
            st.rerun()
        st.markdown("---")
        for opt in options:
            st.checkbox(str(opt), key=f"{state_key}__{opt}")

    return [opt for opt in options if st.session_state.get(f"{state_key}__{opt}", True)]


st.sidebar.title("🔎 필터 / 검색")
search_keyword = st.sidebar.text_input("🔍 키워드 검색 (제목 / 매칭키워드)", "")

agency_options = sorted(df[COL_AGENCY].unique().tolist())
selected_agencies = popover_multiselect("🏢 기관", agency_options, "sel_agency")

grade_order = ["상", "중", "하", "미분류"]
grade_options = [g for g in grade_order if g in df[COL_GRADE].unique()]
selected_grades = popover_multiselect("⭐ 등급", grade_options, "sel_grade")

keyword_options = get_unique_keywords(df, COL_KEYWORDS)
selected_keywords = popover_multiselect("🏷️ 키워드", keyword_options, "sel_keyword")

status_options = sorted(df[COL_STATUS].unique().tolist())
selected_status = popover_multiselect("📌 상태", status_options, "sel_status")

st.sidebar.markdown("---")
st.sidebar.caption("🎯 AI 연관도 점수 필터")
score_range = st.sidebar.slider("점수 범위", 0, 100, (0, 100))
only_pending = st.sidebar.checkbox("⚪ AI 분석 대기중인 공고만 보기")

st.sidebar.markdown("---")
if last_updated:
    st.sidebar.caption(f"🕒 마지막 데이터 갱신: {last_updated.strftime('%Y-%m-%d %H:%M')}")
st.sidebar.caption("⏰ 매일 아침 8시 자동 수집")
if not is_gemini_ready():
    st.sidebar.warning("⚠️ Gemini API 키가 설정되지 않았습니다. .env 파일을 확인해 주세요.")

filtered = df[
    df[COL_AGENCY].isin(selected_agencies)
    & df[COL_GRADE].isin(selected_grades)
    & df[COL_STATUS].isin(selected_status)
].copy()

if keyword_options and len(selected_keywords) < len(keyword_options):
    if selected_keywords:
        pattern = "|".join(re.escape(k) for k in selected_keywords)
        filtered = filtered[filtered[COL_KEYWORDS].astype(str).str.contains(pattern, case=False, na=False, regex=True)]
    else:
        filtered = filtered.iloc[0:0]

if search_keyword.strip():
    kw = search_keyword.strip()
    mask = filtered[COL_TITLE].astype(str).str.contains(kw, case=False, na=False) | filtered[COL_KEYWORDS].astype(str).str.contains(kw, case=False, na=False)
    filtered = filtered[mask]

if only_pending:
    filtered = filtered[filtered[COL_AI_SCORE] < 0]
elif score_range != (0, 100):
    filtered = filtered[
        (filtered[COL_AI_SCORE] >= score_range[0]) & (filtered[COL_AI_SCORE] <= score_range[1])
    ]

FIELD_LABELS = {
    "_track": "AI 구분", COL_AGENCY: "기관", COL_SOURCE: "수집소스", COL_GUBUN: "공고유형",
    COL_POST_TYPE: "게시유형", COL_TITLE: "제목", COL_DEPT: "담당부서", COL_MANAGER: "담당자",
    COL_REG_DATE: "등록일", COL_DUE_DATE: "마감일", COL_BUDGET: "예산", COL_ATTACH: "첨부",
    COL_VIEWS: "조회수", COL_GRADE: "등급", COL_CATEGORY: "카테고리", COL_KEYWORDS: "매칭키워드",
    COL_SOLUTION: "추천솔루션", COL_STATUS: "상태", COL_CREATED_AT: "등록시각",
    COL_UPDATED_AT: "갱신시각", COL_CONTENT: "본문",
}


def render_ai_summary_block(row):
    cache_key = row.get(COL_KEY) or row.get(COL_TITLE)
    cached = st.session_state.ai_summary_cache.get(cache_key)

    if not is_gemini_ready():
        st.warning("Gemini API 키가 설정되지 않았습니다. .env 파일에 GEMINI_API_KEY를 추가한 뒤 앱을 다시 실행해 주세요.")
        return

    if cached and not str(cached).startswith("__ERROR__"):
        st.success(cached)
        if st.button("🔄 다시 요약하기", key=f"resum_{cache_key}"):
            st.session_state.ai_summary_cache.pop(cache_key, None)
            st.rerun()
    else:
        if st.button("🤖 AI 요약 생성하기", key=f"gen_{cache_key}"):
            with st.spinner("AI가 공고를 분석하고 있습니다..."):
                result = generate_ai_summary(row)
            if result is None:
                st.warning("Gemini API가 설정되지 않아 요약을 생성할 수 없습니다.")
            elif str(result).startswith("__ERROR__"):
                st.error(f"요약 생성 중 오류가 발생했습니다: {result.replace('__ERROR__:', '')}")
            else:
                st.success(result)


def render_detail_body(row):
    score = row.get(COL_AI_SCORE, -1)
    band = get_score_band(score)
    score_display = "분석대기" if score is None or score < 0 else f"{int(score)}점"

    st.markdown(f"**{grade_badge} {row[COL_TITLE]}**")
    st.caption("👆 제목을 누르면 원문 공고 페이지로 이동합니다.")

    st.markdown(
        f"""
        <div style="background:{band['bg']};border-left:6px solid {band['border']};
                    border-radius:10px;padding:14px 18px;margin-bottom:14px;">
            <div style="font-size:15px;font-weight:800;color:{band['text']};margin-bottom:8px;">
                {row.get('_track', '')} &nbsp;|&nbsp; {band['emoji']}  연관도 {band['label']} ({score_display})
            </div>
            <div style="font-size:13.5px;color:#333;margin-bottom:4px;">
                🧭 <b>AI 구분 판단근거</b>: {row.get(COL_TRACK_REASON) or '근거 없음'}
            </div>
            <div style="font-size:13.5px;color:#333;">
                🎯 <b>AI 연관도 판단근거</b>: {row.get(COL_AI_REASON) or '근거 없음'}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("#### 🤖 AI 핵심 요약")
    render_ai_summary_block(row)

    st.markdown("---")
    st.markdown("#### 📄 전체 공고 내용")

    ordered_fields = [
        "_track", COL_AGENCY, COL_DEPT, COL_MANAGER, COL_GUBUN, COL_POST_TYPE,
        COL_GRADE, COL_CATEGORY, COL_STATUS, COL_REG_DATE, COL_DUE_DATE,
        COL_BUDGET, COL_VIEWS, COL_ATTACH, COL_KEYWORDS, COL_SOLUTION,
        COL_CONTENT, COL_CREATED_AT, COL_UPDATED_AT,
    ]
    for field in ordered_fields:
        if field not in row.index:
            continue
        value = row.get(field)
        if value is None or str(value).strip() in ("", "nan", "None"):
            continue
        label = FIELD_LABELS.get(field, field)
        st.write(f"**{label}:** {value}")


if hasattr(st, "dialog"):
    @st.dialog("공고 상세 보기", width="large")
    def show_detail_dialog(row):
        render_detail_body(row)
else:
    def show_detail_dialog(row):
        with st.expander("📄 공고 상세 보기", expanded=True):
            render_detail_body(row)


# ------------------------------------------------------------
# 대탭 - 화면 최상단 (헤더보다 위)
# ------------------------------------------------------------
main_tab_dash, main_tab_news, main_tab_trend = st.tabs(
    ["📋 사업공고 분석", "📰 IT 뉴스", "📈 트렌드 분석"]
)

with main_tab_dash:
    st.title("📋 정부 IT 사업 AI 분석")

    st.markdown("### 🧭 사업 구분")
    rnd_in_filtered = (filtered["_track"] == TRACK_RND).sum()
    biz_in_filtered = (filtered["_track"] == TRACK_BIZ).sum()
    total_in_filtered = len(filtered)

    def _reset_track():
        st.session_state.track_filter = None

    def _toggle_track(value):
        st.session_state.track_filter = None if st.session_state.track_filter == value else value

    t1, t2, t3 = st.columns(3)
    with t1:
        render_toggle_card("전체 보기", total_in_filtered, "trk_all",
                            st.session_state.track_filter is None, PALETTE_TOTAL,
                            on_click=_reset_track, height=90, font_size=22)
    with t2:
        render_toggle_card(TRACK_RND, rnd_in_filtered, "trk_rnd",
                            st.session_state.track_filter == TRACK_RND, PALETTE_RND,
                            on_click=_toggle_track, args=(TRACK_RND,), height=90, font_size=22)
    with t3:
        render_toggle_card(TRACK_BIZ, biz_in_filtered, "trk_biz",
                            st.session_state.track_filter == TRACK_BIZ, PALETTE_BIZ,
                            on_click=_toggle_track, args=(TRACK_BIZ,), height=90, font_size=22)

    st.caption("💡 각 공고를 클릭하면 AI가 왜 R&D/사업부로 구분했는지 판단 근거를 함께 확인할 수 있습니다.")

    tab_filtered = filtered
    if st.session_state.track_filter:
        tab_filtered = tab_filtered[tab_filtered["_track"] == st.session_state.track_filter]

    tab_filtered = tab_filtered.sort_values(["_track", "_reg_date_parsed"], ascending=[True, False]) if st.session_state.track_filter is None else tab_filtered.sort_values("_reg_date_parsed", ascending=False)

    soon_mask = (
        tab_filtered["_due_date_parsed"].notna()
        & (tab_filtered["_due_date_parsed"] >= today)
        & (tab_filtered["_due_date_parsed"] <= today + timedelta(days=3))
    )
    in_progress_count = (tab_filtered[COL_STATUS] == "진행중").sum()
    high_grade_count = (tab_filtered[COL_GRADE] == "상").sum()
    soon_count = soon_mask.sum()
    total_count = len(tab_filtered)

    st.markdown("---")

    def _reset_quick():
        st.session_state.quick_filter = None

    def _toggle_quick(value):
        st.session_state.quick_filter = None if st.session_state.quick_filter == value else value

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        render_toggle_card("진행 중인 공고", in_progress_count, "card_in_progress",
                            st.session_state.quick_filter == "in_progress", PALETTE_PROGRESS,
                            on_click=_toggle_quick, args=("in_progress",))
    with c2:
        render_toggle_card("관련 높은 공고", high_grade_count, "card_high_grade",
                            st.session_state.quick_filter == "high_grade", PALETTE_HIGH_GRADE,
                            on_click=_toggle_quick, args=("high_grade",))
    with c3:
        render_toggle_card("마감 3일 이내", soon_count, "card_due_soon",
                            st.session_state.quick_filter == "due_soon", PALETTE_DUE_SOON,
                            on_click=_toggle_quick, args=("due_soon",))
    with c4:
        render_toggle_card("전체 공고", total_count, "card_total",
                            st.session_state.quick_filter is None, PALETTE_FILTER_TOTAL,
                            on_click=_reset_quick)

    if st.session_state.quick_filter:
        label_map = {"in_progress": "진행중 공고", "high_grade": "등급 '상' 공고", "due_soon": "마감 3일 이내 공고"}
        st.info(f"🔎 현재 '{label_map[st.session_state.quick_filter]}' 만 보고 있습니다. 카드를 다시 누르면 해제됩니다.")

    display_df = tab_filtered.copy()
    if st.session_state.quick_filter == "in_progress":
        display_df = display_df[display_df[COL_STATUS] == "진행중"]
    elif st.session_state.quick_filter == "high_grade":
        display_df = display_df[display_df[COL_GRADE] == "상"]
    elif st.session_state.quick_filter == "due_soon":
        display_df = display_df[soon_mask]

    display_df = display_df.sort_values([COL_AI_SCORE, "_reg_date_parsed"], ascending=[False, False])
    display_df = display_df.reset_index(drop=True)
    st.markdown("---")

    tab_detail, tab_summary = st.tabs(["📑 상세보기", "⭐ 요약보기"])

    with tab_detail:
        st.subheader(f"전체 공고 목록 ({len(display_df)}건)")
        st.caption("💡 목록에서 '보기'를 누르면 팝업으로 전체 내용과 AI 판단근거가 표시됩니다. 배지 색으로 연관도를 한눈에 확인하세요.")

        if display_df.empty:
            st.info("조건에 맞는 공고가 없습니다.")
        else:
            for idx, row in display_df.iterrows():
                grade_badge = "🔴" if row[COL_GRADE] == "상" else ("🟡" if row[COL_GRADE] == "중" else "")
                due = row[COL_DUE_DATE] if pd.notna(row[COL_DUE_DATE]) else "미정"
                score = row.get(COL_AI_SCORE, -1)

                with st.container(border=True):
                    col_main, col_btn = st.columns([6, 1])
                    with col_main:
                        st.markdown(f"**{row.get('_track', '')} {grade_badge} {row[COL_TITLE]}**")
                        st.markdown(render_meta_line(row[COL_AGENCY], due, row[COL_STATUS], score), unsafe_allow_html=True)
                    with col_btn:
                        if st.button("보기", key=f"view_btn_{idx}", use_container_width=True):
                            show_detail_dialog(row)

    with tab_summary:
        st.subheader("⭐ AI 분석 기반 핵심 공고 요약")
        st.caption(" 솔루션과의 연관도가 높다고 AI가 판단한 공고를 우선순위 순으로 보여줍니다.")

        PRIORITY_THRESHOLD = 60
        priority_df = display_df[display_df[COL_AI_SCORE] >= PRIORITY_THRESHOLD].copy()

        if priority_df.empty:
            priority_df = display_df[display_df[COL_GRADE] == "상"].copy()
            st.caption("⚠️ 아직 AI 연관도 분석이 완료된 공고가 부족해 임시로 등급 '상' 공고를 표시합니다.")

        priority_df = priority_df.sort_values(COL_AI_SCORE, ascending=False)

        if st.button("🤖 AI 요약 일괄 생성 / 새로고침"):
            if is_gemini_ready() and not priority_df.empty:
                progress = st.progress(0.0, text="AI 요약 생성 중...")
                total = len(priority_df)
                for i, (_, r) in enumerate(priority_df.iterrows(), start=1):
                    generate_ai_summary(r)
                    progress.progress(i / total, text=f"AI 요약 생성 중... ({i}/{total})")
                progress.empty()
                st.success("AI 요약 생성이 완료되었습니다.")
            elif not is_gemini_ready():
                st.warning("Gemini API 키가 설정되지 않아 요약을 생성할 수 없습니다.")
            else:
                st.info("표시할 공고가 없습니다.")

        if priority_df.empty:
            st.info("표시할 공고가 없습니다.")
        else:
            for _, row in priority_df.iterrows():
                score = row.get(COL_AI_SCORE, -1)
                due = row[COL_DUE_DATE] if pd.notna(row[COL_DUE_DATE]) else "미정"

                with st.container(border=True):
                    st.markdown(f"**[{row[COL_TITLE]}]({row[COL_URL]})**")
                    st.markdown(
                        f'<span style="color:#6b6b6b;font-size:0.85em;">{row[COL_AGENCY]} · 등급 {row[COL_GRADE]} · 마감 {due}</span>'
                        f'&nbsp;&nbsp;{score_badge_html(score)}',
                        unsafe_allow_html=True,
                    )
                    if row.get(COL_AI_REASON):
                        st.caption(f"🎯 AI 판단근거: {row.get(COL_AI_REASON)}")

                    cache_key = row.get(COL_KEY) or row.get(COL_TITLE)
                    cached = st.session_state.ai_summary_cache.get(cache_key)
                    if cached and not str(cached).startswith("__ERROR__"):
                        st.markdown(f"> {cached}")
                    elif cached and str(cached).startswith("__ERROR__"):
                        st.error("요약 생성 중 오류가 발생했습니다. 다시 시도해 주세요.")
                    else:
                        st.caption("🤖 아직 AI 요약이 생성되지 않았습니다. 위쪽 '일괄 생성' 버튼을 눌러 주세요.")

    st.markdown("---")
    st.caption("본 대시보드는 매일 아침 8시 자동 수집 데이터를 기준으로 표시합니다. 새로고침(F5) 또는 오른쪽 상단 ⟳ 버튼으로 최신화할 수 있습니다.")


# ------------------------------------------------------------
# IT 뉴스 대탭
# -  연관도 TOP 10 (AI 스코어링, 경쟁사 키워드 제외)
# - 경쟁사 동향 섹션 (단순 키워드 매칭, AI 호출 없음)
# - 고정 모니터링 키워드 / 경쟁사 키워드 둘 다 편집 가능
# - 네이버 API의 HTML 엔티티(&quot; 등) / <b> 하이라이트 태그 클리닝
# - 소스 배지 + 말줄임(ellipsis)로 가독성 개선
# ------------------------------------------------------------
with main_tab_news:
    st.title("📰 IT 뉴스 통합 보기")
    st.caption("AI가  솔루션 연관도를 분석해 관련도 높은 뉴스를 상단에 배치하고, 경쟁사 동향은 별도로 모아 보여줍니다.")

    # 뉴스 행 hover 효과 (한 번만 주입)
    st.markdown(
        """
        <style>
        .news-row { padding:8px 10px; border-radius:6px; margin-bottom:2px; transition:background 0.15s; }
        .news-row:hover { background:#f5f5f5; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    SOURCE_COLOR = {"naver": "#03c75a", "google": "#4285f4", "boan": "#e53935"}
    SOURCE_LABEL = {"naver": "Naver", "google": "Google", "boan": "보안뉴스"}
    SOURCE_BADGE_TEXT = {"naver": "N", "google": "G", "boan": "보안"}

    def _clean_naver_text(raw: str) -> str:
        """네이버 검색 API가 내려주는 <b> 하이라이트 태그와 &quot; 같은 HTML 엔티티를 제거"""
        if not raw:
            return ""
        text = re.sub(r"</?b>", "", raw)
        text = unescape(text)
        return text.strip()

    def _badge_html(src):
        color = SOURCE_COLOR.get(src, "#888")
        label = SOURCE_BADGE_TEXT.get(src, src)
        return (
            f'<span style="background:{color};color:#fff;font-size:10px;font-weight:700;'
            f'padding:1px 6px;border-radius:10px;margin-right:6px;white-space:nowrap;'
            f'vertical-align:middle;">{label}</span>'
        )

    def _title_link_html(item, max_width="100%"):
        color = SOURCE_COLOR.get(item.get("_src"), "#1a1a1a")
        title = escape(str(item.get("title") or "(제목 없음)"))
        url = item.get("url") or item.get("link") or "#"
        return (
            f'<a href="{escape(url)}" target="_blank" title="{title}" '
            f'style="color:{color};font-weight:600;font-size:14px;line-height:1.5;'
            f'text-decoration:none;display:inline-block;max-width:{max_width};'
            f'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle;">'
            f'{title}</a>'
        )

    def _tag(items, src):
        if src == "naver":
            cleaned = []
            for it in items:
                it2 = dict(it)
                it2["title"] = _clean_naver_text(it2.get("title", ""))
                cleaned.append(it2)
            items = cleaned
        return [{**it, "_src": src} for it in items]

    @st.cache_data(ttl=600)
    def _cached_score_relevance(titles_tuple, srcs_tuple):
        items = [{"title": t, "source": s} for t, s in zip(titles_tuple, srcs_tuple)]
        return score_news_relevance(items)

    def _score_group(items):
        if not items:
            return items, None
        titles_tuple = tuple(it["title"] for it in items)
        srcs_tuple = tuple(it["_src"] for it in items)
        score_map, score_err = _cached_score_relevance(titles_tuple, srcs_tuple)
        for i, it in enumerate(items):
            it["_score"] = score_map.get(i, -1)
        return items, score_err

    def render_news_table(items_by_src, max_rows=8):
        def cell(item):
            if not item:
                return '<span style="color:#bbb;">범위 내 기사 부족</span>'
            return _badge_html(item.get("_src")) + _title_link_html(item, max_width="260px")

        g_list = items_by_src.get("google", [])
        n_list = items_by_src.get("naver", [])
        b_list = items_by_src.get("boan", [])
        rows = min(max(len(g_list), len(n_list), len(b_list), 1), max_rows)

        rows_html = ""
        for i in range(rows):
            g = g_list[i] if i < len(g_list) else None
            n = n_list[i] if i < len(n_list) else None
            b = b_list[i] if i < len(b_list) else None
            rows_html += (
                f'<tr>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{cell(g)}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{cell(n)}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{cell(b)}</td>'
                f'</tr>'
            )

        st.markdown(
            f"""
            <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
            <thead><tr>
                <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #4285f4;color:#4285f4;">🟦 Google 뉴스</th>
                <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #03c75a;color:#03c75a;">🟩 Naver 뉴스</th>
                <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #e53935;color:#e53935;">🟥 보안뉴스</th>
            </tr></thead>
            <tbody>{rows_html}</tbody>
            </table>
            """,
            unsafe_allow_html=True,
        )

    def render_top_news_list(items, n=10):
        seen = set()
        uniq = []
        for it in sorted(items, key=lambda x: -x.get("_score", -1)):
            if it["title"] in seen:
                continue
            seen.add(it["title"])
            uniq.append(it)
            if len(uniq) >= n:
                break

        if not uniq:
            st.info("아직 분석된 뉴스가 없습니다. 아래에서 키워드를 검색해 주세요.")
            return

        for rank, it in enumerate(uniq, start=1):
            score = it.get("_score", -1)
            score_txt = f"{score}점" if score >= 0 else "분석실패"
            st.markdown(
                f'<div class="news-row">'
                f'<span style="color:#999;font-weight:700;margin-right:6px;">{rank}.</span>'
                f'{_badge_html(it["_src"])}'
                f'{_title_link_html(it, max_width="65%")}'
                f'<span style="font-size:0.8em;color:#888;margin-left:8px;">🎯 {score_txt}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

    def render_competitor_list(items, competitor_keywords, n=15):
        seen = set()
        uniq = []
        for it in items:
            if it["title"] in seen:
                continue
            seen.add(it["title"])
            uniq.append(it)
            if len(uniq) >= n:
                break

        if not uniq:
            st.info("경쟁사 관련 뉴스가 없습니다.")
            return

        for it in uniq:
            matched = next((kw for kw in competitor_keywords if kw.lower() in it["title"].lower()), None)
            kw_badge = (
                f'<span style="font-size:0.8em;background:#fff3e0;color:#e65100;'
                f'border:1px solid #fb8c00;border-radius:6px;padding:1px 6px;margin-left:8px;">🎯 {escape(matched)}</span>'
                if matched else ""
            )
            st.markdown(
                f'<div class="news-row">'
                f'{_badge_html(it["_src"])}'
                f'{_title_link_html(it, max_width="70%")}'
                f'{kw_badge}'
                f'</div>',
                unsafe_allow_html=True,
            )

    # --- AI 추천 키워드 ---
    @st.cache_data(ttl=86400)
    def _cached_recommend_keywords(sample_titles_tuple):
        return recommend_keywords(list(sample_titles_tuple))

    rec_col1, rec_col2 = st.columns([5, 1])
    with rec_col1:
        st.markdown("**💡 AI 추천 키워드** (최근 공고 트렌드 +  솔루션 연관성 기반, 클릭하면 검색어에 추가됩니다)")
    with rec_col2:
        if st.button("🔄 추천 새로고침", key="refresh_rec_kw"):
            st.session_state.recommended_keywords = None
            _cached_recommend_keywords.clear()

    if st.session_state.recommended_keywords is None:
        if is_gemini_ready():
            sample_titles = tuple(df[COL_TITLE].dropna().astype(str).head(60).tolist())
            with st.spinner("AI가 최근 공고를 분석해 추천 키워드를 뽑는 중..."):
                rec_list, rec_err = _cached_recommend_keywords(sample_titles)
            st.session_state.recommended_keywords = rec_list if rec_list else []
            if rec_err:
                st.caption(f"⚠️ 추천 키워드 생성 실패: {rec_err}")
        else:
            st.session_state.recommended_keywords = []

    if st.session_state.recommended_keywords:
        chip_cols = st.columns(len(st.session_state.recommended_keywords))
        for i, rec in enumerate(st.session_state.recommended_keywords):
            with chip_cols[i]:
                if st.button(f"➕ {rec['keyword']}", key=f"rec_kw_{i}", help=rec.get("reason", ""), use_container_width=True):
                    if rec["keyword"] not in st.session_state.news_selected_keywords:
                        st.session_state.news_selected_keywords.append(rec["keyword"])
                        st.rerun()
    else:
        st.caption("추천 키워드가 아직 없습니다. (Gemini 미설정이거나 분석 실패)")

    # --- 키워드 관리 ( 고정 / 경쟁사 각각 편집) ---
    mgmt_col1, mgmt_col2 = st.columns(2)
    with mgmt_col1:
        with st.expander("⚙️ 고정 모니터링 키워드 관리"):
            fixed_kw_text = st.text_area(
                "쉼표로 구분해서 입력하세요. (연관도 TOP 10 분석에 사용됩니다)",
                value=", ".join(st.session_state.fixed_keywords),
                height=100,
                key="fixed_kw_editor",
            )
            fkw_col1, fkw_col2 = st.columns(2)
            with fkw_col1:
                if st.button("💾 저장 및 재수집", key="save_fixed_kw", use_container_width=True):
                    new_list = [k.strip() for k in fixed_kw_text.split(",") if k.strip()]
                    if new_list:
                        st.session_state.fixed_keywords = new_list
                        st.cache_data.clear()
                        st.success("고정 키워드가 저장되었습니다.")
                        st.rerun()
                    else:
                        st.warning("키워드를 1개 이상 입력해 주세요.")
            with fkw_col2:
                if st.button("↩️ 기본값 초기화", key="reset_fixed_kw", use_container_width=True):
                    st.session_state.fixed_keywords = list(DEFAULT_FIXED_KEYWORDS)
                    st.cache_data.clear()
                    st.rerun()

    with mgmt_col2:
        with st.expander("🎯 경쟁사 동향 키워드 관리"):
            comp_kw_text = st.text_area(
                "쉼표로 구분해서 입력하세요. (경쟁사 동향 섹션에만 사용되며,  연관도 분석에는 포함되지 않습니다)",
                value=", ".join(st.session_state.competitor_keywords),
                height=100,
                key="competitor_kw_editor",
            )
            ckw_col1, ckw_col2 = st.columns(2)
            with ckw_col1:
                if st.button("💾 저장 및 재수집", key="save_comp_kw", use_container_width=True):
                    new_list = [k.strip() for k in comp_kw_text.split(",") if k.strip()]
                    if new_list:
                        st.session_state.competitor_keywords = new_list
                        st.cache_data.clear()
                        st.success("경쟁사 키워드가 저장되었습니다.")
                        st.rerun()
                    else:
                        st.warning("키워드를 1개 이상 입력해 주세요.")
            with ckw_col2:
                if st.button("↩️ 기본값 초기화", key="reset_comp_kw", use_container_width=True):
                    st.session_state.competitor_keywords = list(DEFAULT_COMPETITOR_KEYWORDS)
                    st.cache_data.clear()
                    st.rerun()

    st.markdown("---")

    kw_input_col, kw_btn_col = st.columns([5, 1])
    with kw_input_col:
        custom_kw = st.text_input(
            "🔍 검색 키워드 (쉼표로 여러 개 입력 가능,  연관도 분석에 포함됩니다)",
            value=", ".join(st.session_state.news_selected_keywords),
        )
    with kw_btn_col:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        search_clicked = st.button("🔎 검색", type="primary", use_container_width=True)

    if search_clicked:
        st.session_state.news_selected_keywords = [k.strip() for k in custom_kw.split(",") if k.strip()]

    keywords = st.session_state.news_selected_keywords
    fixed_keywords = st.session_state.fixed_keywords
    competitor_keywords = st.session_state.competitor_keywords

    digest_clicked = st.button("🤖 오늘의 IT 뉴스 AI 요약 생성", use_container_width=False)

    if not is_naver_ready():
        st.warning("⚠️ NAVER_CLIENT_ID / NAVER_CLIENT_SECRET이 설정되지 않아 네이버 뉴스는 비어서 표시됩니다.")

    # =========================================================
    # 1) 데이터 선(先) 수집
    # =========================================================
    all_titles_for_digest = []
    all_items_pool = []  #  연관도 TOP10 후보 풀 (경쟁사 키워드 결과는 절대 섞지 않음)

    @st.cache_data(ttl=600)
    def _cached_fetch_fixed_monitoring(fixed_keywords_tuple):
        fk = list(fixed_keywords_tuple)
        google_q = " OR ".join(fk)
        google_items, google_err = fetch_google_news_rss(google_q, max_items=10)
        boan_items, boan_err = fetch_boannews(keywords=fk, max_items=10)

        naver_items = []
        naver_err = None
        seen_titles = set()
        for kw in fk:
            items, err = fetch_naver_news(kw, display=3)
            if err:
                naver_err = err
                continue
            for it in items:
                if it["title"] not in seen_titles:
                    seen_titles.add(it["title"])
                    naver_items.append(it)
        naver_items = naver_items[:10]

        return naver_items, google_items, boan_items, naver_err, google_err, boan_err

    fx_naver, fx_google, fx_boan, fx_naver_err, fx_google_err, fx_boan_err = _cached_fetch_fixed_monitoring(tuple(fixed_keywords))
    fx_items = _tag(fx_google, "google") + _tag(fx_naver, "naver") + _tag(fx_boan, "boan")
    fx_items, fx_score_err = _score_group(fx_items)
    all_titles_for_digest.extend([it["title"] for it in fx_items])
    all_items_pool.extend(fx_items)

    fx_by_src = {
        "google": sorted([it for it in fx_items if it["_src"] == "google"], key=lambda x: -x["_score"]),
        "naver": sorted([it for it in fx_items if it["_src"] == "naver"], key=lambda x: -x["_score"]),
        "boan": sorted([it for it in fx_items if it["_src"] == "boan"], key=lambda x: -x["_score"]),
    }

    @st.cache_data(ttl=600)
    def _cached_fetch_competitor_news(competitor_keywords_tuple):
        ck = list(competitor_keywords_tuple)
        google_q = " OR ".join(ck)
        google_items, google_err = fetch_google_news_rss(google_q, max_items=10)
        boan_items, boan_err = fetch_boannews(keywords=ck, max_items=10)

        naver_items = []
        naver_err = None
        seen_titles = set()
        for kw in ck:
            items, err = fetch_naver_news(kw, display=5)
            if err:
                naver_err = err
                continue
            for it in items:
                if it["title"] not in seen_titles:
                    seen_titles.add(it["title"])
                    naver_items.append(it)
        naver_items = naver_items[:10]

        return naver_items, google_items, boan_items, naver_err, google_err, boan_err

    cp_naver, cp_google, cp_boan, cp_naver_err, cp_google_err, cp_boan_err = _cached_fetch_competitor_news(tuple(competitor_keywords))
    cp_items_raw = _tag(cp_google, "google") + _tag(cp_naver, "naver") + _tag(cp_boan, "boan")
    cp_items = [
        it for it in cp_items_raw
        if any(kw.lower() in it["title"].lower() for kw in competitor_keywords)
    ]

    @st.cache_data(ttl=600)
    def _cached_fetch_keyword_news(keyword):
        naver_items, naver_err = fetch_naver_news(keyword, display=6)
        google_items, google_err = fetch_google_news_rss(keyword, max_items=6)
        boan_items, boan_err = fetch_boannews(keywords=[keyword], max_items=6)
        return naver_items, google_items, boan_items, naver_err, google_err, boan_err

    kw_results = {}
    for kw in keywords:
        naver_items, google_items, boan_items, naver_err, google_err, boan_err = _cached_fetch_keyword_news(kw)
        items = _tag(google_items, "google") + _tag(naver_items, "naver") + _tag(boan_items, "boan")
        items, score_err = _score_group(items)
        all_titles_for_digest.extend([it["title"] for it in items])
        all_items_pool.extend(items)
        kw_results[kw] = {
            "by_src": {
                "google": sorted([it for it in items if it["_src"] == "google"], key=lambda x: -x["_score"]),
                "naver": sorted([it for it in items if it["_src"] == "naver"], key=lambda x: -x["_score"]),
                "boan": sorted([it for it in items if it["_src"] == "boan"], key=lambda x: -x["_score"]),
            },
            "naver_err": naver_err, "google_err": google_err, "boan_err": boan_err,
            "score_err": score_err, "total": len(items),
        }

    all_titles_for_digest = list(dict.fromkeys(all_titles_for_digest))
    st.session_state["all_titles_for_digest"] = all_titles_for_digest

    # =========================================================
    # 2) 렌더링
    # =========================================================
    if digest_clicked:
        if not is_gemini_ready():
            st.warning("Gemini API 키가 설정되지 않아 요약할 수 없습니다.")
        elif not all_titles_for_digest:
            st.info("요약할 뉴스가 없습니다. 먼저 키워드를 검색해 주세요.")
        else:
            with st.spinner("AI가 오늘의 뉴스를 요약하는 중..."):
                digest_text, digest_err = generate_news_digest(all_titles_for_digest)
            if digest_err:
                st.error(f"요약 생성 실패: {digest_err}")
            else:
                st.markdown("##### 🤖 오늘의 IT 뉴스 AI 요약")
                st.success(digest_text)

    st.markdown("---")
    top_col, comp_col = st.columns(2)

    with top_col:
        st.markdown("### 🏆 AI 선정  연관도 TOP 10")
        st.caption("고정 키워드 + 검색 키워드 뉴스 중 AI가  솔루션과의 연관도를 분석한 결과입니다. (경쟁사 키워드는 제외됩니다)")
        render_top_news_list(all_items_pool, n=10)

    with comp_col:
        st.markdown("### 🎯 경쟁사 동향")
        st.caption("경쟁사 키워드(" + " · ".join(competitor_keywords) + ")가 제목에 포함된 뉴스만 모았습니다. (AI 분석 없이 단순 매칭)")
        cp_err_msgs = []
        if cp_google_err:
            cp_err_msgs.append(f"구글: {cp_google_err}")
        if cp_naver_err:
            cp_err_msgs.append(f"네이버: {cp_naver_err}")
        if cp_boan_err:
            cp_err_msgs.append(f"보안뉴스: {cp_boan_err}")
        if cp_err_msgs:
            st.caption("⚠️ " + " / ".join(cp_err_msgs))
        render_competitor_list(cp_items, competitor_keywords, n=15)

    st.markdown("---")
    st.markdown("### 🚨 고정 모니터링 키워드 ( 솔루션 핵심 이슈)")
    st.caption("고정 키워드: " + " · ".join(fixed_keywords))
    fx_err_msgs = []
    if fx_google_err:
        fx_err_msgs.append(f"구글: {fx_google_err}")
    if fx_naver_err:
        fx_err_msgs.append(f"네이버: {fx_naver_err}")
    if fx_boan_err:
        fx_err_msgs.append(f"보안뉴스: {fx_boan_err}")
    if fx_score_err:
        fx_err_msgs.append(f"AI 관련도 분석: {fx_score_err} (기본 순서로 표시)")
    if fx_err_msgs:
        st.caption("⚠️ " + " / ".join(fx_err_msgs))
    render_news_table(fx_by_src, max_rows=10)

    st.markdown("### 🔍 키워드별 뉴스")
    if not keywords:
        st.info("검색할 키워드를 입력하거나 위의 추천 키워드를 클릭해 주세요.")
    for kw in keywords:
        res = kw_results[kw]
        st.markdown(f"#### 🔍 {kw} ({res['total']}건)")
        err_msgs = []
        if res["google_err"]:
            err_msgs.append(f"구글: {res['google_err']}")
        if res["naver_err"]:
            err_msgs.append(f"네이버: {res['naver_err']}")
        if res["boan_err"]:
            err_msgs.append(f"보안뉴스: {res['boan_err']}")
        if res["score_err"]:
            err_msgs.append(f"AI 관련도 분석: {res['score_err']} (기본 순서로 표시)")
        if err_msgs:
            st.caption("⚠️ " + " / ".join(err_msgs))
        render_news_table(res["by_src"])


# ------------------------------------------------------------
# 트렌드 분석 대탭 (IT 뉴스 탭 데이터 수집이 끝난 뒤에 위치해야
# st.session_state["all_titles_for_digest"]가 최신값으로 채워져 있음)
# ------------------------------------------------------------
with main_tab_trend:
    st.title("📈 오늘의 IT 뉴스 트렌드 키워드")
    st.caption("매일 아침 자동 수집된 뉴스에서 AI가 핵심 키워드를 추출하고, 판단 근거와 함께 시각화합니다.")

    from trend_store import load_latest_trend, load_trend_history, save_trend_snapshot
    from ai_utils import extract_trend_keywords
    import plotly.express as px

    trend_df = load_latest_trend()

    top_bar_col, refresh_col = st.columns([5, 1])
    with top_bar_col:
        if not trend_df.empty:
            st.caption(f"🕒 마지막 분석 일자: {trend_df['snapshot_date'].iloc[0]}")
        else:
            st.caption("아직 저장된 트렌드 분석 결과가 없습니다.")
    with refresh_col:
        manual_run = st.button("🤖 지금 재분석", use_container_width=True)

    if manual_run:
        if not is_gemini_ready():
            st.warning("Gemini API 키가 설정되지 않아 분석할 수 없습니다.")
        else:
            with st.spinner("AI가 오늘의 뉴스에서 트렌드 키워드를 추출하는 중..."):
                titles_for_trend = st.session_state.get("all_titles_for_digest", [])
                if not titles_for_trend:
                    kw_result, kw_err = [], "먼저 'IT 뉴스' 탭에서 키워드를 검색해 뉴스를 수집해 주세요."
                else:
                    kw_result, kw_err = extract_trend_keywords(titles_for_trend)
            if kw_err:
                st.error(f"분석 실패: {kw_err}")
            elif kw_result:
                save_trend_snapshot(kw_result)
                st.success("트렌드 분석이 완료되어 저장되었습니다.")
                st.rerun()
            else:
                st.info("분석할 뉴스가 부족합니다.")

    trend_df = load_latest_trend()

    if trend_df.empty:
        st.info("아직 트렌드 데이터가 없습니다. '지금 재분석' 버튼을 눌러 첫 분석을 실행해 주세요.")
    else:
        trend_df = trend_df.sort_values("importance", ascending=False)

        st.markdown("### 🔥 중요도 기준 키워드")
        fig = px.bar(
            trend_df, x="importance", y="keyword", orientation="h",
            color="importance", color_continuous_scale="Reds",
            labels={"importance": "중요도", "keyword": "키워드"}, height=420,
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"}, margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("### 📊 언급 빈도 트리맵")
        fig2 = px.treemap(
            trend_df, path=["keyword"], values="count",
            color="importance", color_continuous_scale="Oranges",
        )
        fig2.update_layout(margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig2, use_container_width=True)

        st.markdown("### 🧩 키워드별 근거")
        for _, row in trend_df.iterrows():
            with st.expander(f"🔑 {row['keyword']} · 중요도 {row['importance']}점 · {row['count']}건"):
                st.write(f"**AI 판단 근거:** {row.get('reason', '')}")
                samples = row.get("sample_titles", [])
                if samples:
                    st.caption("관련 뉴스 제목:")
                    for s in samples:
                        st.write(f"- {s}")

        history_df = load_trend_history(days=14)
        if not history_df.empty:
            st.markdown("### 📅 최근 14일 트렌드 변화")
            top_keywords = trend_df["keyword"].head(6).tolist()
            hist_top = history_df[history_df["keyword"].isin(top_keywords)]
            fig3 = px.line(hist_top, x="snapshot_date", y="importance", color="keyword", markers=True)
            fig3.update_layout(margin=dict(l=10, r=10, t=30, b=10))
            st.plotly_chart(fig3, use_container_width=True)
