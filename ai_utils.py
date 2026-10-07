import os
import re
import json

from dotenv import load_dotenv
load_dotenv()

try:
    import google.generativeai as genai
except ImportError:
    genai = None

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

_model = None


def is_gemini_ready():
    return bool(GEMINI_API_KEY) and genai is not None


def _get_model():
    global _model
    if not is_gemini_ready():
        return None
    if _model is None:
        genai.configure(api_key=GEMINI_API_KEY)
        _model = genai.GenerativeModel(MODEL_NAME)
    return _model


def _call(prompt, json_mode=True):
    model = _get_model()
    if model is None:
        return None, "Gemini API 키가 설정되지 않았습니다."
    try:
        cfg = {"response_mime_type": "application/json"} if json_mode else {}
        resp = model.generate_content(prompt, generation_config=cfg)
        return (resp.text or "").strip(), None
    except Exception as e:
        return None, str(e)


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
    prompt = f"""아래 뉴스·공고 제목들을 분석해서 핵심 키워드를 15~30개 추출해라.

각 키워드마다 다음 항목을 포함해라:
- keyword: 키워드 자체
- importance: 0~100 중요도 점수 (언급 빈도 + 업계 영향력을 종합 고려)
- count: 해당 키워드가 포함된 제목 수
- category: 이 키워드가 속하는 주제 분류명을 네가 직접 정해라.
  (예시일 뿐 그대로 쓰지 말고 실제 내용에 맞게 자유롭게 명명: 'AI', '사이버보안', '클라우드', '정책/제도', '산업동향' 등)
  특정 회사의 제품 카테고리로 분류하지 말고, 업계 전반의 주제로 분류해라.
- reason: 1줄 판단 근거
- sample_titles: 근거가 된 실제 제목 2~3개 (목록에 있는 제목 그대로)

[제목 목록]
{titles_text}

출력은 JSON 배열로만.
"""
    text, err = _call(prompt, json_mode=True)
    if err:
        return [], err
    result = _parse_json(text, [])
    # 거의 동일한 키워드가 중복 추출되는 경우 1차 정리 (importance 높은 쪽을 우선 유지)
    result = sorted(result, key=lambda x: -(x.get("importance") or 0))
    result = _dedupe_similar_keywords(result)
    return result, None


# ------------------------------------------------------------
# 5. 오늘의 헤드라인 — govit-briefing 스타일 한 줄 헤드라인 + 부연 2문장
# ------------------------------------------------------------
def generate_headline(titles):
    titles_text = "\n".join(f"- {t}" for t in titles[:40])
    prompt = f"""너는 공공 IT 시장 리서치 애널리스트다.
아래 공고/뉴스 제목들만 근거로, 오늘자 브리핑의 헤드라인을 작성해라.
- headline: 임팩트 있는 한 줄 (예: "AI 예산은 늘고 클라우드는 줄었다 — 10월은 마감 몰빵의 달" 같은 톤), 데이터에 없는 수치는 넣지 마라.
- subtext: 2문장 이내 부연 설명.

[제목 목록]
{titles_text}

JSON으로만 출력: {{"headline": "...", "subtext": "..."}}
"""
    text, err = _call(prompt, json_mode=True)
    if err:
        return None, err
    return _parse_json(text, None), None


# ------------------------------------------------------------
# 6. 오늘의 핵심 이슈 카드 — AI가 직접 클러스터링해서 테마 3~4개 생성
# ------------------------------------------------------------
def generate_key_issues(items_text, n=4):
    prompt = f"""아래는 오늘 기준 연관도가 높은 공공 IT 공고/뉴스 목록이다.
이 목록을 분석해서 핵심 이슈 테마 {n}개로 묶어라. 각 테마마다:
- theme: 테마명 (10자 내외)
- impact: "매우높음" | "높음" | "보통" | "낮음" 중 하나 (이 테마가 사업 기회에 미치는 영향도)
- confidence: "High" | "Medium" | "Low" (근거 자료의 신뢰도)
- summary: 1문장 핵심 요약
- count: 이 테마에 해당하는 항목 수(목록 기준으로 추정)

[목록]
{items_text}

JSON 배열로만 출력.
"""
    text, err = _call(prompt, json_mode=True)
    if err:
        return [], err
    return _parse_json(text, []), None


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
def match_titles_to_product(product_name, product_desc, titles, _err_default=None):
    """1차 키워드 매칭이 0건일 때 호출하는 AI 폴백.
    실질적으로 연관 있는 제목만 추려서 반환, 없으면 빈 배열."""
    if not titles:
        return [], None
    model = _get_model()
    if model is None:
        return [], "Gemini API가 설정되지 않았습니다."
    prompt = (
        f"다음은 공공 IT 공고/과제 제목 목록입니다.\n"
        f"'{product_name}' ({product_desc})과 실질적으로 연관된 공고 제목만 "
        f"아래 목록에 있는 문자열 그대로 골라 JSON 배열로 반환하세요.\n"
        f"억지로 끼워맞추지 말고, 연관 있는 게 전혀 없으면 빈 배열 []을 반환하세요.\n"
        f"반드시 JSON 배열만 출력하고 다른 설명은 넣지 마세요.\n\n"
        + "\n".join(f"- {t}" for t in titles[:150])
    )
    try:
        resp = model.generate_content(prompt)
        text = resp.text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        result = json.loads(text)
        if isinstance(result, list):
            return [str(x) for x in result], None
        return [], None
    except Exception as e:
        return [], str(e)

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
    prompt = f"""아래 오늘의 IT 뉴스 제목들을 분석해서 3~4문장으로 종합 요약해라.
제목에 없는 내용은 추측하지 말고, 공공 IT 영업/R&D 관점에서 어떤 의미가 있는지 짚어줘라.

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
    if not is_gemini_ready():
        return [], "Gemini API 키가 설정되지 않았습니다."

    prompt = f"""다음은 공공 IT 공고/과제 제목 목록입니다.
'{group_name}' ({group_desc})와(과) 실질적으로 연관된 항목만 골라,
목록에 있는 제목을 "정확히 그대로" JSON 배열로 반환하세요.
연관된 항목이 전혀 없으면 빈 배열 []을 반환하세요. 억지로 끼워맞추지 마세요.

[제목 목록]
{chr(10).join(f"- {t}" for t in titles[:150])}

출력 형식: ["제목1", "제목2", ...] 형태의 JSON 배열만 출력하세요."""

    try:
        # ↓↓↓ 아래 3줄을 기존 generate_opportunity_bullets 함수에서 쓰는
        #     모델 호출 방식(예: model.generate_content(prompt) 등)으로 그대로 교체해줘
        model = _get_model()
        resp = model.generate_content(prompt)
        raw = resp.text.strip()

        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(t) for t in result], None
        return [], "AI 응답 형식 오류"
    except Exception as e:
        return [], str(e)

# ------------------------------------------------------------
# 11. 뉴스 제목 쉬운말 변환 — 초등학생도 이해 가능한 수준으로 압축
# ------------------------------------------------------------
def simplify_news_titles(items):
    """items: [{"title": ...}, ...]
    반환: ([{"title": 원문제목, "simple": "쉬운 한 문장"}, ...], error)"""
    if not items:
        return [], None
    lines = "\n".join(f"- {it.get('title','')}" for it in items)
    prompt = f"""아래 뉴스 제목들을 초등학교 5~6학년도 바로 이해할 수 있는
아주 쉬운 한국어 한 문장으로 바꿔라. 전문 용어는 쉬운 말로 풀고,
원래 의미는 바꾸지 말고 20~35자 내외로 압축해라.
입력 제목을 "title" 필드에 원문 그대로 포함하고, 쉬운 문장은 "simple" 필드에 적어라.

[뉴스 목록]
{lines}

JSON 배열로만 출력: [{{"title": "...", "simple": "..."}}]
"""
    text, err = _call(prompt, json_mode=True)
    if err:
        return [], err
    return _parse_json(text, []), None
    