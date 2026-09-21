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
import re


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
# ------------------------------------------------------------
_ETNEWS_RSS_URL = "https://rss.etnews.com/Section901.xml"


def fetch_etnews_rss(keywords=None, max_items: int = 15):
    try:
        resp = requests.get(_ETNEWS_RSS_URL, timeout=REQUEST_TIMEOUT)
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
                "source": "전자신문",
                "topic": "IT",
            })
            if len(results) >= max_items:
                break
        return results, None
    except Exception as e:
        return [], str(e)


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
