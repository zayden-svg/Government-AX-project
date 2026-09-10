# ai_utils.py
import os
import re
import json

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
GEMINI_MODEL_NAME = "gemini-2.5-flash"

_gemini_ready = False
if _genai_available and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_ready = True
    except Exception:
        _gemini_ready = False


def is_gemini_ready():
    return _gemini_ready


# ------------------------------------------------------------
# 자사 솔루션/역량 프로필 - 연관도 판단 기준
# 실제 회사 상황에 맞게 자유롭게 수정해 주세요.
# ------------------------------------------------------------
PRODUCT_PROFILE = """
- 회사 핵심 사업: 웹/앱 대기열·트래픽 관리 솔루션, 온라인 신원확인/인증, AI 기반 이상 트래픽 탐지
- 관심 기술 분야: AI/빅데이터, 클라우드 인프라, 사이버보안, 공공/금융 시스템 고도화, 재해복구(DR), 통합관제
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


def build_priority_prompt(info_block: str) -> str:
    return f"""당신은 IT 솔루션 기업의 사업개발 담당자를 돕는 어시스턴트입니다.
아래 [자사 프로필]을 기준으로 [공고 정보]가 자사 사업/영업/R&D 관점에서
얼마나 연관성이 높은지 0~100 사이의 점수로 평가해 주세요.

[자사 프로필]
{PRODUCT_PROFILE}

[공고 정보]
{info_block}

반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트를 추가하지 마세요.
{{"score": 0~100 사이 정수, "reason": "왜 그 점수를 주었는지 1문장 이유"}}
"""


def generate_priority_score(info_block: str):
    """반환: (score:int|None, reason:str, error:str|None)"""
    if not _gemini_ready:
        return None, "", "Gemini API 키가 설정되지 않았습니다."
    try:
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        resp = model.generate_content(build_priority_prompt(info_block))
        text = (resp.text or "").strip()
        data = _extract_json(text)
        if not data:
            return None, "", "AI 응답 파싱 실패"
        score = max(0, min(100, int(data.get("score", 0))))
        reason = str(data.get("reason", "")).strip()
        return score, reason, None
    except Exception as e:
        return None, "", str(e)


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
        resp = model.generate_content(build_summary_prompt(info_block))
        return (resp.text or "").strip(), None
    except Exception as e:
        return None, str(e)
