# ai_utils.py
import os
import re
import json
import time
import asyncio
from collections import deque

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv("gemini_api.env")  # 별도 파일로 키를 관리 중인 경우도 함께 로드
except ImportError:
    pass

try:
    import google.generativeai as genai
    _genai_available = True
except ImportError:
    _genai_available = False

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
# 분류/점수 판단처럼 가벼운 작업은 Flash-Lite가 더 빠르고, 무료 티어 분당 요청 한도(RPM)도 더 넉넉함
GEMINI_MODEL_NAME = "gemini-2.5-flash-lite"

# AI 호출 1건당 최대 이만큼(초)까지만 기다리고, 넘으면 실패로 처리하고 다음으로 넘어감
AI_TIMEOUT_SECONDS = 25

# 동시에 몇 건까지 병렬로 Gemini에 요청할지 (너무 크게 잡으면 429 에러 위험)
AI_MAX_CONCURRENCY = 8

# 분당 몇 건까지 허용할지 (Flash-Lite 무료 한도 15RPM보다 낮게 여유를 둠)
AI_RPM_LIMIT = 14

_gemini_ready = False
if _genai_available and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_ready = True
    except Exception:
        _gemini_ready = False


def is_gemini_ready():
    return _gemini_ready


PRODUCT_PROFILE = """
- 회사 핵심 사업: 웹/앱 대기열·트래픽 관리 솔루션(NetFUNNEL, 온프렘/SaaS 모두 지원),
  온라인 신원확인/부정접속 방어 솔루션(봇매니저 SaaS), AI 기반 이상 트래픽 탐지
- 관심 기술 분야: AI/빅데이터, 클라우드 인프라, 사이버보안, 공공/금융 시스템 고도화,
  재해복구(DR), 통합관제, 대량접속 제어
- 관심 고객: 공공기관, 금융기관, 대형 포털/커머스사
- 관심 사업 형태: SI/SM 용역, 시스템 구축·고도화, R&D 공동연구, AI 솔루션 실증사업
"""


def _extract_json(text: str):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def _extract_json_array(text: str):
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def build_analysis_prompt(info_block: str) -> str:
    return f"""당신은 IT 솔루션 기업의 사업개발 담당자를 돕는 어시스턴트입니다.
아래 [자사 프로필]을 참고하여 [공고 정보]에 대해 두 가지를 판단하세요.

[자사 프로필]
{PRODUCT_PROFILE}

[공고 정보]
{info_block}

판단할 내용:
1. track: 이 공고가 "연구개발·기술개발·실증 성격의 R&D 과제"에 가까운지,
   "입찰·용역·제품구매처럼 매출과 직결되는 사업부 과제"에 가까운지
   단순 키워드가 아니라 공고의 실제 성격(연구비 지원 방식인지, 조달/구매 계약 방식인지)을
   근거로 판단하세요. 반드시 "RND" 또는 "BIZ" 중 하나만 답하세요.
2. track_reason: 왜 그렇게 판단했는지 1문장 이유.
3. score: 자사 프로필을 기준으로 영업 또는 연구협력 관점의 연관도를 0~100 사이 정수로 평가.
4. score_reason: 왜 그 점수를 주었는지 1문장 이유.

정보가 부족해서 확신하기 어려우면, score는 낮게 주고 score_reason에 "정보 부족으로 판단 어려움"
이라고 명시하세요. 절대 근거 없이 추측해서 확정적으로 답하지 마세요.

반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트를 절대 추가하지 마세요.
{{"track": "RND 또는 BIZ", "track_reason": "...", "score": 0~100 사이 정수, "score_reason": "..."}}
"""


def _parse_analysis_response(text_resp: str):
    data = _extract_json(text_resp)
    if not data:
        return None
    track = str(data.get("track", "")).strip().upper()
    if track not in ("RND", "BIZ"):
        track = "BIZ"
    try:
        score = max(0, min(100, int(data.get("score", 0))))
    except Exception:
        score = None
    return {
        "track": track,
        "track_reason": str(data.get("track_reason", "")).strip(),
        "score": score,
        "score_reason": str(data.get("score_reason", "")).strip(),
    }


# ------------------------------------------------------------
# 동기(순차) 버전 - 단건 분석이 필요할 때(예: app.py에서 특정 공고 재분석) 사용
# ------------------------------------------------------------
def analyze_posting(info_block: str, max_retries: int = 1):
    if not _gemini_ready:
        return {"error": "Gemini API 키가 설정되지 않았습니다."}

    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            model = genai.GenerativeModel(GEMINI_MODEL_NAME)
            resp = model.generate_content(
                build_analysis_prompt(info_block),
                request_options={"timeout": AI_TIMEOUT_SECONDS},
            )
            parsed = _parse_analysis_response((resp.text or "").strip())
            if not parsed:
                last_error = "AI 응답 파싱 실패"
                continue
            return parsed
        except Exception as e:
            last_error = str(e)
            continue

    return {"error": f"{max_retries + 1}회 시도 후 실패: {last_error}"}


# ------------------------------------------------------------
# 분당 요청 수 제한기 (leaky bucket) - 여러 코루틴이 동시에 써도 안전하게 카운트
# ------------------------------------------------------------
class _RateLimiter:
    def __init__(self, max_calls_per_minute: int):
        self.max_calls = max_calls_per_minute
        self._timestamps = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] > 60:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_calls:
                    self._timestamps.append(now)
                    return
                wait_time = 60 - (now - self._timestamps[0]) + 0.05
                await asyncio.sleep(wait_time)


_rate_limiter = _RateLimiter(AI_RPM_LIMIT)


async def _analyze_posting_async(info_block: str, max_retries: int = 1):
    if not _gemini_ready:
        return {"error": "Gemini API 키가 설정되지 않았습니다."}

    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            await _rate_limiter.acquire()
            model = genai.GenerativeModel(GEMINI_MODEL_NAME)
            resp = await model.generate_content_async(
                build_analysis_prompt(info_block),
                request_options={"timeout": AI_TIMEOUT_SECONDS},
            )
            parsed = _parse_analysis_response((resp.text or "").strip())
            if not parsed:
                last_error = "AI 응답 파싱 실패"
                continue
            return parsed
        except Exception as e:
            last_error = str(e)
            await asyncio.sleep(2)
            continue

    return {"error": f"{max_retries + 1}회 시도 후 실패: {last_error}"}


async def analyze_postings_batch(items, progress_cb=None, max_retries: int = 1):
    """
    items: [(key, info_block), ...] 형태의 리스트
    progress_cb: 건 하나 끝날 때마다 (key, result) 로 호출되는 콜백 (선택)
    반환값: {key: result_dict, ...}
    """
    semaphore = asyncio.Semaphore(AI_MAX_CONCURRENCY)
    results = {}

    async def worker(key, info_block):
        async with semaphore:
            result = await _analyze_posting_async(info_block, max_retries=max_retries)
        results[key] = result
        if progress_cb:
            progress_cb(key, result)

    tasks = [asyncio.create_task(worker(k, ib)) for k, ib in items]
    if tasks:
        await asyncio.gather(*tasks)
    return results


def build_summary_prompt(info_block: str) -> str:
    return f"""당신은 정부 R&D/IT 사업 공고를 분석하는 어시스턴트입니다.
아래 공고 정보를 참고하여 실무자가 5초 안에 핵심만 파악할 수 있도록
아주 간결하게 2~3문장으로 요약해 주세요.

작성 규칙:
1) 공고의 핵심 내용, 우리 조직에 왜 중요한지, 마감일/예산/자격 조건처럼
   놓치면 안 되는 정보만 압축해서 담을 것.
2) 마감일, 예산, 자격조건, 사업명 등 핵심 키워드에는 반드시
   마크다운 굵게(**단어**) 표시를 할 것.
3) 목록(bullet)이나 번호 매기기는 쓰지 말고 줄글로 쓸 것.
4) 주어진 정보에 없는 내용은 추측하지 말 것.

[공고 정보]
{info_block}
"""


def generate_summary(info_block: str):
    """반환: (summary_text:str|None, error:str|None)"""
    if not _gemini_ready:
        return None, "Gemini API 키가 설정되지 않았습니다."
    try:
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        resp = model.generate_content(
            build_summary_prompt(info_block),
            request_options={"timeout": AI_TIMEOUT_SECONDS},
        )
        return (resp.text or "").strip(), None
    except Exception as e:
        return None, str(e)


# ------------------------------------------------------------
# [신규] 추천 키워드 생성 - 자사 프로필 + 최근 공고 제목을 근거로 뉴스 검색용 키워드 추천
# ------------------------------------------------------------
def build_keyword_recommendation_prompt(sample_titles) -> str:
    joined = "\n".join(f"- {t}" for t in sample_titles[:60])
    return f"""당신은 IT 솔루션 기업의 사업개발 담당자를 돕는 어시스턴트입니다.
아래 [자사 프로필]과 [최근 수집된 공고 제목 목록]을 참고하여,
IT 뉴스 검색에 사용하면 좋을 대표 키워드를 5~8개 추천하세요.

각 키워드는 반드시 아래 둘 중 하나 이상을 근거로 선정하세요:
1) 최근 공고 제목에서 실제로 자주 등장하는 주제/기술 트렌드
2) 자사 프로필(솔루션/기술분야/고객군)과 직접적인 연관성

[자사 프로필]
{PRODUCT_PROFILE}

[최근 수집된 공고 제목 목록]
{joined}

반드시 아래 JSON 배열 형식으로만 답변하세요. 다른 텍스트를 절대 추가하지 마세요.
[{{"keyword": "짧은 키워드", "reason": "왜 이 키워드를 추천했는지 1문장"}}, ...]
"""


def recommend_keywords(sample_titles, max_keywords: int = 8):
    """반환: (추천리스트, error). 추천리스트는 [{"keyword":..., "reason":...}, ...]"""
    if not _gemini_ready:
        return [], "Gemini API 키가 설정되지 않았습니다."
    if not sample_titles:
        return [], "분석할 공고 데이터가 없습니다."
    try:
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        resp = model.generate_content(
            build_keyword_recommendation_prompt(sample_titles),
            request_options={"timeout": AI_TIMEOUT_SECONDS},
        )
        data = _extract_json_array((resp.text or "").strip())
        if not data:
            return [], "AI 응답 파싱 실패"
        results = []
        for d in data[:max_keywords]:
            kw = str(d.get("keyword", "")).strip()
            reason = str(d.get("reason", "")).strip()
            if kw:
                results.append({"keyword": kw, "reason": reason})
        return results, None
    except Exception as e:
        return [], str(e)


# ------------------------------------------------------------
# [신규] 뉴스 다이제스트 - 여러 뉴스 제목을 모아서 오늘의 트렌드를 한번에 요약
# ------------------------------------------------------------
def build_news_digest_prompt(titles) -> str:
    joined = "\n".join(f"- {t}" for t in titles[:40])
    return f"""당신은 IT 사업개발 담당자를 돕는 어시스턴트입니다.
아래는 오늘 수집된 IT 뉴스 제목 목록입니다. 이 제목들만 근거로 삼아 오늘의 핵심 트렌드를
3~4문장으로 요약해 주세요.

작성 규칙:
1) 여러 기사에서 반복적으로 등장하는 주제나 키워드가 있다면 언급할 것.
2) 가능하다면 자사(대기열/트래픽 관리, 신원확인/부정접속 방어, AI 이상탐지 솔루션 기업) 관점에서
   왜 주목할 만한지도 함께 짚을 것.
3) 목록(bullet)이나 번호 매기기 없이 줄글로 작성할 것.
4) 핵심 키워드에는 마크다운 굵게(**단어**) 표시를 할 것.
5) 주어진 제목 목록에 없는 내용은 절대 추측하지 말 것.

[오늘의 뉴스 제목 목록]
{joined}
"""


def generate_news_digest(titles):
    """반환: (요약텍스트:str|None, error:str|None)"""
    if not _gemini_ready:
        return None, "Gemini API 키가 설정되지 않았습니다."
    if not titles:
        return None, "요약할 뉴스 제목이 없습니다."
    try:
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        resp = model.generate_content(
            build_news_digest_prompt(titles),
            request_options={"timeout": AI_TIMEOUT_SECONDS},
        )
        return (resp.text or "").strip(), None
    except Exception as e:
        return None, str(e)

def build_relevance_batch_prompt(items):
    lines = [f"{i}. [{it.get('source', '')}] {it['title']}" for i, it in enumerate(items)]
    joined = "\n".join(lines)
    return f"""당신은 IT 기업의 사업 전략 분석가입니다. 아래 [회사 프로필]을 참고하여,
[뉴스 목록]에 있는 각 뉴스 제목이 이 회사의 솔루션과 얼마나 연관이 있는지 0~100점으로 평가하세요.
점수가 높을수록 자사 영업/사업 기회와 직접적으로 연관됨을 의미합니다.

[회사 프로필]
{PRODUCT_PROFILE}

[뉴스 목록]
{joined}

아래 JSON 배열 형식으로만 답하세요. 다른 설명이나 코드블록 표시(```) 없이 순수 JSON만 출력하세요.
[{{"index": 0, "score": 87}}, {{"index": 1, "score": 12}}]
"""


def score_news_relevance(items):
    """items: [{"title": str, "source": str}, ...] -> ({index: score}, error)"""
    if not items:
        return {}, None
    if not is_gemini_ready():
        return {}, "Gemini API 키가 설정되지 않았습니다."
    try:
        prompt = build_relevance_batch_prompt(items)
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        response = model.generate_content(
            prompt,
            request_options={"timeout": AI_TIMEOUT_SECONDS},
        )
        data = _extract_json_array(response.text)
        if not isinstance(data, list):
            return {}, "AI 응답 형식이 올바르지 않습니다."
        score_map = {}
        for d in data:
            try:
                score_map[int(d["index"])] = int(d["score"])
            except (KeyError, ValueError, TypeError):
                continue
        return score_map, None
    except Exception as e:
        return {}, str(e)

def build_trend_keyword_prompt(titles):
    joined = "\n".join(f"- {t}" for t in titles)
    return f"""
다음은 오늘 수집된 IT/공공 뉴스 제목 목록이다.

[분석 대상 제품 프로필]
{PRODUCT_PROFILE}

[뉴스 제목 목록]
{joined}

위 뉴스 제목들을 분석해서, 오늘 가장 두드러지는 핵심 트렌드 키워드를 최대 12개까지 추출해라.
각 키워드는 실제 뉴스 제목에 등장한 단어/구절이거나 그것을 요약한 표현이어야 한다.
단순 빈도가 아니라, 위 제품 프로필과의 사업적 연관성(영업 기회, 경쟁 동향, 시장 이슈)도 함께 고려해서 중요도를 매겨라.

각 키워드마다 다음 정보를 JSON 배열로 반환해라:
- keyword: 키워드(2~10자 내외)
- count: 이 키워드가 언급된 것으로 보이는 뉴스 건수(추정치)
- importance: 1~100 사이 중요도 점수 (사업 연관성 + 빈도 종합)
- reason: 왜 이 키워드가 중요한지 1문장 근거
- sample_titles: 관련 실제 뉴스 제목 1~3개 (원문 그대로)

반드시 JSON 배열만 출력해라. 다른 설명 텍스트는 포함하지 마라.
예시: [{{"keyword": "서버다운", "count": 4, "importance": 88, "reason": "...", "sample_titles": ["...", "..."]}}]
"""


def extract_trend_keywords(titles, top_n=12):
    if not titles:
        return [], None
    if not is_gemini_ready():
        return [], "Gemini API 키가 설정되지 않았습니다."
    try:
        prompt = build_trend_keyword_prompt(titles[:200])
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        response = model.generate_content(prompt, request_options={"timeout": AI_TIMEOUT_SECONDS})
        data = _extract_json_array(response.text)
        if not isinstance(data, list):
            return [], "AI 응답 형식이 올바르지 않습니다."
        cleaned = []
        for d in data:
            try:
                cleaned.append({
                    "keyword": str(d["keyword"]).strip(),
                    "count": int(d.get("count", 0)),
                    "importance": int(d.get("importance", 0)),
                    "reason": str(d.get("reason", "")).strip(),
                    "sample_titles": list(d.get("sample_titles", []))[:3],
                })
            except (KeyError, ValueError, TypeError):
                continue
        cleaned.sort(key=lambda x: -x["importance"])
        return cleaned[:top_n], None
    except Exception as e:
        return [], str(e)
