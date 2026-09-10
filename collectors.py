# collectors.py
import os
import re
import time
import traceback
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from settings import setting
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

STANDARD_FIELDS = [
    "source", "agency", "gubun", "title", "dept", "manager",
    "reg_date", "due_date", "budget", "attach", "views", "url", "content"
]


def base_record(**kwargs):
    rec = {f: "" for f in STANDARD_FIELDS}
    rec.update(kwargs)
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


def safe_get(url, params=None, headers=None, timeout=10, retries=2, session=None):
    h = headers or HEADERS
    req = session.get if session else requests.get
    last_err = None
    for _ in range(retries + 1):
        try:
            resp = req(url, params=params, headers=h, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_err = e
            time.sleep(1)
    code = getattr(getattr(last_err, "response", None), "status_code", None)
    raise RuntimeError(f"HTTP 수집 실패 ({code or 'network'})") from None


def _strip_html(raw_html):
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ------------------------------------------------------------------
# 1. 조달청 (나라장터 OpenAPI) - BidPublicInfoService
# ------------------------------------------------------------------
def parse_g2b_xml(xml_content, limit=10):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_content)
    code = root.findtext('.//resultCode')
    if root.findtext('.//errMsg') or code not in ('00', '000', '0'):
        raise RuntimeError('나라장터 API 응답 오류 — 인증키·활용신청 승인 확인 필요')
    results = []
    for item in root.findall('.//item')[:limit]:
        raw = {child.tag: (child.text or '').strip() for child in item}
        title = raw.get('bidNtceNm', '')
        number = raw.get('bidNtceNo', '')
        if not title or not number:
            continue
        attachments = []
        for i in range(1, 11):
            link = raw.get(f'ntceSpecDocUrl{i}', '')
            if link.startswith('https://'):
                attachments.append({'name': raw.get(f'ntceSpecFileNm{i}', f'첨부 {i}'), 'url': link})
        results.append(base_record(
            source='API', agency='조달청', organization=raw.get('dminsttNm') or raw.get('ntceInsttNm'),
            organization_type='공공조달', notice_number=number, revision=raw.get('bidNtceOrd', ''),
            title=title, gubun='입찰공고', dept=raw.get('ntceInsttNm', ''),
            reg_date=normalize_date(raw.get('bidNtceDt', '')),
            due_date=normalize_date(raw.get('bidClseDt', '')),
            budget=raw.get('presmptPrce') or raw.get('asignBdgtAmt', ''),
            url=raw.get('bidNtceDtlUrl') or raw.get('bidNtceUrl', ''),
            content='', attachments=attachments, raw_payload=raw,
        ))
    return results


def fetch_g2b(limit=10):
    key = setting('G2B_SERVICE_KEY')
    if not key:
        raise RuntimeError('나라장터 서버 환경변수가 없습니다.')
    from datetime import timedelta
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    # First bounded batch; nationwide completeness is not claimed.
    params = {'serviceKey': key, 'pageNo': '1', 'numOfRows': str(limit), 'inqryDiv': '1',
              'inqryBgnDt': (now - timedelta(days=7)).strftime('%Y%m%d') + '0000',
              'inqryEndDt': now.strftime('%Y%m%d') + '2359', 'type': 'xml'}
    url = 'https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch'
    resp = safe_get(url, params=params, timeout=20, retries=1)
    return parse_g2b_xml(resp.content, limit)


# ------------------------------------------------------------------
# 2. 행정안전부 (MOIS) - requests + BeautifulSoup
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
# ------------------------------------------------------------------
def fetch_nipa(limit=10):
    url = "https://www.nipa.kr/home/2-3"
    resp = safe_get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")

    results = []
    for row in rows[:limit]:
        a_tag = row.find("a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        if not title:
            continue

        href = a_tag.get("href", "")
        detail_url = href if href.startswith("http") else "https://www.nipa.kr" + href

        cells = row.find_all("td")
        manager = cells[-2].get_text(strip=True) if len(cells) >= 2 else ""
        reg_date = cells[-1].get_text(strip=True) if cells else ""

        rec = base_record(
            source="SCRAPE", agency="NIPA", gubun="입찰공고",
            title=title, dept="", manager=manager,
            reg_date=normalize_date(reg_date), due_date="", budget="",
            attach="", views="", url=detail_url, content=title,
        )
        results.append(rec)

    return results


# ------------------------------------------------------------------
# 4. KERIS - Playwright
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

                    rec = base_record(
                        source="SCRAPE", agency="KERIS", gubun="입찰공고",
                        title=title, dept="재무회계부", manager="",
                        reg_date=normalize_date(reg_date), due_date=normalize_date(due_date),
                        budget="", attach="", views="", url=detail_url, content=title,
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

                    rec = base_record(
                        source="SCRAPE", agency="AIHub", gubun="사업공고",
                        title=title, dept="", manager="",
                        reg_date=normalize_date(reg_date), due_date="", budget="",
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
#    (공지사항 menu_cd=000010, 보도자료 menu_cd=000012 통합 수집)
# ------------------------------------------------------------------
def _fetch_ai_strategy_menu(session, menu_cd, gubun_nm, limit):
    list_url = f"https://www.aikorea.go.kr/web/board/brdList.do?menu_cd={menu_cd}"
    ajax_url = "https://www.aikorea.go.kr/web/board/ajax/list.do"

    # 1) 목록 페이지 방문 -> JSESSIONID 쿠키 확보
    safe_get(list_url, session=session)

    # 2) AJAX 목록 호출 (Referer, X-Requested-With 필수)
    headers = dict(HEADERS)
    headers.update({
        "Referer": list_url,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    params = {
        "menu_cd": menu_cd,
        "currentPage": "1",
        "searchData": "contdata",
        "searchText": "",
    }

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
            content=_strip_html(item.get("cont", "")),
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

import re

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
        page.wait_for_timeout(3000)

        for page_no in range(1, max_pages + 1):
            if page_no > 1:
                page.evaluate(f"f_bsnsAncmListForm_search({page_no})")
                page.wait_for_timeout(2000)

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
                    etc_info.get("세부사업명", ""),
                    etc_info.get("통합공고명", ""),
                    etc_info.get("내역사업명", ""),
                    final_title,
                ]
                content = " / ".join([c for c in content_parts if c])

                # ancmId + sorgnBsnsCd 조합으로 유일키를 만들어 지역별 세부공고가 서로 덮어쓰지 않도록 함
                detail_url = f"{IRIS_VIEW_URL}?ancmId={ancm_id}&sorgnBsnsCd={sorgn_bsns_cd}"

                rec = base_record(
                    source="SCRAPE", agency="IRIS", gubun="사업공고",
                    title=final_title, dept=dept, manager="", organization=org,
                    notice_number=f"{ancm_id}:{sorgn_bsns_cd}",
                    reg_date="", application_start=normalize_date(rcve_from), due_date=normalize_date(rcve_to),
                    budget="", attach="", views="", url=detail_url, content=content,
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
    """담당자 칸에 기관명/숫자가 들어간 오류를 걸러내고, 사람 이름처럼 보일 때만 통과시킴."""
    if not raw:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    # 숫자(전화번호, 내선번호 등)가 포함되면 담당자명이 아닌 것으로 판단
    if any(ch.isdigit() for ch in text):
        return ""
    # 기관/부서를 뜻하는 글자가 포함되면 걸러냄
    if any(hint in text for hint in _ORG_HINT_CHARS):
        return ""
    # 한글 이름(2~4자) 또는 영문 이름 패턴만 허용
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
    t = re.sub(r"\(.*?\)", "", t)          # 괄호 안 내용 제거 (예: 수정, 재공고 표기 등)
    t = re.sub(r"20\d{2}년?도?", "", t)     # 연도 표기 제거 (사업연도 차이는 같은 과제로 봄)
    t = re.sub(r"[^가-힣A-Za-z0-9]", "", t)  # 공백/특수문자 제거
    return t.strip().lower()


def build_dedup_hash(title, reg_date="", due_date=""):
    norm_title = _normalize_for_dedup(title)
    norm_period = _normalize_for_dedup(str(reg_date)) + _normalize_for_dedup(str(due_date))
    base = norm_title + "|" + norm_period
    return hashlib.md5(base.encode("utf-8")).hexdigest()


# ------------------ NTIS 국가R&D통합공고 ------------------
def fetch_ntis(limit=20):
    url = "https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do"
    resp = safe_get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")

    results = []
    for row in rows[:limit]:
        a_tag = row.find("a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        if not title:
            continue

        cells = row.find_all("td")
        dept = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        reg_date = cells[2].get_text(strip=True) if len(cells) > 2 else ""
        due_date = cells[3].get_text(strip=True) if len(cells) > 3 else ""

        rec = base_record(
            source="SCRAPE", agency="NTIS", gubun="국가R&D통합공고",
            title=title, dept=dept, manager=clean_manager_name(""),
            reg_date=normalize_date(reg_date), due_date=normalize_date(due_date),
            budget="", attach="", views="",
            url="https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do", content=title,
        )
        rec["dedup_hash"] = build_dedup_hash(title, reg_date, due_date)
        results.append(rec)

    return results


# ------------------ TIPA (중소기업기술정보진흥원) ------------------
def fetch_tipa(limit=20):
    url = "https://www.tipa.or.kr/s040101"
    resp = safe_get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")

    results = []
    for row in rows[:limit]:
        a_tag = row.find("a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        if not title:
            continue
        href = a_tag.get("href", "")
        detail_url = href if href.startswith("http") else "https://www.tipa.or.kr" + href

        cells = row.find_all("td")
        reg_date = cells[-1].get_text(strip=True) if cells else ""

        rec = base_record(
            source="SCRAPE", agency="TIPA", gubun="지원사업공고",
            title=title, dept="중소벤처기업부", manager="",
            reg_date=normalize_date(reg_date), due_date="",
            budget="", attach="", views="",
            url=detail_url, content=title,
        )
        rec["dedup_hash"] = build_dedup_hash(title, reg_date)
        results.append(rec)

    return results


# ------------------ KIAT (한국산업기술진흥원, k-pass) ------------------
def fetch_kiat(limit=20):
    url = "https://k-pass.kr/notice/ancList.do"
    resp = safe_get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")

    results = []
    for row in rows[:limit]:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        gubun = cells[1].get_text(strip=True)
        a_tag = cells[2].find("a")
        title = a_tag.get_text(strip=True) if a_tag else cells[2].get_text(strip=True)
        if not title:
            continue
        period_text = cells[3].get_text(strip=True)
        reg_date, due_date = "", ""
        if "~" in period_text:
            parts = period_text.split("~")
            reg_date, due_date = parts[0].strip(), parts[1].split("[")[0].strip()

        rec = base_record(
            source="SCRAPE", agency="KIAT", gubun=gubun or "사업공고",
            title=title, dept="산업통상부", manager="",
            reg_date=normalize_date(reg_date), due_date=normalize_date(due_date),
            budget="", attach="", views="",
            url="https://k-pass.kr/notice/ancList.do", content=title,
        )
        rec["dedup_hash"] = build_dedup_hash(title, reg_date, due_date)
        results.append(rec)

    return results


# ------------------ 연구개발특구진흥재단 (INNOPOLIS) ------------------
def fetch_innopolis(limit=20):
    url = "https://www.innopolis.or.kr/board/list?menuId=MENU00404&pageNum=1&rowCnt=" + str(limit)
    resp = safe_get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")

    results = []
    for row in rows[:limit]:
        a_tag = row.find("a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        if not title:
            continue
        href = a_tag.get("href", "")
        detail_url = href if href.startswith("http") else "https://www.innopolis.or.kr" + href

        cells = row.find_all("td")
        reg_date = cells[-2].get_text(strip=True) if len(cells) >= 2 else ""
        views = cells[-1].get_text(strip=True) if cells else ""

        rec = base_record(
            source="SCRAPE", agency="INNOPOLIS", gubun="사업공고",
            title=title, dept="과학기술정보통신부", manager="",
            reg_date=normalize_date(reg_date), due_date="",
            budget="", attach="", views=views,
            url=detail_url, content=title,
        )
        rec["dedup_hash"] = build_dedup_hash(title, reg_date)
        results.append(rec)

    return results

def parse_nia_detail(html, url, notice_number):
    soup = BeautifulSoup(html, 'html.parser')
    root = soup.select_one('#sub_contentsArea2.detail_type01')
    body = root.select_one('.con_area') if root else None
    if root is None or body is None:
        raise ValueError('NIA 상세 본문 구조 변경 — 저장하지 않음')
    title_el = root.select_one('.title') or root.select_one('.subject')
    # The first direct heading of the detail container is the original title.
    if title_el is None:
        title_el = root.find(['h3', 'h4', 'h5'])
    title = title_el.get_text(' ', strip=True) if title_el else ''
    date_match = re.search(r'\b(20\d{2}\.\d{2}\.\d{2})\b', root.get_text(' ', strip=True))
    links = {}
    for a in root.select('a[href]'):
        link = urljoin(url, a['href'])
        if '/common/board/Download.do?' in link and link.startswith('https://www.nia.or.kr/'):
            links[link] = {'name': a.get_text(' ', strip=True), 'url': link}
    return base_record(source='SCRAPE', agency='NIA', organization='한국지능정보사회진흥원',
        organization_type='공공기관', title=title, notice_number=notice_number,
        reg_date=normalize_date(date_match.group(1)) if date_match else '',
        url=url, content=body.get_text('\n', strip=True), attachments=list(links.values()),
        raw_html=str(root), gubun='기관 공고')


def fetch_nia(limit=10):
    import urllib.robotparser
    robot = urllib.robotparser.RobotFileParser()
    response = safe_get('https://www.nia.or.kr/robots.txt', retries=0)
    robot.parse(response.text.splitlines())
    list_url = 'https://www.nia.or.kr/site/nia_kor/ex/bbs/List.do?cbIdx=78336'
    if not robot.can_fetch(HEADERS['User-Agent'], list_url):
        raise RuntimeError('NIA robots.txt에서 수집을 허용하지 않습니다.')
    soup = BeautifulSoup(safe_get(list_url).content, 'html.parser')
    links = soup.select('a[onclick*="doBbsFView"]')
    if not links:
        raise ValueError('NIA 목록 구조 확인 필요 — 빈 목록을 정상 수집으로 처리하지 않음')
    results = []
    for a in links[:limit]:
        match = re.search(r"doBbsFView\('([0-9]+)','([0-9]+)','([0-9]+)','([0-9]+)'\)", a['onclick'])
        if not match:
            raise ValueError('NIA 공고 식별번호 추출 실패')
        board, number, _, parent = match.groups()
        url = f'https://www.nia.or.kr/site/nia_kor/ex/bbs/View.do?cbIdx={board}&bcIdx={number}&parentSeq={parent}'
        if not robot.can_fetch(HEADERS['User-Agent'], url):
            raise RuntimeError('NIA 상세 수집이 허용되지 않습니다.')
        time.sleep(0.5)
        rec = parse_nia_detail(safe_get(url).content, url, f'{board}:{number}')
        if not rec['title']:
            rec['title'] = re.sub(r'\(새 ?글\)|-?첨부파일\s*있음', '', a.get('title', '')).strip()
        results.append(rec)
    return results


# ------------------------------------------------------------------
# 콜렉터 레지스트리
# ------------------------------------------------------------------
COLLECTORS = {
    "NIA": fetch_nia,
    "행정안전부": fetch_mois,
    "NIPA": fetch_nipa,
    "KERIS": fetch_keris,
    "AIHub": fetch_aihub,
    "국가AI전략위원회": fetch_ai_strategy,
    "조달청": fetch_g2b,
    "IRIS": fetch_iris,
    "NTIS": fetch_ntis,          # 신규
    "TIPA": fetch_tipa,          # 신규
    "KIAT": fetch_kiat,          # 신규
    "INNOPOLIS": fetch_innopolis,  # 신규
}



def run_all_collectors(limit=10):
    all_results = {}
    for name, fn in COLLECTORS.items():
        try:
            records = fn(limit=limit)
            print(f"[OK] {name}: {len(records)}건 수집")
            all_results[name] = records
        except Exception as e:
            print(f"[FAIL] {name} 수집 실패: {e}")
            # Do not print request URLs or secrets in tracebacks.
            all_results[name] = []
    return all_results

