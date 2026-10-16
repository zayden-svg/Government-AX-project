import os
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import html


def _clean_naver_text(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"</?b>", "", raw)
    text = html.unescape(text)
    return text.strip()


NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "").strip()
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
DAUM_REST_API_KEY = os.environ.get("DAUM_REST_API_KEY", "").strip()

REQUEST_TIMEOUT = 8


def _strip_html(text_val: str) -> str:
    if not text_val:
        return ""
    return re.sub(r"<[^>]+>", "", text_val).strip()


def is_naver_ready() -> bool:
    return bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET)


def is_daum_ready() -> bool:
    return bool(DAUM_REST_API_KEY)


# ------------------------------------------------------------
# 1) 네이버 뉴스 검색 (공식 오픈API, 무료 하루 25,000회)
# ------------------------------------------------------------
def fetch_naver_news(query: str, display: int = 10):
    if not is_naver_ready():
        return [], "네이버 API 키(NAVER_CLIENT_ID/SECRET)가 설정되지 않았습니다."
    try:
        resp = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            params={"query": query, "display": display, "sort": "date"},
            headers={
                "X-Naver-Client-Id": NAVER_CLIENT_ID,
                "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        results = []
        for it in items:
            results.append({
                "title": _clean_naver_text(it.get("title", "")),
                "description": _clean_naver_text(it.get("description", "")),
                "link": it.get("link", ""),
                "pubDate": it.get("pubDate", ""),
                "source": "naver",
            })
        return results, None
    except Exception as e:
        return [], str(e)


# ------------------------------------------------------------
# 2) 구글 뉴스 RSS (키 불필요)
# ------------------------------------------------------------
def fetch_google_news_rss(query: str, max_items: int = 10):
    try:
        url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        results = []
        for item in root.findall("./channel/item")[:max_items]:
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            pub_date = item.findtext("pubDate", "")
            source_el = item.find("source")
            src_name = source_el.text if source_el is not None else "구글뉴스"
            results.append({
                "title": title,
                "summary": f"({src_name})",
                "url": link,
                "pub_date": pub_date,
                "source": "구글",
                "topic": query,
            })
        return results, None
    except Exception as e:
        return [], str(e)


# ------------------------------------------------------------
# 3) 보안뉴스 RSS (전체기사 피드, 키 불필요) - 로컬에서 주제 키워드로 필터링
# ------------------------------------------------------------
_BOANNEWS_RSS_URL = "https://cdn.boannews.com/rss/gn_rss_allArticle.xml"


def fetch_boannews(keywords=None, max_items: int = 15):
    try:
        resp = requests.get(_BOANNEWS_RSS_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        results = []
        for item in root.findall("./channel/item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            pub_date = item.findtext("pubDate", "")
            desc = _strip_html(item.findtext("description", ""))

            if keywords:
                haystack = f"{title} {desc}"
                if not any(kw in haystack for kw in keywords):
                    continue

            results.append({
                "title": title,
                "summary": desc[:150],
                "url": link,
                "pub_date": pub_date,
                "source": "보안뉴스",
                "topic": "사이버보안",
            })
            if len(results) >= max_items:
                break
        return results, None
    except Exception as e:
        return [], str(e)


# ------------------------------------------------------------
# 4) 전자신문 RSS ("오늘의뉴스" 카테고리, 키 불필요) - 로컬에서 주제 키워드로 필터링
#    (중복 정의되어 있던 두 번째 버전은 필드명이 달라 삭제, 이 버전만 유지)
# ------------------------------------------------------------
_ETNEWS_RSS_URL = "https://rss.etnews.com/Section901.xml"
_ETNEWS_HEADERS = {"User-Agent": "Mozilla/5.0 (Gov-Tracker news collector)", "Accept": "application/rss+xml, application/xml, */*"}
_ETNEWS_CACHE = {"at": 0.0, "items": []}     # 같은 실행 안에서 키워드마다 RSS를 다시 받지 않도록 10분 보관
_ETNEWS_TTL = 600


def _etnews_feed_items():
    """전자신문 RSS(최신 기사 30건 안팎)를 한 번만 받아 보관. 실패하면 1번 더 시도.
    (예전: 키워드마다 몇 초 간격으로 다시 요청 → 전자신문 서버가 거절하면 0건으로 저장되던 문제)"""
    now = time.time()
    if _ETNEWS_CACHE["items"] and now - _ETNEWS_CACHE["at"] < _ETNEWS_TTL:
        return _ETNEWS_CACHE["items"], None
    err = None
    for attempt in range(2):
        try:
            resp = requests.get(_ETNEWS_RSS_URL, timeout=REQUEST_TIMEOUT + 4, headers=_ETNEWS_HEADERS)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            items = []
            for item in root.findall("./channel/item"):
                items.append({
                    "title": html.unescape(item.findtext("title", "") or "").strip(),
                    "summary": _strip_html(item.findtext("description", ""))[:150],
                    "url": item.findtext("link", ""),
                    "pub_date": item.findtext("pubDate", ""),
                    "source": "전자신문",
                    "topic": "IT",
                })
            if items:
                _ETNEWS_CACHE.update(at=now, items=items)
                return items, None
            err = "RSS 비어 있음"
        except Exception as e:
            err = str(e)
        time.sleep(1.5 * (attempt + 1))
    return _ETNEWS_CACHE["items"], err       # 실패 시 직전에 받아 둔 기사라도


def _etnews_from_google(keyword, max_items):
    """보조 경로: 구글 뉴스에서 전자신문(etnews.com) 기사만 검색 — 키워드 맞춤 기사를 더 넓게 찾음"""
    found, err = fetch_google_news_rss(f"{keyword} site:etnews.com", max_items=max_items)
    out = []
    for it in found:
        t = re.sub(r"\s+-\s*전자신문\s*$", "", str(it.get("title", ""))).strip()
        out.append({**it, "title": t, "summary": "", "source": "전자신문", "topic": keyword})
    return out, err


def fetch_etnews_rss(keywords=None, max_items: int = 15):
    """전자신문 기사. 1) 전자신문 RSS를 키워드로 거름  2) 모자라면 구글 뉴스의 전자신문 기사로 채움.
    keywords가 없으면 RSS 최신 기사를 그대로."""
    feed, feed_err = _etnews_feed_items()
    kws = [k for k in (keywords or []) if str(k).strip()]
    if kws:
        def _hit(txt):     # 'AI'처럼 짧은 영문은 대소문자 그대로(maintain 같은 단어에 걸리지 않게), 나머지는 대소문자 무시
            return any((k in txt) if (len(k) <= 3 and k.isascii()) else (k.lower() in txt.lower()) for k in kws)
        results = [it for it in feed if _hit(f"{it['title']} {it['summary']}")]
    else:
        results = list(feed)
    results = results[:max_items]
    if kws and len(results) < max_items:
        seen = {re.sub(r"\s+", "", it["title"])[:30] for it in results}
        for kw in kws:
            extra, _ = _etnews_from_google(kw, max_items)
            for it in extra:
                key = re.sub(r"\s+", "", it["title"])[:30]
                if key and key not in seen:
                    seen.add(key)
                    results.append(it)
                if len(results) >= max_items:
                    break
            if len(results) >= max_items:
                break
    if results:
        return results, None
    return [], feed_err or "조건에 맞는 전자신문 기사 없음"


# ------------------------------------------------------------
# 5) 다음(Daum) - 공식 "뉴스" 검색 카테고리가 없어서, 웹문서 검색(최신순)으로 근사 대체.
#    카카오 디벨로퍼스 확인 결과 뉴스 전용 API는 현재 제공되지 않음.
# ------------------------------------------------------------
def fetch_daum_web(query: str, size: int = 10):
    if not is_daum_ready():
        return [], "다음(Daum) REST API 키(DAUM_REST_API_KEY)가 설정되지 않았습니다."
    try:
        resp = requests.get(
            "https://dapi.kakao.com/v2/search/web",
            params={"query": query, "sort": "recency", "size": size},
            headers={"Authorization": f"KakaoAK {DAUM_REST_API_KEY}"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        docs = resp.json().get("documents", [])
        results = []
        for d in docs:
            results.append({
                "title": _strip_html(d.get("title", "")),
                "summary": _strip_html(d.get("contents", "")),
                "url": d.get("url", ""),
                "pub_date": d.get("datetime", ""),
                "source": "다음(웹문서·근사)",
                "topic": query,
            })
        return results, None
    except Exception as e:
        return [], str(e)


# ------------------------------------------------------------
# 통합 수집: 여러 주제 x 여러 소스를 합쳐서 하나의 리스트로 반환
# ------------------------------------------------------------
def collect_news(topics, use_naver=True, use_google=True, use_boannews=True, use_etnews=True, use_daum=False, per_topic=8):
    all_results = []
    errors = []

    for topic in topics:
        if use_naver:
            items, err = fetch_naver_news(topic, display=per_topic)
            all_results.extend(items)
            if err:
                errors.append(f"네이버({topic}): {err}")

        if use_google:
            items, err = fetch_google_news_rss(topic, max_items=per_topic)
            all_results.extend(items)
            if err:
                errors.append(f"구글({topic}): {err}")

        if use_daum:
            items, err = fetch_daum_web(topic, size=per_topic)
            all_results.extend(items)
            if err:
                errors.append(f"다음({topic}): {err}")

    if use_boannews:
        items, err = fetch_boannews(keywords=topics, max_items=15)
        all_results.extend(items)
        if err:
            errors.append(f"보안뉴스: {err}")

    if use_etnews:
        items, err = fetch_etnews_rss(keywords=topics, max_items=15)
        all_results.extend(items)
        if err:
            errors.append(f"전자신문: {err}")

    seen_titles = set()
    deduped = []
    for item in all_results:
        norm_title = re.sub(r"\s+", "", item["title"])[:40]
        if norm_title in seen_titles:
            continue
        seen_titles.add(norm_title)
        deduped.append(item)

    return deduped, errors
