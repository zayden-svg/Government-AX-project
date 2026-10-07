# news_pool.py
# 뉴스 수집 + AI 연관도 점수 — 아침 배치(briefing_batch.py)와 대시보드가 같은 형태의 결과를 쓰도록 공통화
from news_utils import (
    fetch_naver_news, fetch_google_news_rss, fetch_boannews, fetch_etnews_rss, _clean_naver_text,
)
from ai_utils import score_news_relevance, is_ai_ready
from common import procurement_boost_score

SOURCES = ["google", "naver", "boan", "etnews"]


def fetch_keyword_news(keyword, per_source=6):
    """반환: {"google": [...], "naver": [...], "boan": [...], "etnews": [...]} (각 항목에 _src 표시)"""
    naver, _ = fetch_naver_news(keyword, display=per_source)
    google, _ = fetch_google_news_rss(keyword, max_items=per_source)
    boan, _ = fetch_boannews(keywords=[keyword], max_items=per_source)
    etnews, _ = fetch_etnews_rss(keywords=[keyword], max_items=per_source)
    return {
        "google": tag_items(google, "google"),
        "naver": tag_items(naver, "naver"),
        "boan": tag_items(boan, "boan"),
        "etnews": tag_items(etnews, "etnews"),
    }


def tag_items(items, src):
    out = []
    for it in items or []:
        it2 = dict(it)
        if src == "naver":
            it2["title"] = _clean_naver_text(it2.get("title", ""))
        it2["_src"] = src
        out.append(it2)
    return out


def score_items(items):
    """AI 연관도(0~100) + 자사 제품 단어 가산점(+15). AI 실패 시 -1(분석실패)"""
    if not items:
        return items
    score_map = {}
    if is_ai_ready():
        score_map, _err = score_news_relevance([{"title": it.get("title", "")} for it in items])
    for i, it in enumerate(items):
        base = score_map.get(i, -1) if score_map else -1
        it["_score"] = base if base < 0 else min(100, base + procurement_boost_score(it.get("title", "")))
    return items


def build_pool(keywords, per_source=6):
    pool = []
    for kw in keywords:
        by_src = fetch_keyword_news(kw, per_source=per_source)
        items = by_src["google"] + by_src["naver"] + by_src["boan"] + by_src["etnews"]
        pool.extend(score_items(items))
    seen, dedup = set(), []
    for it in pool:
        t = it.get("title", "")
        if t and t not in seen:
            seen.add(t)
            dedup.append(it)
    return dedup
