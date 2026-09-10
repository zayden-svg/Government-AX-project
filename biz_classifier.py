"""
biz_classifier.py
나라장터 실제 낙찰 데이터(NetFUNNEL/MBUSTER 5년치 138건)를 기반으로
사업명에서 관련도를 판단하는 분류기.
gov_tracker.py의 기존 YELLOW_KEYWORDS / classify_and_score를 이 내용으로 교체하면 됩니다.
"""

# ── 1차(강한 신호) ──────────────────────────────────────────
TIER1_KEYWORDS = {
    "트래픽·접속제어": {
        "keywords": ["대기열", "순번대기", "대기시스템", "접속대기", "대기관리",
                     "대량접속제어", "대량접속", "대량제어", "대량접근제어",
                     "접속자순차처리", "순차처리", "유량제어", "트래픽제어",
                     "트래픽관리", "트래픽", "접속제어", "성능제어", "동시접속",
                     "대기알림", "접속부하관리"],
        "product": "NetFUNNEL"
    },
    "업무유형(예약·청약·접수·시험)": {
        "keywords": ["청약", "전자청약", "예약시스템", "통합예약", "원서접수",
                     "접수대기", "수강신청", "채용시스템", "통합채용",
                     "능력검정시험", "자격시험", "IBT시스템", "큐넷"],
        "product": "NetFUNNEL"
    },
    "매크로·부정접속방어": {
        "keywords": ["매크로 탐지", "매크로탐지", "매크로 차단", "매크로차단",
                     "매크로 방지", "매크로방지", "부정접속", "위변조방지"],
        "product": "MBUSTER"
    },
    "시스템구축·전환": {
        "keywords": ["차세대", "고도화", "통합플랫폼", "통합시스템",
                     "통합관리시스템", "통합채용시스템"],
        "product": "NetFUNNEL"
    },
}

# ── 2차(보조 신호, 단독으로는 약하지만 결합시 유효) ─────────────
TIER2_KEYWORDS = {
    "보조신호": {
        "keywords": ["홈페이지 개편", "홈페이지 개선", "홈페이지 구축",  # "홈페이지 구축" 추가
                     "인프라 확충", "인프라 고도화",
                     "노후장비 교체", "클라우드 전환", "클라우드 네이티브",
                     "안정화", "DR", "재해복구", "정보화기반 강화", "성능개선"],
        "product": "NetFUNNEL/MBUSTER 공통 검토"
    }
}


def classify_and_score(title: str, content: str = "") -> dict:
    """
    사업명(및 사업내용)을 받아 등급, 매칭 카테고리, 추천 솔루션, 매칭 키워드를 반환.
    등급 기준:
      상 = 1차 키워드(트래픽제어/업무유형/매크로/시스템전환) 매칭
      중 = 2차 키워드만 매칭
      하 = 매칭 없음
    """
    text = f"{title} {content}"
    matched_tier1 = []
    matched_tier2 = []
    categories = set()
    products = set()

    for category, info in TIER1_KEYWORDS.items():
        for kw in info["keywords"]:
            if kw in text:
                matched_tier1.append(kw)
                categories.add(category)
                products.add(info["product"])

    for category, info in TIER2_KEYWORDS.items():
        for kw in info["keywords"]:
            if kw in text:
                matched_tier2.append(kw)
                categories.add(category)
                products.add(info["product"])

    if matched_tier1:
        grade = "상"
    elif matched_tier2:
        grade = "중"
    else:
        grade = "하"

    return {
        "등급": grade,
        "카테고리": ", ".join(sorted(categories)) if categories else "-",
        "추천솔루션": ", ".join(sorted(products)) if products else "-",
        "매칭키워드": ", ".join(matched_tier1 + matched_tier2) if (matched_tier1 or matched_tier2) else "-",
    }


# ── 자체 검증 테스트 (실제 낙찰 사업명 15건으로 정확도 확인) ─────
if __name__ == "__main__":
    sample_titles = [
        "교육행정기관 누리집 기능고도화 사업 관련 상용 S/W 구매",
        "데이터센터 노후장비 교체 및 인프라 확충(매크로탐지 및 차단 솔루션) 심판정보국",
        "LH청약플러스 대량접속제어 솔루션 갱신 사업",
        "차세대 국가자산처분시스템(온비드) 구축 사업 관련 SW구매(3차)",
        "2025년 인터넷 원서접수센터 운영(대기관리솔루션)",
        "차세대 지급결제플랫폼 라이선스 SW 구매",
        "청약 대기열 관리시스템 추가 라이선스 및 서버 구매",
        "채용시스템 접속부하관리솔루션 구매",
        "한국사능력검정시험 대량접속제어시스템 라이선스 구매",
        "중앙대학교 MBUSTER v2.0 매크로 탐지 및 차단 솔루션 구매",
        "부산청년플랫폼 홈페이지 구축 상용 SW",
        "국립공원 예약시스템 관련 통신소프트웨어 구매",
        "[혁신][3-1-4-5] 수강신청시스템 순번대기솔루션 구매",
        "일반 사무용품 구매",  # 무관 사업명 예시 (하 등급 확인용)
        "청소용역 계약",       # 무관 사업명 예시 (하 등급 확인용)
    ]

    print(f"{'등급':<4}{'카테고리':<28}{'추천솔루션':<20}{'제목'}")
    print("-" * 100)
    for t in sample_titles:
        r = classify_and_score(t)
        print(f"{r['등급']:<4}{r['카테고리']:<28}{r['추천솔루션']:<20}{t[:40]}")
