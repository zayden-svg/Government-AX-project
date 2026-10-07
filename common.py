# common.py
# 대시보드(app.py)와 매일 아침 배치(main.py·briefing_batch.py·alert_mailer.py)가
# 함께 쓰는 설정·판별 함수 모음. 한 곳만 고치면 양쪽에 동시에 반영된다.
import os
import re
import time
from datetime import datetime

try:   # 로컬 PC 실행 시 .env 파일의 키를 먼저 읽어 둠
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ------------------------------------------------------------
# 시간대 — 서버(Streamlit Cloud·GitHub Actions)는 UTC라 한국시간으로 고정
# ------------------------------------------------------------
os.environ["TZ"] = "Asia/Seoul"
try:
    time.tzset()
except AttributeError:      # Windows는 tzset 없음 (로컬 PC는 이미 한국시간)
    pass


def now_kst():
    return datetime.now()


def read_secret(name, default=""):
    """환경변수 → Streamlit secrets 순서로 값을 찾는다 (GitHub Actions / Streamlit Cloud 겸용)."""
    val = (os.getenv(name) or "").strip()
    if val:
        return val
    try:
        import streamlit as st
        return str(st.secrets.get(name, default) or default).strip()
    except Exception:
        return default


# ------------------------------------------------------------
# 자사 솔루션 키워드 / R&D 도메인 키워드
# ------------------------------------------------------------
PRODUCT_KEYWORDS = {
    "넷퍼넬 (NF)": {"desc": "가상 대기실 · 트래픽·대기열 관리",
                   "keywords": ["넷퍼넬", "NetFUNNEL", "가상대기실", "가상 대기실", "대기열", "대기방", "대기 페이지", "대기페이지",
                                "트래픽 제어", "트래픽 관리", "트래픽 폭주", "동시접속", "접속량", "진입 허용", "서버 다운", "서버다운",
                                "먹통", "수강신청", "청약", "예매", "티켓", "선착순", "예약 시스템", "예약시스템", "통합예약",
                                "대량접속제어", "대량접속", "순번대기", "순차처리", "접속자 순차처리", "유량제어", "접속제어",
                                "트랜잭션 제어", "접속대기", "대기시스템"]},
    "넷퍼넬API (NFA)": {"desc": "API 트래픽 제어 · AI 에이전트 트래픽 대응",
                      "keywords": ["넷퍼넬API", "넷퍼넬 API", "NetFUNNEL API", "NFA",
                                   "API 트래픽", "API 트래픽 제어", "API 대기열", "대기열 API", "API 제어",
                                   "에이전트 트래픽", "LLM 트래픽", "API 요청"]},
    "봇매니저 (BM)": {"desc": "봇 탐지·차단 · 매크로·어뷰징 방어",
                    "keywords": ["봇매니저", "봇 매니저", "BotManager", "MBUSTER", "엠버스터", "엠버스터v2", "MBUSTER v2.0",
                                 "봇탐지", "봇 탐지", "봇 차단", "악성봇", "악성 봇", "매크로", "매크로탐지", "매크로 탐지",
                                 "매크로차단", "매크로 차단", "어뷰징", "부정예약", "부정 예약", "부정접속", "부정 접속",
                                 "크리덴셜", "스크래핑", "리셀", "되팔이", "선점구매", "선점 구매"]},
    "로드테스터 (LT)": {"desc": "웹·앱 부하테스트(성능 검증)",
                     "keywords": ["로드테스터", "로드 테스터", "LoadTester", "Load Tester",
                                  "부하테스트", "부하 테스트", "부하시험", "부하 시험", "성능테스트", "성능 테스트",
                                  "스트레스 테스트", "가상사용자", "가상 사용자"]},
}

INTEGRATED_RND_DOMAINS = {
    "AI 모델": {"desc": "생성형 · 에이전틱 AI 등 모델 개발", "keywords": ["AI", "인공지능", "생성형", "LLM", "거대언어", "에이전틱", "머신러닝", "딥러닝", "파운데이션 모델", "파운데이션모델"]},
    "데이터": {"desc": "학습용 데이터 · 데이터셋 구축", "keywords": ["데이터", "데이터셋", "학습용", "빅데이터", "데이터 품질", "벤치마크"]},
    "보안·인증": {"desc": "사이버보안 · 취약점 · 인증", "keywords": ["보안", "사이버", "취약점", "침해", "인증", "신원확인", "신원 확인"]},
    "클라우드": {"desc": "클라우드 · GPU · 인프라", "keywords": ["클라우드", "컨테이너", "쿠버네티스", "GPU", "데이터센터", "데이터 센터", "서버"]},
    "표준·정책": {"desc": "표준화 · 정책연구 · 실태조사", "keywords": ["표준", "정책", "기획", "실태조사", "성과분석", "가이드"]},
}

PRODUCT_TO_DOMAIN = {
    "넷퍼넬 (NF)": ["클라우드", "표준·정책"],
    "넷퍼넬API (NFA)": ["AI 모델", "데이터"],
    "봇매니저 (BM)": ["보안·인증"],
    "로드테스터 (LT)": ["클라우드"],
}

PROCUREMENT_BOOST_KEYWORDS = sorted({kw for info in PRODUCT_KEYWORDS.values() for kw in info["keywords"]})
SOLUTION_KEYWORDS_LOWER = [k.lower() for k in PROCUREMENT_BOOST_KEYWORDS]

# 뉴스 기본 키워드 (아침 배치가 미리 수집해 두는 묶음)
DEFAULT_NEWS_KEYWORDS = ["AI", "공공IT"]
SOLUTION_NEWS_KEYWORDS = ["넷퍼넬", "봇매니저", "MBUSTER", "부하테스트", "대기열"]

# ------------------------------------------------------------
# 경쟁사
# ------------------------------------------------------------
COMPETITOR_DEFAULT = ["DynaPath", "다이나패스", "다이내패스", "EverSafe", "에버세이프",
                      "엑스큐", "xQueue", "소프트베이스", "큐잇", "Queue-it", "데브와이", "메가펜스"]
COMPETITOR_ALIASES = {
    "dynapath": ["dynapath", "다이나패스", "다이내패스"],
    "eversafe": ["eversafe", "에버세이프"],
}


def competitor_variants(name_list):
    out = []
    for name in name_list or []:
        key = str(name).strip().lower()
        if not key:
            continue
        out.append(key)
        out.extend(COMPETITOR_ALIASES.get(key, []))
    return list(dict.fromkeys(out))


def is_competitor_match(text, competitor_keywords=None):
    low = str(text or "").lower()
    return any(v in low for v in competitor_variants(competitor_keywords or COMPETITOR_DEFAULT))


def is_solution_related(text):
    low = str(text or "").lower()
    return any(k in low for k in SOLUTION_KEYWORDS_LOWER)


def procurement_boost_score(title):
    return 15 if is_solution_related(title) else 0


# ------------------------------------------------------------
# 행안부 게시판 — 사업·공모 성격이 아닌 일반 보도자료 제외용
# ------------------------------------------------------------
MOIS_KEEP_WORDS = ["공고", "입찰", "모집", "공모", "용역", "사업 안내", "선정", "지원사업", "수요조사"]


def is_mois_noise(agency, title):
    return "행정안전부" in str(agency or "") and not any(w in str(title or "") for w in MOIS_KEEP_WORDS)


# ------------------------------------------------------------
# 지역 판별 — 기관명·부서명·제목·본문에 들어간 시·도/시·군 이름으로 판단
#   (오인식이 잦은 단어는 일부러 뺐음: 대전환→대전, 경기 침체→경기, 강진(지진) 등)
# ------------------------------------------------------------
REGION_PATTERNS = {
    "서울": [r"서울", r"강남구", r"서초구", r"송파구", r"강동구", r"마포구", r"용산구", r"성동구", r"광진구", r"동대문구",
           r"중랑구", r"성북구", r"강북구", r"도봉구", r"노원구", r"은평구", r"서대문구", r"양천구", r"구로구", r"금천구",
           r"영등포구", r"동작구", r"관악구", r"종로구"],
    "인천": [r"인천", r"송도", r"강화군", r"옹진군", r"인하대"],
    "경기": [r"경기도", r"경기권", r"수원", r"성남", r"용인", r"고양시", r"화성시", r"부천", r"안산", r"안양", r"남양주",
           r"평택", r"의정부", r"시흥", r"파주", r"김포", r"광명시", r"하남시", r"오산시", r"이천시", r"판교"],
    "강원": [r"강원", r"춘천", r"원주", r"강릉", r"동해시", r"태백", r"속초", r"삼척", r"홍천", r"횡성", r"영월", r"평창",
           r"정선", r"철원", r"화천", r"양구", r"양양"],
    "충북": [r"충북", r"충청북도", r"청주", r"충주", r"제천"],
    "충남": [r"충남", r"충청남도", r"천안", r"아산", r"보령", r"서산", r"논산", r"당진", r"홍성"],
    "대전": [r"대전(?!환)", r"한밭"],
    "세종": [r"세종시", r"세종특별자치시"],
    "전북": [r"전북", r"전라북도", r"전주", r"군산", r"익산", r"정읍", r"남원", r"김제", r"진안", r"무주", r"임실", r"순창",
           r"고창", r"부안"],
    "전남": [r"전남(?!편)", r"전라남도", r"목포", r"여수", r"순천", r"나주", r"광양", r"담양", r"곡성", r"구례", r"고흥",
           r"화순", r"장흥", r"해남", r"영암", r"무안", r"함평", r"완도", r"신안"],
    "광주": [r"광주광역시", r"광주과학기술원", r"GIST", r"조선대", r"(?<!경기도 )(?<!경기 )광주(?!시)"],
    "경북": [r"경북", r"경상북도", r"포항", r"경주", r"구미", r"안동", r"김천", r"영주", r"상주"],
    "경남": [r"경남", r"경상남도", r"창원", r"진주", r"김해", r"양산", r"거제", r"통영", r"사천", r"밀양"],
    "대구": [r"대구"],
    "울산": [r"울산"],
    "부산": [r"부산"],
    "제주": [r"제주", r"서귀포"],
}
_REGION_RE = {r: re.compile("|".join(p)) for r, p in REGION_PATTERNS.items()}
ALL_REGIONS = list(REGION_PATTERNS.keys())
NATIONAL_LABEL = "전국·중앙"            # 지역이 안 잡힌 공고(중앙부처·전국 공모 등)
MY_REGIONS_DEFAULT = ["서울", "인천", "강원", "전북", "전남", "광주", "제주"]   # 담당: 서울·인천·강원·전라·제주


def detect_regions(*texts):
    blob = " ".join(str(t or "") for t in texts)
    found = [r for r, rx in _REGION_RE.items() if rx.search(blob)]
    return found


def region_label(regions):
    return "·".join(regions) if regions else NATIONAL_LABEL


def match_regions(row_regions, selected, include_national=True):
    """row_regions: 리스트, selected: 고른 지역 리스트. 고른 게 없으면 전부 통과."""
    if not selected:
        return True
    if not row_regions:
        return include_national
    return any(r in selected for r in row_regions)


# ------------------------------------------------------------
# 메일 알림 기본값
# ------------------------------------------------------------
ALERT_MIN_SCORE_DEFAULT = 70
DEFAULT_SUBSCRIBER = "zayden@stclab.com"


def allowed_email_domains():
    """메일 알림 등록 허용 도메인 (공개 사이트라 외부인이 아무 주소나 등록하는 것을 막음).
    바꾸려면 Secrets에 ALLOWED_EMAIL_DOMAINS="stclab.com,example.com" 형식으로 지정."""
    raw = read_secret("ALLOWED_EMAIL_DOMAINS") or "stclab.com"
    return [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def validate_email(email):
    email = str(email or "").strip().lower()
    if not EMAIL_RE.match(email):
        return None, "이메일 형식이 올바르지 않습니다."
    domains = allowed_email_domains()
    if domains and email.split("@")[-1] not in domains:
        return None, f"사내 메일({', '.join('@' + d for d in domains)})만 등록할 수 있습니다."
    return email, None
