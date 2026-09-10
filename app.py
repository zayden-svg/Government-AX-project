import inspect
import re
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from ai_utils import is_gemini_ready, generate_summary

DB_PATH = "gov_tracker.db"
TABLE_NAME = "postings"

st.set_page_config(page_title="정부 IT 사업 AI 분석 대시보드", page_icon="📋", layout="wide")

# ------------------ DB 컬럼명 ------------------
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
COL_CONTENT = "content"  # 존재하지 않을 수도 있어 아래에서 항상 존재 여부를 먼저 확인함

# ------------------ 트랙(R&D / 사업부) 분류 설정 ------------------
TRACK_RND = "R&D 과제"
TRACK_BIZ = "사업부 과제"

AGENCY_DEFAULT_TRACK = {
    "IRIS": TRACK_RND,
    "국가AI전략위원회": TRACK_RND,
    "AIHub": TRACK_RND,
    "KERIS": TRACK_RND,
    "NIPA": TRACK_BIZ,
    "조달청": TRACK_BIZ,
    "행정안전부": TRACK_BIZ,
}

RND_KEYWORDS = ["연구개발", "r&d", "기술개발", "지원계획", "수요조사", "지원사업", "실증", "공모"]
BIZ_KEYWORDS = ["입찰", "용역", "구매", "공사", "제안요청", "나라장터", "낙찰", "계약", "위탁"]


def classify_track(row):
    text = " ".join(str(row.get(c, "")) for c in [COL_GUBUN, COL_POST_TYPE, COL_TITLE]).lower()
    if any(k.lower() in text for k in BIZ_KEYWORDS):
        return TRACK_BIZ
    if any(k.lower() in text for k in RND_KEYWORDS):
        return TRACK_RND
    return AGENCY_DEFAULT_TRACK.get(row.get(COL_AGENCY, ""), TRACK_RND)


# ------------------ 데이터 로드 ------------------
@st.cache_data(ttl=300)
def load_data():
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(f"SELECT * FROM {TABLE_NAME}", conn)
    conn.close()
    return df


df = load_data()
if df.empty:
    st.warning("데이터가 없습니다. python main.py를 먼저 실행해주세요.")
    st.stop()

for c in [COL_GRADE, COL_CATEGORY, COL_STATUS, COL_AGENCY]:
    df[c] = df[c].fillna("미분류").replace("", "미분류")

df["_reg_date_parsed"] = pd.to_datetime(df[COL_REG_DATE], errors="coerce")
df["_due_date_parsed"] = pd.to_datetime(df[COL_DUE_DATE], errors="coerce")
df["_track"] = df.apply(classify_track, axis=1)

if "ai_priority_score" not in df.columns:
    df["ai_priority_score"] = None
df["ai_priority_score"] = pd.to_numeric(df["ai_priority_score"], errors="coerce").fillna(-1).astype(int)

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


# ------------------ AI 정보 블록 / 요약 생성 ------------------
def build_info_block(row):
    lines = []

    def add(label, value):
        if value is not None and str(value).strip() not in ("", "nan", "None", "미분류"):
            lines.append(f"- {label}: {value}")

    add("공고 제목", row.get(COL_TITLE))
    add("주관부처 / 수행기관", f"{row.get(COL_DEPT, '')} / {row.get(COL_AGENCY, '')}")
    add("공고 유형", row.get(COL_GUBUN))
    add("사업 구분(R&D/사업부)", row.get("_track"))
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
    text, error = generate_summary(build_info_block(row))
    if error:
        return f"__ERROR__:{error}"
    st.session_state.ai_summary_cache[key] = text
    return text


# ------------------ 재사용 가능한 토글형 카드 버튼 ------------------
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


# ------------------ 사이드바 ------------------
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
if last_updated:
    st.sidebar.caption(f"🕒 마지막 데이터 갱신: {last_updated.strftime('%Y-%m-%d %H:%M')}")
st.sidebar.caption("매일 아침 자동 수집 예정 (작업 스케줄러 등록 후 적용)")
if not is_gemini_ready():
    st.sidebar.warning("⚠️ Gemini API 키가 설정되지 않았습니다. .env 또는 gemini_api.env 파일을 확인해 주세요.")

# ------------------ 사이드바 필터 적용 ------------------
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

# ------------------ 제목 + 트랙 토글 ------------------
st.title("📋 정부 IT 사업 AI 분석 대시보드")

st.markdown("### 🧭 사업 구분 (가장 먼저 확인하세요)")
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
    render_toggle_card("🔬 R&D 과제", rnd_in_filtered, "trk_rnd",
                        st.session_state.track_filter == TRACK_RND, PALETTE_RND,
                        on_click=_toggle_track, args=(TRACK_RND,), height=90, font_size=22)
with t3:
    render_toggle_card("💼 사업부 과제", biz_in_filtered, "trk_biz",
                        st.session_state.track_filter == TRACK_BIZ, PALETTE_BIZ,
                        on_click=_toggle_track, args=(TRACK_BIZ,), height=90, font_size=22)

st.caption("💡 자동 분류 기준: 제목/공고유형에 '입찰·용역·구매' 등이 있으면 사업부 과제, '연구개발·R&D·지원계획' 등이 있으면 R&D 과제로 분류하고, 애매한 경우 기관 특성으로 판단합니다.")

if st.session_state.track_filter:
    filtered = filtered[filtered["_track"] == st.session_state.track_filter]

filtered = filtered.sort_values(["_track", "_reg_date_parsed"], ascending=[True, False]) if st.session_state.track_filter is None else filtered.sort_values("_reg_date_parsed", ascending=False)

# ------------------ 4개 상단 카드 ------------------
soon_mask = (
    filtered["_due_date_parsed"].notna()
    & (filtered["_due_date_parsed"] >= today)
    & (filtered["_due_date_parsed"] <= today + timedelta(days=3))
)
in_progress_count = (filtered[COL_STATUS] == "진행중").sum()
high_grade_count = (filtered[COL_GRADE] == "상").sum()
soon_count = soon_mask.sum()
total_count = len(filtered)

st.markdown("---")


def _reset_quick():
    st.session_state.quick_filter = None


def _toggle_quick(value):
    st.session_state.quick_filter = None if st.session_state.quick_filter == value else value


c1, c2, c3, c4 = st.columns(4)
with c1:
    render_toggle_card("진행중 공고", in_progress_count, "card_in_progress",
                        st.session_state.quick_filter == "in_progress", PALETTE_PROGRESS,
                        on_click=_toggle_quick, args=("in_progress",))
with c2:
    render_toggle_card("등급 '상' 공고", high_grade_count, "card_high_grade",
                        st.session_state.quick_filter == "high_grade", PALETTE_HIGH_GRADE,
                        on_click=_toggle_quick, args=("high_grade",))
with c3:
    render_toggle_card("마감 3일 이내", soon_count, "card_due_soon",
                        st.session_state.quick_filter == "due_soon", PALETTE_DUE_SOON,
                        on_click=_toggle_quick, args=("due_soon",))
with c4:
    render_toggle_card("필터 표시 건수", total_count, "card_total",
                        st.session_state.quick_filter is None, PALETTE_FILTER_TOTAL,
                        on_click=_reset_quick)

if st.session_state.quick_filter:
    label_map = {"in_progress": "진행중 공고", "high_grade": "등급 '상' 공고", "due_soon": "마감 3일 이내 공고"}
    st.info(f"🔎 현재 '{label_map[st.session_state.quick_filter]}' 만 보고 있습니다. 카드를 다시 누르면 해제됩니다.")

display_df = filtered.copy()
if st.session_state.quick_filter == "in_progress":
    display_df = display_df[display_df[COL_STATUS] == "진행중"]
elif st.session_state.quick_filter == "high_grade":
    display_df = display_df[display_df[COL_GRADE] == "상"]
elif st.session_state.quick_filter == "due_soon":
    display_df = display_df[soon_mask]

display_df = display_df.sort_values(["ai_priority_score", "_reg_date_parsed"], ascending=[False, False])
display_df = display_df.reset_index(drop=True)
st.markdown("---")

FIELD_LABELS = {
    "_track": "구분", COL_AGENCY: "기관", COL_SOURCE: "수집소스", COL_GUBUN: "공고유형",
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
        st.warning("Gemini API 키가 설정되지 않았습니다. .env 또는 gemini_api.env 파일에 GEMINI_API_KEY를 추가한 뒤 앱을 다시 실행해 주세요.")
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
    track_emoji = "🔬" if row.get("_track") == TRACK_RND else "💼"
    score = row.get("ai_priority_score", -1)

    st.markdown(f"### {track_emoji} [{row[COL_TITLE]}]({row[COL_URL]})")
    st.caption("👆 제목을 누르면 원문 공고 페이지로 이동합니다.")

    info_col1, info_col2 = st.columns(2)
    with info_col1:
        st.markdown(f"**{row.get('_track', '')}**")
    with info_col2:
        if score >= 0:
            st.markdown(f"**🎯 자사 연관도: {int(score)}점**")
        else:
            st.markdown("**🎯 자사 연관도: 분석 대기**")

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


# Streamlit 버전에 따라 st.dialog 지원 여부가 다르므로 안전하게 분기
if hasattr(st, "dialog"):
    @st.dialog("공고 상세 보기", width="large")
    def show_detail_dialog(row):
        render_detail_body(row)
else:
    def show_detail_dialog(row):
        with st.expander("📄 공고 상세 보기", expanded=True):
            render_detail_body(row)


# ------------------ 탭 정의 ------------------
tab_detail, tab_summary = st.tabs(["📑 상세보기", "⭐ 요약보기"])

rename_map = dict(FIELD_LABELS)
rename_map[COL_URL] = "원문링크"

with tab_detail:
    st.subheader(f"전체 공고 목록 ({len(display_df)}건)")
    st.caption("💡 목록에서 '보기'를 누르면 팝업으로 전체 내용과 AI 요약이 표시됩니다. (가로 스크롤 없는 카드형 리스트)")

    if display_df.empty:
        st.info("조건에 맞는 공고가 없습니다.")
    else:
        for idx, row in display_df.iterrows():
            track_emoji = "🔬" if row.get("_track") == TRACK_RND else "💼"
            grade_badge = "🔴" if row[COL_GRADE] == "상" else ("🟡" if row[COL_GRADE] == "중" else "")
            due = row[COL_DUE_DATE] if pd.notna(row[COL_DUE_DATE]) else "미정"
            score = row.get("ai_priority_score", -1)
            score_text = f"🎯 연관도 {int(score)}점" if score >= 0 else "🎯 연관도 분석 대기"

            with st.container(border=True):
                col_main, col_btn = st.columns([6, 1])
                with col_main:
                    st.markdown(f"**{track_emoji} {grade_badge} {row[COL_TITLE]}**")
                    st.caption(f"{row[COL_AGENCY]} · 마감 {due} · {row[COL_STATUS]} · {score_text}")
                with col_btn:
                    if st.button("보기", key=f"view_btn_{idx}", use_container_width=True):
                        show_detail_dialog(row)

with tab_summary:
    st.subheader("⭐ AI 분석 기반 핵심 공고 요약")
    st.caption("자사 솔루션과의 연관도가 높다고 AI가 판단한 공고를 우선순위 순으로 보여줍니다. 각 공고 아래 줄에 AI 핵심 요약이 함께 표시됩니다.")

    PRIORITY_THRESHOLD = 60
    priority_df = display_df[display_df["ai_priority_score"] >= PRIORITY_THRESHOLD].copy()

    if priority_df.empty:
        priority_df = display_df[display_df[COL_GRADE] == "상"].copy()
        st.caption("⚠️ 아직 AI 연관도 분석이 완료된 공고가 부족해 임시로 등급 '상' 공고를 표시합니다. python main.py 재실행 시 자동으로 채워집니다.")

    priority_df = priority_df.sort_values("ai_priority_score", ascending=False)

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
            track_emoji = "🔬" if row.get("_track") == TRACK_RND else "💼"
            score = row.get("ai_priority_score", -1)
            score_text = f"🎯 연관도 {int(score)}점" if score >= 0 else "🎯 연관도 분석 대기"
            due = row[COL_DUE_DATE] if pd.notna(row[COL_DUE_DATE]) else "미정"

            with st.container(border=True):
                st.markdown(f"**{track_emoji} [{row[COL_TITLE]}]({row[COL_URL]})**")
                st.caption(f"{row[COL_AGENCY]} · 등급 {row[COL_GRADE]} · 마감 {due} · {score_text}")

                cache_key = row.get(COL_KEY) or row.get(COL_TITLE)
                cached = st.session_state.ai_summary_cache.get(cache_key)
                if cached and not str(cached).startswith("__ERROR__"):
                    st.markdown(f"> {cached}")
                elif cached and str(cached).startswith("__ERROR__"):
                    st.error("요약 생성 중 오류가 발생했습니다. 다시 시도해 주세요.")
                else:
                    st.caption("🤖 아직 AI 요약이 생성되지 않았습니다. 위쪽 '일괄 생성' 버튼을 눌러 주세요.")

st.markdown("---")
st.caption("본 대시보드는 python main.py 실행 시점 기준 데이터를 표시합니다. 새 데이터 수집 후 브라우저 새로고침(F5) 또는 오른쪽 상단 ⟳ 버튼을 눌러 주세요.")
