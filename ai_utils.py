import os
import re
import json
import time
import asyncio

from dotenv import load_dotenv
load_dotenv()

try:
    import anthropic
except ImportError:
    anthropic = None


def _read_secret(name, default=""):
    """환경변수 → Streamlit secrets 순서로 값을 찾는다 (GitHub Actions / Streamlit Cloud 겸용)."""
    val = os.getenv(name, "").strip()
    if val:
        return val
    try:
        import streamlit as st
        return str(st.secrets.get(name, default)).strip()
    except Exception:
        return default


ANTHROPIC_API_KEY = _read_secret("ANTHROPIC_API_KEY")
# 대량 분류·요약용 기본 모델: 가장 빠르고 저렴한 Haiku. 바꾸려면 CLAUDE_MODEL 환경변수만 수정.
MODEL_NAME = _read_secret("CLAUDE_MODEL") or "claude-haiku-4-5"
MAX_TOKENS = 2048

# main.py 일괄 분석용 동시성/속도 제한 (Claude 콘솔 Limits 화면의 등급에 맞춰 조정)
AI_MAX_CONCURRENCY = int(_read_secret("AI_MAX_CONCURRENCY") or 4)
AI_RPM_LIMIT = int(_read_secret("AI_RPM_LIMIT") or 30)

SYSTEM_PROMPT = (
    "너는 에스티씨랩(STCLab) 공공사업부를 돕는 공공 IT 분석가다. "
    "주어진 정보에 없는 내용은 절대 지어내지 않는다. "
    "JSON을 요구받으면 설명·머리말·코드블록 없이 JSON만 출력한다."
)

_client = None
_async_client = None


def is_ai_ready():
    return bool(ANTHROPIC_API_KEY) and anthropic is not None


def _get_client():
    global _client
    if not is_ai_ready():
        return None
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=3, timeout=60)
    return _client


def _get_async_client():
    global _async_client
    if not is_ai_ready():
        return None
    if _async_client is None:
        _async_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, max_retries=3, timeout=60)
    return _async_client


def _resp_text(resp):
    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()


def _call(prompt, json_mode=True, max_tokens=MAX_TOKENS):
    """동기 호출. 반환: (텍스트, 오류메시지)"""
    client = _get_client()
    if client is None:
        return None, "Claude API 키(ANTHROPIC_API_KEY)가 설정되지 않았습니다."
    try:
        suffix = "\n\n반드시 JSON만 출력해라." if json_mode else ""
        resp = client.messages.create(
            model=MODEL_NAME,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt + suffix}],
        )
        return _resp_text(resp), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _parse_json(text, fallback):
    if not text:
        return fallback
    cleaned = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r"(\[.*\]|\{.*\})", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return fallback


def _dedupe_similar_keywords(items, key_field="keyword"):
    """유사/중복 키워드 1차 필터링 — 완전 동일하거나 한쪽이 다른쪽을 포함하는 짧은 변형을 제거.
    예: 'AI'와 '인공지능'처럼 의미는 겹치지만 문자열이 다른 경우는 AI 판단을 신뢰하고 그대로 두되,
    공백/대소문자만 다른 중복이나 완전 부분 포함 관계만 제거한다."""
    seen_norm = []
    result = []
    for item in items:
        kw = str(item.get(key_field, "")).strip()
        if not kw:
            continue
        norm = kw.lower().replace(" ", "")
        is_dup = False
        for prev_norm in seen_norm:
            if norm == prev_norm or norm in prev_norm or prev_norm in norm:
                is_dup = True
                break
        if is_dup:
            continue
        seen_norm.append(norm)
        result.append(item)
    return result


# ------------------------------------------------------------
# 1. 공고 상세 요약 — 근거 기반, 1~2문장
# ------------------------------------------------------------
def generate_summary(info_block):
    prompt = f"""너는 공공 IT 영업 담당자를 돕는 분석가다.
아래 공고 정보를 근거로, 담당자가 10초 안에 읽을 수 있도록 핵심을 1~2문장으로 한국어 요약해라.
규칙:
- 공고 정보에 없는 내용은 절대 만들어내지 말 것(추측 금지).
- 숫자·마감일·기관명이 있으면 반드시 포함할 것.
- 출력은 요약 문장만, 다른 설명이나 머리말 없이.

[공고 정보]
{info_block}
"""
    text, err = _call(prompt, json_mode=False)
    return text, err


# ------------------------------------------------------------
# 2. 공고 1줄 핵심 요약 배치 생성 (목록용, 비용 절감을 위해 묶어서 호출)
# ------------------------------------------------------------
def summarize_titles_oneline(batch):
    """batch: [{"key":..., "title":..., "agency":...}, ...]
    반환: [{"key":..., "summary":...}, ...]
    """
    if not batch:
        return [], None
    items_text = "\n".join(f'- key:{b["key"]} | 제목:{b["title"]} | 기관:{b.get("agency","")}' for b in batch)
    prompt = f"""아래 공고 목록 각각을 한국어로 15~25자 내외의 핵심 한줄 요약으로 바꿔라.
무엇을 하는 사업/과제인지 핵심만 압축하고, 제목에 없는 내용은 추측해서 넣지 마라.
입력에 있는 key 값을 그대로 포함해서 JSON 배열로만 출력해라.
형식: [{{"key": "...", "summary": "..."}}]

[공고 목록]
{items_text}
"""
    text, err = _call(prompt, json_mode=True)
    if err:
        return [], err
    result = _parse_json(text, [])
    return result, None


# ------------------------------------------------------------
# 3. 객관적 IT 트렌드 키워드 추천 — 자사 제품명 배제 + 유사 키워드 중복 제거
# ------------------------------------------------------------
def recommend_keywords(titles, exclude_terms=None, n=8):
    exclude_terms = exclude_terms or ["넷퍼넬", "NetFUNNEL", "넷퍼넬API", "봇매니저", "BotManager", "로드테스터", "LoadTester", "에스티씨랩"]
    titles_text = "\n".join(f"- {t}" for t in titles[:80])
    prompt = f"""너는 공공 IT 시장을 객관적으로 분석하는 애널리스트다.
아래는 최근 수집된 공고·뉴스 제목 목록이다. 이 제목들만 근거로, 지금 공공 IT 업계에서
떠오르고 있는 '객관적인 트렌드 키워드' {n}개 내외를 뽑아라.

반드시 지킬 규칙:
1) 다음 단어들은 특정 회사의 자사 제품명이므로 절대 키워드로 추천하지 마라: {", ".join(exclude_terms)}
2) '정부', '사업', '공고'처럼 너무 포괄적인 단어 대신 구체적인 트렌드 단어를 뽑아라.
3) 실제 제목에 등장했거나 그로부터 합리적으로 도출되는 단어만 사용해라(없는 트렌드 지어내지 말 것).
4) 서로 거의 같은 의미의 키워드를 중복으로 뽑지 말고, 각 키워드는 서로 명확히 구분되는 주제여야 한다.
5) 각 키워드마다 왜 선택했는지 1줄 이유를 붙여라.

[제목 목록]
{titles_text}

출력은 JSON 배열로만: [{{"keyword": "...", "reason": "..."}}]
"""
    text, err = _call(prompt, json_mode=True)
    if err:
        return [], err
    result = _parse_json(text, [])
    # 1차: 자사 제품명 2차 필터링 (모델이 규칙을 어겼을 경우 대비)
    filtered = [r for r in result if not any(ex.lower() in str(r.get("keyword", "")).lower() for ex in exclude_terms)]
    # 2차: 문자열 수준의 중복/포함 관계 키워드 제거 (예: "AI" vs "AI 기술"처럼 한쪽이 다른쪽을 포함하는 경우)
    deduped = _dedupe_similar_keywords(filtered)
    return deduped, None


# ------------------------------------------------------------
# 4. 트렌드 키워드 추출 — 카테고리를 AI가 자유롭게 명명 (3개 고정값 제거)
# ------------------------------------------------------------
def extract_trend_keywords(titles):
    titles_text = "\n".join(f"- {t}" for t in titles[:150])
    prompt = f"""아래 뉴스·공고 제목들을 분석해서 핵심 키워드를 15~20개 추출해라.

각 키워드마다 다음 항목을 포함해라:
- keyword: 키워드 자체
- importance: 0~100 중요도 점수 (언급 빈도 + 업계 영향력을 종합 고려)
- count: 해당 키워드가 포함된 제목 수
- category: 이 키워드가 속하는 주제 분류명을 네가 직접 정해라.
  (예시일 뿐 그대로 쓰지 말고 실제 내용에 맞게 자유롭게 명명: 'AI', '사이버보안', '클라우드', '정책/제도', '산업동향' 등)
  특정 회사의 제품 카테고리로 분류하지 말고, 업계 전반의 주제로 분류해라.
- reason: 1줄 판단 근거
- sample_titles: 근거가 된 실제 제목 최대 2개 (목록에 있는 제목 그대로)

[제목 목록]
{titles_text}

출력은 JSON 배열로만.
"""
    text, err = _call(prompt, json_mode=True, max_tokens=6000)
    if err:
        return [], err
    result = _parse_json(text, [])
    if isinstance(result, dict):          # {"keywords": [...]} 형태로 감싸서 줘도 처리
        result = next((v for v in result.values() if isinstance(v, list)), [])
    result = [r for r in result if isinstance(r, dict) and r.get("keyword")]
    # 거의 동일한 키워드가 중복 추출되는 경우 1차 정리 (importance 높은 쪽을 우선 유지)
    result = sorted(result, key=lambda x: -(x.get("importance") or 0))
    result = _dedupe_similar_keywords(result)
    return result, None


# ------------------------------------------------------------
# 통합보기 분석 공통 원칙 — 공공 IT 데일리 브리핑(공공사업팀 보고서) 형식을 따른다
#   ① 사실과 추정을 구분: 목록에 있는 사실만 단정, 해석·전망은 문장 끝에 "(추정)"
#   ② 숫자(예산·건수·날짜)는 목록에 있는 값만 사용 — 계산·창작 금지
#   ③ 항상 "우리 사업 영향"과 "지금 할 일"로 연결 (제품: NF·NFA·BM·LT)
#   ④ 공공 영업 특성 반영: 예산 편성 시기, 조달 경로(나라장터·사전규격·수의계약), 청탁금지법 등은 '확인 필요'로 표시
# ------------------------------------------------------------
BRIEFING_RULES = """[작성 원칙]
- 목록에 있는 사실만 단정한다. 해석·전망 문장은 끝에 "(추정)"을 붙인다.
- 예산·건수·날짜 숫자는 목록에 적힌 값만 쓴다. 계산하거나 지어내지 않는다.
- 문장은 짧고 쉽게(초등학생도 이해), 전문용어는 풀어 쓴다.
- 제품 약어: NF=넷퍼넬(웹 접속 대기열·트래픽 제어), NFA=넷퍼넬API(API 트래픽 제어, 안정화 단계),
  BM=봇매니저/MB=엠버스터(매크로·봇 차단), LT=로드테스터(부하테스트, 개발 중).
- 법·규정(청탁금지법, 조달 규정 등) 판단은 단정하지 말고 "확인 필요"로 쓴다."""


def generate_headline(items):
    """items: 문자열 목록(제목) 또는 '제목 · 기관 · 예산 · 마감 · 연관도' 형태의 한 줄 요약 목록"""
    items_text = "\n".join(f"- {t}" for t in items[:40])
    prompt = f"""너는 공공 IT 시장 리서치 애널리스트다. 아래 오늘의 공고·과제·뉴스 목록만 근거로 데일리 브리핑 헤드라인을 써라.
{BRIEFING_RULES}

[출력]
- headline: 오늘 가장 중요한 흐름 한 줄 (25자 내외)
- subtext: 왜 중요한지 1~2문장
- points: 오늘의 핵심 3가지. 각 항목 = 사실 1문장(기관·사업명·예산/마감 등 구체값 포함) + 필요하면 짧은 해석. 50자 내외.

[목록]
{items_text}

JSON으로만 출력: {{"headline": "...", "subtext": "...", "points": ["...", "...", "..."]}}
"""
    text, err = _call(prompt, json_mode=True)
    if err:
        return None, err
    obj = _parse_json(text, None)
    if isinstance(obj, dict):
        obj["points"] = [str(p).strip() for p in (obj.get("points") or []) if str(p).strip()][:3]
    return obj, None


# ------------------------------------------------------------
# 6. 오늘의 핵심 이슈 카드 — 테마별로 묶고 '우리 사업 영향'까지
# ------------------------------------------------------------
def generate_key_issues(items_text, n=4):
    prompt = f"""아래는 오늘 기준 연관도가 높은 공공 IT 공고·과제·뉴스 목록이다. 핵심 이슈 테마 {n}개로 묶어라.
{BRIEFING_RULES}

각 테마:
- theme: 테마명 (10자 내외, 명사형)
- impact: "매우높음" | "높음" | "보통" | "낮음" (자사 사업 기회 관점의 영향도)
- confidence: "High" | "Medium" | "Low" (근거 자료의 양·확실성)
- count: 이 테마에 묶은 목록 항목 수 (실제로 센 값)
- summary: 무슨 일이 있는지 사실 1문장 (대표 기관·사업명 포함)
- biz_impact: 우리 사업 영향 1문장 (해석이면 끝에 "(추정)")
- products: 관련 제품 약어 배열 (예: ["NF","BM"]), 없으면 []

[목록]
{items_text}

JSON 배열로만 출력.
"""
    text, err = _call(prompt, json_mode=True, max_tokens=3000)
    if err:
        return [], err
    res = _parse_json(text, [])
    return (res if isinstance(res, list) else []), None


# ------------------------------------------------------------
# 6-1. 사업부 / R&D 별 브리핑 요약 (PDF 요약본용) — 헤드라인 · 동향 카드 · Action Item
# ------------------------------------------------------------
def generate_track_brief(track_label, item_lines, news_titles, guide_lines):
    """track_label: '사업부' 또는 'R&D'. 반환: ({headline, points, issues, actions}, 오류)"""
    from datetime import date as _date
    items_txt = "\n".join(f"- {x}" for x in item_lines[:30]) or "- (항목 없음)"
    news_txt = "\n".join(f"- {x}" for x in news_titles[:12]) or "- (뉴스 없음)"
    guide_txt = "\n".join(f"- {x}" for x in guide_lines[:12]) or "- (없음)"
    owner_hint = ("사업부 영업 / 사업부 제안 / 사업부 기술지원" if track_label == "사업부"
                  else "R&D 기획 / R&D 연구 / 사업부 연계")
    prompt = f"""너는 에스티씨랩 공공사업팀의 데일리 브리핑 작성자다. 아래 [{track_label}] 공고·과제와 뉴스만 근거로 {track_label} 담당자용 요약을 써라.
오늘 날짜: {_date.today().isoformat()}
{BRIEFING_RULES}

[출력]
- headline: 오늘 {track_label}에서 가장 중요한 흐름 한 줄 (25자 내외)
- points: 오늘의 헤드라인 3개. 각 항목은 사실 1문장(기관·사업명·예산·마감 구체값) + 필요하면 짧은 해석. 60자 내외.
  가장 중요한 단어 1~2개는 **굵게** 표시.
- issues: 동향 카드 3개. 각 카드
  · tag: "분류 — 대표 기관" (예: "정책·제도 — 행정안전부", "발주 — 한국교육학술정보원")
  · title: 한 줄 제목
  · summary: 사실 1~2문장
  · impact: 우리 사업 영향 1문장 (해석이면 끝에 "(추정)")
  · products: 관련 제품 약어 배열 (NF, NFA, BM, LT 중), 없으면 []
  · pri: "red"(매우 중요·마감 임박) | "orange"(중요) | "amber"(보통) | "green"(참고)
- actions: 오늘의 Action Item 3~5개. 각 항목
  · action: 담당자가 바로 할 구체 행동 1문장 (사업명·기관 포함)
  · owner: 담당 ({owner_hint} 중 하나)
  · due: 기한 ("10/13까지"처럼 목록의 마감일 기준, 마감이 없으면 "상시")
- 마감이 이미 지난 항목은 쓰지 않는다. 마감이 가까운 순으로 우선한다.

[{track_label} 공고·과제]
{items_txt}

[관련 뉴스]
{news_txt}

[제품별 대응 가이드 요약]
{guide_txt}

JSON 객체로만 출력: {{"headline": "...", "points": ["..."], "issues": [{{"tag": "...", "title": "...", "summary": "...", "impact": "...", "products": [], "pri": "orange"}}], "actions": [{{"action": "...", "owner": "...", "due": "..."}}]}}
"""
    text, err = _call(prompt, json_mode=True, max_tokens=3500)
    if err:
        return None, err
    obj = _parse_json(text, None)
    if not isinstance(obj, dict):
        return None, "AI 응답 형식 오류"
    obj["points"] = [str(p).strip() for p in (obj.get("points") or []) if str(p).strip()][:3]
    obj["issues"] = [i for i in (obj.get("issues") or []) if isinstance(i, dict) and i.get("title")][:4]
    obj["actions"] = [a for a in (obj.get("actions") or []) if isinstance(a, dict) and a.get("action")][:5]
    return obj, None


# ------------------------------------------------------------
# 6-2. 제품별 대응 가이드 — 관련 이슈 / 우리 사업 영향 / 지금 할 일
# ------------------------------------------------------------
def generate_product_guide(product_blocks):
    """product_blocks: {제품약어: {"name":..., "desc":..., "items":[한 줄 요약...]}}
    반환: ({제품약어: {"issue":..., "impact":..., "actions":[...], "none": bool}}, 오류)"""
    blocks_txt = []
    for code, b in product_blocks.items():
        lines = "\n".join(f"  - {x}" for x in b.get("items", [])[:12]) or "  - (관련 항목 없음)"
        blocks_txt.append(f"[{code}] {b.get('name', '')} — {b.get('desc', '')}\n{lines}")
    from datetime import date as _date
    prompt = f"""너는 에스티씨랩 공공사업팀의 영업 전략 담당이다. 제품별로 오늘 연결된 공고·과제·뉴스를 보고 대응 가이드를 써라.
오늘 날짜: {_date.today().isoformat()}
{BRIEFING_RULES}

제품마다:
- issue: 관련 이슈 — 어떤 공고·과제·뉴스가 걸렸는지 1문장 (사업명·기관 그대로)
- impact: 우리 사업 영향 1~2문장 (해석이면 끝에 "(추정)")
- actions: 지금 할 일 1~2개 (담당자가 바로 할 수 있는 구체 행동: 원문·제안요청서 확인, 사전규격 의견 제출,
  수요기관 담당 부서 확인, 레퍼런스·제안자료 준비, 마감일 일정 등록 등. 제품 성숙도(NFA 안정화·LT 개발 중)를 고려)
- none: 관련 항목이 없거나 억지 연결뿐이면 true (그때 issue/impact/actions는 빈 값)
- 강조: 각 문장에서 가장 중요한 말(사업명·기관명·핵심 이슈·할 일의 핵심 동사구) 1~2곳을 **굵게** 표시한다.
  예산·마감일은 화면에서 자동으로 굵게 처리되므로 따로 표시하지 않아도 된다. 문장 전체를 굵게 하지 않는다.
억지로 끼워 맞추지 마라. 관련성이 약하면 none=true.

[제품별 '직접 연관' 판단 기준 — 아래 근거가 제목·요약에 있을 때만 그 제품의 이슈로 쓴다]
{chr(10).join(f"- {k}: {v}" for k, v in PRODUCT_DIRECT_CRITERIA.items())}
- 아래 목록에 들어 있어도 위 기준에 안 맞는 항목은 무시하고 쓰지 마라.
- 'AI'·'보안'·'데이터'라는 단어만 같은 과제(예: 사이버공격 대응 플랫폼, ODA 타당성 조사, 일반 SW 라이선스)는 연결하지 않는다.
- 마감일이 이미 지난 항목은 쓰지 않는다. 마감이 가까운 순으로 우선한다.
- 연관 항목이 1~2건뿐이어도 근거가 확실하면 그 항목만 쓴다.

{chr(10).join(blocks_txt)}

JSON 객체로만 출력: {{"NF": {{"issue": "...", "impact": "...", "actions": ["..."], "none": false}}, ...}}
"""
    text, err = _call(prompt, json_mode=True, max_tokens=3000)
    if err:
        return {}, err
    res = _parse_json(text, {})
    out = {}
    if isinstance(res, dict):
        for code, v in res.items():
            if not isinstance(v, dict):
                continue
            acts = v.get("actions") or []
            if isinstance(acts, str):
                acts = [acts]
            out[str(code)] = {
                "issue": str(v.get("issue") or "").strip(),
                "impact": str(v.get("impact") or "").strip(),
                "actions": [str(a).strip() for a in acts if str(a).strip()][:3],
                "none": bool(v.get("none")) or not str(v.get("issue") or "").strip(),
            }
    return out, None


# ------------------------------------------------------------
# 7. 역할별 대응 전략 — 한 줄 액션 아이템
# ------------------------------------------------------------
def generate_action_strategies(role, items_txt):
    prompt = f"""너는 공공 IT 영업/R&D 조직의 전략 어드바이저다.
보는 사람 역할: {role}
아래 공고 목록을 근거로, 이 역할이 지금 당장 해야 할 액션을 한 줄씩 제시해라.
각 항목은 다음 필드를 가진다:
- 대상: 어떤 공고/과제에 대한 것인지
- 액션: 구체적으로 무엇을 해야 하는지 (한 줄)
- 마감: 마감일 또는 '상시'
- 담당: 제안하는 담당 역할(예: 영업팀, R&D팀, 컨소시엄 담당 등)

공고 정보에 없는 내용은 추측해서 만들지 마라.

[공고 목록]
{items_txt}

JSON 배열로만 출력.
"""
    text, err = _call(prompt, json_mode=True)
    if err:
        return [], err
    return _parse_json(text, []), None


# ------------------------------------------------------------
# 8. 기회영역 비교 — 사업부 vs R&D 한줄 bullet 3개씩
# ------------------------------------------------------------
PRODUCT_DIRECT_CRITERIA = {
    "NF": "접속 폭주·대기열·동시접속이 생기는 대국민 서비스 (수강신청, 예약·예매, 청약, 원서접수, 선착순 신청, 티켓, 지원금 신청 오픈, 투표, 대국민 포털 구축·고도화)",
    "NFA": "API 호출량 제어·외부 연계 API 트래픽·API 게이트웨이가 사업 범위에 있는 것, 대국민 챗봇·LLM 서비스의 요청 폭주 대응 (AI 교육·AI 플랫폼 도입·AI 콘텐츠·GPU/NPU 인프라는 해당 없음)",
    "BM": "매크로·봇·부정예약·암표·어뷰징·크리덴셜 공격·자동화 접속 차단 (DDoS 장비, 악성코드 분석, 백신·보안장비 구매, 취약점 점검, 보안관제, 개인정보영향평가는 해당 없음)",
    "LT": "부하·성능·스트레스 테스트, 오픈 전 성능 검증, 대량 접속 장애 원인 점검",
}
_PRODUCT_CODE = {"넷퍼넬 (NF)": "NF", "넷퍼넬API (NFA)": "NFA", "봇매니저 (BM)": "BM", "로드테스터 (LT)": "LT"}


def match_titles_to_product(product_name, product_desc, titles, _err_default=None):
    """1차 키워드 매칭이 0건일 때 호출하는 AI 폴백.
    제품이 '직접' 쓰일 근거가 제목에 있는 것만 반환 (AI·보안·데이터 같은 단어만 같은 공고는 제외), 없으면 빈 배열."""
    if not titles:
        return [], None
    code = _PRODUCT_CODE.get(product_name, "")
    crit = PRODUCT_DIRECT_CRITERIA.get(code, product_desc)
    prompt = (
        f"다음은 공공 IT 공고/과제 제목 목록입니다.\n"
        f"'{product_name}' ({product_desc}) 제품이 그 사업에 '직접' 들어갈 근거가 제목에 있는 것만 고르세요.\n"
        f"[직접 연관 기준] {crit}\n"
        f"- 'AI'·'보안'·'데이터'·'시스템' 같은 단어만 같은 공고는 고르지 마세요.\n"
        f"- 확실한 것만 고르세요. 해당이 없으면 빈 배열 []이 정상입니다.\n"
        f"- 제목은 아래 목록의 문자열 그대로 JSON 배열로 반환하세요.\n\n"
        + "\n".join(f"- {t}" for t in titles[:150])
    )
    text, err = _call(prompt, json_mode=True)
    if err:
        return [], err
    result = _parse_json(text, [])
    return ([str(x) for x in result], None) if isinstance(result, list) else ([], None)


def generate_opportunity_bullets(biz_titles, rnd_titles):
    biz_text = "\n".join(f"- {t}" for t in biz_titles[:30])
    rnd_text = "\n".join(f"- {t}" for t in rnd_titles[:30])
    prompt = f"""아래는 사업부(수주·용역) 공고 목록과 R&D(개발과제) 공고 목록이다.
각각에 대해 지금 가장 주목해야 할 기회를 3개씩 뽑아라. 목록에 없는 내용은 만들지 마라.

각 항목은 다음 두 필드를 가진 객체로 작성해라:
- text: 왜 주목해야 하는지 한 줄 설명(기회 포인트)
- related_title: 이 기회의 근거가 된 공고 제목을 [사업부 공고]/[R&D 공고] 목록에 있는 제목 중 하나와
  '정확히 동일한 문자열'로 적어라. 절대 요약하거나 줄이지 말고 원문 그대로 복사해라.

[사업부 공고]
{biz_text}

[R&D 공고]
{rnd_text}

JSON으로 출력: {{"biz": [{{"text": "...", "related_title": "..."}}, ...], "rnd": [{{"text": "...", "related_title": "..."}}, ...]}}
"""
    text, err = _call(prompt, json_mode=True)
    if err:
        return {"biz": [], "rnd": []}, err
    result = _parse_json(text, {"biz": [], "rnd": []})

    def _normalize(items):
        normed = []
        for it in (items or []):
            if isinstance(it, dict):
                normed.append({
                    "text": str(it.get("text", "")).strip(),
                    "related_title": str(it.get("related_title", "")).strip(),
                })
            else:
                # 혹시 모델이 과거처럼 문자열만 줘도 안 깨지게 방어
                normed.append({"text": str(it).strip(), "related_title": ""})
        return normed

    result["biz"] = _normalize(result.get("biz"))
    result["rnd"] = _normalize(result.get("rnd"))
    return result, None


# ------------------------------------------------------------
# 9. 오늘의 IT 뉴스 종합 요약 (기존 유지)
# ------------------------------------------------------------
def generate_news_digest(titles):
    titles_text = "\n".join(f"- {t}" for t in titles[:60])
    prompt = f"""아래 오늘의 IT 뉴스 제목들을 분석해서 3~4문장으로 종합 분석해라.
제목에 없는 내용은 추측하지 말고, 공공 IT 영업/R&D 관점에서 어떤 의미가 있는지 짚어줘라. 해석 문장은 끝에 "(추정)".

[제목 목록]
{titles_text}
"""
    text, err = _call(prompt, json_mode=False)
    return text, err


# ------------------------------------------------------------
# 10. 뉴스-솔루션 연관도 스코어링 (당사 솔루션 기준, 의도적으로 솔루션 중심 유지)
# ------------------------------------------------------------
def score_news_relevance(items):
    titles_text = "\n".join(f"{i}. {it['title']}" for i, it in enumerate(items))
    prompt = f"""너는 에스티씨랩(대기열/트래픽 제어, 봇 차단, 부하테스트 솔루션 기업)의 영업 분석가다.
아래 뉴스 제목들이 이 솔루션들과 얼마나 연관 있는지 0~100점으로 평가해라.
인덱스 번호를 key로, 점수를 value로 하는 JSON 객체로만 출력해라.

[뉴스 목록]
{titles_text}

형식: {{"0": 85, "1": 40, ...}}
"""
    text, err = _call(prompt, json_mode=True)
    if err:
        return {}, err
    raw = _parse_json(text, {})
    try:
        return {int(k): int(v) for k, v in raw.items()}, None
    except Exception:
        return {}, "점수 파싱 실패"

def match_titles_to_group(group_name: str, group_desc: str, titles: list):
    """
    키워드 사전 매칭이 0건일 때 AI가 실질 연관성을 판단해 보강하는 폴백 함수.
    연관된 게 전혀 없으면 빈 리스트를 반환한다(억지로 끼워맞추지 않음).
    반환: (titles_list, error_str_or_None)
    """
    if not titles:
        return [], None
    if not is_ai_ready():
        return [], "Claude API 키가 설정되지 않았습니다."

    prompt = f"""다음은 공공 IT 공고/과제 제목 목록입니다.
'{group_name}' ({group_desc})와(과) 실질적으로 연관된 항목만 골라,
목록에 있는 제목을 "정확히 그대로" JSON 배열로 반환하세요.
연관된 항목이 전혀 없으면 빈 배열 []을 반환하세요. 억지로 끼워맞추지 마세요.

[제목 목록]
{chr(10).join(f"- {t}" for t in titles[:150])}

출력 형식: ["제목1", "제목2", ...] 형태의 JSON 배열만 출력하세요."""

    text, err = _call(prompt, json_mode=True)
    if err:
        return [], err
    result = _parse_json(text, None)
    if isinstance(result, list):
        return [str(t) for t in result], None
    return [], "AI 응답 형식 오류"


# ------------------------------------------------------------
# 11. 뉴스 제목 쉬운말 변환 — 초등학생도 이해 가능한 수준으로 압축
# ------------------------------------------------------------
def simplify_news_titles(items):
    """items: [{"title": ...}, ...]
    반환: ([{"title": 원문제목, "simple": "쉬운 한 문장"}, ...], error)"""
    if not items:
        return [], None
    lines = "\n".join(f"- {it.get('title','')}" for it in items)
    prompt = f"""아래 뉴스 제목들을 누구나 바로 이해할 수 있는 쉬운 한국어 한 문장으로 다시 써라.
규칙:
- 사실을 바꾸거나 부풀리지 마라. 제목에 없는 감정·평가·비유를 넣지 마라.
- 숫자·기관명·회사명·제품명은 그대로 남겨라.
- 어려운 전문용어만 쉬운 말로 풀고, 25~40자로 맞춰라.
- 입력 제목을 "title"에 원문 그대로, 쉬운 문장을 "simple"에 적어라.

[뉴스 목록]
{lines}

JSON 배열로만 출력: [{{"title": "...", "simple": "..."}}]
"""
    text, err = _call(prompt, json_mode=True)
    if err:
        return [], err
    return _parse_json(text, []), None


# ------------------------------------------------------------
# 12. [신규] 공고 일괄 분석 — main.py 전용 (수집 직후 1회만 실행, 결과는 DB에 저장)
#     한 번 호출로 사업구분·연관도·한줄요약·상세요약을 모두 받아
#     대시보드가 열릴 때마다 AI를 다시 부르지 않도록 한다.
# ------------------------------------------------------------
ANALYSIS_VERSION = "2026-10-08"   # 지시문을 바꾸면 날짜를 올림 → 다음 아침 수집 때 진행 중 공고를 1회 다시 분석

ANALYZE_PROMPT = """아래 공공 공고 1건을 분석해라.

[자사 솔루션]
- NF(넷퍼넬): 웹 접속 대기열·지연접속. 수강신청·예약·접수·티켓팅·지원금 신청 등 접속 폭주 대응
- BM/MB(봇매니저·엠버스터): 매크로·봇 차단. 예매·수강신청·예약 공정성, 부정접속 방지
- NFA(넷퍼넬API): API 트래픽 분산·제어
- LT(로드테스터): 부하·성능 테스트

[판단 기준]
- track: "BIZ"(입찰·용역·구매 등 수주 대상 사업) 또는 "RND"(정부 연구개발 과제·지원사업 공모)
- score: 자사 솔루션과의 연관도 0~100
  · 80+ : 대량 접속·예약·접수·수강신청·티켓·선착순·매크로 차단·부하테스트가 사업 범위에 직접 포함
  · 40~79 : 홈페이지·포털·통합예약·정보시스템 구축/고도화처럼 접속 폭주 대비가 필요할 수 있는 웹 사업
  · 0~39 : 일반 IT(데이터·AI 모델·인프라)지만 접속 폭주와 무관한 사업
  · 0~15 : 보도자료·포상·행사·교육·인력 모집·성과 공모 등 사업이 아닌 글
- track_reason·score_reason: 공고 내용에 근거한 이유 한 줄. 위 점수 구간·기준 숫자(예: 40~79, 80+)나 '범위'라는 말은 쓰지 마라
- oneline: 무엇을 하는 사업인지 20자 내외 한 줄
- summary: 초등학생도 이해하도록 쉬운 말 1~2문장. 마감일·기관명이 정보에 있으면 포함
  · 금액은 [공고 정보]의 '예산' 값을 글자 그대로 옮겨라. 본문 숫자로 바꾸거나 단위를 다시 계산하지 마라. 예산이 '미표기'면 금액을 쓰지 마라
  · 주어는 '수요·주관기관'으로 써라. 조달청은 입찰 대행기관이므로 주어로 쓰지 마라

[공고 정보]
{info}

JSON 형식: {{"track": "BIZ", "track_reason": "한 줄 근거", "score": 0, "score_reason": "한 줄 근거", "oneline": "...", "summary": "..."}}"""


def _normalize_analysis(obj):
    if not isinstance(obj, dict):
        return {"error": "AI 응답 형식 오류"}
    track = str(obj.get("track", "")).upper().strip()
    if track not in ("BIZ", "RND"):
        track = "RND" if "R" in track else "BIZ"
    try:
        score = max(0, min(100, int(float(obj.get("score", 0)))))
    except Exception:
        score = 0
    return {
        "track": track,
        "track_reason": str(obj.get("track_reason", "")).strip()[:300],
        "score": score,
        "score_reason": str(obj.get("score_reason", "")).strip()[:300],
        "oneline": str(obj.get("oneline", "")).strip()[:80],
        "summary": str(obj.get("summary", "")).strip()[:600],
    }


async def analyze_postings_batch(pending, progress_cb=None):
    """pending: [(uniq_key, info_block), ...]
    반환: {uniq_key: {"track","track_reason","score","score_reason","oneline","summary"} 또는 {"error": ...}}"""
    if not is_ai_ready():
        return {k: {"error": "Claude API 키 미설정"} for k, _ in pending}
    # 실행할 때마다 새 클라이언트 (asyncio.run을 여러 번 불러도 연결이 꼬이지 않도록)
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, max_retries=4, timeout=60)

    sem = asyncio.Semaphore(AI_MAX_CONCURRENCY)
    gap = 60.0 / max(AI_RPM_LIMIT, 1)
    lock = asyncio.Lock()
    last_sent = {"t": 0.0}
    results = {}

    async def _throttle():
        async with lock:
            wait = last_sent["t"] + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            last_sent["t"] = time.monotonic()

    fatal = {"msg": None}   # 크레딧 부족·키 오류면 나머지는 호출하지 않고 바로 실패 처리 (시간 낭비 방지)

    async def _one(key, info):
        async with sem:
            if fatal["msg"]:
                results[key] = {"error": fatal["msg"]}
                if progress_cb:
                    progress_cb(key, results[key])
                return
            await _throttle()
            try:
                resp = await client.messages.create(
                    model=MODEL_NAME,
                    max_tokens=700,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": ANALYZE_PROMPT.format(info=info)}],
                )
                res = _normalize_analysis(_parse_json(_resp_text(resp), None))
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"[:200]
                if "credit balance" in msg or "authentication_error" in msg or "invalid x-api-key" in msg.lower():
                    fatal["msg"] = "크레딧 부족 또는 API 키 오류 — 이번 실행은 AI 분석 생략"
                res = {"error": msg}
            results[key] = res
            if progress_cb:
                progress_cb(key, res)

    try:
        await asyncio.gather(*(_one(k, info) for k, info in pending))
    finally:
        try:
            await client.close()
        except Exception:
            pass
    return results
