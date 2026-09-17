# test_news.py - news_utils.py 단독 테스트용 (문제 원인 즉시 확인)
from news_utils import (
    fetch_naver_news, fetch_google_news_rss, fetch_boannews,
    is_naver_ready, is_daum_ready
)

print("=== 환경변수 로딩 확인 ===")
print("네이버 키 준비됨:", is_naver_ready())
print("다음 키 준비됨:", is_daum_ready())

print("\n=== 네이버 뉴스 테스트 ===")
items, err = fetch_naver_news("AI", display=3)
print("결과 건수:", len(items), "| 에러:", err)
for it in items[:3]:
    print(" -", it["title"])

print("\n=== 구글 뉴스 테스트 ===")
items, err = fetch_google_news_rss("AI", max_items=3)
print("결과 건수:", len(items), "| 에러:", err)
for it in items[:3]:
    print(" -", it["title"])

print("\n=== 보안뉴스 테스트 ===")
items, err = fetch_boannews(keywords=["AI", "사이버보안"], max_items=3)
print("결과 건수:", len(items), "| 에러:", err)
for it in items[:3]:
    print(" -", it["title"])
