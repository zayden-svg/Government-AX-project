# 연결테스트.py
from biz_classifier import classify_and_score

samples = [
    "LH청약플러스 대량접속제어 솔루션 갱신 사업",
    "일반 사무용품 구매",
    "부산청년플랫폼 홈페이지 구축 상용 SW"
]

for title in samples:
    result = classify_and_score(title)
    print(f"제목: {title}")
    print(f"  → 등급: {result['등급']}, 카테고리: {result['카테고리']}, "
          f"추천솔루션: {result.get('추천솔루션', '-')}, 매칭키워드: {result['매칭키워드']}")
    print()
