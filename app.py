import common  # noqa: F401  (한국시간 고정 — 다른 모듈보다 먼저)

import re
import json
from datetime import datetime, timedelta
from html import escape, unescape

import pandas as pd
import plotly.express as px
import streamlit as st

from common import (
    PRODUCT_KEYWORDS, INTEGRATED_RND_DOMAINS, PRODUCT_TO_DOMAIN, PROCUREMENT_BOOST_KEYWORDS,
    COMPETITOR_DEFAULT, is_competitor_match, procurement_boost_score, is_mois_noise,
    detect_regions, region_label, match_regions, ALL_REGIONS, MY_REGIONS_DEFAULT, NATIONAL_LABEL,
    DEFAULT_NEWS_KEYWORDS, SOLUTION_NEWS_KEYWORDS, ALERT_MIN_SCORE_DEFAULT, validate_email,
)
from ai_utils import (
    is_ai_ready, generate_summary, recommend_keywords,
    generate_news_digest, score_news_relevance,
    generate_headline, generate_key_issues,
    summarize_titles_oneline, simplify_news_titles,
)
from db2 import get_engine
from news_utils import (
    fetch_naver_news, fetch_google_news_rss, fetch_boannews, fetch_etnews_rss,
    is_naver_ready, _clean_naver_text,
)
from trend_store import load_latest_trend, load_trend_history
from pdf_report import build_daily_report_pdf
from procurement_store import load_results, load_reorder_candidates
from store import (
    load_cache_many, upsert_subscriber, delete_subscriber, get_subscriber, count_subscribers,
)
from briefing_batch import (
    K_NEWS_DEFAULT, K_NEWS_SRC10, K_NEWS_SOLUTION, K_NEWS_SIMPLE, K_REC_KEYWORDS,
    K_HEADLINE, K_ISSUES, K_DIGEST, K_PRODUCT_AI, K_META,
)

TABLE_NAME = "postings"

st.set_page_config(page_title="정부 IT 사업 AI 분석 대시보드", page_icon="📋", layout="wide")

# ============================================================
# 테마 — Streamlit 기본 테마(⋮ 메뉴 → Settings)를 그대로 따른다.
# 위젯(버튼·입력창·표)은 config.toml의 [theme.light]/[theme.dark]가 자동 처리하고,
# 직접 그린 HTML 카드만 아래 C 팔레트로 색을 맞춘다. → 다크모드에서 위젯만 흰색으로 남던 문제의 근본 해결
# ============================================================
def _detect_theme():
    try:
        th = st.context.theme            # Streamlit 1.46 이상
        val = getattr(th, "type", None) or getattr(th, "base", None)
        if val is None and hasattr(th, "get"):
            val = th.get("type") or th.get("base")
        return "dark" if str(val).lower() == "dark" else "light"
    except Exception:
        return "light"


THEME = _detect_theme()

LIGHT = dict(
    bg="#FAFBFD", surface="#FFFFFF", surface2="#F6F8FB", surface3="#EEF3FF",
    border="#E4E9F0", text="#191F28", text_body="#374151", text_muted="#6B7684",
    accent="#2D5BFF", accent_soft="#EEF3FF", navy="#191F28", navy_text="#FFFFFF",
    row_border="#EEEEEE",
    danger_bg="#FEE4E2", danger_text="#B42318", danger_border="#D6453D",
    warn_bg="#FEF0C7", warn_text="#93370D", warn_border="#C2410C",
    success_bg="#ECFDF3", success_text="#067647", success_border="#0F9D58",
)
DARK = dict(
    bg="#15171C",
    surface="#1C1F26",
    surface2="#242832",
    surface3="#2E333F",
    border="#5B6472",
    text="#D7DCE4",
    text_body="#F0F2F5",
    text_muted="#9AA3AE",
    accent="#5B9BFF",
    accent_soft="#1E2A44",
    navy="#0E1116",
    navy_text="#FFFFFF",
    row_border="#333945",
    danger_bg="#2B1113",
    danger_text="#FF8A80",
    danger_border="#B4454A",
    warn_bg="#2B2210",
    warn_text="#FFCF5C",
    warn_border="#D98A1F",
    success_bg="#0F241A",
    success_text="#6FE2A0",
    success_border="#2E9E5B",
)


C = DARK if THEME == "dark" else LIGHT


def _tc(light_hex, dark_hex):
    return dark_hex if THEME == "dark" else light_hex


tcol1, tcol_mail, tcol_pdf, tcol_dark = st.columns([7.0, 1.5, 1.3, 1])
with tcol_pdf:
    pdf_top_slot = st.empty()
    pdf_top_slot.markdown(
        f"<div style='text-align:center;font-size:11px;color:{C['text_muted']};padding-top:9px;'>📄 PDF 준비 중...</div>",
        unsafe_allow_html=True,
    )
with tcol_mail:
    # ------------------------------------------------------------
    # 📧 메일 알림 등록 — 매일 아침 연관도 높은 신규 공고를 메일로 받기 (사내 메일만)
    # ------------------------------------------------------------
    with st.popover("📧 메일 알림", use_container_width=True):
        st.markdown("**매일 아침 영업 기회 메일 받기**")
        st.caption("오늘 새로 올라온 공고 중 AI 연관도가 기준 이상인 것 + 경쟁사 수주 사업 재발주 예상을 보내드립니다.")
        mail_input = st.text_input("이메일", key="alert_email", placeholder="name@stclab.com")
        mail_email, mail_err = validate_email(mail_input) if mail_input else (None, None)
        if mail_err:
            st.error(mail_err)
        current_sub = None
        if mail_email:
            try:
                current_sub = get_subscriber(mail_email)
            except Exception as e:
                st.error(f"구독 정보를 불러오지 못했습니다: {e}")
            st.caption("✅ 이미 등록된 주소입니다. 아래에서 조건을 바꾸거나 해제할 수 있습니다." if current_sub
                       else "ℹ️ 아직 등록되지 않은 주소입니다.")
        mail_regions = st.multiselect(
            "받을 지역 (비우면 전체)", ALL_REGIONS,
            default=(current_sub["regions"] if current_sub else MY_REGIONS_DEFAULT),
            key=f"alert_regions_{mail_email or 'new'}",
        )
        mail_national = st.checkbox(
            f"지역이 안 적힌 공고({NATIONAL_LABEL})도 받기",
            value=(current_sub["include_national"] if current_sub else True),
            key=f"alert_national_{mail_email or 'new'}",
        )
        mail_score = st.slider(
            "AI 연관도 기준(점 이상)", 40, 95,
            value=(current_sub["min_score"] if current_sub else ALERT_MIN_SCORE_DEFAULT), step=5,
            key=f"alert_score_{mail_email or 'new'}",
        )
        mb1, mb2 = st.columns(2)
        with mb1:
            if st.button("등록·저장", key="alert_save", type="primary", use_container_width=True, disabled=not mail_email):
                try:
                    res = upsert_subscriber(mail_email, mail_regions, mail_national, mail_score)
                    st.success("등록했습니다." if res == "created" else "조건을 저장했습니다.")
                except Exception as e:
                    st.error(f"저장 실패: {e}")
        with mb2:
            if st.button("알림 해제", key="alert_delete", use_container_width=True,
                         disabled=not (mail_email and current_sub)):
                try:
                    delete_subscriber(mail_email)
                    st.success("해제했습니다. 더 이상 메일이 가지 않습니다.")
                except Exception as e:
                    st.error(f"해제 실패: {e}")
        st.caption(f"현재 등록 {count_subscribers()}명 · 매일 아침 8시 자동수집 후 발송")


with tcol_dark:
    st.markdown(
        f"<div style='text-align:center;font-size:11px;color:{C['text_muted']};padding-top:9px;' "
        f"title='우측 상단 ⋮ 메뉴 → Settings → Theme에서 라이트/다크 전환'>"
        f"{'🌙 다크' if THEME == 'dark' else '☀️ 라이트'}</div>",
        unsafe_allow_html=True,
    )

# 다크모드 전용 CSS (Python 3.11에서도 동작하도록 중첩 f-string을 변수로 분리)
_DARK_CSS_1 = "" if THEME != "dark" else f"""
/* [1] elevation 계층: 카드/패널/팝오버가 배경보다 점진적으로 밝아지게 */
.gt-surface, div[data-testid="stVerticalBlockBorderWrapper"] {{
    background: {C['surface']} !important;
    border: 1px solid {C['border']} !important;
}}
div[data-testid="stPopover"] > div {{
    background: {C['surface2']} !important;
    border: 1px solid {C['border']} !important;
}}

/* [2] 매직넘버 기준 텍스트 — 헤딩(7:1) / 본문(15:1) 분리 적용 */
h1, h2, h3, h4,
[data-testid="stMarkdownContainer"] h1,
[data-testid="stMarkdownContainer"] h2,
[data-testid="stMarkdownContainer"] h3,
[data-testid="stMarkdownContainer"] h4,
[data-testid="stExpander"] summary p {{
    color: {C['text']} !important;
}}
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] span,
[data-testid="stMarkdownContainer"] li,
[data-testid="stCaptionContainer"] p,
[data-testid="stMetricValue"],
label {{
    color: {C['text_body']} !important;
}}

/* [3] 모든 테두리 매직넘버50 기준으로 가시성 확보 (이전엔 거의 안 보였던 부분) */
div[data-testid="stVerticalBlockBorderWrapper"],
div[data-testid="stExpander"],
.gt-surface, .gt-mon-card {{
    border: 1px solid {C['border']} !important;
}}

/* [4] 검색창 — 실제 testid 기준 재작성 */
div[data-testid="stTextInputRootElement"] {{
    background: #ECEEF1 !important;
    border: 1.5px solid {C['border']} !important;
    border-radius: 10px !important;
}}
div[data-testid="stTextInputField"] {{
    color: #000000 !important;
    background: transparent !important;
}}
div[data-testid="stTextInputField"]::placeholder {{
    color: #5B6472 !important;
}}

/* 검색 버튼 + 추천 키워드 칩 + AI 추천 키워드 버튼 — secondary 계열 전부 */
button[data-testid^="stBaseButton-secondary"] {{
    background: #ECEEF1 !important;
    border: 1px solid {C['border']} !important;
}}
button[data-testid^="stBaseButton-secondary"] p,
button[data-testid^="stBaseButton-secondary"] span,
button[data-testid^="stBaseButton-secondary"] div {{
    color: #000000 !important;
}}


/* [5] 시스템 색상(알림창) — 다크모드 역할 반전: 아이콘/텍스트 밝게, 배경 매우 어둡게 */
div[data-testid="stAlert"] {{
    background: {C['surface2']} !important;
    border: 1px solid {C['border']} !important;
    border-radius: 10px;
}}
div[data-testid="stAlert"] p, div[data-testid="stAlert"] div {{
    color: {C['text_body']} !important;
}}
"""
_DARK_CSS_2 = "" if THEME != "dark" else f"""
div[data-testid="stVerticalBlockBorderWrapper"] {{
    border: 1.5px solid #6B7684 !important;
}}
"""

st.markdown(
    f"""
    <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css');

    html, body, .stApp, [class*="css"] {{
        font-family: 'Pretendard Variable', Pretendard, -apple-system, 'Malgun Gothic', sans-serif !important;
        color: {C['text_body']} !important;
        font-size: 16px;
    }}
    .stApp {{ background: {C['bg']} !important; }}

    .block-container {{
        max-width: 1280px;
        padding-top: 1rem;
        padding-bottom: 2.4rem;
    }}

    h1 {{ font-size: 26px; }}
    h2 {{ font-size: 22px; }}
    h3 {{ font-size: 19px; }}
    h4 {{ font-size: 17px; }}
    h1, h2, h3, h4,
    [data-testid="stMarkdownContainer"] h1,
    [data-testid="stMarkdownContainer"] h2,
    [data-testid="stMarkdownContainer"] h3,
    [data-testid="stMarkdownContainer"] h4 {{
        letter-spacing: -0.02em; font-weight: 800; line-height: 1.4; color: {C['text']} !important;
    }}
    h4 {{ margin: 16px 0 8px; }}

    p, span, div, label {{ font-size: 15.5px; }}

    button:focus, button:focus-visible, button:active,
    [tabindex]:focus, [tabindex]:focus-visible,
    a:focus, a:focus-visible,
    div[data-testid="stButton"] button:focus,
    div[data-testid="stPopover"] button:focus,
    div[data-testid="stPopover"] button:focus-visible {{
        outline: none !important;
        box-shadow: none !important;
    }}

    /* === 수정 #1: 탭 글씨 — 다크모드에서 opacity 때문에 회색으로 보이던 문제 보정 ===
           ↓ 비활성 탭도 선명하게 보이게 하려면 아래 opacity 숫자를 0~1 사이로 조절하면 됨 */
    button[data-baseweb="tab"] {{
        font-weight: 700;
        color: {C['text']} !important;
        opacity: {_tc('0.55', '0.8')};
        font-size: 16px;
        padding: 8px 14px;
        transition: opacity .15s ease;
    }}
    button[data-baseweb="tab"]:hover {{ opacity: 0.95; }}
    button[data-baseweb="tab"][aria-selected="true"] {{
        color: {C['text']} !important;
        opacity: 1 !important;
        font-weight: 800 !important;
    }}
    div[data-testid="stTabs"] button[data-baseweb="tab"] p,
    div[data-testid="stTabs"] button[data-baseweb="tab"] div,
    div[data-testid="stTabs"] button[data-baseweb="tab"] {{
        color: {_tc(C['text'], '#FFFFFF')} !important;
    }}

    div[data-testid="stExpander"] {{
        background: {C['surface']} !important;
        border: 1px solid {C['border']} !important;
        border-radius: 10px;
        margin-bottom: 10px;
    }}
    div[data-testid="stExpander"] summary {{
        background: {C['surface2']} !important;
        border-radius: 10px 10px 0 0;
        font-weight: 700;
        font-size: 16px;
        padding: 10px 14px !important;
        color: {C['text']} !important;
    }}

    div[data-testid="stExpander"][open] summary {{ border-bottom: 1px solid {C['border']}; }}

    div[data-testid="stVerticalBlockBorderWrapper"] {{
        background: {C['surface']} !important;
        border: 1px solid {C['border']} !important;
        border-radius: 10px !important;
    }}
    div[data-testid="stVerticalBlockBorderWrapper"] p,
    div[data-testid="stVerticalBlockBorderWrapper"] span,
    div[data-testid="stVerticalBlockBorderWrapper"] div {{ color: {C['text_body']}; }}

    div[data-testid="stVerticalBlockBorderWrapper"] div[data-testid="stVerticalBlock"] {{
        gap: 0.3rem !important;
    }}

    /* === 수정 #2: 핵심동향 이슈카드 — 팝오버(제목) 바로 아래 간격을 추가로 눌러줌 ===
           ↓ 간격을 더 좁히거나 넓히려면 margin-bottom 숫자(px)만 바꾸면 됨 */
    div[data-testid="stVerticalBlockBorderWrapper"] [data-testid="element-container"] {{
        margin-bottom: 0 !important;
    }}
    div[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stPopover"] {{
    margin-bottom: -15px !important;
    }}
    div[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stPopover"] button {{
    padding-bottom: 0 !important;
    line-height: 1.15 !important;
    }}

    /* === 수정 #3: 버튼(kind=secondary) 전부 흰 배경 + 검정 글씨로 전역 고정 ===
           토글카드(render_toggle_card)처럼 이미 자체 색상 CSS가 걸린 버튼은
           선택자가 더 구체적이라 이 규칙에 덮이지 않음 */
    .stButton > button {{ border-radius: 8px; font-weight: 700; font-size: 13px; padding: 0.35rem 0.8rem; transition: all .15s ease; }}
    .stButton > button[kind="primary"] {{ background: {C['accent']} !important; border: 1px solid {C['accent']} !important; color: #fff !important; }}
    .stButton > button[kind="primary"] p,
    .stButton > button[kind="primary"] span {{ color: #fff !important; }}
    .stButton > button[kind="secondary"] {{
        background: #FFFFFF !important;
        border: 1px solid #D7DBE3 !important;
        color: #111111 !important;
    }}
    .stButton > button[kind="secondary"] p,
    .stButton > button[kind="secondary"] span,
    .stButton > button[kind="secondary"] div {{ color: #111111 !important; }}
    .stButton > button[kind="secondary"]:hover {{ border-color: {C['accent']}; }}
    .stButton > button[kind="secondary"]:hover p,
    .stButton > button[kind="secondary"]:hover span {{ color: {C['accent']} !important; }}

    div[data-testid="stTextInput"] div[data-baseweb="input"],
    div[data-testid="stTextInput"] div[data-baseweb="base-input"] {{
        background: {C['surface']} !important;
        border: 1.5px solid {_tc('#D7DBE3', '#5B636F')} !important;
        border-radius: 8px !important;
        box-shadow: none !important;
        outline: none !important;
    }}
    div[data-testid="stTextInput"]:focus-within div[data-baseweb="input"],
    div[data-testid="stTextInput"]:focus-within div[data-baseweb="base-input"] {{
        border-color: {C['accent']} !important;
        box-shadow: 0 0 0 2px {C['accent_soft']} !important;
    }}
    div[data-testid="stTextInput"] input {{
        background: transparent !important;
        color: {C['text']} !important;
        outline: none !important;
        box-shadow: none !important;
    }}
    div[data-testid="stTextInput"] input::placeholder,
    div[data-baseweb="input"] input::placeholder {{
        color: {C['text_muted']} !important;
        opacity: 1 !important;
    }}
    div[data-testid="stTextArea"] textarea {{
        background: {C['surface']} !important;
        color: {C['text']} !important;
        border: 1px solid {C['border']} !important;
    }}

    div[role="radiogroup"] {{ gap: 8px; flex-wrap: wrap; }}
    div[role="radiogroup"] label {{
        display: flex !important;
        align-items: center;
        gap: 6px;
        background: {C['surface']} !important;
        border: 1.5px solid {C['border']};
        border-radius: 999px;
        padding: 5px 16px;
        font-size: 13px;
        color: {C['text_body']} !important;
        transition: all .15s ease;
    }}
    div[role="radiogroup"] label p {{ margin: 0; color: {C['text_body']} !important; }}
    div[role="radiogroup"] label:has(input:checked) {{
        border-color: {C['accent']};
        background: {C['surface3']} !important;
        font-weight: 700;
    }}
    div[role="radiogroup"] label:has(input:checked) p {{ color: {C['text']} !important; }}

    .stDataFrame [role="columnheader"] {{ background: {C['navy']} !important; color: {C['navy_text']} !important; font-weight: 700 !important; font-size: 14px !important; }}
    .stDataFrame [role="gridcell"] {{ font-size: 14px !important; color: {C['text_body']} !important; background: {C['surface']} !important; }}
    .stDataFrame a {{ color: {C['accent']} !important; font-weight: 600; text-decoration: none; }}
    .stDataFrame a:hover {{ text-decoration: underline; }}

    .news-row {{ padding: 6px 10px; border-radius: 8px; margin-bottom: 2px; transition: background .15s; font-size: 14.5px; }}
    .news-row:hover {{ background: {C['surface2']}; }}

    .gt-surface {{ background: {C['surface2']}; border: 1px solid {C['border']}; border-radius: 10px; }}
    .gt-surface-strong {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 10px; }}
    .gt-row {{ border-bottom: 1px solid {C['row_border']}; }}
    .gt-muted {{ color: {C['text_muted']}; }}
    .gt-text {{ color: {C['text']}; }}
    .gt-body {{ color: {C['text_body']}; }}

    .gt-ai-oneline {{
        display: flex; align-items: flex-start; gap: 6px;
        background: {C['accent_soft']}; border-left: 3px solid {C['accent']};
        border-radius: 6px; padding: 5px 9px; margin: 4px 0 6px;
        font-size: 12.5px; color: {C['accent']}; font-weight: 600; line-height: 1.5;
    }}

    div[data-testid="stAlert"] {{ border-radius: 10px; font-size: 16px; background: {C['surface2']} !important; border: 1px solid {C['border']} !important; }}
    div[data-testid="stAlert"] p, div[data-testid="stAlert"] div {{ color: {C['text_body']} !important; }}

    .stCaption, [data-testid="stCaptionContainer"] {{ font-size: 13.5px !important; color: {C['text_muted']} !important; }}

    a {{ color: {C['accent']}; }}

    .gt-report-sheet {{
        background: {C['surface']};
        margin: 0 0 14px;
        padding: 18px 22px;
        border-radius: 10px;
        border: 1px solid {C['border']};
    }}
    .gt-report-meta {{ font-size: 11px; color: {C['text_muted']}; font-weight: 700; letter-spacing: .08em; }}
    .gt-stat-grid {{ display:flex; border-top:1px solid {C['border']}; border-bottom:1px solid {C['border']}; margin: 12px 0 0; }}
    .gt-stat-box {{ flex:1; text-align:center; padding:14px 8px; border-right:1px solid {C['border']}; }}
    .gt-stat-box:last-child {{ border-right:none; }}
    .gt-stat-num {{ font-size:26px; font-weight:800; color:{C['text']}; }}
    .gt-stat-label {{ font-size:11px; color:{C['text_muted']}; margin-top:4px; line-height:1.4; }}

    /* === 수정 #4: mc2/mc3 네이티브 컨테이너 박스 스타일 (st.container(key=...)에 적용) === */
    .st-key-mc2_kw_box, .st-key-mc3_comp_box {{
        background:{C['surface']}; border:1px solid {C['border']}; border-radius:10px;
        padding:14px 16px; height:100%;
    }}

    .gt-mon-card {{ background:{C['surface']}; border:1px solid {C['border']}; border-radius:10px; padding:14px 16px; height:100%; display:flex; flex-direction:column; gap:6px; overflow:hidden; box-sizing:border-box; }}
    .gt-mon-card-label {{ font-size:10.5px; font-weight:700; letter-spacing:.06em; color:{C['text_muted']}; text-transform:uppercase; white-space:normal; word-break:keep-all; }}
    .gt-mon-card-value {{ font-size:24px; font-weight:800; color:{C['text']}; margin-top:4px; white-space:normal; word-break:keep-all; }}
    .gt-mon-delta {{ font-size:11px; color:{C['success_text']}; font-weight:700; margin-left:5px; }}
    .gt-mon-bar-row {{ display:flex; align-items:center; gap:8px; font-size:12px; margin-top:7px; color:{C['text_body']}; flex-wrap:wrap; }}
    .gt-mon-bar-track {{ flex:1; height:6px; border-radius:4px; background:{C['surface3']}; overflow:hidden; min-width:40px; }}
    .gt-mon-bar-fill {{ height:100%; border-radius:4px; }}
    .gt-mon-source-row {{ display:flex; align-items:center; gap:8px; font-size:12.5px; padding:3px 0; color:{C['text_body']}; }}
    .gt-mon-dot {{ width:9px; height:9px; border-radius:50%; flex-shrink:0; }}
    .gt-mon-main {{ background:{C['surface']}; border:1px solid {C['border']}; border-radius:10px; overflow:hidden; }}
    .gt-mon-live {{ display:flex; justify-content:flex-end; align-items:center; gap:14px; padding:10px 16px; border-bottom:1px solid {C['border']}; font-size:12px; color:{C['text_muted']}; font-weight:700; }}
    .gt-mon-live-dot {{ width:7px; height:7px; border-radius:50%; background:{C['success_text']}; display:inline-block; margin-right:5px; box-shadow:0 0 0 3px {C['success_bg']}; }}
    .gt-mon-row {{ display:flex; align-items:center; gap:10px; padding:10px 16px; border-bottom:1px solid {C['row_border']}; transition:background .15s; }}
    .gt-mon-row:last-child {{ border-bottom:none; }}
    .gt-mon-row:hover {{ background:{C['surface2']}; }}
    .gt-mon-src-tag {{ font-size:10.5px; font-weight:700; padding:2px 7px; border-radius:5px; color:#fff; white-space:nowrap; min-width:50px; text-align:center; flex-shrink:0; }}
    .gt-mon-title-link {{ flex:1; font-size:13px; font-weight:600; color:{C['text']}; text-decoration:none; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; }}
    .gt-mon-title-link:hover {{ color:{C['accent']}; text-decoration:underline; }}
    .gt-mon-status {{ font-size:10px; font-weight:800; padding:2px 9px; border-radius:5px; white-space:nowrap; letter-spacing:.03em; flex-shrink:0; }}
    .gt-mon-time {{ font-size:11.5px; color:{C['text_muted']}; min-width:32px; text-align:right; flex-shrink:0; }}

    .gt-news-card {{ display:flex; align-items:center; gap:10px; padding:10px 16px; border-bottom:1px solid {C['row_border']}; text-decoration:none; transition:background .15s; }}
    .gt-news-card:last-child {{ border-bottom:none; }}
    .gt-news-card:hover {{ background:{C['surface2']}; }}
    .gt-news-card-title {{ flex:1; font-size:13px; font-weight:600; color:{C['text']}; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; }}
    .gt-news-card-right {{ display:flex; align-items:center; gap:8px; flex-shrink:0; }}
    /* === 수정 #5: 뉴스 수집처별 보기 전용 — 로고/뱃지 없이 제목 전체가 줄바꿈되며 보이는 변형 === */
    .gt-news-card-title-wrap {{ white-space:normal !important; line-height:1.45; overflow:visible; text-overflow:unset; word-break:keep-all; }}

    div[data-testid="stPopover"] button {{
        width: 100%;
        text-align: left !important;
        background: transparent !important;
        border: none !important;
        padding: 0 !important;
        font-size: 13.5px !important;
        font-weight: 800 !important;
        color: {C['text']} !important;
        justify-content: flex-start !important;
        white-space: normal !important;
        line-height: 1.4 !important;
    }}
    div[data-testid="stPopover"] button:hover {{
        color: {C['accent']} !important;
        text-decoration: underline;
    }}
    div[data-testid="stPopover"] button p {{ color: inherit !important; font-size: inherit !important; font-weight: inherit !important; }}

    .gt-popover-item {{ padding:10px 6px; border-bottom:1px solid {C['row_border']}; line-height:1.6; }}
    .gt-popover-item:last-child {{ border-bottom:none; }}

    .gt-guide-card-wrap {{ min-height: 178px; display:flex; flex-direction:column; gap:6px; }}

    @media (max-width: 768px) {{
        .block-container {{ padding-left: 0.8rem; padding-right: 0.8rem; padding-top: 0.6rem; }}
        html, body, .stApp, [class*="css"] {{ font-size: 15px; }}
        h1 {{ font-size: 20px; }} h2 {{ font-size: 18px; }} h3 {{ font-size: 17px; }} h4 {{ font-size: 15.5px; }}
        div[role="radiogroup"] label {{ padding: 5px 10px; font-size: 13.5px; }}
        .stButton > button {{ width: 100%; white-space: normal !important; line-height: 1.3; min-height: 38px; }}
        div[data-testid="stExpander"] summary {{ font-size: 14.5px; padding: 8px 10px !important; }}
        .news-row {{ font-size: 13.5px; }}
        .gt-stat-grid {{ flex-wrap: wrap; }}
        .gt-stat-box {{ flex: 1 1 50%; border-bottom: 1px solid {C['border']}; }}

        div[data-testid="stHorizontalBlock"] {{ flex-direction: column !important; }}
        div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {{
            width: 100% !important; flex: 1 1 100% !important; margin-bottom: 6px;
        }}
        .gt-keep-row div[data-testid="stHorizontalBlock"] {{ flex-direction: row !important; }}
        .gt-keep-row div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {{
            width: auto !important; margin-bottom: 0;
        }}
    }}
    div[data-testid="stHorizontalBlock"] {{ align-items: stretch; }}
    div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {{ display: flex; }}
    div[data-testid="stHorizontalBlock"] > div[data-testid="column"] > div {{ width: 100%; }}

    header[data-testid="stHeader"] {{ visibility: hidden; height: 0; }}
    div[data-testid="stToolbar"] {{ visibility: hidden; display: none; }}
    div[data-testid="stDecoration"] {{ visibility: hidden; }}
    div[data-testid="stStatusWidget"] {{ visibility: hidden; display: none; }}
    #MainMenu {{ visibility: hidden; }}

    /* ============================================================
   KRDS 선명한 화면 모드(다크모드) 원칙 반영 패치
   - 계층 밝기 순서 / 매직넘버 대비 / 흰색 눈부심 방지 / 시스템색 역할 반전
   ============================================================ */

{_DARK_CSS_1}

{_DARK_CSS_2}

.st-key-gt_issue_cards_row div[data-testid="stHorizontalBlock"] {{
    gap: 8px !important;
    column-gap: 8px !important;
}}

    
    .gt-opp-link:hover{{ text-decoration:underline !important; }}

    </style>
    """,
    unsafe_allow_html=True,
)

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
        return {"label": "분석대기", "emoji": "⚪", "bg": C["surface2"], "text": C["text_muted"], "border": C["border"]}
    if score >= 80:
        return {"label": "매우높음", "emoji": "🔥", "bg": C["danger_bg"], "text": C["danger_text"], "border": C["danger_border"]}
    if score >= 60:
        return {"label": "높음", "emoji": "🟠", "bg": C["warn_bg"], "text": C["warn_text"], "border": C["warn_border"]}
    if score >= 40:
        return {"label": "보통", "emoji": "🟡", "bg": C["surface3"], "text": C["text"], "border": C["border"]}
    return {"label": "낮음", "emoji": "⚪", "bg": C["surface2"], "text": C["text_muted"], "border": C["border"]}


def score_badge_html(score):
    band = get_score_band(score)
    score_text = "분석대기" if (score is None or score < 0) else f"{int(score)}점"
    return (
        f'<span style="background:{band["bg"]};color:{band["text"]};'
        f'border:1px solid {band["border"]};border-radius:999px;padding:1px 9px;'
        f'font-weight:700;font-size:11.5px;white-space:nowrap;">'
        f'{band["emoji"]} {band["label"]} · {score_text}</span>'
    )


def format_budget_eok(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "-", ""):
        return None
    digits = re.sub(r"[^\d.]", "", s)
    if not digits:
        return None
    try:
        num = float(digits)
    except ValueError:
        return None
    if num <= 0:
        return None
    eok = num / 100_000_000
    if eok >= 0.1:
        eok_r = round(eok, 1)
        return f"{int(eok_r)}억" if eok_r == int(eok_r) else f"{eok_r}억"
    man = num / 10_000
    if man >= 1:
        man_r = round(man, 1)
        return f"{int(man_r)}만원" if man_r == int(man_r) else f"{man_r}만원"
    return f"{int(num):,}원"


# === 신규: budget 컬럼이 비어있을 때 content(본문) 텍스트에서 금액을 직접 찾아내는 폴백 ===
BUDGET_TEXT_PATTERNS = [
    r"사업\s*금액\s*[:：]\s*([\d,]+)\s*원",
    r"추정\s*가격\s*[:：]\s*([\d,]+)\s*원",
    r"계약\s*금액\s*[:：]\s*([\d,]+)\s*원",
    r"예산\s*금액\s*[:：]\s*([\d,]+)\s*원",
]

def extract_budget_fallback(content_text):
    if not content_text or str(content_text).strip() in ("", "nan", "None"):
        return None
    text = str(content_text)
    for pattern in BUDGET_TEXT_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


def get_budget_display(row, content_col=COL_CONTENT, budget_col=COL_BUDGET):
    formatted = format_budget_eok(row.get(budget_col))
    if formatted:
        return formatted
    fallback_raw = extract_budget_fallback(row.get(content_col))
    return format_budget_eok(fallback_raw) if fallback_raw else None

def render_meta_line(agency, due, status, score, budget=None):
    badge = score_badge_html(score)
    budget_txt = format_budget_eok(budget)
    budget_part = f" · 예산 {budget_txt}" if budget_txt else ""
    return (
        f'<span style="color:{C["text_muted"]};font-size:0.82em;">{agency} · 마감 {due} · {status}{budget_part}</span>'
        f'&nbsp;&nbsp;{badge}'
    )


FIXED_NEWS_KEYWORDS = ["차세대", "시스템"]


def _product_tags_for_title(title_text, kw_text=""):
    """=== 수정: Ⅲ 표의 '제품연관' 컬럼 — 제목+매칭키워드로 NF/NFA/BM/LT 태그를 뽑음 ==="""
    combined = f"{title_text} {kw_text}".lower()
    tags = []
    for pname, pinfo in PRODUCT_KEYWORDS.items():
        short = pname.split("(")[-1].replace(")", "").strip()
        if any(k.lower() in combined for k in pinfo["keywords"]):
            tags.append(short)
    return "·".join(tags) if tags else "-"


def _domain_tags_for_title(title_text, kw_text=""):
    """=== 수정: Ⅲ 표의 '기술도메인' 컬럼 ==="""
    combined = f"{title_text} {kw_text}".lower()
    tags = [dname for dname, dinfo in INTEGRATED_RND_DOMAINS.items() if any(k.lower() in combined for k in dinfo["keywords"])]
    return "·".join(tags) if tags else "-"


@st.cache_data(ttl=600)
def _cached_procurement_results():
    return load_results(days=30)


@st.cache_data(ttl=600)
def _cached_reorder(competitors_tuple, include_solution):
    return load_reorder_candidates(list(competitors_tuple), horizon_days=180, include_solution=include_solution)


@st.cache_data(ttl=300)
def load_data():
    engine = get_engine()
    df = pd.read_sql_query(f"SELECT * FROM {TABLE_NAME}", engine)
    return df


BRIEF_KEYS = [K_NEWS_DEFAULT, K_NEWS_SRC10, K_NEWS_SOLUTION, K_NEWS_SIMPLE, K_REC_KEYWORDS,
              K_HEADLINE, K_ISSUES, K_DIGEST, K_PRODUCT_AI, K_META]


@st.cache_data(ttl=300)
def load_briefing():
    """아침 배치(briefing_batch.py)가 미리 만들어 둔 결과 — 없으면 빈 값 (화면은 실시간 생성으로 대체)"""
    return load_cache_many(BRIEF_KEYS)


def brief(key):
    val, _ = BRIEF.get(key, (None, None))
    return val


def brief_time(key):
    _, ts = BRIEF.get(key, (None, None))
    return ts


try:
    df = load_data()
except Exception as e:
    st.error(f"DB에서 공고를 불러오지 못했습니다. Streamlit Secrets의 DATABASE_URL을 확인해 주세요. ({type(e).__name__})")
    st.stop()
if df.empty:
    st.warning("데이터가 없습니다. GitHub Actions에서 자동수집(Run workflow)을 먼저 실행해 주세요.")
    st.stop()

BRIEF = load_briefing()
if COL_KEY in df.columns:      # 예전 DB에 같은 공고가 두 줄 들어간 경우 한 줄만 표시
    df = df.drop_duplicates(subset=[COL_KEY], keep="last")

for c in [COL_GRADE, COL_CATEGORY, COL_STATUS, COL_AGENCY]:
    if c in df.columns:
        df[c] = df[c].fillna("미분류").replace("", "미분류")

for c in [COL_TRACK, COL_TRACK_REASON, COL_AI_REASON, COL_TITLE, COL_DEPT, COL_KEYWORDS, COL_URL,
          COL_DUE_DATE, COL_REG_DATE, COL_BUDGET, COL_CONTENT, "ai_oneline", "ai_summary"]:
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

# [정리] 마감일이 지난 공고는 화면에서 제외 (마감일 미표기 건은 유지)
df = df[df["_due_date_parsed"].isna() | (df["_due_date_parsed"] >= today)].copy()

# [정리] 행안부 게시판의 일반 보도자료 제외 — 사업·공모 성격 단어가 있는 글만 남김
df = df[[not is_mois_noise(a, t) for a, t in zip(df[COL_AGENCY], df[COL_TITLE])]].copy()
if df.empty:
    st.warning("진행 중인 공고가 없습니다. 자동수집(GitHub Actions) 실행 여부를 확인해주세요.")
    st.stop()

# [지역] 기관명·부서·제목·본문으로 시·도 판별 → 담당 지역 필터에 사용
df["_regions"] = [detect_regions(a, d, t, str(c)[:300]) for a, d, t, c in
                  zip(df[COL_AGENCY], df[COL_DEPT], df[COL_TITLE], df[COL_CONTENT])]
df["_region_label"] = df["_regions"].map(region_label)
last_updated = None
if COL_UPDATED_AT in df.columns:
    parsed = pd.to_datetime(df[COL_UPDATED_AT], errors="coerce")
    if parsed.notna().any():
        last_updated = parsed.max()

if "ai_summary_cache" not in st.session_state:
    st.session_state.ai_summary_cache = {}
if "oneline_summary_cache" not in st.session_state:
    st.session_state.oneline_summary_cache = {}
if "track_filter" not in st.session_state:
    st.session_state.track_filter = None
if "quick_filter" not in st.session_state:
    st.session_state.quick_filter = None
if "news_selected_keywords" not in st.session_state:
    st.session_state.news_selected_keywords = []
if "biz_keyword_filter" not in st.session_state:
    st.session_state.biz_keyword_filter = []


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


def _stored(row, col):
    v = row.get(col) if hasattr(row, "get") else None
    v = "" if v is None else str(v).strip()
    return "" if v in ("nan", "None") else v


def generate_ai_summary(row):
    key = row.get(COL_KEY) or row.get(COL_TITLE)
    if key in st.session_state.ai_summary_cache:
        return st.session_state.ai_summary_cache[key]
    stored = _stored(row, "ai_summary")          # main.py가 미리 만들어 둔 요약 (AI 호출 0회)
    if stored:
        st.session_state.ai_summary_cache[key] = stored
        return stored
    if not is_ai_ready():
        return None
    text_val, error = generate_summary(build_info_block(row))
    if error:
        return f"__ERROR__:{error}"
    st.session_state.ai_summary_cache[key] = text_val
    return text_val


ONELINE_AUTO_LIMIT = 40


def ensure_oneline_summaries(rows_df, auto=True):
    if rows_df.empty:
        return
    need = []
    for _, r in rows_df.iterrows():
        key = r.get(COL_KEY) or r.get(COL_TITLE)
        stored = _stored(r, "ai_oneline")
        if stored:
            st.session_state.oneline_summary_cache[key] = stored
            continue
        if key not in st.session_state.oneline_summary_cache:
            need.append({"key": key, "title": r.get(COL_TITLE), "agency": r.get(COL_AGENCY)})
    if not need or not is_ai_ready():
        return
    if auto:
        need = need[:ONELINE_AUTO_LIMIT]
    if not need:
        return
    results, err = summarize_titles_oneline(need)
    if err:
        return
    for item in results:
        k = item.get("key")
        s = item.get("summary")
        if k and s:
            st.session_state.oneline_summary_cache[k] = s


def get_oneline_summary(row):
    if not hasattr(row, "get"):          # 제목 문자열만 넘어와도 멈추지 않도록 방어
        return st.session_state.oneline_summary_cache.get(row)
    key = row.get(COL_KEY) or row.get(COL_TITLE)
    return _stored(row, "ai_oneline") or st.session_state.oneline_summary_cache.get(key)

if "news_simple_cache" not in st.session_state:
    st.session_state.news_simple_cache = {}


def ensure_news_simple(items, limit=50):
    if not is_ai_ready() or not items:
        return
    need = []
    for it in items:
        key = re.sub(r"\s+", "", str(it.get("title", "")))[:40]
        if key and key not in st.session_state.news_simple_cache:
            need.append({"title": it.get("title", "")})
    if not need:
        return
    need = need[:limit]
    results, err = simplify_news_titles(need)
    if err:
        return
    for r in results:
        k = re.sub(r"\s+", "", str(r.get("title", "")))[:40]
        if k and r.get("simple"):
            st.session_state.news_simple_cache[k] = r["simple"]


def get_news_simple(it):
    key = re.sub(r"\s+", "", str(it.get("title", "")))[:40]
    return st.session_state.news_simple_cache.get(key)


def render_toggle_card(label, count, key, active, colors, on_click=None, args=None, height=82, font_size=21):
    use_key = True
    try:
        box = st.container(key=key)
    except TypeError:
        box = st.container()
        use_key = False

    inactive_text = C['text'] if THEME == "dark" else colors['light_text']

    if use_key:
        st.markdown(
            f"""
            <style>
            .st-key-{key} button {{
                height: {height}px;
                width: 100%;
                border-radius: 12px;
                border: none;
                white-space: pre-line;
                text-align: left;
                padding: 10px 14px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.07);
                transition: transform 0.15s ease, box-shadow 0.15s ease;
                line-height: 1.25;
            }}
            .st-key-{key} button:hover {{
                transform: translateY(-2px);
                box-shadow: 0 6px 14px rgba(0,0,0,0.14);
            }}
            .st-key-{key} button::first-line {{
                font-size: {font_size}px;
                font-weight: 800;
            }}
            .st-key-{key} button[kind="secondary"] {{
                background: linear-gradient(135deg, {colors['light_bg']} 0%, {C['surface']} 100%) !important;
                color: {inactive_text} !important;
                border-left: 5px solid {colors['border']} !important;
            }}
            .st-key-{key} button[kind="secondary"]::first-line {{ color: {inactive_text} !important; }}
            .st-key-{key} button[kind="primary"] {{
                background: {colors['active_bg']} !important;
                color: {colors['active_text']} !important;
                border-left: 5px solid {colors['active_bg']} !important;
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



PALETTE_TOTAL = dict(light_bg=C['surface2'], light_text=C['text'], border=C['border'], active_bg=C['navy'], active_text=C['navy_text'])
PALETTE_RND = dict(light_bg=C['accent_soft'], light_text=C['accent'], border=C['accent'], active_bg=C['accent'], active_text="#ffffff")
PALETTE_BIZ = dict(light_bg=C['success_bg'], light_text=C['success_text'], border=C['success_border'], active_bg=C['success_border'], active_text="#ffffff")
PALETTE_PROGRESS = dict(light_bg=C['accent_soft'], light_text=C['accent'], border=C['accent'], active_bg=C['accent'], active_text="#ffffff")
PALETTE_HIGH_GRADE = dict(light_bg=C['danger_bg'], light_text=C['danger_text'], border=C['danger_border'], active_bg=C['danger_border'], active_text="#ffffff")
PALETTE_DUE_SOON = dict(light_bg=C['warn_bg'], light_text=C['warn_text'], border=C['warn_border'], active_bg=C['warn_border'], active_text="#ffffff")
PALETTE_FILTER_TOTAL = dict(light_bg=C['surface2'], light_text=C['text'], border=C['border'], active_bg=C['navy'], active_text=C['navy_text'])


# ------------------------------------------------------------
# 통합 검색 — 화면 최상단
# ------------------------------------------------------------
trend_top_df = load_latest_trend()

search_form_box = st.container(key="gt_keep_search_row")
with search_form_box:
    st.markdown('<div class="gt-keep-row">', unsafe_allow_html=True)
    with st.form("top_search_form", clear_on_submit=False):
        search_col, btn_col = st.columns([6, 1])
        with search_col:
            search_keyword = st.text_input(
                "검색어",
                key="int_top_search",
                placeholder="예: AI, 클라우드, 대기열, 부하테스트",
                label_visibility="collapsed",
            )
        with btn_col:
            st.form_submit_button("🔍 검색", use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)

@st.cache_data(ttl=86400, show_spinner=False)
def _daily_rec_keywords(day_key, titles_tuple):
    """아침 배치 결과가 없을 때만: 하루 1번(모든 접속자 공용) AI 추천 키워드 생성"""
    rec, _err = recommend_keywords(list(titles_tuple))
    return rec or []


def _get_rec_keywords():
    rec = brief(K_REC_KEYWORDS)
    if isinstance(rec, list) and rec:
        return [r for r in rec if isinstance(r, dict) and r.get("keyword")]
    if not is_ai_ready():
        return []
    sample = df[COL_TITLE].dropna().astype(str).head(80).tolist()
    if not trend_top_df.empty and "keyword" in trend_top_df.columns:
        sample += trend_top_df["keyword"].dropna().astype(str).head(20).tolist()
    with st.spinner("AI가 오늘의 추천 키워드를 뽑는 중... (하루 1번만)"):
        return [r for r in _daily_rec_keywords(datetime.now().strftime("%Y-%m-%d"), tuple(sample))
                if isinstance(r, dict) and r.get("keyword")]


def _set_search(kw):
    st.session_state.int_top_search = kw


def _clear_search():
    st.session_state.int_top_search = ""


REC_KEYWORDS = _get_rec_keywords()

chip_box = st.container(key="search_chip_row")
with chip_box:
    _rec_kws = [r["keyword"] for r in REC_KEYWORDS][:8]
    if _rec_kws:
        _chip_cols = st.columns(len(_rec_kws) + 1)
        for _i, _kw in enumerate(_rec_kws):
            with _chip_cols[_i]:
                help_txt = next((r.get("reason", "") for r in REC_KEYWORDS if r["keyword"] == _kw), "")
                st.button(_kw, key=f"top_rec_{_i}", help=help_txt, use_container_width=True,
                          on_click=_set_search, args=(_kw,))
        with _chip_cols[-1]:
            st.button("✖", key="clear_search_chips", help="검색어 지우기", use_container_width=True,
                      on_click=_clear_search)

# ------------------------------------------------------------
# 📍 지역 필터 — 내 담당 지역(서울·인천·강원·전라·제주)만 바로 보기
# ------------------------------------------------------------
if "my_regions" not in st.session_state:
    st.session_state.my_regions = list(MY_REGIONS_DEFAULT)

rf1, rf2, rf3 = st.columns([2.2, 2.2, 5.6])
with rf1:
    region_only = st.toggle("📍 내 담당 지역만 보기", value=False, key="region_only",
                            help="기관명·부서·제목에 들어간 지역명으로 판별합니다.")
with rf2:
    include_national = st.toggle(f"{NATIONAL_LABEL} 공고 포함", value=True, key="region_include_national",
                                 help="지역이 적혀 있지 않은 공고(중앙부처·전국 공모 등)도 함께 봅니다.")
with rf3:
    with st.popover(f"담당 지역: {', '.join(st.session_state.my_regions) or '없음'}", use_container_width=True):
        picked = st.multiselect("담당 지역 선택", ALL_REGIONS, default=st.session_state.my_regions, key="my_regions_pick")
        if st.button("적용", key="my_regions_apply"):
            st.session_state.my_regions = picked
            st.rerun()

df_all = df   # 필터 전 원본 (전체 건수 비교용)
search_keyword = str(st.session_state.get("int_top_search") or "").strip()
if search_keyword:
    _pat = re.escape(search_keyword)
    _m = (df[COL_TITLE].astype(str).str.contains(_pat, case=False, na=False)
          | df[COL_KEYWORDS].astype(str).str.contains(_pat, case=False, na=False)
          | df[COL_AGENCY].astype(str).str.contains(_pat, case=False, na=False)
          | df[COL_DEPT].astype(str).str.contains(_pat, case=False, na=False))
    df = df[_m].copy()
if region_only:
    df = df[[match_regions(r, st.session_state.my_regions, include_national) for r in df["_regions"]]].copy()

if search_keyword or region_only:
    _parts = []
    if search_keyword:
        _parts.append(f"검색어 '{search_keyword}'")
    if region_only:
        _parts.append(f"지역 {', '.join(st.session_state.my_regions)}" + (f" + {NATIONAL_LABEL}" if include_national else ""))
    st.info(f"🔎 {' · '.join(_parts)} 기준으로 {len(df)}건을 보고 있습니다. (전체 {len(df_all)}건)")
    if df.empty:
        st.warning("조건에 맞는 공고가 없습니다. 검색어를 지우거나 지역 필터를 꺼 주세요.")
        df = df_all.iloc[0:0].copy()

filtered = df.copy()


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
    cached = st.session_state.ai_summary_cache.get(cache_key) or _stored(row, "ai_summary")
    if cached:
        st.success(cached)
        return

    if not is_ai_ready():
        st.warning("Claude API 키가 설정되지 않았습니다. Streamlit Secrets에 ANTHROPIC_API_KEY를 추가해 주세요.")
        return

    if cached and not str(cached).startswith("__ERROR__"):
        st.success(cached)
    else:
        if st.button("🤖 AI 요약 생성하기", key=f"gen_{cache_key}"):
            with st.spinner("AI가 공고를 분석하고 있습니다..."):
                result = generate_ai_summary(row)
            if result is None:
                st.warning("Claude API가 설정되지 않아 요약을 생성할 수 없습니다.")
            elif str(result).startswith("__ERROR__"):
                st.error(f"요약 생성 중 오류가 발생했습니다: {result.replace('__ERROR__:', '')}")
            else:
                st.success(result)


def render_detail_body(row):
    score = row.get(COL_AI_SCORE, -1)
    band = get_score_band(score)
    score_display = "분석대기" if score is None or score < 0 else f"{int(score)}점"
    grade_badge = "🔴" if row[COL_GRADE] == "상" else ("🟡" if row[COL_GRADE] == "중" else "")

    title_url = row.get(COL_URL) or ""
    st.markdown(f"{grade_badge} {row[COL_TITLE]}")
    if title_url:
        st.link_button("🔗 원문 공고 페이지로 이동", title_url, use_container_width=False)
    else:
        st.caption("⚠️ 원문 링크 정보가 없습니다.")

    st.markdown(
        f"""
        <div style="background:{band['bg']};border-left:5px solid {band['border']};
                    border-radius:10px;padding:12px 16px;margin-bottom:12px;">
            <div style="font-size:14px;font-weight:800;color:{band['text']};margin-bottom:6px;">
                {row.get('_track', '')} &nbsp;|&nbsp; {band['emoji']}  연관도 {band['label']} ({score_display})
            </div>
            <div style="font-size:12.5px;color:{C['text_body']};margin-bottom:4px;">
                🧭 <b>AI 구분 판단근거</b>: {row.get(COL_TRACK_REASON) or '근거 없음'}
            </div>
            <div style="font-size:12.5px;color:{C['text_body']};">
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
        if field == COL_BUDGET:
            formatted = format_budget_eok(value)
            display_value = f"{formatted} (원문: {value})" if formatted else value
            st.write(f"**{label}:** {display_value}")
        else:
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
# 뉴스 렌더링 공통 헬퍼
# ------------------------------------------------------------
SOURCE_COLOR = {"naver": "#03c75a", "google": "#4285f4", "boan": "#e53935", "etnews": "#8e24aa"}
SOURCE_BADGE_TEXT = {"naver": "N", "google": "G", "boan": "보안", "etnews": "전자"}
SOURCE_LABEL_KO = {"google": "구글", "naver": "네이버", "boan": "보안뉴스", "etnews": "전자신문"}


def _badge_html(src):
    color = SOURCE_COLOR.get(src, "#888")
    label = SOURCE_BADGE_TEXT.get(src, src)
    return (
        f'<span style="background:{color};color:#fff;font-size:10px;font-weight:700;'
        f'padding:1px 6px;border-radius:10px;margin-right:6px;white-space:nowrap;'
        f'vertical-align:middle;">{label}</span>'
    )


def _relative_time(dt_val):
    if dt_val is None:
        return "-"
    try:
        ts = pd.Timestamp(dt_val)
        if pd.isna(ts):
            return "-"
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
    except Exception:
        return "-"
    now = pd.Timestamp(datetime.now())
    secs = (now - ts).total_seconds()
    if secs < 0:
        return "방금"
    if secs < 3600:
        return f"{max(1, int(secs // 60))}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    days = int(secs // 86400)
    return "Yest" if days == 1 else f"{days}d"


def _news_pub_dt(it):
    return it.get("pubDate") or it.get("pub_date")


def _dedup_by_title(items):
    """=== 수정 #6: 플랫폼별 중복 제거 — 제목 앞부분(공백 제거) 기준 ==="""
    seen = set()
    out = []
    for it in items:
        key = re.sub(r"\s+", "", str(it.get("title", "")))[:40]
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _collection_window_start(now=None):
    """=== 수정 #7: '전날 09:00 ~ 당일 08:00' 수집 사이클의 시작점(가장 최근 09:00)을 계산 ===
    예: 지금이 10/6 07:30이면 → 10/5 09:00을 기준점으로 삼음.
    지금이 10/6 10:00이면 → 10/6 09:00을 기준점으로 삼음.
    ↓ 기준 시각(9시)을 바꾸고 싶으면 아래 hour=9 숫자만 바꾸면 됨."""
    now = now or datetime.now()
    anchor = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now < anchor:
        anchor -= timedelta(days=1)
    return anchor


def _within_collection_window(pub_val):
    try:
        t = pd.Timestamp(pub_val)
        if pd.isna(t):
            return False
        if t.tzinfo is not None:
            t = t.tz_localize(None)
        return t >= _collection_window_start()
    except Exception:
        return False


# 뱃지는 '좋다/나쁘다'가 아니라 '자사 솔루션과 얼마나 관련 있는지'를 뜻함 (예전 POSITIVE 표기는 오해 소지)
NEWS_STATUS_STYLE = {
    "positive": {"label": "연관 높음", "bg": lambda: C['success_bg'], "text": lambda: C['success_text']},
    "risk":     {"label": "경쟁사",   "bg": lambda: C['danger_bg'],  "text": lambda: C['danger_text']},
    "watch":    {"label": "관련",     "bg": lambda: C['warn_bg'],    "text": lambda: C['warn_text']},
    "neutral":  {"label": "",        "bg": lambda: C['surface3'],   "text": lambda: C['text_muted']},
}


def news_status_badge_html(status_key):
    if status_key == "neutral":
        return ""
    s = NEWS_STATUS_STYLE.get(status_key, NEWS_STATUS_STYLE["neutral"])
    return f'<span class="gt-mon-status" style="background:{s["bg"]()};color:{s["text"]()};">{s["label"]}</span>'

def get_news_status(item):
    title_low = str(item.get("title", "")).lower()
    comp_kws = st.session_state.get("competitor_keywords", COMPETITOR_DEFAULT)
    if is_competitor_match(title_low, comp_kws):
        return "risk"
    score = item.get("_score", -1)
    if score >= 75:
        return "positive"
    if score >= 50:
        return "watch"
    return "neutral"

def mon_row_html(src_label, src_color, title, url, status_key, time_str, orig_title=None):
    title_esc = escape(str(title or "(제목 없음)"))
    tip_esc = escape(str(orig_title or title or ""))      # 마우스를 올리면 원문 제목
    url_esc = escape(str(url or "#"))
    return (
        f'<a href="{url_esc}" target="_blank" class="gt-news-card">'
        f'<span class="gt-mon-src-tag" style="background:{src_color};">{escape(str(src_label))}</span>'
        f'<span class="gt-news-card-title" title="원문: {tip_esc}">{title_esc}</span>'
        f'<span class="gt-news-card-right">{news_status_badge_html(status_key)}'
        f'<span class="gt-mon-time">{escape(str(time_str))}</span></span>'
        f'</a>'
    )


def mon_row_plain_html(title, url, status_key, time_str, orig_title=None):
    """뉴스 수집처별 보기 전용 — 로고/뱃지 없이 제목 전체 표시 (마우스를 올리면 원문 제목)"""
    title_esc = escape(str(title or "(제목 없음)"))
    tip_esc = escape(str(orig_title or title or ""))
    url_esc = escape(str(url or "#"))
    return (
        f'<a href="{url_esc}" target="_blank" class="gt-news-card" title="원문: {tip_esc}">'
        f'<span class="gt-news-card-title gt-news-card-title-wrap">{title_esc}</span>'
        f'<span class="gt-news-card-right">{news_status_badge_html(status_key)}'
        f'<span class="gt-mon-time">{escape(str(time_str))}</span></span>'
        f'</a>'
    )


def _title_link_html(item, max_width="100%"):
    color = SOURCE_COLOR.get(item.get("_src"), C["text"])
    title = escape(str(item.get("title") or "(제목 없음)"))
    url = item.get("url") or item.get("link") or "#"
    return (
        f'<a href="{escape(url)}" target="_blank" title="{title}" '
        f'style="color:{color};font-weight:600;font-size:13px;line-height:1.5;'
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
        base = score_map.get(i, -1)
        boost = procurement_boost_score(it["title"])
        it["_score"] = base if base < 0 else min(100, base + boost)
    return items, score_err


@st.cache_data(ttl=600)
def _cached_fetch_keyword_news(keyword):
    naver_items, naver_err = fetch_naver_news(keyword, display=6)
    google_items, google_err = fetch_google_news_rss(keyword, max_items=6)
    boan_items, boan_err = fetch_boannews(keywords=[keyword], max_items=6)
    etnews_items, etnews_err = fetch_etnews_rss(keywords=[keyword], max_items=6)
    return naver_items, google_items, boan_items, etnews_items, naver_err, google_err, boan_err, etnews_err


@st.cache_data(ttl=180)
def _cached_fetch_keyword_news_10(keyword):
    """=== 수정 #7: 뉴스 수집처별 보기 전용 — 플랫폼당 10건, ttl을 짧게(3분) 둬서 실시간성 확보 ===
    ↓ 건수를 바꾸려면 display/max_items 숫자만, 캐시 주기를 바꾸려면 ttl 숫자(초)만 수정하면 됨."""
    naver_items, _ = fetch_naver_news(keyword, display=10)
    google_items, _ = fetch_google_news_rss(keyword, max_items=10)
    boan_items, _ = fetch_boannews(keywords=[keyword], max_items=10)
    etnews_items, _ = fetch_etnews_rss(keywords=[keyword], max_items=10)
    return naver_items, google_items, boan_items, etnews_items


@st.cache_data(ttl=86400)
def _cached_recommend_keywords(sample_titles_tuple):
    return recommend_keywords(list(sample_titles_tuple))


def _render_news_row(it, rank=None, max_width="70%"):
    rank_html = f'<span style="color:{C["text_muted"]};font-weight:700;margin-right:6px;">{rank}.</span>' if rank else ""
    sc = it.get("_score", -1)
    sc_txt = f"{sc}점" if sc >= 0 else "분석실패"
    row_col, info_col = st.columns([12, 1])
    with row_col:
        st.markdown(
            f'<div class="news-row">{rank_html}{_badge_html(it["_src"])}{_title_link_html(it, max_width=max_width)}'
            f'<span style="font-size:0.78em;color:{C["text_muted"]};margin-left:8px;">🎯 {sc_txt}</span></div>',
            unsafe_allow_html=True,
        )
    with info_col:
        with st.popover("🔍"):
            st.markdown(f"**{escape(str(it.get('title','')))}**")
            st.caption(f"출처: {SOURCE_BADGE_TEXT.get(it.get('_src'), it.get('_src'))} · AI 연관도 {sc_txt}")
            url = it.get("url") or it.get("link") or "#"
            st.markdown(f"[🔗 새 탭에서 원문 열기]({url})")


def _fetch_news_pool_for_keywords(keywords_list):
    pool = []
    for kw in keywords_list:
        naver_items, google_items, boan_items, etnews_items, *_errs = _cached_fetch_keyword_news(kw)
        items = _tag(google_items, "google") + _tag(naver_items, "naver") + _tag(boan_items, "boan") + _tag(etnews_items, "etnews")
        items, _ = _score_group(items)
        pool.extend(items)
    seen_t = set()
    dedup = []
    for it in pool:
        if it["title"] not in seen_t:
            seen_t.add(it["title"])
            dedup.append(it)
    return dedup


# ------------------------------------------------------------
# 대탭
# ------------------------------------------------------------
main_tab_integrated, main_tab_dash, main_tab_news, main_tab_trend = st.tabs(
    ["🔗 통합보기", "📋 사업/R&D과제", "📰 IT 뉴스", "🧭 솔루션 분석"]
)

# ------------------------------------------------------------
# 사업/R&D과제 대탭 (변경 없음)
# ------------------------------------------------------------
with main_tab_dash:
    st.markdown(
        f"""
        <div style="background:#191F28;border-radius:12px;padding:16px 20px;margin-bottom:8px;">
            <div style="font-size:10px;font-weight:700;letter-spacing:.12em;color:#9DB4FF;">GOV-TRACKER · 사업/R&D과제</div>
            <div style="font-size:19px;font-weight:800;color:#FFFFFF;margin-top:3px;">정부 IT 사업 AI 분석</div>
            <div style="font-size:12px;color:#C9D4E2;margin-top:4px;">매일 아침 8시 수집된 공고를 AI가 분류·점수화한 전체 현황입니다.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    _rnd_cnt_all = int((filtered["_track"] == TRACK_RND).sum())
    _biz_cnt_all = int((filtered["_track"] == TRACK_BIZ).sum())
    _total_cnt_all = len(filtered)
    _score_valid_all = filtered[filtered[COL_AI_SCORE] >= 0]
    _avg_score_all = f"{_score_valid_all[COL_AI_SCORE].mean():.0f}점" if not _score_valid_all.empty else "-"

    st.markdown(
        f"""
        <div class="gt-report-sheet">
            <div class="gt-report-meta">GOV-TRACKER · PORTFOLIO SNAPSHOT</div>
            <div class="gt-stat-grid">
                <div class="gt-stat-box"><div class="gt-stat-num">{_total_cnt_all}건</div><div class="gt-stat-label">전체 공고·과제</div></div>
                <div class="gt-stat-box"><div class="gt-stat-num">{_rnd_cnt_all}건</div><div class="gt-stat-label">R&D 과제</div></div>
                <div class="gt-stat-box"><div class="gt-stat-num">{_biz_cnt_all}건</div><div class="gt-stat-label">사업부 과제</div></div>
                <div class="gt-stat-box"><div class="gt-stat-num">{_avg_score_all}</div><div class="gt-stat-label">평균 AI 연관도</div></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(f"<div style='font-size:15px;font-weight:800;color:{C['text']};margin:12px 0 6px;'>사업 구분 필터</div>", unsafe_allow_html=True)
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
                            on_click=_reset_track)
    with t2:
        render_toggle_card(TRACK_RND, rnd_in_filtered, "trk_rnd",
                            st.session_state.track_filter == TRACK_RND, PALETTE_RND,
                            on_click=_toggle_track, args=(TRACK_RND,))
    with t3:
        render_toggle_card(TRACK_BIZ, biz_in_filtered, "trk_biz",
                            st.session_state.track_filter == TRACK_BIZ, PALETTE_BIZ,
                            on_click=_toggle_track, args=(TRACK_BIZ,))

    st.caption("💡 각 공고를 클릭하면 AI가 왜 R&D/사업부로 구분했는지 판단 근거를 함께 확인할 수 있습니다.")

    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        track_counts = filtered["_track"].value_counts().reset_index()
        track_counts.columns = ["track", "count"]
        if not track_counts.empty:
            fig_track = px.pie(
                track_counts, names="track", values="count", hole=0.55,
                color="track",
                color_discrete_map={TRACK_RND: C['accent'], TRACK_BIZ: C['success_border']},
            )
            fig_track.update_layout(
                margin=dict(l=10, r=10, t=10, b=10), height=260,
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=C['text_body']), showlegend=True,
                legend=dict(font=dict(color=C['text_body'])),
            )
            st.markdown("**사업부 vs R&D 비율**")
            st.plotly_chart(fig_track, use_container_width=True)
    with chart_col2:
        kw_series = filtered[COL_KEYWORDS].dropna().astype(str)
        kw_counter = {}
        for raw in kw_series:
            for piece in re.split(r"[,/;·]", raw):
                p = piece.strip()
                if p and p not in ("nan", "None"):
                    kw_counter[p] = kw_counter.get(p, 0) + 1
        if kw_counter:
            kw_df = pd.DataFrame(sorted(kw_counter.items(), key=lambda x: -x[1])[:15], columns=["keyword", "count"])
            fig_kw = px.bar(
                kw_df, x="keyword", y="count", text="count",
                color_discrete_sequence=[C['accent']],
            )
            fig_kw.update_traces(textposition="outside")
            fig_kw.update_layout(
                margin=dict(l=10, r=10, t=20, b=10), height=260,
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=C['text_body']),
                xaxis_title="AI 추천 키워드", yaxis_title="노출 횟수",
                xaxis=dict(tickangle=-30),
            )
            st.markdown("**AI 추천 키워드 적중 분포**")
            st.plotly_chart(fig_kw, use_container_width=True)
        else:
            st.markdown("**AI 추천 키워드 적중 분포**")
            st.info("매칭된 키워드 데이터가 없습니다.")

    tab_filtered = filtered
    if st.session_state.track_filter:
        tab_filtered = tab_filtered[tab_filtered["_track"] == st.session_state.track_filter]

    tab_filtered = tab_filtered.sort_values(COL_AI_SCORE, ascending=False)

    soon_mask = (
        tab_filtered["_due_date_parsed"].notna()
        & (tab_filtered["_due_date_parsed"] >= today)
        & (tab_filtered["_due_date_parsed"] <= today + timedelta(days=3))
    )
    in_progress_count = (tab_filtered[COL_STATUS] == "진행중").sum()
    high_grade_count = (tab_filtered[COL_AI_SCORE] >= 60).sum()
    soon_count = soon_mask.sum()
    total_count = len(tab_filtered)

    st.markdown("---")

    def _reset_quick():
        st.session_state.quick_filter = None

    def _toggle_quick(value):
        st.session_state.quick_filter = None if st.session_state.quick_filter == value else value

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        render_toggle_card("전체 공고", total_count, "card_total",
                            st.session_state.quick_filter is None, PALETTE_FILTER_TOTAL,
                            on_click=_reset_quick)
    with c2:
        render_toggle_card("진행 중인 공고", in_progress_count, "card_in_progress",
                            st.session_state.quick_filter == "in_progress", PALETTE_PROGRESS,
                            on_click=_toggle_quick, args=("in_progress",))
    with c3:
        render_toggle_card("관련 높은 공고", high_grade_count, "card_high_grade",
                            st.session_state.quick_filter == "high_grade", PALETTE_HIGH_GRADE,
                            on_click=_toggle_quick, args=("high_grade",))
    with c4:
        render_toggle_card("마감 3일 이내", soon_count, "card_due_soon",
                            st.session_state.quick_filter == "due_soon", PALETTE_DUE_SOON,
                            on_click=_toggle_quick, args=("due_soon",))

    if st.session_state.quick_filter:
        label_map = {"in_progress": "진행중 공고", "high_grade": "AI 연관도 60점 이상 공고", "due_soon": "마감 3일 이내 공고"}
        st.info(f"🔎 현재 '{label_map[st.session_state.quick_filter]}' 만 보고 있습니다. 카드를 다시 누르면 해제됩니다.")

    display_df = tab_filtered.copy()
    if st.session_state.quick_filter == "in_progress":
        display_df = display_df[display_df[COL_STATUS] == "진행중"]
    elif st.session_state.quick_filter == "high_grade":
        display_df = display_df[display_df[COL_AI_SCORE] >= 60]
    elif st.session_state.quick_filter == "due_soon":
        display_df = display_df[soon_mask]

    display_df = display_df.sort_values([COL_AI_SCORE, "_reg_date_parsed"], ascending=[False, False])
    display_df = display_df.reset_index(drop=True)
    st.markdown("---")

    tab_detail, tab_summary = st.tabs(["📑 상세보기", "⭐ AI핵심요약"])

    def _full_list_table_html(table_df):
        if table_df.empty:
            return f'<div style="padding:16px;color:{C["text_muted"]};">조건에 맞는 공고가 없습니다.</div>'

        head = (
            f'<div style="display:grid;grid-template-columns:2.6fr 1fr 1fr 0.7fr 0.9fr 0.9fr 1fr;'
            f'background:{C["navy"]};color:{C["navy_text"]};font-size:11px;font-weight:700;">'
            f'<div style="padding:9px 12px;">공고명/과제명</div><div style="padding:9px 12px;">주관기관</div>'
            f'<div style="padding:9px 12px;">공고기관</div><div style="padding:9px 12px;">지역</div><div style="padding:9px 12px;">마감일</div>'
            f'<div style="padding:9px 12px;">예산</div><div style="padding:9px 12px;">AI연관도</div></div>'
        )
        body = ""
        for _, r in table_df.iterrows():
            budget_txt = format_budget_eok(r.get(COL_BUDGET)) if COL_BUDGET in table_df.columns else None
            body += (
                f'<div style="display:grid;grid-template-columns:2.6fr 1fr 1fr 0.7fr 0.9fr 0.9fr 1fr;'
                f'border-bottom:1px solid {C["row_border"]};background:{C["surface"]};">'
                f'<div style="padding:9px 12px;"><a href="{escape(str(r.get(COL_URL) or "#"))}" target="_blank" '
                f'style="color:{C["text"]};font-weight:600;font-size:12px;text-decoration:none;">{escape(str(r[COL_TITLE]))}</a></div>'
                f'<div style="padding:9px 12px;font-size:11.5px;color:{C["text_body"]};">{escape(str(r.get(COL_DEPT,"-") or "-"))}</div>'
                f'<div style="padding:9px 12px;font-size:11.5px;color:{C["text_body"]};">{escape(str(r.get(COL_AGENCY,"-") or "-"))}</div>'
                f'<div style="padding:9px 12px;font-size:11.5px;color:{C["text_body"]};">{escape(str(r.get("_region_label") or "-"))}</div>'
                f'<div style="padding:9px 12px;font-size:11.5px;color:{C["text_body"]};">{escape(str(r[COL_DUE_DATE] or "미정"))}</div>'
                f'<div style="padding:9px 12px;font-size:11.5px;color:{C["text_body"]};">{budget_txt or "-"}</div>'
                f'<div style="padding:9px 12px;">{score_badge_html(r.get(COL_AI_SCORE,-1))}</div></div>'
            )
        return f'<div style="border:1px solid {C["border"]};border-radius:10px;overflow:hidden;max-height:640px;overflow-y:auto;">{head}{body}</div>'

    with tab_detail:
        st.markdown(_full_list_table_html(display_df), unsafe_allow_html=True)
        st.caption("💡 공고명을 클릭하면 바로 원문 공고로 이동합니다.")

    with tab_summary:
        st.subheader("⭐ AI핵심요약")

        PRIORITY_THRESHOLD = 60
        priority_df = display_df[display_df[COL_AI_SCORE] >= PRIORITY_THRESHOLD].copy()

        if priority_df.empty:
            priority_df = display_df[display_df[COL_GRADE] == "상"].copy()

        priority_df = priority_df.sort_values(COL_AI_SCORE, ascending=False)

        refresh_summary = st.button("🔄 새로고침", key="refresh_ai_summary_all")
        if refresh_summary:
            if is_ai_ready() and not priority_df.empty:
                progress = st.progress(0.0, text="AI 요약 생성 중...")
                total = len(priority_df)
                for i, (_, r) in enumerate(priority_df.iterrows(), start=1):
                    generate_ai_summary(r)
                    progress.progress(i / total, text=f"AI 요약 생성 중... ({i}/{total})")
                progress.empty()
            elif not is_ai_ready():
                st.warning("Claude API 키가 설정되지 않아 요약을 생성할 수 없습니다.")

        # 요약은 아침 자동수집 때 미리 만들어 DB에 저장됨 → 화면을 열 때 AI를 다시 부르지 않음
        for _, _r in priority_df.iterrows():
            _sv = _stored(_r, "ai_summary")
            if _sv:
                st.session_state.ai_summary_cache.setdefault(_r.get(COL_KEY) or _r.get(COL_TITLE), _sv)

        if priority_df.empty:
            st.info("표시할 공고가 없습니다.")
        else:
            for _, row in priority_df.iterrows():
                score = row.get(COL_AI_SCORE, -1)
                due = row[COL_DUE_DATE] if pd.notna(row[COL_DUE_DATE]) else "미정"

                with st.container(border=True):
                    st.markdown(f"[{row[COL_TITLE]}]({row[COL_URL]})")
                    budget_txt = format_budget_eok(row.get(COL_BUDGET))
                    budget_part = f" · 예산 {budget_txt}" if budget_txt else ""
                    st.markdown(
                        f'<span style="color:{C["text_muted"]};font-size:0.82em;">{row[COL_AGENCY]} · 등급 {row[COL_GRADE]} · 마감 {due}{budget_part}</span>'
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
                        st.error("요약 생성 중 오류가 발생했습니다. 새로고침 버튼을 눌러 다시 시도해 주세요.")
                    else:
                        st.caption("🤖 아직 요약이 없습니다. 위 '🔄 새로고침'을 누르면 지금 생성합니다.")

    st.markdown("---")
    st.caption("본 대시보드는 매일 아침 8시 자동 수집 데이터를 기준으로 표시합니다. 새로고침(F5) 또는 오른쪽 상단 ⟳ 버튼으로 최신화할 수 있습니다.")

# ------------------------------------------------------------
# IT 뉴스 대탭
# ------------------------------------------------------------
with main_tab_news:
    st.markdown(
        f"""
        <div style="background:#191F28;border-radius:12px;padding:16px 20px;margin-bottom:8px;">
            <div style="font-size:10px;font-weight:700;letter-spacing:.12em;color:#9DB4FF;">뉴스 및 미디어 모니터링</div>
            <div style="font-size:19px;font-weight:800;color:#FFFFFF;margin-top:3px;">IT 뉴스 브리핑</div>
            <div style="font-size:12px;color:#C9D4E2;margin-top:4px;">Google·네이버·보안뉴스·전자신문 4개 소스를 수집하고, AI 연관도 기준으로 정리했습니다.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.container(key="fixed_kw_row"):
        st.markdown("**📌 고정 키워드**")
        fixed_cols = st.columns(len(FIXED_NEWS_KEYWORDS) + 4)
        for i, fkw in enumerate(FIXED_NEWS_KEYWORDS):
            with fixed_cols[i]:
                if st.button(f"📌 {fkw}", key=f"fixed_kw_{i}", use_container_width=True):
                    if fkw not in st.session_state.news_selected_keywords:
                        st.session_state.news_selected_keywords.append(fkw)
                        st.rerun()

    st.markdown("**🤖 AI 추천 키워드** — 누르면 해당 키워드 뉴스를 실시간으로 모아 봅니다.")
    news_rec = REC_KEYWORDS[:8]
    with st.container(key="rec_kw_chip_row"):
        if news_rec:
            chip_cols = st.columns(len(news_rec))
            for i, rec in enumerate(news_rec):
                with chip_cols[i]:
                    is_selected = rec["keyword"] in st.session_state.news_selected_keywords
                    chip_label = f"✓ {rec['keyword']}" if is_selected else f"➕ {rec['keyword']}"
                    if st.button(chip_label, key=f"rec_kw_{i}", help=rec.get("reason", ""),
                                 use_container_width=True, type="primary" if is_selected else "secondary"):
                        if is_selected:
                            st.session_state.news_selected_keywords.remove(rec["keyword"])
                        else:
                            st.session_state.news_selected_keywords.append(rec["keyword"])
                        st.rerun()
        else:
            st.caption("추천 키워드가 아직 없습니다. (아침 자동수집 후 표시됩니다)")

    if st.session_state.news_selected_keywords:
        sel_col1, sel_col2 = st.columns([5, 1])
        with sel_col1:
            st.caption("선택된 키워드: " + ", ".join(st.session_state.news_selected_keywords))
        with sel_col2:
            if st.button("🧹 초기화", key="reset_news_kw", use_container_width=True):
                st.session_state.news_selected_keywords = []
                st.rerun()

    keywords = st.session_state.news_selected_keywords
    base_query_kws = keywords if keywords else list(DEFAULT_NEWS_KEYWORDS)

    # 키워드를 따로 고르지 않았으면 → 아침에 미리 모아 둔 뉴스 사용 (즉시 표시)
    _stored_pool = brief(K_NEWS_DEFAULT)
    use_stored_news = (not keywords) and isinstance(_stored_pool, list) and len(_stored_pool) > 0
    if use_stored_news:
        all_items_pool = [it for it in _stored_pool if isinstance(it, dict) and it.get("title")]
        st.caption(f"🕗 아침 자동수집 결과 ({brief_time(K_NEWS_DEFAULT) or '-'} 기준) · 키워드를 고르면 실시간으로 다시 모읍니다.")
    else:
        with st.spinner("정보 수집 중..."):
            all_items_pool = _fetch_news_pool_for_keywords(base_query_kws)
    all_titles_for_digest = list(dict.fromkeys(it["title"] for it in all_items_pool))
    st.session_state["all_titles_for_digest"] = all_titles_for_digest
    st.session_state["all_items_pool_cache"] = all_items_pool

    _stored_digest = brief(K_DIGEST)
    if use_stored_news and _stored_digest:
        st.markdown("##### 🤖 오늘의 IT 뉴스 AI 요약")
        st.success(_stored_digest)
        digest_clicked = False
    else:
        digest_clicked = st.button("🤖 오늘의 IT 뉴스 AI 요약 생성", use_container_width=False)
    if digest_clicked:
        if not is_ai_ready():
            st.warning("Claude API 키가 설정되지 않아 요약할 수 없습니다.")
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

    if not is_naver_ready():
        st.warning("⚠️ NAVER_CLIENT_ID / NAVER_CLIENT_SECRET이 설정되지 않아 네이버 뉴스는 비어서 표시됩니다.")

    st.markdown("---")

    mc1, mc2, mc3 = st.columns(3)

    total_mentions = len(all_items_pool)
    with mc1:
        st.markdown(
            f"""
            <div class="gt-mon-card">
                <div class="gt-mon-card-label">Mentions · 수집 기사</div>
                <div class="gt-mon-card-value">{total_mentions}건</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # === 수정 #4: mc2를 실제 container(key=...)로 교체 — 기존엔 markdown div가 다른 markdown 호출에서 닫혀 박스가 제대로 안 그려졌음 ===
    with mc2:
        kw_box2 = st.container(key="mc2_kw_box")
        with kw_box2:
            st.markdown('<div class="gt-mon-card-label">상위 키워드</div>', unsafe_allow_html=True)
            kw_hit_counts = {}
            for it in all_items_pool:
                for kw in base_query_kws:
                    if kw.lower() in it["title"].lower():
                        kw_hit_counts[kw] = kw_hit_counts.get(kw, 0) + 1
            if not kw_hit_counts:
                st.caption("아직 집계된 키워드가 없습니다.")
            else:
                max_hit = max(kw_hit_counts.values())
                KW_BAR_COLORS = [C['accent'], C['success_text'], C['warn_text'], C['danger_text'], C['text_muted']]
                for i, (kw, cnt) in enumerate(sorted(kw_hit_counts.items(), key=lambda x: -x[1])):
                    # === 정확한 비율: 최댓값 기준 pct 계산, 0건은 막대 0px로 비워둠 ===
                    pct = int(round(cnt / max_hit * 100)) if max_hit else 0
                    color = KW_BAR_COLORS[i % len(KW_BAR_COLORS)]
                    st.markdown(
                        f"""
                        <div class="gt-mon-bar-row">
                            <span style="width:62px;flex-shrink:0;font-weight:700;color:{color};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{escape(kw)}</span>
                            <span class="gt-mon-bar-track"><span class="gt-mon-bar-fill" style="width:{max(pct,4)}%;background:{color};"></span></span>
                            <span style="width:36px;text-align:right;flex-shrink:0;">{cnt}건</span>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

    with mc3:
        comp_box = st.container(key="mc3_comp_box")
        with comp_box:
            st.markdown('<div class="gt-mon-card-label">경쟁사 동향</div>', unsafe_allow_html=True)
            with st.popover("⚙️ 경쟁사 키워드 관리"):
                if "competitor_keywords" not in st.session_state:
                    st.session_state.competitor_keywords = list(COMPETITOR_DEFAULT)
                comp_kw_text = st.text_area("쉼표로 구분 입력 (영/한 둘 다 등록해도 되고, 하나만 입력해도 자동 매칭됩니다)", value=", ".join(st.session_state.competitor_keywords), height=70)
                if st.button("저장", key="save_comp_kw"):
                    st.session_state.competitor_keywords = [k.strip() for k in comp_kw_text.split(",") if k.strip()]
                    st.rerun()
            competitor_keywords = st.session_state.get("competitor_keywords", COMPETITOR_DEFAULT)
            # === is_competitor_match가 COMPETITOR_ALIASES로 영/한 자동 치환하므로 키워드 하나만 등록해도 매칭됨 ===
            cp_matches = [it for it in all_items_pool if is_competitor_match(it["title"], competitor_keywords)]
            if cp_matches:
                for it in sorted(cp_matches, key=lambda x: -x.get("_score", -1))[:4]:
                    t_raw = _news_pub_dt(it)
                    st.markdown(
                        mon_row_html(SOURCE_BADGE_TEXT.get(it["_src"], it["_src"]), SOURCE_COLOR.get(it["_src"], "#888"),
                                     it["title"], it.get("url") or it.get("link"), "risk", _relative_time(t_raw)),
                        unsafe_allow_html=True,
                    )
            else:
                st.caption("관련 기사 없음")

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    ranked = sorted(all_items_pool, key=lambda x: -x.get("_score", -1))
    _stored_src10 = brief(K_NEWS_SRC10)
    if use_stored_news and isinstance(_stored_src10, dict) and _stored_src10:
        src_pool_map = {k: [it for it in (_stored_src10.get(k) or []) if isinstance(it, dict)]
                        for k in ["google", "naver", "boan", "etnews"]}
    else:
        src_query_kw = (base_query_kws[0] if base_query_kws else "AI")
        naver_10, google_10, boan_10, etnews_10 = _cached_fetch_keyword_news_10(src_query_kw)
        src_pool_map = {
            "google": _tag(google_10, "google"),
            "naver": _tag(naver_10, "naver"),
            "boan": _tag(boan_10, "boan"),
            "etnews": _tag(etnews_10, "etnews"),
        }

    _stored_simple = brief(K_NEWS_SIMPLE)
    if isinstance(_stored_simple, dict):
        for _k, _v in _stored_simple.items():
            st.session_state.news_simple_cache.setdefault(_k, _v)
    if not use_stored_news:   # 실시간으로 모은 뉴스만 그 자리에서 쉬운말 변환
        _simplify_batch = list(ranked[:10])
        for _src_key in ["google", "naver", "boan", "etnews"]:
            _simplify_batch.extend(src_pool_map[_src_key][:10])
        ensure_news_simple(_simplify_batch, limit=50)


    def _display_title(it):
        # 캐시에 쉬운말이 있으면 그걸 보여주고, 없으면 원문 제목. 링크는 항상 원문 URL로 연결됨.
        return get_news_simple(it) or it.get("title", "(제목 없음)")


    # 2) TOP 10 뉴스 — 단독 섹션, 접기/펼치기
    with st.expander("🔴 TOP 10 뉴스", expanded=True):
        seen = set()
        shown = 0
        rows_html = ""
        for it in ranked:
            if it["title"] in seen:
                continue
            seen.add(it["title"])
            t_raw = _news_pub_dt(it)
            rows_html += mon_row_html(
                SOURCE_BADGE_TEXT.get(it["_src"], it["_src"]), SOURCE_COLOR.get(it["_src"], "#888"),
                _display_title(it), it.get("url") or it.get("link"), get_news_status(it), _relative_time(t_raw),
                orig_title=it.get("title"),
            )
            shown += 1
            if shown >= 10:
                break
        if shown == 0:
            rows_html = '<div style="padding:20px;text-align:center;color:' + C['text_muted'] + ';font-size:13px;">아직 분석된 뉴스가 없습니다. 추천 키워드를 클릭해 주세요.</div>'
        st.markdown(f'<div class="gt-mon-main">{rows_html}</div>', unsafe_allow_html=True)

    # 3) 전체 뉴스보기 — 별도 섹션, 독립적으로 접기/펼치기 (기본 접힌 상태)
    with st.expander("📡 전체 뉴스보기 (플랫폼당 10건 · 당일 09:00 이후 실시간 갱신)", expanded=False):
        src_cols = st.columns(4)
        for i, src_key in enumerate(["google", "naver", "boan", "etnews"]):
            with src_cols[i]:
                st.markdown(
                    f"""
                    <div class="gt-mon-card-label" style="margin-bottom:6px;">
                        <span class="gt-mon-dot" style="background:{SOURCE_COLOR[src_key]};display:inline-block;margin-right:5px;"></span>
                        {SOURCE_LABEL_KO[src_key]}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                raw_items = src_pool_map[src_key]
                windowed = [it for it in raw_items if _within_collection_window(_news_pub_dt(it))]
                src_items = _dedup_by_title(windowed if windowed else raw_items)[:10]
                if not src_items:
                    st.caption("수집된 기사 없음")
                else:
                    rows_html_src = '<div class="gt-mon-main">'
                    for it in src_items:
                        t_raw = it.get("pubDate") or it.get("pub_date")
                        rows_html_src += mon_row_plain_html(
                            _display_title(it),
                            it.get("url") or it.get("link"),
                            get_news_status({**it, "_score": -1}),
                            _relative_time(t_raw),
                            orig_title=it.get("title"),
                        )
                    rows_html_src += "</div>"
                    st.markdown(rows_html_src, unsafe_allow_html=True)
#------------------------------------------------------------
#솔루션 분석 대탭 — 변경 없음 (기존 로직 유지)
#------------------------------------------------------------
with main_tab_trend:
    if last_updated:
        st.caption(f"마지막 데이터 갱신: {last_updated.strftime('%Y-%m-%d %H:%M')}")

    def _is_within_1day(pub_val):
        try:
            t = pd.Timestamp(pub_val)
            if pd.isna(t):
                return False
            if t.tzinfo is not None:
                t = t.tz_localize(None)
            return (pd.Timestamp(datetime.now()) - t) <= timedelta(days=1)
        except Exception:
            return False

    _stored_sol = brief(K_NEWS_SOLUTION)
    if isinstance(_stored_sol, list) and _stored_sol:
        solution_news_pool_all = [it for it in _stored_sol if isinstance(it, dict) and it.get("title")]
    else:
        solution_news_pool_all = _fetch_news_pool_for_keywords(list(SOLUTION_NEWS_KEYWORDS))
    solution_news_pool = [n for n in solution_news_pool_all if _is_within_1day(_news_pub_dt(n))]
    st.caption(f"📰 뉴스는 최근 24시간 이내 기사만 반영합니다. (대상 {len(solution_news_pool)}건 / 전체 수집 {len(solution_news_pool_all)}건"
               + (f" · 아침 자동수집 {brief_time(K_NEWS_SOLUTION)} 기준)" if isinstance(_stored_sol, list) and _stored_sol else ")"))

    def _count_postings_for(keywords_list):
        pat = "|".join(re.escape(k) for k in keywords_list)
        mask = (
            df[COL_TITLE].astype(str).str.contains(pat, case=False, na=False)
            | df[COL_KEYWORDS].astype(str).str.contains(pat, case=False, na=False)
        )
        return df[mask]

    def _count_news_for(keywords_list):
        return [it for it in solution_news_pool if any(k.lower() in it["title"].lower() for k in keywords_list)]

    solution_rows = []
    for sname, sinfo in PRODUCT_KEYWORDS.items():
        p_rows = _count_postings_for(sinfo["keywords"])
        n_rows = _count_news_for(sinfo["keywords"])
        solution_rows.append({
            "name": sname, "desc": sinfo["desc"],
            "posting_rows": p_rows, "news_rows": n_rows,
            "total": len(p_rows) + len(n_rows),
        })

    st.markdown("#### 자사 솔루션별 수집 현황 (공고·개발과제·최근 1일 뉴스 통합)")
    sol_cols = st.columns(4)
    SOL_COLORS = [C['accent'], C['success_text'], C['warn_text'], C['danger_text']]
    for i, srow in enumerate(solution_rows):
        with sol_cols[i]:
            st.markdown(
                f"""
                <div class="gt-mon-card" style="border-left:4px solid {SOL_COLORS[i % len(SOL_COLORS)]};">
                    <div class="gt-mon-card-label">{escape(srow['name'])}</div>
                    <div class="gt-mon-card-value">{srow['total']}건</div>
                    <div style="font-size:11px;color:{C['text_muted']};margin-top:4px;">{escape(srow['desc'])}</div>
                    <div style="font-size:11px;color:{C['text_muted']};margin-top:6px;">공고 {len(srow['posting_rows'])}건 · 뉴스(1일) {len(srow['news_rows'])}건</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    kw_chart_df = pd.DataFrame({
        "solution": [s["name"] for s in solution_rows],
        "count": [s["total"] for s in solution_rows],
    })
    fig_sol = px.bar(
        kw_chart_df, x="solution", y="count", text="count",
        color="solution",
        color_discrete_sequence=SOL_COLORS,
    )
    fig_sol.update_traces(textposition="outside")
    fig_sol.update_layout(
        margin=dict(l=10, r=10, t=20, b=10), height=260,
        paper_bgcolor=C['surface'], plot_bgcolor=C['surface'],
        font=dict(color=C['text_body']), showlegend=False,
        xaxis_title="자사 솔루션", yaxis_title="수집 건수(공고+뉴스)",
        xaxis=dict(gridcolor=C['border']), yaxis=dict(gridcolor=C['border']),
    )
    st.plotly_chart(fig_sol, use_container_width=True)

    st.markdown("---")

    st.markdown("#### 🏆 자사 솔루션 연관도 TOP 10 (공고·개발·최근1일뉴스 통합)")

    top_candidates = []
    seen_titles_top = set()
    for srow in solution_rows:
        for _, r in srow["posting_rows"].iterrows():
            if r[COL_TITLE] in seen_titles_top:
                continue
            seen_titles_top.add(r[COL_TITLE])
            budget_txt = format_budget_eok(r.get(COL_BUDGET))
            meta_txt = f"{r.get(COL_AGENCY, '')} · 마감 {r[COL_DUE_DATE] if pd.notna(r[COL_DUE_DATE]) else '미정'}"
            if budget_txt:
                meta_txt += f" · 예산 {budget_txt}"
            top_candidates.append({
                "type": "공고", "solution": srow["name"],
                "title": r[COL_TITLE], "url": r.get(COL_URL) or "#",
                "score": r.get(COL_AI_SCORE, -1) if r.get(COL_AI_SCORE, -1) >= 0 else 50,
                "meta": meta_txt,
            })
        for it in srow["news_rows"]:
            if it["title"] in seen_titles_top:
                continue
            seen_titles_top.add(it["title"])
            top_candidates.append({
                "type": "뉴스", "solution": srow["name"],
                "title": it["title"], "url": it.get("url") or it.get("link") or "#",
                "score": it.get("_score", 50),
                "meta": SOURCE_LABEL_KO.get(it["_src"], it["_src"]) + " · 최근 1일",
            })

    top_candidates = sorted(top_candidates, key=lambda x: -x["score"])[:10]

    if not top_candidates:
        st.info("아직 자사 솔루션과 관련된 수집 데이터가 충분하지 않습니다. (뉴스는 최근 1일 이내만 집계됩니다)")
    else:
        for i, cand in enumerate(top_candidates, start=1):
            type_color = C['accent'] if cand["type"] == "공고" else C['success_text']
            st.markdown(
                f"""
                <div style="display:flex;align-items:center;gap:10px;padding:9px 12px;
                            border-bottom:1px solid {C['row_border']};">
                    <span style="color:{C['text_muted']};font-weight:700;width:20px;flex-shrink:0;">{i}</span>
                    <span style="background:{type_color};color:#fff;font-size:10px;font-weight:700;
                                padding:2px 7px;border-radius:5px;flex-shrink:0;">{cand['type']}</span>
                    <span style="background:{C['surface3']};color:{C['text']};font-size:10px;font-weight:700;
                                padding:2px 7px;border-radius:5px;flex-shrink:0;">{escape(cand['solution'])}</span>
                    <a href="{escape(cand['url'])}" target="_blank" title="{escape(cand['title'])}"
                        style="flex:1;color:{C['text']};font-weight:600;font-size:13px;text-decoration:none;
                        overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;">{escape(cand['title'])}</a>
                    <span style="font-size:11px;color:{C['text_muted']};flex-shrink:0;">{escape(str(cand['meta']))}</span>
                    {score_badge_html(cand['score'])}
                </div>
                """,
                unsafe_allow_html=True,
            )
    # ------------------------------------------------------------
    # [신규] 조달 결과 — 낙찰·계약 (최근 30일, IT 관련만)
    #   경쟁사 수주 / 자사 솔루션 관련 사업을 한눈에 → 영업 타깃·가격 근거로 활용
    # ------------------------------------------------------------
    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
    st.markdown("#### 🏆 조달 결과 — 낙찰·계약 (최근 30일)")
    res_df = _cached_procurement_results()
    if res_df.empty:
        st.info("아직 수집된 낙찰·계약 결과가 없습니다. (조달청 낙찰·계약 API 활용신청 승인 후 다음 자동수집부터 표시)")
    else:
        _comp_kws = st.session_state.get("competitor_keywords", COMPETITOR_DEFAULT)
        _sol_kws = [k.lower() for info in PRODUCT_KEYWORDS.values() for k in info["keywords"]]
        res_df = res_df.fillna("")
        res_df["_comp"] = res_df["company"].map(lambda c: is_competitor_match(c, _comp_kws))
        res_df["_sol"] = res_df["title"].map(lambda t: any(k in str(t).lower() for k in _sol_kws))

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("낙찰", f"{(res_df['kind'] == '낙찰').sum()}건")
        k2.metric("계약", f"{(res_df['kind'] == '계약').sum()}건")
        k3.metric("🔴 경쟁사 수주", f"{int(res_df['_comp'].sum())}건")
        k4.metric("🟢 자사 솔루션 관련", f"{int(res_df['_sol'].sum())}건")

        only_hot = st.toggle("경쟁사·자사 관련 건만 보기", value=False, key="proc_hot_only")
        view = res_df[res_df["_comp"] | res_df["_sol"]] if only_hot else res_df
        view = view.assign(
            표시=["🔴 경쟁사" if c else ("🟢 자사관련" if so else "") for c, so in zip(view["_comp"], view["_sol"])],
            금액=view["amount"].map(lambda v: format_budget_eok(v) or "-"),
        )
        if view.empty:
            st.caption("해당 조건의 결과가 없습니다.")
        st.dataframe(
            view[["표시", "kind", "event_date", "title", "agency", "company", "금액", "url"]].rename(columns={
                "kind": "구분", "event_date": "일자", "title": "사업명", "agency": "수요기관",
                "company": "수주업체", "url": "링크",
            }),
            hide_index=True, use_container_width=True, height=420,
            column_config={"링크": st.column_config.LinkColumn("링크", display_text="열기")},
        )
        st.caption("출처: 조달청 나라장터 낙찰정보·계약정보 서비스(공공데이터포털). 사업명에 IT 관련 단어가 있거나 정보화사업으로 표시된 건만 수집.")

    # ------------------------------------------------------------
    # [신규] 🔁 재발주 예상 — 경쟁사가 수주한 사업의 계약 종료 전 선제 영업
    #   예상 발주 시점 = 계약 종료일 − 60일 (낙찰 건은 계약기간 정보가 없어 1년으로 추정)
    # ------------------------------------------------------------
    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
    st.markdown("#### 🔁 재발주 예상 — 경쟁사 수주 사업 (향후 6개월)")
    ro_c1, ro_c2 = st.columns([3, 2])
    with ro_c2:
        ro_include_sol = st.toggle("자사 제품 관련 사업도 함께 보기", value=False, key="reorder_include_sol",
                                   help="경쟁사 수주 건 외에, 사업명에 대기열·예약·수강신청 등이 들어간 사업도 포함합니다.")
    reorder_df = _cached_reorder(tuple(st.session_state.get("competitor_keywords", COMPETITOR_DEFAULT)), ro_include_sol)
    if reorder_df.empty:
        st.info("아직 재발주 예상 건이 없습니다. 낙찰·계약 결과가 쌓이면 자동으로 표시됩니다. "
                "(첫 자동수집 때 최근 1년치 자사 관련 사업을 한 번 조회합니다)")
    else:
        soon_n = int((reorder_df["d_day"] <= 30).sum())
        with ro_c1:
            st.caption(f"총 {len(reorder_df)}건 · 30일 이내 발주 예상 {soon_n}건 — 발주 1~2개월 전 기관 담당자 접촉을 권장합니다.")
        _rv = reorder_df.assign(
            예상발주=[f"{e} (D-{max(int(d), 0)})" if int(d) >= 0 else f"{e} (시점 지남)" for e, d in zip(reorder_df["expected"], reorder_df["d_day"])],
            금액=reorder_df["amount"].map(lambda v: format_budget_eok(v) or "-"),
            계약종료=[f"{d} (추정)" if est == "Y" else d for d, est in zip(reorder_df["end_date"], reorder_df["end_est"])],
        )
        st.dataframe(
            _rv[["표시", "예상발주", "title", "agency", "company", "금액", "계약종료", "url"]].rename(columns={
                "title": "사업명", "agency": "수요기관", "company": "수주업체", "url": "링크"}),
            hide_index=True, use_container_width=True, height=min(420, 38 + 35 * len(_rv)),
            column_config={"링크": st.column_config.LinkColumn("링크", display_text="열기")},
        )
@st.cache_data(ttl=86400, show_spinner=False)
def _daily_headline(day_key, titles_tuple):
    obj, err = generate_headline(list(titles_tuple))
    return obj if (not err and isinstance(obj, dict)) else None


@st.cache_data(ttl=86400, show_spinner=False)
def _daily_issues(day_key, items_txt):
    issues, err = generate_key_issues(items_txt, n=4)
    return issues if (not err and isinstance(issues, list)) else []


# ------------------------------------------------------------
# 통합보기 대탭
# ------------------------------------------------------------
with main_tab_integrated:
    WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]
    now_dt = datetime.now()
    now_str = f"{now_dt.year}년 {now_dt.month}월 {now_dt.day}일({WEEKDAY_KO[now_dt.weekday()]}) {now_dt.strftime('%H:%M')} 기준"

    header_col1, header_col2 = st.columns([6, 1])
    with header_col1:
        st.markdown(
            f"""
            <div style="background:{C['navy']};border-radius:12px;padding:16px 20px;margin-bottom:6px;">
                <div style="font-size:10px;font-weight:700;letter-spacing:.12em;color:#9AA3B2;">GOV-TRACKER · DAILY BRIEFING</div>
                <div style="font-size:19px;font-weight:800;color:{C['navy_text']};margin-top:3px;">IT 동향 및 R&D 통합 보고서</div>
                <div style="font-size:12px;color:#C9D4E2;margin-top:4px;">공공 IT 사업 공고·연구개발 과제·IT 동향을 매일 아침 수집하고, AI가 분류·점수화·요약한 결과입니다.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if last_updated:
            st.caption(f"마지막 데이터 갱신: {last_updated.strftime('%Y-%m-%d %H:%M')}")
    with header_col2:
        st.caption(now_str)
        if st.button("🔄 새로고침", key="refresh_briefing", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    if not is_ai_ready():
        st.warning("Claude API 키가 설정되지 않았습니다. Streamlit Secrets에 ANTHROPIC_API_KEY를 추가해 주세요.")

    # 데이터 필터링 (오늘 수집 및 마감 임박)
    if COL_CREATED_AT in df.columns:
        df["_created_date_parsed"] = pd.to_datetime(df[COL_CREATED_AT], errors="coerce")
        if df["_created_date_parsed"].notna().any():
            today_new_df = df[
                df["_created_date_parsed"].notna() & (df["_created_date_parsed"].dt.date == now_dt.date())
            ].sort_values(COL_AI_SCORE, ascending=False)
        else:
            today_new_df = df[
                df["_reg_date_parsed"].notna() & (df["_reg_date_parsed"].dt.date == now_dt.date())
            ].sort_values(COL_AI_SCORE, ascending=False)
    else:
        today_new_df = df[
            df["_reg_date_parsed"].notna() & (df["_reg_date_parsed"].dt.date == now_dt.date())
        ].sort_values(COL_AI_SCORE, ascending=False)

    due_soon3_df = df[
        df["_due_date_parsed"].notna()
        & (df["_due_date_parsed"] >= pd.Timestamp(now_dt.date()))
        & (df["_due_date_parsed"] <= pd.Timestamp(now_dt.date()) + timedelta(days=3))
    ].sort_values(COL_AI_SCORE, ascending=False)

    due_soon14_n = int(df[
        df["_due_date_parsed"].notna()
        & (df["_due_date_parsed"] >= pd.Timestamp(now_dt.date()))
        & (df["_due_date_parsed"] <= pd.Timestamp(now_dt.date()) + timedelta(days=14))
    ].shape[0])

    need_action_n = int(df[df[COL_AI_SCORE] >= 60].shape[0])
    rnd_df = df[df["_track"] == TRACK_RND]
    biz_df = df[df["_track"] == TRACK_BIZ]

    guide_role = st.radio(
        "보는 사람 역할",
        ("🗂️ 전체", "💼 사업부", "🔬 R&D"),
        horizontal=True,
        key="integrated_guide_role",
        label_visibility="collapsed",
    )

    def _kpi_card(label, value, color="#2D5BFF"):
        st.markdown(
            f"""
            <div class="gt-surface" style="padding:12px 12px 10px;text-align:center;">
                <div style="font-size:22px;font-weight:800;color:{color};">{value}</div>
                <div class="gt-muted" style="font-size:11.5px;margin-top:3px;">{label}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ----------------------------------------------------------------
    # Ⅰ. 핵심 요약 (KPI 및 AI 헤드라인)
    # ----------------------------------------------------------------
    with st.container():
        KPI_BLUE = _tc("#2D5BFF", "#5B9BFF")
        KPI_RED = _tc("#B42318", "#FF6B6B")
        KPI_ORANGE = _tc("#C2410C", "#FFA94D")
        KPI_GREEN = _tc("#0F9D58", "#4ADE80")

        k1, k2, k3, k4 = st.columns(4)
        with k1:
            _kpi_card("오늘 수집 신규 공고", f"{len(today_new_df)}건", KPI_BLUE)
        with k2:
            _kpi_card("대응 필요 공고", f"{need_action_n}건", KPI_RED)
        with k3:
            _kpi_card("D-14 이내 마감", f"{due_soon14_n}건", KPI_ORANGE)
        with k4:
            _kpi_card("연구개발(R&D) 과제", f"{len(rnd_df)}건", KPI_GREEN)

        st.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)

        # 오늘의 헤드라인 — 아침 배치 결과 우선, 없으면 하루 1번(모든 접속자 공용) 실시간 생성
        headline_obj = brief(K_HEADLINE)
        headline_time = brief_time(K_HEADLINE)
        if not isinstance(headline_obj, dict) or not headline_obj.get("headline"):
            headline_obj, headline_time = None, None
            if is_ai_ready() and not df.empty:
                _hl_titles = [r[COL_TITLE] for _, r in pd.concat([today_new_df.head(8), due_soon3_df.head(5)]).iterrows()]
                if len(_hl_titles) < 5:
                    _hl_titles += df.sort_values(COL_AI_SCORE, ascending=False)[COL_TITLE].astype(str).head(12).tolist()
                with st.spinner("AI가 오늘의 헤드라인을 작성하는 중... (하루 1번만)"):
                    headline_obj = _daily_headline(now_dt.strftime("%Y-%m-%d"), tuple(dict.fromkeys(_hl_titles))[:25])
                headline_time = now_dt.strftime("%Y-%m-%d %H:%M")
        st.session_state[f"integrated_headline_{now_dt.date().isoformat()}"] = ("OK", headline_obj) if headline_obj else (None, None)

        if headline_obj:
            st.markdown(
                f"""
                <div class="gt-surface" style="border-left:5px solid {C['accent']};padding:16px 20px;margin-top:4px;">
                    <span style="color:{C["text_muted"]};font-weight:700;border-bottom:2px solid {C["accent"]};padding-bottom:2px;">AI 핵심요약</span>
                    <span class="gt-muted" style="font-size:10.5px;margin-left:8px;">{escape(str(headline_time or '')[5:16])} 생성</span>
                    <p class="gt-text" style="margin-top:10px;margin-bottom:4px;font-size:16px;font-weight:700;line-height:1.4;">{escape(str(headline_obj.get('headline','')))}</p>
                    <p class="gt-body" style="margin-top:6px;margin-bottom:0;font-size:13.5px;line-height:1.75;font-weight:500;color:{C['text_body']};">{escape(str(headline_obj.get('subtext','')))}</p>
                </div>
                """,
                unsafe_allow_html=True,
            )
        elif not is_ai_ready():
            st.caption("AI 헤드라인은 Claude API 키 설정 후 표시됩니다.")

    st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)

    # ----------------------------------------------------------------
    # Ⅱ. 핵심 동향
    # ----------------------------------------------------------------
    with st.expander("Ⅱ. 핵심 동향 — 빈도 × 영향 기준 이슈 테마", expanded=True):
        issues_payload_now = brief(K_ISSUES)
        if not isinstance(issues_payload_now, list) or not issues_payload_now:
            issues_payload_now = []
            if is_ai_ready() and not df.empty:
                _top = df[df[COL_AI_SCORE] >= 60].sort_values(COL_AI_SCORE, ascending=False).head(25)
                if _top.empty:
                    _top = df.sort_values(COL_AI_SCORE, ascending=False).head(25)
                _txt = "\n".join(f"- {r[COL_TITLE]} ({r[COL_AGENCY]})" for _, r in _top.iterrows())
                with st.spinner("AI가 핵심 이슈를 테마별로 묶는 중... (하루 1번만)"):
                    issues_payload_now = _daily_issues(now_dt.strftime("%Y-%m-%d"), _txt)
        issues_payload_now = [i for i in issues_payload_now if isinstance(i, dict)][:4]
        st.session_state[f"integrated_issues_{now_dt.date().isoformat()}"] = ("OK", issues_payload_now)

        if issues_payload_now:
            issue_row_box = st.container(key="gt_issue_cards_row")
            with issue_row_box:
                cols = st.columns(len(issues_payload_now), gap="small")
                for i, issue in enumerate(issues_payload_now):
                    with cols[i]:
                        with st.container(border=True):
                            st.markdown(f"**{escape(str(issue.get('theme','')))}**")
                            meta = " · ".join(x for x in [str(issue.get("impact") or ""), f"{issue.get('count')}건" if issue.get("count") else ""] if x)
                            if meta:
                                st.caption(meta)
                            st.markdown(f"<div style='font-size:12px;'>{escape(str(issue.get('summary','')))}</div>", unsafe_allow_html=True)
        else:
            st.caption("아직 이슈 카드가 없습니다. (아침 자동수집 후 표시됩니다)")


    # ----------------------------------------------------------------
    # Ⅲ. 주요 사업/과제 (현황표)
    # ----------------------------------------------------------------
    def _biz_rnd_table_html(sub_df, accent_color, empty_msg):
        rows = sub_df.sort_values(COL_AI_SCORE, ascending=False).head(8)
        if rows.empty:
            return f'<div style="padding:16px;color:{C["text_muted"]};">{empty_msg}</div>'
        head = (
            f'<div style="display:grid;grid-template-columns:2.4fr 1fr 0.7fr 1fr 1.3fr; background:{C["navy"]}; '
            f'color:{C["navy_text"]}; padding:9px; font-size:10.5px;font-weight:700;">'
            f'<div>사업명</div><div>기관</div><div>지역</div><div>예산 · 마감</div><div>현황</div></div>'
        )
        body = ""
        for _, r in rows.iterrows():
            budget_txt = format_budget_eok(r.get(COL_BUDGET)) or "예산 미정"
            due_txt = str(r.get(COL_DUE_DATE) or "").strip()
            due_txt = f"~{due_txt[5:]}" if len(due_txt) >= 10 else "마감 미정"
            body += (
                f'<div style="display:grid;grid-template-columns:2.4fr 1fr 0.7fr 1fr 1.3fr; border-bottom:1px solid {C["row_border"]}; padding:10px; font-size:11.5px;">'
                f'<div><a href="{escape(str(r.get(COL_URL) or "#"))}" target="_blank" style="font-weight:700; color:{C["text"]}; text-decoration:none;">{escape(str(r[COL_TITLE]))}</a></div>'
                f'<div>{escape(str(r.get(COL_AGENCY) or "-"))}</div>'
                f'<div>{escape(str(r.get("_region_label") or "-"))}</div>'
                f'<div>{escape(budget_txt)}<br><span style="color:{C["text_muted"]};">{escape(due_txt)}</span></div>'
                f'<div>{score_badge_html(r.get(COL_AI_SCORE,-1))}</div></div>'
            )
        return f'<div style="border:1px solid {C["border"]}; border-radius:10px; overflow:hidden;">{head}{body}</div>'

    with st.expander("Ⅲ. 주요 사업/과제 — 사업부(수주) · R&D(개발과제) 현황표", expanded=True):
        st.markdown("##### 💼 사업부 — 연관도 높은 사업 Top 8")
        st.markdown(_biz_rnd_table_html(biz_df, C['accent'], "공고 없음"), unsafe_allow_html=True)
        st.markdown("##### 🔬 R&D — 연관도 높은 과제 Top 8")
        st.markdown(_biz_rnd_table_html(rnd_df, C['success_text'], "과제 없음"), unsafe_allow_html=True)

    # ----------------------------------------------------------------
    # Ⅳ. 제품별 대응 가이드  (※ 여기가 원본 파일에서 들여쓰기가 깨진 지점이었음 — 전부 수정됨)
    # ----------------------------------------------------------------
    def _match_rows(sub, keywords):
        """키워드 기반 1차 필터링"""
        if sub is None or sub.empty or not keywords:
            return sub.iloc[0:0] if sub is not None else df.iloc[0:0]
        pat = "|".join(re.escape(k) for k in keywords)
        mask = (
            sub[COL_TITLE].astype(str).str.contains(pat, case=False, na=False)
            | sub[COL_KEYWORDS].astype(str).str.contains(pat, case=False, na=False)
        )
        return sub[mask]

    _product_ai = brief(K_PRODUCT_AI) if isinstance(brief(K_PRODUCT_AI), dict) else {}

    def _match_rows_with_ai_fallback(sub, keywords, product_name, product_desc, cache_key):
        """1차: 키워드 매칭. 0건이면 아침 배치에서 AI가 문맥으로 골라 둔 결과 사용 (화면에서 AI 호출 없음).
        진짜 연관 없으면 억지로 채우지 않고 빈 상태 유지."""
        hits = _match_rows(sub, keywords)
        if not hits.empty or sub is None or sub.empty:
            return hits
        matched_titles = _product_ai.get(cache_key) or []
        if not matched_titles:
            return hits
        return sub[sub[COL_TITLE].astype(str).isin([str(t) for t in matched_titles])]

    with st.expander("Ⅳ. 제품별 대응 가이드 — NF · NFA · BM · LT", expanded=True):
        pg_cols = st.columns(4)
        PG_COLORS = [_tc("#0d47a1", "#5B9BFF"), _tc("#004d40", "#2DD4BF"), _tc("#e65100", "#FF9D5C"), _tc("#1b5e20", "#66BB6A")]

        for i, (pname, pinfo) in enumerate(PRODUCT_KEYWORDS.items()):
            biz_hits = _match_rows_with_ai_fallback(
                biz_df, pinfo["keywords"], pname, pinfo["desc"], cache_key=f"biz_{pname}"
            ).sort_values(COL_AI_SCORE, ascending=False)

            domain_kws = []
            for dname in PRODUCT_TO_DOMAIN.get(pname, []):
                domain_kws.extend(INTEGRATED_RND_DOMAINS.get(dname, {}).get("keywords", []))

            rnd_hits = _match_rows_with_ai_fallback(
                rnd_df, domain_kws, pname, pinfo["desc"], cache_key=f"rnd_{pname}"
            ).sort_values(COL_AI_SCORE, ascending=False)

            with pg_cols[i]:
                color = PG_COLORS[i % len(PG_COLORS)]
                st.markdown(
                    f"""
                    <div class="gt-surface" style="border-top:4px solid {color};padding:12px;">
                        <div style="font-size:14px;font-weight:800;color:{color};">{pname}</div>
                        <div style="font-size:11px;color:{C['text_muted']};margin-top:4px;height:32px;overflow:hidden;">{pinfo['desc']}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                total_hits = len(rnd_hits) + len(biz_hits)
                with st.popover(f"🔍 {pname} 대응 과제 보기 ({total_hits}건)", use_container_width=True):
                    st.markdown(f"**🔬 R&D 추천 ({len(rnd_hits)}건)**")
                    if rnd_hits.empty:
                        st.caption("추천된 R&D 과제가 없습니다.")
                    else:
                        for _, r in rnd_hits.head(5).iterrows():
                            one_line = get_oneline_summary(r)
                            st.markdown(
                                f"""
                                <div class='gt-popover-item' style="height:64px;overflow:hidden;">
                                    <a href='{escape(str(r.get(COL_URL) or '#'))}' target='_blank'
                                       style='color:{C['success_text']};text-decoration:none;font-weight:600;
                                              display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>
                                        {escape(str(r[COL_TITLE]))}
                                    </a>
                                    <div style='font-size:11px;color:{C['text_muted']};margin-top:3px;
                                                display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;'>
                                        {escape(str(one_line or '요약 생성 중...'))}
                                    </div>
                                </div>
                                """,
                                unsafe_allow_html=True,
                            )

                    st.markdown("---")
                    st.markdown(f"**💼 사업 공고 ({len(biz_hits)}건)**")
                    if biz_hits.empty:
                        st.caption("관련 사업 공고가 없습니다.")
                    else:
                        for _, r in biz_hits.head(5).iterrows():
                            one_line = get_oneline_summary(r)
                            st.markdown(
                                f"""
                                <div class='gt-popover-item' style="height:64px;overflow:hidden;">
                                    <a href='{escape(str(r.get(COL_URL) or '#'))}' target='_blank'
                                       style='color:{C['accent']};text-decoration:none;font-weight:600;
                                              display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>
                                        {escape(str(r[COL_TITLE]))}
                                    </a>
                                    <div style='font-size:11px;color:{C['text_muted']};margin-top:3px;
                                                display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;'>
                                        {escape(str(one_line or '요약 생성 중...'))}
                                    </div>
                                </div>
                                """,
                                unsafe_allow_html=True,
                            )


    # ----------------------------------------------------------------
    # Ⅴ. 주요 동향 분석 — 5단계 + 근거 키워드
    # ----------------------------------------------------------------
    with st.expander("Ⅴ. 주요 동향 분석 — 오늘의 트렌드(5단계) · 최근 7일 워치리스트", expanded=False):
        if not trend_top_df.empty and "category" in trend_top_df.columns:
            top_cats = trend_top_df.groupby("category")["count"].sum().sort_values(ascending=False).head(5)
            CAT_COLORS_6 = [
                _tc("#0d47a1", "#5B9BFF"), _tc("#e65100", "#FF9D5C"), _tc("#37474f", "#90A4AE"),
                _tc("#880e4f", "#F06FA3"), _tc("#004d40", "#2DD4BF"), _tc("#6B7684", "#9AA3AE"),
            ]
            card_cols = st.columns(len(top_cats)) if len(top_cats) else st.columns(1)
            for i, (cat, cnt) in enumerate(top_cats.items()):
                color = CAT_COLORS_6[i % len(CAT_COLORS_6)]
                cat_rows = trend_top_df[trend_top_df["category"] == cat].sort_values("importance", ascending=False)
                kw_evidence = "".join(
                    f'<div style="font-size:10.5px;color:{C["text_muted"]};margin-top:2px;">· {escape(str(kr["keyword"]))} (중요도 {kr["importance"]}점)</div>'
                    for _, kr in cat_rows.head(3).iterrows()
                )
                with card_cols[i]:
                    st.markdown(
                        f"""
                        <div style="background:{C['surface2']};border:1px solid {C['border']};border-left:5px solid {color};
                                    border-radius:10px;padding:12px;min-height:148px;">
                            <div style="font-size:12.5px;font-weight:800;color:{color};">{escape(str(cat))}</div>
                            <div style="font-size:19px;font-weight:800;color:{color};margin-top:5px;">{int(cnt)}개</div>
                            <div style="font-size:10.5px;color:{C['text_muted']};margin-top:5px;font-weight:700;">근거 키워드</div>
                            {kw_evidence if kw_evidence else f'<div style="font-size:10.5px;color:{C["text_muted"]};margin-top:2px;">데이터 없음</div>'}
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
        else:
            st.info("아직 오늘의 트렌드 데이터가 없습니다.")

        st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
        st.markdown("**최근 7일 트렌드 워치리스트**")
        hist_df = load_trend_history(days=7)
        if hist_df.empty:
            st.info("아직 최근 7일 트렌드 데이터가 없습니다.")
        else:
            latest_per_kw = (
                hist_df.sort_values("snapshot_date")
                .groupby("keyword", as_index=False)
                .last()
                .sort_values("importance", ascending=False)
                .head(5)
            )
            for _, r in latest_per_kw.iterrows():
                cat = r.get("category", "일반동향")
                st.markdown(
                    f"""
                    <div style="display:flex;align-items:center;gap:10px;padding:8px 12px;
                                border-bottom:1px solid {C['row_border']};">
                        <span style="background:{C['accent']};color:#fff;font-size:10.5px;font-weight:700;
                                    padding:1px 7px;border-radius:6px;">{r['snapshot_date']}</span>
                        <span style="flex:1;font-weight:600;font-size:13px;color:{C['text']};">{escape(str(r['keyword']))}</span>
                        <span style="font-size:11px;color:{C['text_muted']};">{escape(str(cat))} · 중요도 {r['importance']}점</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    # ----------------------------------------------------------------
    # Ⅵ. 분석 방법 및 산식
    # ----------------------------------------------------------------
    with st.expander("Ⅵ. 분석 방법 및 산식", expanded=False):
        st.markdown(
            """
            - **역할 분류(TRACK)**: 수집 시 AI가 각 공고를 연구개발 성격의 과제(RND)와 매출 직결형 사업 과제(BIZ) 중 하나로 판단합니다.
            - **연관도 점수(0~100점)**: 자사 프로필(넷퍼넬·넷퍼넬API·봇매니저·로드테스터, 대기열·트래픽 제어와 봇 방어)을 기준으로 AI가 평가합니다. 60점 이상을 '대응 필요'로 봅니다.
            - **제품별 대응 가이드**: 1차로 제품별 고정 키워드로 매칭하고, 0건이면 AI가 문맥으로 재판단한 뒤(연계 R&D 도메인 포함) 개발과제를 함께 추천합니다.
            - **뉴스 상위노출 보정**: 2021~2026년 조달내역에서 추출한 제품 관련 키워드가 뉴스 제목에 포함되면 AI 연관도 점수에 가중치(+15점)를 더해 우선 노출합니다.
            - **예산 표기**: 원문 예산 금액을 억 단위로 환산해 표기합니다. (예: 1,300,000,000원 → 13억)
            - **뉴스 수집 주기**: 플랫폼당 최신 10건을 당일 09:00(기준시각) 이후 실시간으로 재수집하며, 제목 기준 중복은 자동 제거합니다.
            - **AI 요약**: 최근 수집 공고 제목을 근거로 하루 1회 자동 생성하며, 날짜가 바뀌면 다시 생성됩니다.
            - 제출 전 반드시 원문 공고문의 접수 기간·자격 요건을 확인하십시오.
            """
        )

    # ----------------------------------------------------------------
    # PDF 다운로드 — PDF 리포트용 데이터는 화면 노출 여부와 무관하게 내부적으로 계산
    # ----------------------------------------------------------------
    action_df_for_pdf = df[
        (df["_due_date_parsed"].notna()
         & (df["_due_date_parsed"] >= pd.Timestamp(now_dt.date()))
         & (df["_due_date_parsed"] <= pd.Timestamp(now_dt.date()) + timedelta(days=3)))
        | (df[COL_AI_SCORE] >= 80)
    ].copy().sort_values([COL_AI_SCORE, "_due_date_parsed"], ascending=[False, True]).head(8)

    kpi_for_pdf = [
        ("오늘 신규 공고", f"{len(today_new_df)}건"),
        ("대응필요(60점+)", f"{need_action_n}건"),
        ("D-14 이내 마감", f"{due_soon14_n}건"),
        ("R&D 과제", f"{len(rnd_df)}건"),
    ]
    _, headline_payload = st.session_state.get(f"integrated_headline_{now_dt.date().isoformat()}", (None, None))
    headline_txt = headline_payload.get("headline", "") if isinstance(headline_payload, dict) else ""
    subtext_txt = headline_payload.get("subtext", "") if isinstance(headline_payload, dict) else ""

    _, issues_payload = st.session_state.get(f"integrated_issues_{now_dt.date().isoformat()}", (None, None))
    issues_for_pdf = issues_payload if isinstance(issues_payload, list) else []

    opp_biz_for_pdf = [{"text": f"{r[COL_TITLE]} ({r[COL_AGENCY]})"} for _, r in biz_df.sort_values(COL_AI_SCORE, ascending=False).head(5).iterrows()]
    opp_rnd_for_pdf = [{"text": f"{r[COL_TITLE]} ({r[COL_AGENCY]})"} for _, r in rnd_df.sort_values(COL_AI_SCORE, ascending=False).head(5).iterrows()]

    urgent_for_pdf = []
    for _, r in action_df_for_pdf.head(8).iterrows():
        d_left_txt = "-"
        if pd.notna(r["_due_date_parsed"]):
            d_left = (r["_due_date_parsed"] - pd.Timestamp(now_dt.date())).days
            d_left_txt = f"D-{d_left}" if d_left > 0 else ("D-DAY" if d_left == 0 else "-")
        urgent_for_pdf.append({
            "dday": d_left_txt,
            "title": r[COL_TITLE],
            "agency": r[COL_AGENCY],
            "due": r[COL_DUE_DATE] if pd.notna(r[COL_DUE_DATE]) else "미정",
            "score": r.get(COL_AI_SCORE, -1),
        })

    trend_for_pdf = []
    hist7 = load_trend_history(days=7)
    if not hist7.empty:
        top5 = (
            hist7.sort_values("snapshot_date").groupby("keyword", as_index=False).last()
            .sort_values("importance", ascending=False).head(5)
        )
        for _, r in top5.iterrows():
            trend_for_pdf.append({
                "date": r["snapshot_date"], "keyword": r["keyword"],
                "category": r.get("category", "일반동향"), "importance": r["importance"],
            })

    pdf_bytes = build_daily_report_pdf(
        kpi_for_pdf, headline=headline_txt, subtext=subtext_txt,
        issues=issues_for_pdf, opp_biz=opp_biz_for_pdf, opp_rnd=opp_rnd_for_pdf,
        urgent_rows=urgent_for_pdf, trend_rows=trend_for_pdf,
    )
    pdf_top_slot.download_button(
        "📄 PDF",
        data=pdf_bytes,
        file_name=f"gov_tracker_briefing_{now_dt:%Y%m%d}.pdf",
        mime="application/pdf",
        use_container_width=True,
        key="pdf_download_top",
    )
