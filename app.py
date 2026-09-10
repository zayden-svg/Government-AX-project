"""Streamlit dashboard using preserved originals and stored Gemini analyses."""
import hmac
import io
import json
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# Streamlit Cloud keeps secrets outside Git. CLI/Actions use environment variables.
try:
    for name in ['DATABASE_URL', 'GEMINI_API_KEY', 'G2B_SERVICE_KEY', 'GEMINI_MODEL',
                 'ADMIN_PASSWORD', 'ENABLED_COLLECTORS', 'MAX_ANALYSES_PER_RUN', 'MAX_ANALYSES_PER_DAY']:
        if name in st.secrets:
            os.environ[name] = str(st.secrets[name])
except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
    pass

from ai_utils import CATEGORIES, PROMPT_VERSION, is_gemini_ready, model_name
from db import init_db, list_notices, recent_jobs
from main import run_pipeline, VERIFIED_SOURCES
from product_profile import PRODUCTS, PROFILE_HASH, PROFILE_VERSION
from settings import setting

KST = ZoneInfo('Asia/Seoul')
st.set_page_config(page_title='공공 IT Insight', layout='wide')
st.markdown('''<style>
.block-container {max-width:1400px;padding-top:2rem}
[data-testid="stMetric"] {background:#f0f5fb;border:1px solid #dae4ef;border-radius:12px;padding:16px}
h1,h2,h3 {color:#183450}
</style>''', unsafe_allow_html=True)


def kst_time(value):
    if not value:
        return '미확인'
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(KST).strftime('%Y-%m-%d %H:%M KST')
    except ValueError:
        return '미확인'


def truth_label(value):
    return '판단 어려움' if value is None else ('예' if value else '아니오')


def load_rows():
    engine = init_db()
    return list_notices(PROFILE_HASH, PROMPT_VERSION, model_name(), engine), recent_jobs(engine)


def flatten(item):
    original = item['original']
    ai = item['analysis'] or {}
    return {
        'id': item['id'], '제목': original['original_title'],
        '기관': original['organization'] or original['source'], '수집처': original['source'],
        '지역': original.get('region') or '미확인', '유형': ai.get('category', '분석 대기'),
        '업무 구분': {'RND': 'R&D', 'BIZ': '사업부', 'UNKNOWN': '판단 어려움'}.get(ai.get('track'), '분석 대기'),
        '관련도': ai.get('relevance_score'), '중요도': ai.get('importance_score'),
        '공고일': original.get('published_date') or '', '마감일': original.get('deadline') or '',
        '원문': original['original_url'],
        'R&D': truth_label(ai.get('rnd')), 'AI': truth_label(ai.get('ai_related')),
        'IT': truth_label(ai.get('it_related')), '지원': truth_label(ai.get('support')),
        '태그': ', '.join(ai.get('tags', [])),
        '분석 상태': '분석 완료' if ai else ('본문 확보 필요' if original['content_quality'] != 'body' else '분석 대기'),
    }


def detail(item):
    o, a = item['original'], item['analysis']
    st.subheader(o['original_title'])
    if o['original_url']:
        st.link_button('원문 공고 열기', o['original_url'])
    left, right = st.columns(2)
    with left:
        st.markdown('### 확인된 정보 / 수집 원문')
        if o['data_origin'] == 'legacy_unverified':
            st.warning('기존 DB 이관 자료입니다. 날짜·기관 등 원문 재검증이 필요하며 과거 AI 평가는 사용하지 않습니다.')
        st.write('기관:', o['organization'] or o['source'])
        st.write('공고번호:', o['notice_number'] or '미확인')
        st.write('공고일:', o['published_date'] or '미확인')
        st.write('마감일:', o['deadline'] or '미확인 — 사전공개 의견기한과 입찰기한은 다를 수 있습니다.')
        st.write('예산 원문:', o['budget'] or '미확인')
        st.caption('원문 수집: ' + kst_time(item['collected_at']))
        st.text_area('원문 본문 (AI가 수정하지 않음)', o['original_content'] or '목록 정보만 확보되어 있습니다.',
                     height=340, disabled=True, key='body_' + item['original_id'])
        st.markdown('#### 첨부파일')
        st.caption('현재는 원문 첨부 링크를 보존합니다. 첨부 본문·HWP 분석은 아직 포함되지 않습니다.')
        for file in o['attachments']:
            if str(file.get('url', '')).startswith('https://'):
                st.link_button(file.get('name') or '첨부파일', file['url'])
    with right:
        st.markdown('### AI 분석')
        if not a:
            st.info('분석 대기 또는 본문 확보 필요. 제목 키워드만으로 임의 분류하지 않습니다.')
            return
        st.write(a['summary'])
        st.caption('분석 완료: ' + kst_time(item['analyzed_at']) + ' | ' + model_name())
        st.write('사업 유형:', a['category'], '/', a['subtype'])
        st.write('업무 구분:', {'RND': 'R&D', 'BIZ': '사업부', 'UNKNOWN': '판단 어려움'}[a['track']])
        for label, key in [('R&D', 'rnd'), ('개발·기업지원', 'support'), ('AI', 'ai_related'),
                           ('IT', 'it_related'), ('시스템 구축', 'system_build'), ('클라우드', 'cloud_related'), ('보안', 'security_related')]:
            st.write(label + ': ' + truth_label(a[key]))
        st.write('관련도:', a['relevance_score'] if a['relevance_score'] is not None else '판단 어려움')
        st.write('중요도:', a['importance_score'] if a['importance_score'] is not None else '판단 어려움')
        st.caption('점수는 AI 평가이며 수주 확률이 아닙니다. 사실 여부는 원문을 확인하세요.')
        st.markdown('#### 판단 근거')
        evidence = {e['id']: e for e in a['evidence']}
        for reason in a['reasons']:
            st.write(f"[{reason['kind']}] {reason['claim']}")
            for key in reason['evidence_ids']:
                st.caption(f"원문 인용 ({evidence[key]['field']}): {evidence[key]['quote']}")
        st.markdown('#### 제품별 영업 검토')
        for match in a['product_matches']:
            st.write(f"[{match['kind']}] {match['product']} · {match['score']}점")
            st.write(match['reason'])
            st.caption('제품 근거: ' + match['product_source'])
            for key in match['evidence_ids']:
                st.caption('공고 근거: ' + evidence[key]['quote'])
        for note in a['uncertainties']:
            st.warning('확인 필요: ' + note)


st.title('공공 IT Insight')
st.caption('공공사업 원문과 Gemini 분석을 한곳에서 | 한국시간 오전 8시 정기 수집 목표')
try:
    records, history = load_rows()
except Exception:
    st.error('DB 연결에 실패했습니다. 서버의 DATABASE_URL과 네트워크 설정을 확인해 주세요. 인증정보는 화면에 표시하지 않습니다.')
    st.stop()

if not setting('DATABASE_URL'):
    st.info('현재는 로컬 SQLite 미리보기입니다. 무료 공유 DB 연결과 정기 실행 활성화는 별도 설정이 필요합니다.')
if history:
    last = history[0]
    st.caption(f"최근 실행: {kst_time(last['started_at'])} → {kst_time(last['finished_at'])} · 상태 {last['status']}")
    if last['status'] != 'success':
        st.warning('최근 작업이 완전히 성공하지 않았습니다. 아래 수집 현황에서 실패·분석 대기 상태를 확인하세요.')
else:
    st.info('수집 실행 이력이 없습니다. 데이터가 없다는 뜻과 수집을 완료했다는 뜻은 다릅니다.')

with st.sidebar:
    st.header('탐색')
    screen = st.radio('화면', ['Dashboard', '전체 공고', 'R&D', '개발지원사업', 'AI/IT 사업', '지역별', '수집 현황', '제품 기준'])
    st.divider()
    st.caption('정기 실행 설정 목표: 매일 08:00 KST. 무료 예약 실행은 지연될 수 있으며 실제 완료 시각을 표시합니다.')
    with st.expander('운영자 최신 수집'):
        configured = setting('ADMIN_PASSWORD')
        password = st.text_input('운영자 비밀번호', type='password')
        authorized = bool(configured and hmac.compare_digest(password.encode('utf-8'), configured.encode('utf-8')))
        selected_sources = st.multiselect('수집 대상', VERIFIED_SOURCES, default=['NIA'])
        if not configured:
            st.caption('서버 ADMIN_PASSWORD 설정 후 사용할 수 있습니다.')
        if st.button('최신 정보 수집 및 분석', disabled=not authorized or not selected_sources):
            with st.spinner('제한된 최신 공고를 수집·분석 중입니다. 완료 후 화면이 갱신됩니다.'):
                result = run_pipeline(selected_sources, limit=5, trigger='web')
            st.session_state['last_manual_result'] = result
            st.rerun()
    if st.button('저장된 결과 새로고침'):
        st.rerun()

if screen == '제품 기준':
    st.subheader('현재 적용 중인 제품 기준')
    st.caption(PROFILE_VERSION + ' · 사용자 확인 사항을 우선 적용')
    for product in PRODUCTS:
        with st.container(border=True):
            st.markdown('#### ' + product['name'])
            st.write(product['status'])
            st.write('제공 형태:', ', '.join(product.get('deployment', [])) or '계약 시 확인')
            st.write('기능:', ', '.join(product['capabilities']) or '출시 예정 제품의 상세 기능 미확정')
            st.caption('근거: ' + ' / '.join(product['sources']))
    st.info('구 MBUSTER PDF·기능·낙찰 사례는 분석 기준에서 제외했습니다. MBUSTER는 BotManager 온프레미스의 예정 명칭으로만 관리합니다.')
    st.stop()

if screen == '수집 현황':
    st.subheader('수집·분석 실행 이력')
    st.caption('수집 성공과 분석 완료는 다릅니다. 첫 목록만 수집하는 기관이 있어 전체 공고를 포괄하지 않습니다.')
    for job in history:
        with st.expander(kst_time(job['started_at']) + ' | ' + job['status'], expanded=job == history[0]):
            st.json(json.loads(job['report'] or '{}'))
    st.stop()

if not records:
    st.info('저장된 공고가 없습니다. 서버에서 첫 수집을 실행하거나 운영자 메뉴를 사용해 주세요.')
    st.stop()

frame = pd.DataFrame([flatten(r) for r in records])
with st.form('search'):
    q1, q2, q3 = st.columns([2, 1, 1])
    keyword = q1.text_input('제목·기관·태그 검색', placeholder='예: AI 상담, 예약, NIA')
    agencies = q2.multiselect('기관', sorted(frame['기관'].unique()))
    regions = q3.multiselect('지역', sorted(frame['지역'].unique()))
    c1, c2, c3, c4 = st.columns(4)
    categories = c1.multiselect('사업 유형', ['분석 대기', *CATEGORIES])
    minimum = c2.slider('최소 관련도', 0, 100, 0)
    importance = c3.slider('최소 중요도', 0, 100, 0)
    rnd = c4.selectbox('R&D 여부', ['전체', '예', '아니오', '판단 어려움'])
    c1, c2, c3, c4 = st.columns(4)
    ai_filter = c1.selectbox('AI 여부', ['전체', '예', '아니오', '판단 어려움'])
    it_filter = c2.selectbox('IT 여부', ['전체', '예', '아니오', '판단 어려움'])
    published = c3.date_input('공고일 범위 (선택)', value=[])
    deadline = c4.date_input('마감일 범위 (선택)', value=[])
    st.form_submit_button('검색', width='stretch')

filtered = frame.copy()
if keyword.strip():
    haystack = filtered[['제목', '기관', '수집처', '태그']].fillna('').agg(' '.join, axis=1)
    filtered = filtered[haystack.str.contains(keyword.strip(), case=False, regex=False)]
for column, values in [('기관', agencies), ('지역', regions), ('유형', categories)]:
    if values:
        filtered = filtered[filtered[column].isin(values)]
for column, value in [('R&D', rnd), ('AI', ai_filter), ('IT', it_filter)]:
    if value != '전체':
        filtered = filtered[filtered[column] == value]
for column, value in [('관련도', minimum), ('중요도', importance)]:
    if value:
        filtered = filtered[pd.to_numeric(filtered[column], errors='coerce') >= value]
for column, value in [('공고일', published), ('마감일', deadline)]:
    if len(value) == 2:
        filtered = filtered[(filtered[column] >= value[0].isoformat()) & (filtered[column] <= value[1].isoformat())]
if screen == 'R&D':
    filtered = filtered[filtered['R&D'] == '예']
elif screen == '개발지원사업':
    filtered = filtered[filtered['지원'] == '예']
elif screen == 'AI/IT 사업':
    filtered = filtered[(filtered['AI'] == '예') | (filtered['IT'] == '예')]

if screen == 'Dashboard':
    today = datetime.now(KST).date()
    new_today = sum(r['original']['data_origin'] != 'legacy_unverified' and
                    datetime.fromisoformat(r['created_at']).astimezone(KST).date() == today for r in records)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric('오늘 새로 확보', new_today)
    c2.metric('AI 분석 완료', sum(bool(r['analysis']) for r in records))
    c3.metric('영업 검토 70점 이상', int((pd.to_numeric(frame['관련도'], errors='coerce') >= 70).sum()))
    c4.metric('본문 확보 필요', int((frame['분석 상태'] == '본문 확보 필요').sum()))
    st.caption('오늘 신규는 처음 수집한 날 기준입니다. 공고가 오늘 게시되었다는 뜻은 아닙니다.')

st.subheader(f'{screen} · 검색 결과 {len(filtered)}건')
filtered = filtered.sort_values(['관련도', '공고일'], ascending=[False, False], na_position='last')
if filtered.empty:
    st.info('조건에 맞는 결과가 없습니다. 아직 분석되지 않은 공고는 AI/R&D 필터에서 제외될 수 있습니다.')
else:
    page = st.number_input('페이지', min_value=1, max_value=max(1, (len(filtered) + 19) // 20), step=1)
    current = filtered.iloc[(page-1)*20:page*20]
    columns = ['기관', '제목', '업무 구분', '유형', '지역', '관련도', '공고일', '마감일', '분석 상태']
    st.dataframe(current[columns], hide_index=True, width='stretch')
    items_by_id = {r['id']: r for r in records}
    chosen = st.selectbox('상세 공고 선택', current['id'].tolist(),
                          format_func=lambda key: items_by_id[key]['original']['original_title'])
    with st.container(border=True):
        detail(items_by_id[chosen])
    # Neutralize spreadsheet formulas in externally supplied text.
    exported = filtered.drop(columns=['id']).map(
        lambda v: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v)
    output = io.BytesIO()
    exported.to_excel(output, index=False, engine='openpyxl')
    st.download_button('검색 결과 엑셀 다운로드', output.getvalue(), 'public_it_notices.xlsx',
                       'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

st.divider()
st.caption('원문 사실과 AI 분석·추정은 다릅니다. 본문·첨부가 불충분한 공고는 임의로 판단하지 않습니다.')
