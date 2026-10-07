"""
biz_classifier.py
나라장터 실제 낙찰 데이터(NetFUNNEL/MBUSTER 5년치 138건) + 신규 2제품(NFA/WA) 카테고리 특성 기반
사업명/사업내용에서 등급과 추천솔루션(NF/MB/NFA/WA/일반)을 판단하는 통합 분류기.

- classifier.py(엑셀 기반)는 이 파일로 완전히 대체되었습니다. classifier.py 파일은 삭제하세요.
- main.py가 이 함수의 반환 키("등급","카테고리","추천솔루션","매칭키워드")를 그대로 사용하므로
  키 이름을 절대 바꾸지 마세요.
- NF/MB는 실제 138건 낙찰 데이터 기반 검증된 키워드입니다.
- NFA/WA는 레퍼런스(수주 사례)가 없어 제품 카테고리 특성으로 추정한 키워드입니다.
  실제 매칭 사례가 쌓이면 주기적으로 키워드를 보강해주세요.
"""

# ── 1차(강한 신호) ──────────────────────────────────────────
TIER1_KEYWORDS = {
    "트래픽·접속제어": {
        "keywords": ["대기열", "순번대기", "대기시스템", "접속대기", "대기관리",
                     "대량접속제어", "대량접속", "대량제어", "대량접근제어",
                     "접속자순차처리", "순차처리", "유량제어", "트래픽제어",
                     "트래픽관리", "트래픽", "접속제어", "성능제어", "동시접속",
                     "대기알림", "접속부하관리"],
        "solution": "NF",
    },
    "업무유형(예약·청약·접수·시험)": {
        "keywords": ["청약", "전자청약", "예약시스템", "통합예약", "원서접수",
                     "접수대기", "수강신청", "채용시스템", "통합채용",
                     "능력검정시험", "자격시험", "IBT시스템", "큐넷"],
        "solution": "NF",
    },
    "매크로·부정접속방어": {
        "keywords": ["매크로 탐지", "매크로탐지", "매크로 차단", "매크로차단",
                     "매크로 방지", "매크로방지", "부정접속", "위변조방지",
                     "봇 탐지", "봇탐지", "어뷰징", "부정예약", "부정구매"],
        "solution": "MB",
    },
    "API 트래픽·게이트웨이": {
        "keywords": ["API 관리", "API관리", "API 게이트웨이", "API게이트웨이",
                     "API 트래픽", "API트래픽", "API 대량접속", "API 접속제어",
                     "오픈API 플랫폼", "API 보안", "API 모니터링", "마이크로서비스 API",
                     "공공 API", "공공데이터 API"],
        "solution": "NFA",
    },
    "클라우드 오토스케일·DevOps": {
        "keywords": ["오토스케일", "오토스케일링", "쿠버네티스", "Kubernetes", "K8s",
                     "클라우드 비용 최적화", "클라우드비용", "인프라 자동 확장",
                     "DevOps 자동화", "컨테이너 오케스트레이션", "리소스 자동 조정",
                     "클라우드 네이티브 전환", "탄력적 확장"],
        "solution": "WA",
    },
    "시스템구축·전환": {
        "keywords": ["차세대", "고도화", "통합플랫폼", "통합시스템",
                     "통합관리시스템", "통합채용시스템"],
        "solution": "NF",
    },
}

# ── 2차(보조 신호, 단독으로는 약하지만 결합시 유효) ─────────────
TIER2_KEYWORDS = {
    "보조신호": {
        "keywords": ["홈페이지 개편", "홈페이지 개선", "홈페이지 구축",
                     "인프라 확충", "인프라 고도화",
                     "노후장비 교체", "클라우드 전환", "클라우드 네이티브",
                     "안정화", "DR", "재해복구", "정보화기반 강화", "성능개선"],
        "solution": "NF",
    }
}

# 여러 솔루션이 동시에 매칭될 때 우선순위 (더 구체적인 신호부터 채택)
SOLUTION_PRIORITY = ["MB", "NFA", "WA", "NF"]

SOLUTION_LABELS = {
    "NF": "NetFUNNEL",
    "MB": "MBUSTER",
    "NFA": "NetFUNNEL AP",
    "WA": "Wave Autoscale",
    "일반": "일반",
}


def classify_and_score(title: str, content: str = "") -> dict:
    """
    사업명(및 사업내용)을 받아 등급, 매칭 카테고리, 추천솔루션(NF/MB/NFA/WA/일반 중 1개),
    매칭 키워드를 반환.
    등급 기준: 상 = 1차 키워드 매칭 / 중 = 2차 키워드만 매칭 / 하 = 매칭 없음
    """
    text = f"{title} {content}"
    matched_tier1 = []
    matched_tier2 = []
    categories = set()
    solutions_hit = set()

    for category, info in TIER1_KEYWORDS.items():
        for kw in info["keywords"]:
            if kw in text:
                matched_tier1.append(kw)
                categories.add(category)
                solutions_hit.add(info["solution"])

    for category, info in TIER2_KEYWORDS.items():
        for kw in info["keywords"]:
            if kw in text:
                matched_tier2.append(kw)
                categories.add(category)
                solutions_hit.add(info["solution"])

    if matched_tier1:
        grade = "상"
    elif matched_tier2:
        grade = "중"
    else:
        grade = "하"

    recommended = "일반"
    for sol in SOLUTION_PRIORITY:
        if sol in solutions_hit:
            recommended = sol
            break

    return {
        "등급": grade,
        "카테고리": ", ".join(sorted(categories)) if categories else "-",
        "추천솔루션": recommended,
        "매칭키워드": ", ".join(matched_tier1 + matched_tier2) if (matched_tier1 or matched_tier2) else "-",
    }


if __name__ == "__main__":
    sample_titles = [
        "LH청약플러스 대량접속제어 솔루션 갱신 사업",
        "중앙대학교 MBUSTER v2.0 매크로 탐지 및 차단 솔루션 구매",
        "공공기관 오픈API 플랫폼 API 게이트웨이 고도화 구축 사업",
        "정부 클라우드 전환 사업 쿠버네티스 기반 오토스케일링 인프라 구축",
        "일반 사무용품 구매",
    ]
    for t in sample_titles:
        r = classify_and_score(t)
        print(f"{r['등급']:<4}{r['추천솔루션']:<6}{t}")
