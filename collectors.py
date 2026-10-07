# collectors.py (전체 교체)
import os
import re
import time
import traceback
import concurrent.futures
from datetime import datetime
from urllib.parse import urljoin

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

STANDARD_FIELDS = [
    "source", "agency", "gubun", "title", "dept", "manager",
    "reg_date", "due_date", "budget", "attach", "views", "url", "content"
]

# ------------------ 기관명 한글 정규화 ------------------
AGENCY_NAME_MAP = {
    "NIPA": "정보통신산업진흥원",
    "KERIS": "한국교육학술정보원",
    "AIHub": "한국지능정보사회진흥원(AIHub)",
    "IRIS": "범부처통합연구지원시스템(IRIS)",
    "NTIS": "국가과학기술지식정보서비스(NTIS)",
    "TIPA": "중소기업기술정보진흥원",
    "KIAT": "한국산업기술진흥원",
    "INNOPOLIS": "연구개발특구진흥재단",
    "KISA": "한국인터넷진흥원",
}


def normalize_agency_name(raw):
    key = str(raw).strip()
    return AGENCY_NAME_MAP.get(key, raw)


def base_record(**kwargs):
    rec = {f: "" for f in STANDARD_FIELDS}
    rec.update(kwargs)
    if rec.get("agency"):
        rec["agency"] = normalize_agency_name(rec["agency"])
    return rec


def normalize_date(raw):
    """다양한 형식의 날짜 문자열을 YYYY-MM-DD 형태로 통일"""
    if not raw:
        return ""
    raw = str(raw).strip()
    raw = re.sub(r"[./]", "-", raw)
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    m2 = re.match(r"(\d{8})", raw)
    if m2:
        s = m2.group(1)
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return raw


def safe_get(url, params=None, headers=None, timeout=8, verify=True, retries=2, session=None):
    last_err = None
    h = headers or {"User-Agent": "Mozilla/5.0"}
    requester = session if session is not None else requests
    for _ in range(retries):
        try:
            resp = requester.get(url, params=params, headers=h, timeout=timeout, verify=verify)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise last_err


def _strip_html(raw_html):
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ------------------------------------------------------------------
# === 신규: 사업금액 추출 공통 헬퍼 ===
# 사이트마다 "사업금액/예산규모/배정예산/추정금액/사업비/총사업비/지원금액" 등
# 표현이 제각각이라 하나의 정규식 세트로 통일해서 처리한다.
# ------------------------------------------------------------------
BUDGET_LABEL_PATTERNS = [
    r"사업\s*금액", r"예산\s*규모", r"배정\s*예산", r"추정\s*가격", r"추정\s*금액",
    r"계약\s*금액", r"총\s*사업\s*비", r"사업\s*비", r"지원\s*금액", r"정부\s*지원\s*연구개발비",
]
_BUDGET_LABEL_RE = re.compile(
    r"(?:" + "|".join(BUDGET_LABEL_PATTERNS) + r")\s*[:：]?\s*"
    r"([\d,]+(?:\.\d+)?)\s*(백만원|천만원|만원|원)?"
)


def extract_budget_from_text(text_val):
    """본문 텍스트에서 사업금액/예산 관련 문구를 찾아 '원' 단위 숫자 문자열로 변환"""
    if not text_val:
        return ""
    m = _BUDGET_LABEL_RE.search(str(text_val))
    if not m:
        return ""
    num_str, unit = m.groups()
    try:
        num = float(num_str.replace(",", ""))
    except ValueError:
        return ""
    if unit == "백만원":
        num *= 1_000_000
    elif unit == "천만원":
        num *= 10_000_000
    elif unit == "만원":
        num *= 10_000
    return str(int(num))


def _fetch_budgets_parallel(urls, max_workers=5):
    """=== 신규: requests 기반 수집기 전용 — 상세페이지 N건을 동시에 열어 예산을 뽑음 ===
    ↓ 더 빠르게 하려면 max_workers 숫자를 늘리면 되지만, 상대 서버 과부하/차단 위험이 커지니
    5~8 사이를 권장함."""
    results = [""] * len(urls)

    def _one(i, u):
        if not u:
            return i, ""
        try:
            r = safe_get(u)
            text_val = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
            return i, extract_budget_from_text(text_val)
        except Exception as e:
            print(f"[WARN] 상세페이지 예산 추출 실패({u}): {e}")
            return i, ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_one, i, u) for i, u in enumerate(urls)]
        for f in concurrent.futures.as_completed(futures):
            i, val = f.result()
            results[i] = val
    return results


# ------------------------------------------------------------------
# 1. 조달청 (나라장터 OpenAPI) - BidPublicInfoService
#    예산(presmptPrce/asignBdgtAmt)이 API 필드로 이미 제공되므로 수정 불필요
# ------------------------------------------------------------------
def fetch_g2b(limit=10):
    service_key = os.getenv("G2B_SERVICE_KEY")
    if not service_key:
        print("[SKIP] G2B_SERVICE_KEY가 없어 조달청 수집을 건너뜁니다. .env 파일에 G2B_SERVICE_KEY를 설정해주세요.")
        return []

    import xml.etree.ElementTree as ET

    url = "http://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch"
    today = datetime.now()
    begin = today.replace(day=1).strftime("%Y%m%d") + "0000"
    end = today.strftime("%Y%m%d") + "2359"

    params = {
        "serviceKey": service_key, "pageNo": "1", "numOfRows": str(limit),
        "inqryDiv": "1", "inqryBgnDt": begin, "inqryEndDt": end, "type": "xml",
    }

    resp = safe_get(url, params=params)
    root = ET.fromstring(resp.content)

    err_msg = root.findtext(".//errMsg")
    if err_msg:
        print(f"[FAIL] 조달청 API 오류: {err_msg}")
        return []

    items = root.findall(".//item")
    results = []
    for item in items:
        def g(*names):
            for n in names:
                v = item.findtext(n)
                if v:
                    return v.strip()
            return ""

        title = g("bidNtceNm")
        if not title:
            continue

        rec = base_record(
            source="API", agency="조달청", gubun="입찰공고", title=title,
            dept=g("ntceInsttNm", "dminsttNm"), manager=g("ntceInsttOfclNm"),
            reg_date=normalize_date(g("bidNtceDt", "bidNtceDate")),
            due_date=normalize_date(g("bidClseDt", "bidClseDate")),
            budget=g("presmptPrce", "asignBdgtAmt"),
            attach="", views="",
            url=g("bidNtceDtlUrl", "bidNtceUrl") or "https://www.g2b.go.kr",
            content=title,
        )
        results.append(rec)

    return results[:limit]


# ------------------------------------------------------------------
# 2. 행정안전부 (MOIS) - requests + BeautifulSoup
#    === 공지사항 게시판 특성상 예산 정보가 존재하지 않아 budget 추출 생략 ===
# ------------------------------------------------------------------
def fetch_mois(limit=10):
    url = "https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardList.do"
    params = {"bbsId": "BBSMSTR_000000000008"}
    resp = safe_get(url, params=params)
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")

    results = []
    for row in rows[:limit]:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        title_cell = cells[1]
        a_tag = title_cell.find("a")
        title = a_tag.get_text(strip=True) if a_tag else title_cell.get_text(strip=True)
        if not title:
            continue

        href = a_tag.get("href", "") if a_tag else ""
        if href.startswith("http"):
            detail_url = href
        else:
            m = re.search(r"nttId=(\d+)", href)
            ntt_id = m.group(1) if m else ""
            detail_url = (
                "https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardArticle.do"
                f"?bbsId=BBSMSTR_000000000008&nttId={ntt_id}"
            )

        dept = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        reg_date = cells[4].get_text(strip=True) if len(cells) > 4 else ""
        views = cells[5].get_text(strip=True) if len(cells) > 5 else ""

        rec = base_record(
            source="SCRAPE", agency="행정안전부", gubun="공지",
            title=title, dept=dept, manager="",
            reg_date=normalize_date(reg_date), due_date="", budget="",
            attach="", views=views, url=detail_url, content=title,
        )
        results.append(rec)

    return results


# ------------------------------------------------------------------
# 3. NIPA - requests + BeautifulSoup
#    === 수정: 상세페이지 병렬 방문으로 사업금액 추출 ===
# ------------------------------------------------------------------
def fetch_nipa(limit=10):
    url = "https://www.nipa.kr/home/2-3"
    resp = safe_get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")[:limit]

    prelim = []
    for row in rows:
        a_tag = row.find("a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        if not title:
            continue
        href = a_tag.get("href", "")
        detail_url = href if href.startswith("http") else urljoin("https://www.nipa.kr", href)
        cells = row.find_all("td")
        manager = cells[-2].get_text(strip=True) if len(cells) >= 2 else ""
        reg_date = cells[-1].get_text(strip=True) if cells else ""
        prelim.append({"title": title, "detail_url": detail_url, "manager": manager, "reg_date": reg_date})

    budgets = _fetch_budgets_parallel([p["detail_url"] for p in prelim])

    results = []
    for p, budget_val in zip(prelim, budgets):
        rec = base_record(
            source="SCRAPE", agency="NIPA", gubun="입찰공고",
            title=p["title"], dept="", manager=p["manager"],
            reg_date=normalize_date(p["reg_date"]), due_date="", budget=budget_val,
            attach="", views="", url=p["detail_url"], content=p["title"],
        )
        results.append(rec)

    return results


# ------------------------------------------------------------------
# 4. KERIS - Playwright
#    === 수정: 상세페이지 방문(같은 브라우저 내 new_page 재사용)으로 예산 추출 ===
# ------------------------------------------------------------------
def fetch_keris(limit=10):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("No module named 'playwright'. pip install playwright 후 playwright install chromium 실행 필요")

    url = "https://www.keris.or.kr/main/tender/view/selectTenderList.do?mi=1076"
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=30000)
            page.wait_for_selector("table tbody tr", timeout=10000)
            rows = page.query_selector_all("table tbody tr")

            for idx, row in enumerate(rows[:limit]):
                try:
                    cells = row.query_selector_all("td")
                    if len(cells) < 3:
                        continue
                    title_el = cells[1].query_selector("a") or cells[1]
                    title = title_el.inner_text().strip()
                    if not title:
                        continue

                    href = title_el.get_attribute("href") or ""
                    onclick = title_el.get_attribute("onclick") or ""
                    seq_match = re.search(r"tenderSeq=(\d+)", href) or re.search(r"(\d{4,})", onclick)
                    tender_seq = seq_match.group(1) if seq_match else ""
                    detail_url = (
                        f"https://www.keris.or.kr/main/tender/view/selectTenderInfo.do?mi=1076&tenderSeq={tender_seq}"
                        if tender_seq else url
                    )

                    reg_date = cells[3].inner_text().strip() if len(cells) > 3 else ""
                    due_date = cells[4].inner_text().strip() if len(cells) > 4 else ""

                    budget_val = ""
                    if tender_seq:
                        try:
                            detail_page = browser.new_page()
                            detail_page.goto(detail_url, timeout=15000)
                            detail_page.wait_for_timeout(500)
                            budget_val = extract_budget_from_text(detail_page.inner_text("body"))
                            detail_page.close()
                        except Exception as e:
                            print(f"[WARN] KERIS 상세 예산 추출 실패({detail_url}): {e}")

                    rec = base_record(
                        source="SCRAPE", agency="KERIS", gubun="입찰공고",
                        title=title, dept="재무회계부", manager="",
                        reg_date=normalize_date(reg_date), due_date=normalize_date(due_date),
                        budget=budget_val, attach="", views="", url=detail_url, content=title,
                    )
                    results.append(rec)
                except Exception as e:
                    try:
                        with open(f"debug_keris_{idx}.html", "w", encoding="utf-8") as f:
                            f.write(page.content())
                    except Exception:
                        pass
                    print(f"[WARN] KERIS 행 {idx} 처리 중 오류: {e}")
                    continue
        finally:
            browser.close()

    return results


# ------------------------------------------------------------------
# 5. AIHub - Playwright
#    === 수정: 상세페이지 방문으로 예산 추출 ===
# ------------------------------------------------------------------
def fetch_aihub(limit=10):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("No module named 'playwright'. pip install playwright 후 playwright install chromium 실행 필요")

    url = "https://www.aihub.or.kr/aihubnews/bsnspblanc/list.do?currMenu=133&topMenu=103"
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=30000)
            page.wait_for_selector("table tbody tr", timeout=10000)
            rows = page.query_selector_all("table tbody tr")

            for idx, row in enumerate(rows[:limit]):
                try:
                    cells = row.query_selector_all("td")
                    if len(cells) < 2:
                        continue
                    title_el = cells[1].query_selector("a") or cells[1]
                    title = title_el.inner_text().strip()
                    if not title:
                        continue

                    href = title_el.get_attribute("href") or ""
                    nttsn_match = re.search(r"nttSn=(\d+)", href)
                    nttsn = nttsn_match.group(1) if nttsn_match else ""
                    detail_url = (
                        "https://www.aihub.or.kr/aihubnews/bsnspblanc/view.do"
                        f"?pageIndex=1&nttSn={nttsn}&currMenu=133&topMenu=103&searchCondition=&searchKeyword="
                        if nttsn else url
                    )

                    reg_date = cells[-1].inner_text().strip() if cells else ""

                    budget_val = ""
                    if nttsn:
                        try:
                            detail_page = browser.new_page()
                            detail_page.goto(detail_url, timeout=15000)
                            detail_page.wait_for_timeout(500)
                            budget_val = extract_budget_from_text(detail_page.inner_text("body"))
                            detail_page.close()
                        except Exception as e:
                            print(f"[WARN] AIHub 상세 예산 추출 실패({detail_url}): {e}")

                    rec = base_record(
                        source="SCRAPE", agency="AIHub", gubun="사업공고",
                        title=title, dept="", manager="",
                        reg_date=normalize_date(reg_date), due_date="", budget=budget_val,
                        attach="", views="", url=detail_url, content=title,
                    )
                    results.append(rec)
                except Exception as e:
                    try:
                        with open(f"debug_aihub_{idx}.html", "w", encoding="utf-8") as f:
                            f.write(page.content())
                    except Exception:
                        pass
                    print(f"[WARN] AIHub 행 {idx} 처리 중 오류: {e}")
                    continue
        finally:
            browser.close()

    return results


# ------------------------------------------------------------------
# 6. 국가AI전략위원회 - requests.Session + AJAX(JSON)
#    === 공지/보도자료 특성상 예산 정보 없음 - budget 추출 생략 ===
# ------------------------------------------------------------------
def _fetch_ai_strategy_menu(session, menu_cd, gubun_nm, limit):
    list_url = f"https://www.aikorea.go.kr/web/board/brdList.do?menu_cd={menu_cd}"
    ajax_url = "https://www.aikorea.go.kr/web/board/ajax/list.do"

    safe_get(list_url, session=session)

    headers = dict(HEADERS)
    headers.update({
        "Referer": list_url,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    params = {"menu_cd": menu_cd, "currentPage": "1", "searchData": "contdata", "searchText": ""}

    resp = safe_get(ajax_url, params=params, headers=headers, session=session)
    data = resp.json()

    items = data.get("brdList", [])
    results = []
    for item in items[:limit]:
        num = item.get("num")
        title = (item.get("title") or "").strip()
        if not title or not num:
            continue

        detail_url = (
            "https://www.aikorea.go.kr/web/board/brdDetail.do"
            f"?menu_cd={menu_cd}&num={num}&currentPage=1&searchData=&searchText="
        )

        rec = base_record(
            source="API", agency="국가AI전략위원회", gubun=gubun_nm,
            title=title, dept="국가AI전략위원회", manager=item.get("writer", ""),
            reg_date=normalize_date(item.get("disp_write_dt") or item.get("write_dt")),
            due_date="", budget="",
            attach="있음" if item.get("att_file") == "Y" else "",
            views=item.get("cnt", ""), url=detail_url,
            content=_strip_html(item.get("cont", ""))[:300],
        )
        results.append(rec)

    return results


def fetch_ai_strategy(limit=10):
    session = requests.Session()
    all_results = []

    for menu_cd, gubun_nm in [("000010", "공지사항"), ("000012", "보도자료")]:
        try:
            all_results.extend(_fetch_ai_strategy_menu(session, menu_cd, gubun_nm, limit))
        except Exception as e:
            print(f"[WARN] 국가AI전략위원회({gubun_nm}) 수집 중 오류: {e}")

    all_results.sort(key=lambda r: r.get("reg_date", ""), reverse=True)
    return all_results[:limit]


# ------------------------------------------------------------------
# 7. IRIS (범부처통합연구지원시스템) - Playwright
#    === 수정: 상세페이지 방문으로 예산규모 추출 ===
# ------------------------------------------------------------------
IRIS_LIST_URL = "https://www.iris.go.kr/contents/retrieveBsnsAncmListView.do"
IRIS_VIEW_URL = "https://www.iris.go.kr/contents/retrieveBsnsAncmView.do"

_IRIS_ONCLICK_RE = re.compile(
    r"f_bsnsAncmListForm_view\('([^']*)','([^']*)','([^']*)','([^']*)','([^']*)','([^']*)','([^']*)'\)"
)


def fetch_iris(limit=20, max_pages=3):
    """IRIS(범부처통합연구지원시스템) 사업공고 목록을 Playwright로 수집한다."""
    from playwright.sync_api import sync_playwright

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(IRIS_LIST_URL, timeout=30000)
        try:
            page.wait_for_selector("li:has(a[onclick*='f_bsnsAncmListForm_view'])", timeout=15000)
        except Exception:
            print("[WARN] IRIS 목록이 15초 내에 로드되지 않았습니다. 0건으로 처리합니다.")
            browser.close()
            return results
        page.wait_for_timeout(3000)

        for page_no in range(1, max_pages + 1):
            if page_no > 1:
                try:
                    page.evaluate("window.bsnsAncmTap = window.bsnsAncmTap || 'rcve_prg';")
                    page.evaluate(f"f_bsnsAncmListForm_search({page_no})")
                    page.wait_for_timeout(2000)
                except Exception as e:
                    print(f"[WARN] IRIS {page_no}페이지 이동 실패: {e}")
                    break

            items = page.locator("li:has(a[onclick*='f_bsnsAncmListForm_view'])")
            count = items.count()
            if count == 0:
                break

            for i in range(count):
                li = items.nth(i)

                inst_title = ""
                if li.locator(".inst_title").count() > 0:
                    inst_title = li.locator(".inst_title").first.inner_text().strip()

                link = li.locator("a[onclick*='f_bsnsAncmListForm_view']").first
                title_text = link.inner_text().strip()
                onclick = link.get_attribute("onclick") or ""

                m = _IRIS_ONCLICK_RE.search(onclick)
                if not m:
                    continue
                ancm_id, bsns_yy, sorgn_bsns_cd, bsns_ancm_sn, d_day, rcve_from, rcve_to = m.groups()

                etc_info = {}
                if li.locator(".etc_info span").count() > 0:
                    spans = li.locator(".etc_info span")
                    for j in range(spans.count()):
                        span_text = spans.nth(j).inner_text().strip()
                        for label in ["세부사업명", "통합공고명", "내역사업명", "사업공고명"]:
                            if span_text.startswith(label):
                                etc_info[label] = span_text[len(label):].strip()

                dept, org = "", ""
                if ">" in inst_title:
                    parts = inst_title.split(">")
                    dept = parts[0].strip()
                    org = parts[1].strip()

                final_title = etc_info.get("사업공고명") or title_text
                content_parts = [
                    etc_info.get("세부사업명", ""), etc_info.get("통합공고명", ""),
                    etc_info.get("내역사업명", ""), final_title,
                ]
                content = " / ".join([c for c in content_parts if c])

                detail_url = f"{IRIS_VIEW_URL}?ancmId={ancm_id}&sorgnBsnsCd={sorgn_bsns_cd}"

                # === 신규: 상세페이지 방문해서 예산규모 텍스트 추출 ===
                budget_val = ""
                try:
                    detail_page = browser.new_page()
                    detail_page.goto(detail_url, timeout=15000)
                    detail_page.wait_for_timeout(600)
                    budget_val = extract_budget_from_text(detail_page.inner_text("body"))
                    detail_page.close()
                except Exception as e:
                    print(f"[WARN] IRIS 상세 예산 추출 실패({detail_url}): {e}")

                rec = base_record(
                    source="SCRAPE", agency="IRIS", gubun="사업공고",
                    title=final_title, dept=dept, manager=org,
                    reg_date=normalize_date(rcve_from), due_date=normalize_date(rcve_to),
                    budget=budget_val, attach="", views="", url=detail_url, content=content,
                )
                results.append(rec)

                if len(results) >= limit:
                    browser.close()
                    return results

        browser.close()

    return results


import hashlib


# ------------------ 담당자 정제 ------------------
_ORG_HINT_CHARS = ["부", "청", "원", "실", "센터", "팀", "과", "국", "처", "위원회", "공사", "재단", "협회", "진흥원"]


def clean_manager_name(raw):
    if not raw:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if any(ch.isdigit() for ch in text):
        return ""
    if any(hint in text for hint in _ORG_HINT_CHARS):
        return ""
    if re.fullmatch(r"[가-힣]{2,4}", text):
        return text
    if re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,30}", text):
        return text
    return ""


# ------------------ 중복 과제 판정용 해시 ------------------
def _normalize_for_dedup(text):
    if not text:
        return ""
    t = str(text)
    t = re.sub(r"\(.*?\)", "", t)
    t = re.sub(r"20\d{2}년?도?", "", t)
    t = re.sub(r"[^가-힣A-Za-z0-9]", "", t)
    return t.strip().lower()


def build_dedup_hash(title, reg_date="", due_date=""):
    norm_title = _normalize_for_dedup(title)
    norm_period = _normalize_for_dedup(str(reg_date)) + _normalize_for_dedup(str(due_date))
    base = norm_title + "|" + norm_period
    return hashlib.md5(base.encode("utf-8")).hexdigest()


# ------------------ NTIS 국가R&D통합공고 ------------------
# === 수정: (1) 행마다 실제 상세 URL(view.do) 추출 — 기존엔 전부 같은 목록 URL이 박혀있던 버그 수정
#           (2) 상세페이지 방문으로 사업비 추출 ===
def fetch_ntis(limit=20):
    from playwright.sync_api import sync_playwright

    url = "https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do"
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=30000)
            page.wait_for_selector("a[href*='view.do']", timeout=15000)
            rows = page.query_selector_all("table tbody tr")

            for row in rows:
                if len(results) >= limit:
                    break
                try:
                    a_tag = row.query_selector("a[href*='view.do']")
                    if not a_tag:
                        continue
                    title = a_tag.inner_text().strip()
                    if not title:
                        continue

                    href = a_tag.get_attribute("href") or ""
                    detail_url = href if href.startswith("http") else urljoin(url, href)

                    cells = row.query_selector_all("td")
                    dept = cells[4].inner_text().strip() if len(cells) > 4 else ""
                    reg_date = cells[5].inner_text().strip() if len(cells) > 5 else ""
                    due_date = cells[6].inner_text().strip() if len(cells) > 6 else ""

                    budget_val = ""
                    try:
                        detail_page = browser.new_page()
                        detail_page.goto(detail_url, timeout=15000)
                        detail_page.wait_for_timeout(500)
                        budget_val = extract_budget_from_text(detail_page.inner_text("body"))
                        detail_page.close()
                    except Exception as e:
                        print(f"[WARN] NTIS 상세 예산 추출 실패({detail_url}): {e}")

                    rec = base_record(
                        source="SCRAPE", agency="NTIS", gubun="국가R&D통합공고",
                        title=title, dept=dept, manager=clean_manager_name(""),
                        reg_date=normalize_date(reg_date), due_date=normalize_date(due_date),
                        budget=budget_val, attach="", views="",
                        url=detail_url, content=title,
                    )
                    rec["dedup_hash"] = build_dedup_hash(title, reg_date, due_date)
                    results.append(rec)
                except Exception as e:
                    print(f"[WARN] NTIS 행 처리 중 오류: {e}")
                    continue
        finally:
            browser.close()

    return results


# ------------------ TIPA (중소기업기술정보진흥원) ------------------
# === 수정: 상세페이지 병렬 방문으로 사업비 추출 ===
def fetch_tipa(limit=20):
    url = "https://www.tipa.or.kr/s040101"
    resp = safe_get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")[:limit]

    prelim = []
    for row in rows:
        a_tag = row.find("a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        if not title:
            continue
        href = a_tag.get("href", "")
        detail_url = href if href.startswith("http") else urljoin("https://www.tipa.or.kr", href)
        cells = row.find_all("td")
        reg_date = cells[-1].get_text(strip=True) if cells else ""
        prelim.append({"title": title, "detail_url": detail_url, "reg_date": reg_date})

    budgets = _fetch_budgets_parallel([p["detail_url"] for p in prelim])

    results = []
    for p, budget_val in zip(prelim, budgets):
        rec = base_record(
            source="SCRAPE", agency="TIPA", gubun="지원사업공고",
            title=p["title"], dept="중소벤처기업부", manager="",
            reg_date=normalize_date(p["reg_date"]), due_date="",
            budget=budget_val, attach="", views="",
            url=p["detail_url"], content=p["title"],
        )
        rec["dedup_hash"] = build_dedup_hash(p["title"], p["reg_date"])
        results.append(rec)

    return results


# ------------------ KIAT (한국산업기술진흥원, k-pass) ------------------
# === 수정: (1) 행마다 실제 상세 URL(ancView.do) 추출 — 기존엔 전부 같은 목록 URL이 박혀있던 버그 수정
#           (2) 상세페이지 병렬 방문으로 사업비 추출 ===
def fetch_kiat(limit=20):
    url = "https://k-pass.kr/notice/ancList.do"
    resp = safe_get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")[:limit]

    prelim = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        gubun = cells[1].get_text(strip=True)
        a_tag = cells[2].find("a")
        title = a_tag.get_text(strip=True) if a_tag else cells[2].get_text(strip=True)
        if not title:
            continue

        href = a_tag.get("href", "") if a_tag else ""
        detail_url = href if href.startswith("http") else (urljoin("https://k-pass.kr/notice/", href) if href else url)

        period_text = cells[3].get_text(strip=True)
        reg_date, due_date = "", ""
        if "~" in period_text:
            parts = period_text.split("~")
            reg_date, due_date = parts[0].strip(), parts[1].split("[")[0].strip()

        prelim.append({"gubun": gubun, "title": title, "detail_url": detail_url, "reg_date": reg_date, "due_date": due_date})

    budgets = _fetch_budgets_parallel([p["detail_url"] for p in prelim])

    results = []
    for p, budget_val in zip(prelim, budgets):
        rec = base_record(
            source="SCRAPE", agency="KIAT", gubun=p["gubun"] or "사업공고",
            title=p["title"], dept="산업통상부", manager="",
            reg_date=normalize_date(p["reg_date"]), due_date=normalize_date(p["due_date"]),
            budget=budget_val, attach="", views="",
            url=p["detail_url"], content=p["title"],
        )
        rec["dedup_hash"] = build_dedup_hash(p["title"], p["reg_date"], p["due_date"])
        results.append(rec)

    return results


# ------------------ 연구개발특구진흥재단 (INNOPOLIS) ------------------
# === 수정: 상세페이지 병렬 방문으로 사업비 추출 ===
def fetch_innopolis(limit=20):
    url = "https://www.innopolis.or.kr/board/list?menuId=MENU00404&pageNum=1&rowCnt=" + str(limit)
    resp = safe_get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")

    if not rows and "이용에 불편을 드려서 죄송합니다" in resp.text:
        print("[WARN] INNOPOLIS 사이트 자체 오류 페이지 응답 - 사이트 장애로 추정, 0건 처리")
        return []

    prelim = []
    for row in rows[:limit]:
        a_tag = row.find("a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        if not title:
            continue
        href = a_tag.get("href", "")
        detail_url = href if href.startswith("http") else urljoin("https://www.innopolis.or.kr", href)
        cells = row.find_all("td")
        reg_date = cells[-2].get_text(strip=True) if len(cells) >= 2 else ""
        views = cells[-1].get_text(strip=True) if cells else ""
        prelim.append({"title": title, "detail_url": detail_url, "reg_date": reg_date, "views": views})

    budgets = _fetch_budgets_parallel([p["detail_url"] for p in prelim])

    results = []
    for p, budget_val in zip(prelim, budgets):
        rec = base_record(
            source="SCRAPE", agency="INNOPOLIS", gubun="사업공고",
            title=p["title"], dept="과학기술정보통신부", manager="",
            reg_date=normalize_date(p["reg_date"]), due_date="",
            budget=budget_val, attach="", views=p["views"],
            url=p["detail_url"], content=p["title"],
        )
        rec["dedup_hash"] = build_dedup_hash(p["title"], p["reg_date"])
        results.append(rec)

    return results


# ------------------ KISA (한국인터넷진흥원) 자체 입찰공고 게시판 ------------------
# === 수정: 상세페이지 병렬 방문으로 사업비 추출 ===
def fetch_kisa_bid(limit=20):
    url = "https://www.kisa.or.kr/403"
    resp = safe_get(url, verify=False)
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")

    prelim = []
    for row in rows[:limit]:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        a_tag = row.find("a", href=re.compile(r"postSeq="))
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        if not title:
            continue

        href = a_tag.get("href", "")
        detail_url = href if href.startswith("http") else urljoin("https://www.kisa.or.kr", href)

        reg_date = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        views = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        prelim.append({"title": title, "detail_url": detail_url, "reg_date": reg_date, "views": views})

    # KISA는 인증서 체인 문제로 verify=False 필요 → safe_get 기본 verify=True라 별도 처리
    def _kisa_budgets(urls, max_workers=5):
        results = [""] * len(urls)

        def _one(i, u):
            try:
                r = safe_get(u, verify=False)
                text_val = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
                return i, extract_budget_from_text(text_val)
            except Exception as e:
                print(f"[WARN] KISA 상세 예산 추출 실패({u}): {e}")
                return i, ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(_one, i, u) for i, u in enumerate(urls)]
            for f in concurrent.futures.as_completed(futures):
                i, val = f.result()
                results[i] = val
        return results

    budgets = _kisa_budgets([p["detail_url"] for p in prelim])

    results = []
    for p, budget_val in zip(prelim, budgets):
        rec = base_record(
            source="SCRAPE", agency="KISA", gubun="입찰공고",
            title=p["title"], dept="", manager="",
            reg_date=normalize_date(p["reg_date"]), due_date="",
            budget=budget_val, attach="", views=p["views"],
            url=p["detail_url"], content=p["title"],
        )
        rec["dedup_hash"] = build_dedup_hash(p["title"], p["reg_date"])
        results.append(rec)

    return results


# ------------------------------------------------------------------
# IITP (정보통신기획평가원) 필터 — IRIS 재수집 없이, 이미 받아온 넓은 IRIS 풀에서 골라냄
# === 수정: fetch_iris()를 내부에서 재호출하던 구조 제거 (중복 스크래핑 방지) ===
# ------------------------------------------------------------------
_IITP_HINTS = ["정보통신기획평가원", "IITP", "iitp"]


def filter_iitp_from_iris(iris_records, limit=20):
    results = []
    for rec in iris_records:
        haystack = f"{rec.get('dept','')} {rec.get('manager','')} {rec.get('content','')}"
        if any(h in haystack for h in _IITP_HINTS):
            rec = dict(rec)
            rec["source"] = "SCRAPE"
            rec["agency"] = "정보통신기획평가원(IITP)"
            results.append(rec)
        if len(results) >= limit:
            break
    return results


def fetch_iitp(limit=20, max_pages=3):
    """단독 실행(테스트용)일 때만 자체적으로 IRIS를 수집함. run_all_collectors()에서는
    아래 run_all_collectors 함수가 IRIS 풀을 재사용하므로 이 함수가 호출되지 않음."""
    all_iris = fetch_iris(limit=max(limit * 4, 40), max_pages=max_pages)
    return filter_iitp_from_iris(all_iris, limit=limit)


# ------------------------------------------------------------------
# 콜렉터 레지스트리
# ------------------------------------------------------------------
COLLECTORS = {
    "행정안전부": fetch_mois,
    "NIPA": fetch_nipa,
    "KERIS": fetch_keris,
    "AIHub": fetch_aihub,
    "국가AI전략위원회": fetch_ai_strategy,
    "조달청": fetch_g2b,
    "IRIS": fetch_iris,
    "NTIS": fetch_ntis,
    "TIPA": fetch_tipa,
    "KIAT": fetch_kiat,
    "INNOPOLIS": fetch_innopolis,
    "KISA": fetch_kisa_bid,
    "IITP": fetch_iitp,
}


def run_all_collectors(limit=10):
    """=== 수정: 13개 수집처를 ThreadPoolExecutor로 동시 실행.
    IITP는 IRIS를 한 번만(넓게) 긁어서 재사용 — 중복 스크래핑 제거.
    ↓ max_workers 숫자를 늘리면 더 빨라지지만, 사이트별 차단 위험과 PC 리소스를 고려해 8 권장. """
    all_results = {}
    collectors_to_run = {k: v for k, v in COLLECTORS.items() if k not in ("IRIS", "IITP")}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {executor.submit(fn, limit=limit): name for name, fn in collectors_to_run.items()}

        iris_pool_future = executor.submit(fetch_iris, limit=max(limit * 4, 40))
        future_map[iris_pool_future] = "__IRIS_POOL__"

        for future in concurrent.futures.as_completed(future_map):
            name = future_map[future]
            try:
                records = future.result()
                if name == "__IRIS_POOL__":
                    all_results["IRIS"] = records[:limit]
                    all_results["IITP"] = filter_iitp_from_iris(records, limit=limit)
                    print(f"[OK] IRIS: {len(all_results['IRIS'])}건 수집")
                    print(f"[OK] IITP: {len(all_results['IITP'])}건 수집 (IRIS 결과 재사용, 재수집 없음)")
                else:
                    print(f"[OK] {name}: {len(records)}건 수집")
                    all_results[name] = records
            except Exception as e:
                label = "IRIS/IITP" if name == "__IRIS_POOL__" else name
                print(f"[FAIL] {label} 수집 실패: {e}")
                traceback.print_exc()
                if name == "__IRIS_POOL__":
                    all_results["IRIS"] = []
                    all_results["IITP"] = []
                else:
                    all_results[name] = []

    return all_results
