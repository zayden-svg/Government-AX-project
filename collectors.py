# collectors.py — 수집처별 공고 수집
#   목록에서 제목·등록일·(있으면) 마감일을 읽고, 상세페이지 본문(+필요하면 첨부 공고문)에서
#   예산·마감일·사업종료일을 extract_info.py 공통 규칙으로 뽑는다.
#   원문 링크는 반드시 '그 공고 1건'의 상세페이지 주소로 저장 (목록·통합공고 주소 저장 금지)
import os
import re
import time
import traceback
import concurrent.futures
from datetime import datetime, timedelta
from urllib.parse import urljoin

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from extract_info import extract_all, clean_text
from doc_text import fetch_doc_text, rank_attachments, DOC_EXT_RE

load_dotenv()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
}

G2B_LOOKBACK_DAYS = 7       # 조달청 조회 기간(일)
G2B_MAX_PAGES = 5           # 100건 × 5페이지 = 최대 500건 조회
G2B_IT_KEYWORDS = [   # 너무 넓은 단어(대기·구축·유지보수·성능)는 대기오염·건물보수 등이 섞여 제외
    "정보시스템", "정보화", "시스템", "홈페이지", "누리집", "포털", "플랫폼", "소프트웨어", "SW",
    "클라우드", "보안", "데이터", "AI", "인공지능", "전산", "서버", "네트워크", "웹", "앱", "모바일",
    "예약", "수강신청", "대기열", "트래픽", "부하테스트", "부하시험", "성능시험", "ISP", "ISMP", "차세대",
]
DETAIL_WORKERS = 5          # 상세페이지 동시 방문 수 (상대 서버 부담을 고려해 5 이하 권장)
ATTACH_MAX = 2              # 공고 1건당 읽어 볼 첨부 공고문 최대 개수

from common import MOIS_KEEP_WORDS  # noqa: E402

STANDARD_FIELDS = [
    "source", "agency", "gubun", "title", "dept", "manager",
    "reg_date", "due_date", "budget", "budget_label", "period_end", "ref_no",
    "attach", "views", "url", "content",
]

# ------------------ 기관명 한글 정규화 (예전 약칭 데이터도 같은 이름으로 합침) ------------------
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
    "IITP": "정보통신기획평가원(IITP)",
}


def normalize_agency_name(raw):
    key = str(raw or "").strip()
    return AGENCY_NAME_MAP.get(key, key)


def base_record(**kwargs):
    rec = {f: "" for f in STANDARD_FIELDS}
    rec.update(kwargs)
    if rec.get("agency"):
        rec["agency"] = normalize_agency_name(rec["agency"])
    return rec


def normalize_date(raw):
    """다양한 형식의 날짜 문자열에서 첫 날짜를 YYYY-MM-DD로 (예: '등록일 2026/10/07', '2026.10.07.')"""
    if not raw:
        return ""
    raw = str(raw).strip()
    m = re.search(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", raw)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    m2 = re.search(r"(20\d{6})", raw)
    if m2:
        s = m2.group(1)
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return ""


def safe_get(url, params=None, headers=None, timeout=15, verify=True, retries=2, session=None):
    last_err = None
    h = headers or HEADERS
    requester = session if session is not None else requests
    for i in range(retries):
        try:
            resp = requester.get(url, params=params, headers=h, timeout=timeout, verify=verify)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (i + 1))
    raise last_err


def _strip_html(raw_html):
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ------------------------------------------------------------------
# 상세페이지 → 본문·예산·마감 채우기 (모든 수집처 공통)
# ------------------------------------------------------------------
def page_text(html, title_hint=""):
    """HTML → 본문 글자. 메뉴 글자를 줄이려고 제목이 처음 나오는 지점부터 사용"""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    key = re.sub(r"\s+", " ", str(title_hint or ""))[:14].strip()
    if key:
        i = text.find(key)
        if i > 0:
            text = text[i:]
    return text


def attachments_from_html(html, base_url):
    """상세페이지의 첨부파일 링크 [(파일명, 내려받기 주소)] — 일반 링크 + 기관별 자바스크립트 방식"""
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for a in soup.find_all("a"):
        name = a.get_text(" ", strip=True)
        if not DOC_EXT_RE.search(name or ""):
            continue
        href = (a.get("href") or "").strip()
        onclick = a.get("onclick") or ""
        url = ""
        if href and not href.startswith(("#", "javascript")):
            url = urljoin(base_url, href)
        else:
            m = re.search(r"fnPostAttachDownload\((\d+)\s*,\s*'(\d+)'\s*,\s*(\d+)\s*,\s*'(\w+)'", onclick + href)
            if m:   # KISA
                url = urljoin(base_url, f"/post/fileDownload?menuSeq={m.group(1)}&postSeq={m.group(2)}"
                                        f"&attachSeq={m.group(3)}&lang_type={m.group(4)}")
        if url:
            out.append((name, url))
    return out


def enrich_record(rec, html=None, text=None, base_url="", session=None, verify=True, use_attach=True):
    """상세페이지(필요시 첨부 공고문)에서 예산·마감·사업종료일·본문을 채운다. 목록에서 이미 얻은 값은 유지."""
    body = text if text is not None else (page_text(html, rec.get("title")) if html else "")
    info = extract_all(body, rec.get("reg_date"), html=html)
    missing_budget = not rec.get("budget") and not info["budget"]
    missing_due = not rec.get("due_date") and not info["due_date"]
    if use_attach and html and (missing_budget or missing_due):
        files = rank_attachments(attachments_from_html(html, base_url or rec.get("url", "")))
        for name, url in files[:ATTACH_MAX]:
            atext = fetch_doc_text(url, session=session, headers=HEADERS, verify=verify, name=name)
            if not atext:
                continue
            ainfo = extract_all(atext, rec.get("reg_date"))
            for k in ("budget", "budget_label", "due_date", "due_label", "period_end"):
                if not info.get(k) and ainfo.get(k):
                    info[k] = ainfo[k]
            if len(body) < 600:
                body = (body + "\n" + atext)
            if (rec.get("budget") or info["budget"]) and (rec.get("due_date") or info["due_date"]):
                break
    if not rec.get("budget") and info["budget"]:
        rec["budget"], rec["budget_label"] = info["budget"], info["budget_label"]
    if not rec.get("due_date") and info["due_date"]:
        rec["due_date"] = info["due_date"]
    if not rec.get("period_end") and info["period_end"]:
        rec["period_end"] = info["period_end"]
    if not rec.get("ref_no"):   # 나라장터 입찰공고번호 → 기관 게시판·조달청 중복 공고를 하나로 합칠 때 사용
        m = re.search(r"bidPbancNo=(R\d{2}[A-Z]{2}\d{6,})", html or "") or re.search(r"\b(R\d{2}[A-Z]{2}\d{8})\b", body)
        if m:
            rec["ref_no"] = m.group(1)
    body = clean_text(body)
    if len(body) > len(str(rec.get("content") or "")):
        rec["content"] = body
    return rec


def _enrich_parallel(recs, verify=True, use_attach=True):
    """requests로 열리는 상세페이지를 동시에 방문해 채움 (기관 서버 부담을 고려해 5개씩)"""
    def _one(rec):
        try:
            r = safe_get(rec["url"], verify=verify)
            r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else r.apparent_encoding
            enrich_record(rec, html=r.text, base_url=rec["url"], verify=verify, use_attach=use_attach)
        except Exception as e:
            print(f"[WARN] 상세페이지 읽기 실패({rec.get('url', '')[:70]}): {type(e).__name__}")
        return rec

    with concurrent.futures.ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
        return list(ex.map(_one, recs))


def _pw_open(browser, holder):
    if holder.get("page") is None:
        pg = browser.new_page(extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]})
        pg.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "font", "media") else r.continue_())
        holder["page"] = pg
    return holder["page"]


def _pw_enrich(browser, holder, rec, label, use_attach=True, wait_ms=400):
    """크롬(Playwright)으로 상세페이지를 열어 채움 — 탭 1개 재사용"""
    try:
        pg = _pw_open(browser, holder)
        pg.goto(rec["url"], timeout=25000, wait_until="domcontentloaded")
        pg.wait_for_timeout(wait_ms)
        html = pg.content()
        enrich_record(rec, html=html, base_url=rec["url"], use_attach=use_attach)
    except Exception as e:
        print(f"[WARN] {label} 상세페이지 읽기 실패({rec.get('url', '')[:70]}): {type(e).__name__}: {str(e)[:80]}")
    return rec


def _launch(p):
    return p.chromium.launch(headless=True, args=["--lang=ko-KR"])


def _soup_rows(html, selector="table tbody tr"):
    return BeautifulSoup(html or "", "html.parser").select(selector)


def _browser_html(url, wait_selector=None, timeout=30000):
    """requests가 막힐 때의 대안: 크롬으로 페이지 HTML만 받아오기"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = _launch(p)
        try:
            pg = browser.new_page()
            pg.goto(url, timeout=timeout, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    pg.wait_for_selector(wait_selector, timeout=15000)
                except Exception:
                    pass
            return pg.content()
        finally:
            browser.close()


def _diagnose(label, resp):
    body = resp.text if resp is not None else ""
    head = re.sub(r"\s+", " ", _strip_html(body))[:120]
    print(f"[진단] {label}: 응답 {getattr(resp, 'status_code', '-')}, {len(body)}자, 앞부분: {head}")


# ------------------------------------------------------------------
# 1. 조달청 (나라장터 OpenAPI) — 예산·마감이 API 값으로 제공됨
# ------------------------------------------------------------------
G2B_BID_URL = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch"
G2B_PRESPEC_URL = "https://apis.data.go.kr/1230000/ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoServcPPSSrch"
G2B_SCSBID_URL = "https://apis.data.go.kr/1230000/as/ScsbidInfoService/getScsbidListSttusServcPPSSrch"
G2B_CNTRCT_URL = "https://apis.data.go.kr/1230000/ao/CntrctInfoService/getCntrctInfoListServcPPSSrch"
G2B_RESULT_LOOKBACK_DAYS = 7    # 낙찰·계약 조회 기간(일) — 매일 실행되므로 7일이면 누락 없음

_G2B_DEAD = set()
_G2B_STATE = {"conn_fail": 0}     # 접속 자체가 연속으로 안 되면 이번 실행에서는 더 시도하지 않음 (시간 낭비 방지)


def _g2b_key():
    """인증키 반환. Encoding 키(%2B 등 포함)를 넣어도 자동으로 Decoding 키로 바꿔 이중 변환 오류를 막는다."""
    from urllib.parse import unquote
    key = (os.getenv("G2B_SERVICE_KEY") or "").strip()
    return unquote(key) if "%" in key else key


def _g2b_get(url, params):
    """https 우선, 접속 실패 시 http로 한 번 더. 연속 3회 접속 실패하면 이번 실행에서 조달청 호출 중단."""
    if _G2B_STATE["conn_fail"] >= 3:
        raise ConnectionError("조달청 API 접속 불가(이번 실행에서 재시도 중단)")
    last = None
    for u in (url, url.replace("https://", "http://")):
        for attempt in range(2):
            try:
                resp = requests.get(u, params=params, headers=HEADERS, timeout=(12, 40))
                resp.raise_for_status()
                _G2B_STATE["conn_fail"] = 0
                return resp
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last = e
                time.sleep(3 + attempt * 5)
            except Exception as e:
                last = e
                break
    _G2B_STATE["conn_fail"] += 1
    raise last


def fetch_g2b(limit=10):
    service_key = _g2b_key()
    if not service_key:
        print("[SKIP] G2B_SERVICE_KEY가 없어 조달청 수집을 건너뜁니다.")
        return []

    import xml.etree.ElementTree as ET

    today = datetime.now()
    begin = (today - timedelta(days=G2B_LOOKBACK_DAYS)).strftime("%Y%m%d") + "0000"
    end = today.strftime("%Y%m%d") + "2359"

    items = []
    for page_no in range(1, G2B_MAX_PAGES + 1):
        params = {
            "serviceKey": service_key, "pageNo": str(page_no), "numOfRows": "100",
            "inqryDiv": "1", "inqryBgnDt": begin, "inqryEndDt": end, "type": "xml",
        }
        resp = _g2b_get(G2B_BID_URL, params)
        root = ET.fromstring(resp.content)
        err_msg = root.findtext(".//errMsg") or root.findtext(".//returnAuthMsg")
        if err_msg:
            print(f"[FAIL] 조달청 API 오류: {err_msg}")
            break
        page_items = root.findall(".//item")
        items.extend(page_items)
        if len(page_items) < 100:
            break
    items = [it for it in items if any(k in (it.findtext("bidNtceNm") or "") for k in G2B_IT_KEYWORDS)]
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
        budget = g("asignBdgtAmt") or g("presmptPrce")
        rec = base_record(
            source="API", agency="조달청", gubun="입찰공고", title=title,
            dept=g("dminsttNm", "ntceInsttNm"), manager=g("ntceInsttOfclNm"),
            reg_date=normalize_date(g("bidNtceDt", "bidNtceDate")),
            due_date=normalize_date(g("bidClseDt", "bidClseDate")),
            budget=budget, budget_label="배정예산" if g("asignBdgtAmt") else ("추정가격" if budget else ""),
            attach="", views="",
            url=g("bidNtceDtlUrl", "bidNtceUrl") or "https://www.g2b.go.kr",
            content=(f"{title} / 공고기관: {g('ntceInsttNm')} / 수요기관: {g('dminsttNm')} / "
                     f"입찰공고번호: {g('bidNtceNo')} / 계약방법: {g('cntrctCnclsMthdNm')}"),
        )
        rec["ref_no"] = g("bidNtceNo")
        results.append(rec)

    return results[:limit * 3]


def _g2b_items(url, params, label, max_pages=None):
    """조달청 API를 JSON으로 호출해 item 목록을 페이지별로 모은다. 오류 시 원인을 로그로 남기고 빈 목록."""
    max_pages = max_pages or G2B_MAX_PAGES
    key = _g2b_key()
    if not key:
        print(f"[SKIP] G2B_SERVICE_KEY 미설정 → {label} 건너뜀")
        return []
    if url in _G2B_DEAD:          # 같은 실행에서 이미 인증 실패한 서비스는 다시 부르지 않음
        return []
    out = []
    for page_no in range(1, max_pages + 1):
        q = {"serviceKey": key, "pageNo": str(page_no), "numOfRows": "100", "type": "json", **params}
        resp = None
        try:
            resp = _g2b_get(url, q)
            data = resp.json()
        except ValueError:
            body_txt = resp.text if resp is not None else ""
            msg = re.search(r"<(?:returnAuthMsg|errMsg|resultMsg)>([^<]+)<", body_txt)
            print(f"[FAIL] {label}: {msg.group(1) if msg else body_txt[:120]} (활용신청 승인 여부 확인)")
            _G2B_DEAD.add(url)
            break
        except Exception as e:
            print(f"[FAIL] {label}: {type(e).__name__}: {str(e)[:120]}")
            break
        root = data.get("response") or {}
        header = root.get("header") or {}
        if header.get("resultCode") not in (None, "00"):
            print(f"[FAIL] {label}: {header.get('resultMsg')}")
            break
        items = (root.get("body") or {}).get("items") or []
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        out.extend(items)
        if len(items) < 100:
            break
    return out


def _is_it_title(title, extra_flag=""):
    return str(extra_flag).upper() == "Y" or any(k in (title or "") for k in G2B_IT_KEYWORDS)


def _won(v):
    try:
        return str(int(float(str(v).replace(",", ""))))
    except Exception:
        return ""


# ------------------------------------------------------------------
# 1-2. 조달청 사전규격 — 입찰공고 '전 단계'. 공고 전에 규격서를 미리 보고 영업 선제 대응
# ------------------------------------------------------------------
def fetch_g2b_prespec(limit=10):
    now = datetime.now()
    params = {
        "inqryDiv": "1",   # 1: 등록일시 기준
        "inqryBgnDt": (now - timedelta(days=G2B_LOOKBACK_DAYS)).strftime("%Y%m%d") + "0000",
        "inqryEndDt": now.strftime("%Y%m%d") + "2359",
    }
    results = []
    for it in _g2b_items(G2B_PRESPEC_URL, params, "조달청 사전규격"):
        title = (it.get("prdctClsfcNoNm") or "").strip()
        if not title or not _is_it_title(title, it.get("swBizObjYn")):
            continue
        no = it.get("bfSpecRgstNo", "")
        rec = base_record(
            source="API", agency="조달청(사전규격)", gubun="사전규격", title=title,
            dept=it.get("rlDminsttNm") or it.get("orderInsttNm") or "",
            manager=it.get("ofclNm", ""),
            reg_date=normalize_date(it.get("rgstDt") or it.get("rcptDt")),
            due_date=normalize_date(it.get("opninRgstClseDt")),   # 의견등록 마감일
            budget=_won(it.get("asignBdgtAmt")), budget_label="배정예산" if _won(it.get("asignBdgtAmt")) else "",
            # 사전규격은 나라장터 상세화면 직접 링크가 없어, 해당 건의 규격서(원문 파일)로 연결
            url=it.get("specDocFileUrl1") or "https://www.g2b.go.kr",
            content=(f"{title} / 실수요기관: {it.get('rlDminsttNm', '')} / 발주기관: {it.get('orderInsttNm', '')} / "
                     f"사전규격번호: {no} / SW사업: {it.get('swBizObjYn', '')}"),
        )
        rec["ref_no"] = no
        results.append(rec)
    return results[:limit * 3]


# ------------------------------------------------------------------
# 조달 결과(낙찰·계약) — 영업기회가 아니라 '시장 정보'이므로 postings가 아닌
# procurement_results 테이블에 따로 저장 (main.py → procurement_store.py)
# ------------------------------------------------------------------
def _first_corp_name(corp_list):
    """계약 API 업체목록 '[순번^업체구분^공동도급^업체명^...]' → 업체명 (공동계약이면 '외 N')"""
    names = []
    for p in re.findall(r"\[([^\]]*)\]", str(corp_list or "")):
        f = p.split("^")
        if len(f) >= 4 and f[3].strip():
            names.append(f[3].strip())
    if not names:
        return ""
    return names[0] + (f" 외 {len(names) - 1}" if len(names) > 1 else "")


def _first_dminstt(dminstt_list, fallback=""):
    """계약 API 수요기관목록 '[순번^기관코드^기관명^...]' → 첫 기관명"""
    m = re.search(r"\[([^\]]*)\]", str(dminstt_list or ""))
    if m:
        f = m.group(1).split("^")
        if len(f) >= 3 and f[2].strip():
            return f[2].strip()
    return fallback


# 재발주 추적용 표적 검색어 — 자사 제품(NF·BM·LT) 도입 사업에 자주 들어가는 사업명 단어
G2B_TARGET_KEYWORDS = ["대기열", "접속", "예약", "수강신청", "매크로", "트래픽", "티켓", "부하테스트", "가상대기"]


def _contract_end_date(it):
    """계약 종료일: 총완수일자 → 금차완수일자 → 계약기간 문구('~2026-12-31', '착수일부터 120일') 순으로 찾음"""
    for k in ("ttalScmpltDate", "thtmScmpltDate"):
        d = normalize_date(it.get(k))
        if re.match(r"\d{4}-\d{2}-\d{2}$", d or ""):
            return d
    prd = str(it.get("cntrctPrd") or "")
    dates = re.findall(r"(\d{4})[.\-/년\s]*(\d{1,2})[.\-/월\s]*(\d{1,2})", prd)
    if len(dates) >= 2:
        y, m, d = dates[-1]
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    days = re.search(r"(\d{2,4})\s*일", prd)
    start = normalize_date(it.get("wbgnDate") or it.get("cntrctCnclsDate") or it.get("cntrctDate"))
    if days and re.match(r"\d{4}-\d{2}-\d{2}$", start or ""):
        try:
            return (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=int(days.group(1)))).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return ""


def _plus_one_year(date_str):
    try:
        return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=365)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def _scsbid_row(it):
    title = (it.get("bidNtceNm") or "").strip()
    event = normalize_date(it.get("fnlSucsfDate") or it.get("rlOpengDt"))
    no = it.get("bidNtceNo", "")
    return {
        "kind": "낙찰", "ref_no": f"{no}-{it.get('bidNtceOrd', '')}", "title": title,
        "agency": it.get("dminsttNm", ""), "company": it.get("bidwinnrNm", ""),
        "amount": _won(it.get("sucsfbidAmt")), "rate": str(it.get("sucsfbidRate") or ""),
        "event_date": event,
        "end_date": _plus_one_year(event), "end_est": "Y",      # 낙찰 정보엔 계약기간이 없어 1년으로 추정
        "url": (f"https://www.g2b.go.kr/link/PNPE027_01/single/?bidPbancNo={no}&bidPbancOrd={it.get('bidNtceOrd', '000')}"
                if no else "https://www.g2b.go.kr"),
    }


def _cntrct_row(it):
    title = (it.get("cntrctNm") or "").strip()
    event = normalize_date(it.get("cntrctCnclsDate") or it.get("cntrctDate"))
    end = _contract_end_date(it)
    return {
        "kind": "계약", "ref_no": it.get("untyCntrctNo") or it.get("dcsnCntrctNo", ""), "title": title,
        "agency": _first_dminstt(it.get("dminsttList"), it.get("cntrctInsttNm", "")),
        "company": _first_corp_name(it.get("corpList")),
        "amount": _won(it.get("totCntrctAmt") or it.get("thtmCntrctAmt")), "rate": "",
        "event_date": event,
        "end_date": end or _plus_one_year(event), "end_est": "N" if end else "Y",
        "url": it.get("cntrctDtlInfoUrl") or it.get("cntrctInfoUrl") or "https://www.g2b.go.kr",
    }


def _date_chunks(days_back, chunk_days=30):
    """[오늘-days_back, 오늘] 구간을 30일 단위로 쪼갬 (조달청 API 조회기간 제한 대비)"""
    end = datetime.now()
    start = end - timedelta(days=days_back)
    chunks = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=chunk_days), end)
        chunks.append((cur, nxt))
        cur = nxt + timedelta(days=1)
    return chunks


def fetch_g2b_results(backfill_days=0):
    """조달청 낙찰·계약 결과.
    ① 최근 7일: IT 관련 전체
    ② 표적 검색: 자사 제품 관련 단어가 사업명에 들어간 건 — backfill_days>0이면 그 기간(예: 365일)을 30일씩 나눠 조회
    반환: [{kind, ref_no, title, agency, company, amount, rate, event_date, end_date, end_est, url}, ...]"""
    rows = {}

    def _add(r):
        if r["title"]:
            rows[(r["kind"], r["ref_no"] or r["title"])] = r

    for b, e in _date_chunks(G2B_RESULT_LOOKBACK_DAYS):
        for it in _g2b_items(G2B_SCSBID_URL, {
            "inqryDiv": "2", "inqryBgnDt": b.strftime("%Y%m%d") + "0000", "inqryEndDt": e.strftime("%Y%m%d") + "2359",
        }, "조달청 낙찰"):
            r = _scsbid_row(it)
            if _is_it_title(r["title"]):
                _add(r)
        for it in _g2b_items(G2B_CNTRCT_URL, {
            "inqryDiv": "1", "inqryBgnDate": b.strftime("%Y%m%d"), "inqryEndDate": e.strftime("%Y%m%d"),
        }, "조달청 계약"):
            r = _cntrct_row(it)
            if _is_it_title(r["title"], it.get("infoBizYn")):
                _add(r)

    span = max(int(backfill_days or 0), G2B_RESULT_LOOKBACK_DAYS)
    for kw in G2B_TARGET_KEYWORDS:
        for b, e in _date_chunks(span):
            for it in _g2b_items(G2B_SCSBID_URL, {
                "inqryDiv": "2", "inqryBgnDt": b.strftime("%Y%m%d") + "0000", "inqryEndDt": e.strftime("%Y%m%d") + "2359",
                "bidNtceNm": kw,
            }, f"조달청 낙찰({kw})", max_pages=2):
                _add(_scsbid_row(it))
            for it in _g2b_items(G2B_CNTRCT_URL, {
                "inqryDiv": "1", "inqryBgnDate": b.strftime("%Y%m%d"), "inqryEndDate": e.strftime("%Y%m%d"),
                "cntrctNm": kw,
            }, f"조달청 계약({kw})", max_pages=2):
                _add(_cntrct_row(it))

    out = list(rows.values())
    print(f"[OK] 조달청 낙찰·계약: {len(out)}건 (IT·자사관련, 조회기간 {span}일)")
    return out


# ------------------------------------------------------------------
# 2. 행정안전부 — 보도자료 게시판 중 공고·공모 성격 글만. 본문에서 기간 표기가 있으면 마감으로
# ------------------------------------------------------------------
def fetch_mois(limit=10):
    url = "https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardList.do"
    resp = safe_get(url, params={"bbsId": "BBSMSTR_000000000008"}, timeout=20, retries=3)
    rows = _soup_rows(resp.text)

    results = []
    for row in rows[:limit]:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        a_tag = cells[1].find("a")
        title = a_tag.get_text(strip=True) if a_tag else cells[1].get_text(strip=True)
        if not title or not any(w in title for w in MOIS_KEEP_WORDS):
            continue   # 일반 보도자료는 사업·과제가 아니므로 제외
        href = a_tag.get("href", "") if a_tag else ""
        m = re.search(r"nttId=(\d+)", href + (a_tag.get("onclick", "") if a_tag else ""))
        detail_url = ("https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardArticle.do"
                      f"?bbsId=BBSMSTR_000000000008&nttId={m.group(1)}") if m else url
        results.append(base_record(
            source="SCRAPE", agency="행정안전부", gubun="공지",
            title=title, dept=cells[3].get_text(strip=True) if len(cells) > 3 else "",
            reg_date=normalize_date(cells[4].get_text(strip=True) if len(cells) > 4 else ""),
            views=cells[5].get_text(strip=True) if len(cells) > 5 else "", url=detail_url, content=title,
        ))
    return _enrich_parallel([r for r in results if "nttId=" in r["url"]], use_attach=False) + \
        [r for r in results if "nttId=" not in r["url"]]


# ------------------------------------------------------------------
# 3. NIPA 입찰공고 — 상세 본문에 '사업금액/사업예산', '제출 마감일시/접수마감일시', '납품기한/계약기간'
# ------------------------------------------------------------------
def fetch_nipa(limit=10):
    url = "https://www.nipa.kr/home/2-3"
    resp = safe_get(url)
    rows = _soup_rows(resp.text)[:limit]
    if not rows:
        _diagnose("NIPA", resp)

    recs = []
    for row in rows:
        a_tag = row.find("a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        if not title:
            continue
        href = a_tag.get("href", "")
        cells = row.find_all("td")
        recs.append(base_record(
            source="SCRAPE", agency="NIPA", gubun="입찰공고", title=title,
            manager=cells[-2].get_text(strip=True) if len(cells) >= 2 else "",
            reg_date=normalize_date(cells[-1].get_text(strip=True) if cells else ""),
            url=href if href.startswith("http") else urljoin("https://www.nipa.kr", href), content=title,
        ))
    return _enrich_parallel(recs)


# ------------------------------------------------------------------
# 4. KERIS 입찰공고 (Playwright) — 목록: 등록일·마감일, 상세: 나라장터 첨부 공고서에서 예산
# ------------------------------------------------------------------
KERIS_LIST_URL = "https://www.keris.or.kr/main/tender/view/selectTenderList.do?mi=1076"


def fetch_keris(limit=10):
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        browser = _launch(p)
        holder = {}
        try:
            page = browser.new_page()
            page.goto(KERIS_LIST_URL, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_selector("table tbody tr a[data-tenderseq]", timeout=15000)
            for row in _soup_rows(page.content())[:limit]:
                a = row.select_one("a[data-tenderseq]")
                if not a:
                    continue
                title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).replace("새글", "").strip()
                seq = a.get("data-tenderseq", "")
                dates = [normalize_date(td.get_text(" ", strip=True)) for td in row.select("td.date")]
                if not title or not seq:
                    continue
                results.append(base_record(
                    source="SCRAPE", agency="KERIS", gubun="입찰공고", title=title, dept="재무회계부",
                    reg_date=dates[0] if dates else "", due_date=dates[1] if len(dates) > 1 else "",
                    url=f"https://www.keris.or.kr/main/tender/view/selectTenderInfo.do?mi=1076&tenderSeq={seq}",
                    content=title,
                ))
            for rec in results:
                _pw_enrich(browser, holder, rec, "KERIS")
        finally:
            browser.close()
    return results


# ------------------------------------------------------------------
# 5. AIHub 사업공고 (Playwright) — 목록의 data-key = 상세 nttSn
# ------------------------------------------------------------------
AIHUB_LIST_URL = "https://www.aihub.or.kr/aihubnews/bsnspblanc/list.do?currMenu=133&topMenu=103"


def fetch_aihub(limit=10):
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        browser = _launch(p)
        holder = {}
        try:
            page = browser.new_page()
            page.goto(AIHUB_LIST_URL, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_selector("a.btnView[data-key]", timeout=15000)
            for row in _soup_rows(page.content()):
                a = row.select_one("a.btnView[data-key]")
                if not a:
                    continue          # 검색창 등 공고가 아닌 행
                title = a.get_text(" ", strip=True)
                key = a.get("data-key", "")
                cells = row.find_all("td")
                results.append(base_record(
                    source="SCRAPE", agency="AIHub", gubun="사업공고", title=title,
                    reg_date=normalize_date(cells[2].get_text(" ", strip=True) if len(cells) > 2 else ""),
                    url=f"https://www.aihub.or.kr/aihubnews/bsnspblanc/view.do?nttSn={key}&currMenu=133&topMenu=103",
                    content=title,
                ))
                if len(results) >= limit:
                    break
            for rec in results:
                _pw_enrich(browser, holder, rec, "AIHub")
        finally:
            browser.close()
    return results


# ------------------------------------------------------------------
# 6. 국가AI전략위원회 — requests.Session + AJAX(JSON). 본문(cont)에 기간 표기가 있으면 마감으로
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

    results = []
    for item in data.get("brdList", [])[:limit]:
        num = item.get("num")
        title = (item.get("title") or "").strip()
        if not title or not num:
            continue
        rec = base_record(
            source="API", agency="국가AI전략위원회", gubun=gubun_nm,
            title=title, dept="국가AI전략위원회", manager=item.get("writer", ""),
            reg_date=normalize_date(item.get("disp_write_dt") or item.get("write_dt")),
            attach="있음" if item.get("att_file") == "Y" else "",
            views=item.get("cnt", ""),
            url=("https://www.aikorea.go.kr/web/board/brdDetail.do"
                 f"?menu_cd={menu_cd}&num={num}&currentPage=1&searchData=&searchText="),
        )
        enrich_record(rec, text=_strip_html(item.get("cont", "")), use_attach=False)
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
# 7. IRIS 사업공고 (Playwright)
#    상세 주소는 ancmId만으로는 '통합공고'가 열리거나 오류가 나므로 사업년도·전문기관·공고순번을 모두 넣는다.
# ------------------------------------------------------------------
IRIS_LIST_URL = "https://www.iris.go.kr/contents/retrieveBsnsAncmListView.do"
IRIS_VIEW_URL = "https://www.iris.go.kr/contents/retrieveBsnsAncmView.do"

_IRIS_ONCLICK_RE = re.compile(
    r"f_bsnsAncmListForm_view\('([^']*)','([^']*)','([^']*)','([^']*)','([^']*)','([^']*)','([^']*)'\)"
)


def iris_detail_url(ancm_id, bsns_yy, sorgn_bsns_cd, bsns_ancm_sn):
    return (f"{IRIS_VIEW_URL}?ancmId={ancm_id}&bsnsYyDetail={bsns_yy}"
            f"&sorgnBsnsCd={sorgn_bsns_cd}&bsnsAncmSn={bsns_ancm_sn}")


def fetch_iris(limit=20, max_pages=3):
    """IRIS(범부처통합연구지원시스템) 사업공고 목록을 Playwright로 수집한다."""
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        browser = _launch(p)
        holder = {}
        page = browser.new_page()
        loaded = False
        for attempt in (1, 2):   # 해외 서버에서 느릴 때가 있어 1회 재시도
            try:
                page.goto(IRIS_LIST_URL, timeout=60000, wait_until="domcontentloaded")
                page.wait_for_selector("li:has(a[onclick*='f_bsnsAncmListForm_view'])", timeout=30000)
                loaded = True
                break
            except Exception as e:
                print(f"[WARN] IRIS 목록 로딩 {attempt}차 실패: {str(e)[:120]}")
        if not loaded:
            print("[WARN] IRIS 목록을 불러오지 못했습니다. 0건으로 처리합니다.")
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
                m = _IRIS_ONCLICK_RE.search(link.get_attribute("onclick") or "")
                if not m:
                    continue
                ancm_id, bsns_yy, sorgn_bsns_cd, bsns_ancm_sn, d_day, rcve_from, rcve_to = m.groups()

                etc_info = {}
                spans = li.locator(".etc_info span")
                for j in range(spans.count()):
                    span_text = spans.nth(j).inner_text().strip()
                    for label in ["세부사업명", "통합공고명", "내역사업명", "사업공고명"]:
                        if span_text.startswith(label):
                            etc_info[label] = span_text[len(label):].strip()

                dept, org = "", ""
                if ">" in inst_title:
                    parts = inst_title.split(">")
                    dept, org = parts[0].strip(), parts[1].strip()

                final_title = etc_info.get("사업공고명") or title_text
                content = " / ".join(c for c in [etc_info.get("세부사업명", ""), etc_info.get("통합공고명", ""),
                                                 etc_info.get("내역사업명", ""), final_title] if c)
                results.append(base_record(
                    source="SCRAPE", agency="IRIS", gubun="사업공고",
                    title=final_title, dept=dept, manager=org,
                    reg_date=normalize_date(rcve_from), due_date=normalize_date(rcve_to),
                    url=iris_detail_url(ancm_id, bsns_yy, sorgn_bsns_cd, bsns_ancm_sn), content=content,
                ))
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break

        for rec in results:     # 첨부는 자바스크립트 내려받기라 본문·표에서만 추출
            _pw_enrich(browser, holder, rec, "IRIS", use_attach=False, wait_ms=600)
        browser.close()
    return results


import hashlib  # noqa: E402


# ------------------ 담당자 정제 ------------------
_ORG_HINT_CHARS = ["부", "청", "원", "실", "센터", "팀", "과", "국", "처", "위원회", "공사", "재단", "협회", "진흥원"]


def clean_manager_name(raw):
    if not raw:
        return ""
    text = str(raw).strip()
    if not text or any(ch.isdigit() for ch in text) or any(hint in text for hint in _ORG_HINT_CHARS):
        return ""
    if re.fullmatch(r"[가-힣]{2,4}", text) or re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,30}", text):
        return text
    return ""


# ------------------------------------------------------------------
# NTIS 국가R&D통합공고 (Playwright) — 목록 fn_view('번호') → view.do?roRndUid=번호 (공고 1건 상세)
# ------------------------------------------------------------------
NTIS_LIST_URL = "https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do"


def fetch_ntis(limit=20):
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        browser = _launch(p)
        holder = {}
        try:
            page = browser.new_page()
            page.goto(NTIS_LIST_URL, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_selector("a[onclick*='fn_view']", timeout=15000)
            for row in _soup_rows(page.content()):
                a = row.select_one("a[onclick*='fn_view']")
                if not a:
                    continue
                m = re.search(r"fn_view\('(\d+)'\)", a.get("onclick", ""))
                title = (a.get("title") or a.get_text(" ", strip=True)).strip()
                if not m or not title:
                    continue
                cell = {td.get("data-title", ""): td.get_text(" ", strip=True) for td in row.find_all("td")}
                results.append(base_record(
                    source="SCRAPE", agency="NTIS", gubun="국가R&D통합공고", title=title,
                    dept=cell.get("부처명", ""),
                    reg_date=normalize_date(cell.get("접수일", "")), due_date=normalize_date(cell.get("마감일", "")),
                    url=f"https://www.ntis.go.kr/rndgate/eg/un/ra/view.do?roRndUid={m.group(1)}&flag=rndList",
                    content=title,
                ))
                if len(results) >= limit:
                    break
            for rec in results:   # NTIS 첨부는 자바스크립트 내려받기 → 상세 본문('공고금액')에서 추출
                _pw_enrich(browser, holder, rec, "NTIS", use_attach=False)
        finally:
            browser.close()
    return results


# ------------------------------------------------------------------
# TIPA 공지사항 — 본문이 짧고 '첨부 공고문 참조'가 대부분 → 첨부(hwp·hwpx·pdf)까지 읽음
#   해외 서버에서 목록이 비어 오면 크롬으로 한 번 더 시도
# ------------------------------------------------------------------
TIPA_LIST_URL = "https://www.tipa.or.kr/s040101"


def fetch_tipa(limit=20):
    resp = None
    try:
        resp = safe_get(TIPA_LIST_URL, timeout=20)
        rows = _soup_rows(resp.text)
    except Exception as e:
        print(f"[WARN] TIPA 목록 요청 실패: {type(e).__name__}")
        rows = []
    if not rows:
        if resp is not None:
            _diagnose("TIPA", resp)
        try:
            rows = _soup_rows(_browser_html(TIPA_LIST_URL, "table tbody tr"))
            print(f"[INFO] TIPA 크롬으로 재시도: {len(rows)}행")
        except Exception as e:
            print(f"[WARN] TIPA 크롬 재시도 실패: {type(e).__name__}: {str(e)[:80]}")
    recs = []
    for row in rows[:limit]:
        a_tag = row.select_one("td.subject a") or row.find("a")
        if not a_tag:
            continue
        for badge in a_tag.select("span, i, em, img"):          # 새 글 표시 'N' 배지가 제목에 붙는 것 방지
            if badge.get_text(strip=True).upper() in ("N", "NEW", "") or "new" in " ".join(badge.get("class") or []).lower():
                badge.decompose()
        title = (a_tag.get("title") or a_tag.get_text(" ", strip=True)).strip()
        title = re.sub(r"^\s*N\s*(?=[가-힣\[\(「『<〈'\"‘“])", "", title)
        if not title:
            continue
        href = a_tag.get("href", "")
        cells = row.find_all("td")
        recs.append(base_record(
            source="SCRAPE", agency="TIPA", gubun="지원사업공고", title=title, dept="중소벤처기업부",
            reg_date=normalize_date(cells[-1].get_text(strip=True) if cells else ""),
            url=href if href.startswith("http") else urljoin("https://www.tipa.or.kr", href), content=title,
        ))
    return _enrich_parallel(recs)


# ------------------------------------------------------------------
# KIAT (k-pass) — 목록 ancView('P3108','NEW') → ancView.do?ancId=P3108&gubun=NEW
# ------------------------------------------------------------------
def fetch_kiat(limit=20):
    url = "https://k-pass.kr/notice/ancList.do"
    resp = safe_get(url)
    recs = []
    for row in _soup_rows(resp.text):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        span = row.find(attrs={"onclick": re.compile(r"ancView\(")})
        title = span.get_text(" ", strip=True) if span else cells[2].get_text(" ", strip=True)
        if not title:
            continue
        m = re.search(r"ancView\('([^']+)'\s*,\s*'([^']+)'", span.get("onclick", "")) if span else None
        detail_url = f"https://k-pass.kr/notice/ancView.do?ancId={m.group(1)}&gubun={m.group(2)}" if m else url
        period = cells[3].get_text(" ", strip=True)
        reg_date, due_date = "", ""
        if "~" in period:
            a, b = period.split("~", 1)
            reg_date, due_date = normalize_date(a), normalize_date(b)
        recs.append(base_record(
            source="SCRAPE", agency="KIAT", gubun=cells[1].get_text(strip=True) or "사업공고",
            title=title, dept="산업통상부", reg_date=reg_date, due_date=due_date, url=detail_url, content=title,
        ))
        if len(recs) >= limit:
            break
    return _enrich_parallel([r for r in recs if "ancView.do" in r["url"]]) + \
        [r for r in recs if "ancView.do" not in r["url"]]


# ------------------------------------------------------------------
# 연구개발특구진흥재단 (INNOPOLIS)
# ------------------------------------------------------------------
def fetch_innopolis(limit=20):
    url = "https://www.innopolis.or.kr/board/list?menuId=MENU00404&pageNum=1&rowCnt=" + str(limit)
    resp = safe_get(url)
    rows = _soup_rows(resp.text)
    if not rows and "이용에 불편을 드려서 죄송합니다" in resp.text:
        print("[WARN] INNOPOLIS 사이트 자체 오류 페이지 응답 - 사이트 장애로 추정, 0건 처리")
        return []
    if not rows:
        _diagnose("INNOPOLIS", resp)
    recs = []
    for row in rows[:limit]:
        a_tag = row.find("a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        if not title:
            continue
        href = a_tag.get("href", "")
        cells = row.find_all("td")
        recs.append(base_record(
            source="SCRAPE", agency="INNOPOLIS", gubun="사업공고", title=title, dept="과학기술정보통신부",
            reg_date=normalize_date(cells[-2].get_text(strip=True) if len(cells) >= 2 else ""),
            views=cells[-1].get_text(strip=True) if cells else "",
            url=href if href.startswith("http") else urljoin("https://www.innopolis.or.kr", href), content=title,
        ))
    return _enrich_parallel(recs)


# ------------------------------------------------------------------
# KISA 입찰공고 — 본문 '예산액/소요예산/추정금액', '공개기간/제출기간/마감일시'. 없으면 첨부까지
# ------------------------------------------------------------------
def fetch_kisa_bid(limit=20):
    url = "https://www.kisa.or.kr/403"
    resp = safe_get(url, verify=False)
    recs = []
    for row in _soup_rows(resp.text)[:limit]:
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
        recs.append(base_record(
            source="SCRAPE", agency="KISA", gubun="입찰공고", title=title,
            reg_date=normalize_date(cells[2].get_text(strip=True) if len(cells) > 2 else ""),
            views=cells[3].get_text(strip=True) if len(cells) > 3 else "",
            url=href if href.startswith("http") else urljoin("https://www.kisa.or.kr", href), content=title,
        ))
    return _enrich_parallel(recs, verify=False)


# ------------------------------------------------------------------
# IITP (정보통신기획평가원) 필터 — IRIS 재수집 없이, 이미 받아온 넓은 IRIS 풀에서 골라냄
# ------------------------------------------------------------------
_IITP_HINTS = ["정보통신기획평가원", "IITP", "iitp"]


def filter_iitp_from_iris(iris_records, limit=20):
    results = []
    for rec in iris_records:
        haystack = f"{rec.get('dept','')} {rec.get('manager','')} {rec.get('content','')[:400]}"
        if any(h in haystack for h in _IITP_HINTS):
            rec = dict(rec)
            rec["source"] = "SCRAPE"
            rec["agency"] = "정보통신기획평가원(IITP)"
            results.append(rec)
        if len(results) >= limit:
            break
    return results


def fetch_iitp(limit=20, max_pages=3):
    """단독 실행(테스트용)일 때만 자체적으로 IRIS를 수집함."""
    all_iris = fetch_iris(limit=max(limit * 4, 40), max_pages=max_pages)
    return filter_iitp_from_iris(all_iris, limit=limit)


# ------------------------------------------------------------------
# 단건 보강 — 이미 DB에 있는 공고 중 예산·마감이 빈 건을 원문 링크로 다시 읽어 채움 (main.py에서 사용)
#   requests로 열리는 수집처만 (크롬이 필요한 KERIS·AIHub·NTIS·IRIS는 다음 수집 때 자동 갱신)
# ------------------------------------------------------------------
_REFRESHABLE = {
    "nipa.kr": True, "tipa.or.kr": True, "k-pass.kr": "ancView.do", "kisa.or.kr": "postSeq=",
    "innopolis.or.kr": True, "mois.go.kr": "nttId=",
}


def refreshable_url(url):
    for host, cond in _REFRESHABLE.items():
        if host in (url or ""):
            return cond is True or (cond in url)
    return False


def refresh_records(recs):
    """[{title, reg_date, url, budget, due_date, content...}] → 채워진 레코드 (원본 dict 수정)"""
    by_verify = {True: [], False: []}
    for r in recs:
        by_verify["kisa.or.kr" not in r.get("url", "")].append(r)
    out = []
    for verify, group in by_verify.items():
        if group:
            out.extend(_enrich_parallel(group, verify=verify))
    return out


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
    "조달청(사전규격)": fetch_g2b_prespec,
    "IRIS": fetch_iris,
    "NTIS": fetch_ntis,
    "TIPA": fetch_tipa,
    "KIAT": fetch_kiat,
    "INNOPOLIS": fetch_innopolis,
    "KISA": fetch_kisa_bid,
    "IITP": fetch_iitp,
}

# 크롬을 쓰는(또는 크롬으로 재시도할 수 있는) 수집처 — 한 줄로 차례대로 실행 (IRIS는 별도)
PLAYWRIGHT_COLLECTORS = ("KERIS", "AIHub", "NTIS", "TIPA")


def _run_playwright_group(limit):
    """크롬(Playwright)을 쓰는 수집처는 한 줄로 차례대로 실행 — 여러 크롬을 동시에 띄울 때의 충돌·메모리 부족 방지."""
    out = {}
    try:
        iris_all = fetch_iris(limit=max(limit * 2, 40))
        out["IRIS"] = (iris_all[:limit], None)
        out["IITP"] = (filter_iitp_from_iris(iris_all, limit=limit), None)
    except Exception as e:
        traceback.print_exc()
        out["IRIS"] = (None, e)
        out["IITP"] = (None, e)
    for name in PLAYWRIGHT_COLLECTORS:
        try:
            out[name] = (COLLECTORS[name](limit=limit), None)
        except Exception as e:
            traceback.print_exc()
            out[name] = (None, e)
    return out


def _fill_stats(name, records):
    if not records:
        return ""
    b = sum(1 for r in records if r.get("budget"))
    d = sum(1 for r in records if r.get("due_date"))
    return f" (예산 {b}/{len(records)} · 마감 {d}/{len(records)})"


def run_all_collectors(limit=10):
    """수집처 실행: 일반 사이트(requests)는 동시에, 크롬이 필요한 사이트는 별도 1줄로 차례대로."""
    all_results = {}
    simple = {k: v for k, v in COLLECTORS.items() if k not in ("IRIS", "IITP") + PLAYWRIGHT_COLLECTORS}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {executor.submit(fn, limit=limit): name for name, fn in simple.items()}
        pw_future = executor.submit(_run_playwright_group, limit)

        for future in concurrent.futures.as_completed(list(future_map) + [pw_future]):
            if future is pw_future:
                try:
                    group = future.result()
                except Exception as e:
                    traceback.print_exc()
                    group = {n: (None, e) for n in ("IRIS", "IITP") + PLAYWRIGHT_COLLECTORS}
                for name, (records, err) in group.items():
                    if err is not None:
                        print(f"[FAIL] {name} 수집 실패: {err}")
                        all_results[name] = []
                    else:
                        print(f"[OK] {name}: {len(records)}건 수집{_fill_stats(name, records)}")
                        all_results[name] = records
                continue
            name = future_map[future]
            try:
                records = future.result()
                print(f"[OK] {name}: {len(records)}건 수집{_fill_stats(name, records)}")
                all_results[name] = records
            except Exception as e:
                print(f"[FAIL] {name} 수집 실패: {type(e).__name__}: {str(e)[:160]}")
                all_results[name] = []

    return all_results
