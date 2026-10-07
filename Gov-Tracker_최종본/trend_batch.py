"""
매일 아침 실행: 고정 키워드 뉴스를 모아 AI로 트렌드 키워드를 뽑고 DB에 저장.
사용법: cron / Windows 작업 스케줄러에 등록해서 매일 08:05 정도에 실행.
"""
from news_utils import fetch_naver_news, fetch_google_news_rss, fetch_boannews
from ai_utils import extract_trend_keywords
from trend_store import save_trend_snapshot

FIXED_KEYWORDS = [
    "AI", "예약시스템", "먹통", "접속량", "폭주", "서버다운", "API",
    "트래픽", "매크로", "암표", "서버장애", "비대면", "에스티씨랩", "넷퍼넬", "NetFUNNEL",
]


def collect_all_titles():
    titles = []
    for kw in FIXED_KEYWORDS:
        items, _ = fetch_naver_news(kw, display=5)
        titles.extend([it["title"] for it in items])
    g_items, _ = fetch_google_news_rss(" OR ".join(FIXED_KEYWORDS), max_items=30)
    titles.extend([it["title"] for it in g_items])
    b_items, _ = fetch_boannews(keywords=FIXED_KEYWORDS, max_items=30)
    titles.extend([it["title"] for it in b_items])
    return list(dict.fromkeys(titles))


def main():
    titles = collect_all_titles()
    if not titles:
        print("수집된 뉴스가 없습니다.")
        return
    keywords, err = extract_trend_keywords(titles)
    if err:
        print(f"트렌드 키워드 분석 실패: {err}")
        return
    save_trend_snapshot(keywords)
    print(f"{len(keywords)}개 키워드 저장 완료")


if __name__ == "__main__":
    main()
