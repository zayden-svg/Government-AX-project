"""Approved product facts. Never load the withdrawn legacy MBUSTER brochure."""
import hashlib
import json

PROFILE_VERSION = "2026-09-10-owner-correction-v1"
PRODUCTS = [
    {
        "name": "NetFUNNEL",
        "status": "판매 중",
        "deployment": ["온프레미스", "SaaS"],
        "capabilities": ["가상 대기실", "동시 접속 진입량 제어", "대규모 접속 시 순번 대기"],
        "sources": ["제품소개서_NetFUNNEL_26Q2.pdf 4~5쪽", "사용자 제품 기준 정정 2026-09-10"],
    },
    {
        "name": "NetFUNNEL API",
        "status": "판매 중",
        "capabilities": ["API 대기열 기반 진입 제어", "요청 우선순위 제어", "응답시간 기반 제어", "외부 메트릭 기반 제어"],
        "sources": ["26Q3_NetFUNNEL API_Full.pdf 6, 9, 10, 15쪽"],
    },
    {
        "name": "BotManager",
        "status": "판매 중",
        "deployment": ["SaaS"],
        "capabilities": ["악성 봇 탐지 및 차단", "접속 환경·빈도·패턴 기반 분석", "서비스 공정성 확보"],
        "sources": ["제품소개서_BotManager_26Q2.pdf 4~5쪽", "사용자 제품 기준 정정 2026-09-10"],
    },
    {
        "name": "MBUSTER",
        "status": "2026년 말 제품화 및 판매 예정 — 출시 여부 별도 확인 필요",
        "deployment": ["온프레미스"],
        "capabilities": [],
        "sources": ["사용자 제품 기준 정정 2026-09-10"],
        "restriction": "BotManager 온프레미스 제품의 예정 명칭. 현재 판매 제품으로 추천 금지. 세부 기능·인증·성능은 미확정이며 구 MBUSTER 자료로 보충 금지.",
    },
]
EXCLUDED_DOCUMENTS = ["제품소개서_MBUSTER.pdf"]
PROFILE = {
    "version": PROFILE_VERSION,
    "products": PRODUCTS,
    "rules": [
        "구 MBUSTER PDF와 기존 MBUSTER 키워드·낙찰 사례는 분석 근거에서 제외한다.",
        "통합 제품소개서의 MBUSTER 부분도 사용하지 않는다.",
        "AI·클라우드·보안 사업이라는 이유만으로 특정 제품의 구매 필요성을 단정하지 않는다.",
        "고객 수·성능·매출 효과·보안 인증·온라인 신원확인 역량을 근거 없이 추정하지 않는다.",
        "향후 제품은 출시 예정으로만 표시하며 시간이 지났다고 자동으로 판매 중으로 바꾸지 않는다.",
    ],
}
PROFILE_JSON = json.dumps(PROFILE, ensure_ascii=False, sort_keys=True)
PROFILE_HASH = hashlib.sha256(PROFILE_JSON.encode()).hexdigest()
