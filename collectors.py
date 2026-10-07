# collectors.py (전체 교체)
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

load_dotenv()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

G2B_LOOKBACK_DAYS = 7       # 조달청 조회 기간(일)
G2B_MAX_PAGES = 5           # 100건 × 5페이지 = 최대 500건 조회
G2B_IT_KEYWORDS = [   # 너무 넓은 단어(대기·구축·유지보수·성능)는 대기오염·건물보수 등이 섞여 제외
    "정보시스템", "정보화", "시스템", "홈페이지", "누리집", "포털", "플랫폼", "소프트웨어", "SW",
    "클라우드", "보안", "데이터", "AI", "인공지능", "전산", "서버", "네트워크", "웹", "앱", "모바일",
    "예약", "수강신청", "대기열", "트래픽", "부하테스트", "부하시험", "성능시험", "ISP", "ISMP", "차세대",
]

from common import MOIS_KEEP_WORDS  # noqa: E402

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
    r"총\s*사업\s*비", r"사업\s*금액", r"사업\s*예산", r"예산\s*규모", r"배정\s*예산", r"추정\s*가격",
    r"추정\s*금액", r"기초\s*금액", r"계약\s*금액", r"정부\s*지원\s*연구\s*개발\s*비",
    r"연구\s*개발\s*비", r"지원\s*규모", r"지원\s*금액", r"사업\s*비", r"예\s*산",
]
_BUDGET_LABEL_RE = re.compile(r"(?:" + "|".join(BUDGET_LABEL_PATTERNS) + r")")
# 숫자+단위 조각: "1억", "5,000만", "1,200백만", "350,000천", "150,000,000"
_AMOUNT_TOKEN_RE = re.compile(r"\s*([\d][\d,]*(?:\.\d+)?)\s*(조|억|천만|백만|만|천)?\s*")
_UNIT = {"조": 10**12, "억": 10**8, "천만": 10**7, "백만": 10**6, "만": 10**4, "천": 10**3, None: 1}


def _parse_amount(fragment):
    """'1억 5,000만원' / '50억원' / '1,200백만원' / '350,000천원' → 원 단위 정수. 실패 시 0"""
    first_digit = re.search(r"\d", fragment[:15])   # 라벨 뒤 15자 안에서 첫 숫자 위치
    if not first_digit:
        return 0
    frag = fragment[first_digit.start():]
    total, pos, matched = 0, 0, False
    while True:
        m = _AMOUNT_TOKEN_RE.match(frag, pos)
        if not m or not m.group(1):
            break
        try:
            num = float(m.group(1).replace(",", ""))
        except ValueError:
            break
        total += num * _UNIT[m.group(2)]
        matched = True
        pos = m.end()
        if m.group(2) is None:      # 단위 없는 숫자가 나오면 그 뒤는 더 합치지 않음
            break
    rest = frag[pos:pos + 2]
    if not matched or not rest.startswith("원"):
        return 0                     # '원'으로 끝나지 않으면 금액이 아님(연도·건수 오인 방지)
    return int(total)


def extract_budget_from_text(text_val):
    """본문에서 예산 라벨 뒤의 금액을 찾아 '원' 단위 숫자 문자열로 변환. 못 찾으면 ''"""
    if not text_val:
        return ""
    t = str(text_val)
    for m in _BUDGET_LABEL_RE.finditer(t):
        amount = _parse_amount(t[m.end():m.end() + 40])
        if amount >= 1_000_000:      # 100만원 미만은 오인식으로 간주
            return str(amount)
    return ""


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


def _pw_detail_budget(browser, url, holder, label):
    """[속도 개선] 상세페이지 예산 추출 — 탭 1개를 재사용하고 이미지·폰트·CSS 로딩을 막아 빠르게 연다."""
    try:
        if holder.get("page") is None:
            pg = browser.new_page()
            pg.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "font", "media", "stylesheet") else r.continue_())
            holder["page"] = pg
        pg = holder["page"]
        pg.goto(url, timeout=15000, wait_until="domcontentloaded")
        pg.wait_for_timeout(300)
        return extract_budget_from_text(pg.inner_text("body"))
    except Exception as e:
        print(f"[WARN] {label} 상세 예산 추출 실패({url}): {e}")
        return ""


# ------------------------------------------------------------------
# 1. 조달청 (나라장터 OpenAPI) - BidPublicInfoService
#    예산(presmptPrce/asignBdgtAmt)이 API 필드로 이미 제공되므로 수정 불필요
# ------------------------------------------------------------------
def fetch_g2b(limit=10):
    service_key = _g2b_key()
    if not service_key:
        print("[SKIP] G2B_SERVICE_KEY가 없어 조달청 수집을 건너뜁니다. .env 파일에 G2B_SERVICE_KEY를 설정해주세요.")
        return []

    import xml.etree.ElementTree as ET

    url = "http://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch"
    today = datetime.now()
    begin = (today - timedelta(days=G2B_LOOKBACK_DAYS)).strftime("%Y%m%d") + "0000"
    end = today.strftime("%Y%m%d") + "2359"

    items = []
    for page_no in range(1, G2B_MAX_PAGES + 1):
        params = {
            "serviceKey": service_key, "pageNo": str(page_no), "numOfRows": "100",
            "inqryDiv": "1", "inqryBgnDt": begin, "inqryEndDt": end, "type": "xml",
        }
        resp = safe_get(url, params=params, timeout=15)
        root = ET.fromstring(resp.content)
        err_msg = root.findtext(".//errMsg")
        if err_msg:
            print(f"[FAIL] 조달청 API 오류: {err_msg}")
            break
        page_items = root.findall(".//item")
        items.extend(page_items)
        if len(page_items) < 100:
            break
    # IT 관련 공고만 남김 (전체 용역 공고 중 무작위 10건만 가져오던 문제 해결)
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

    return results[:limit * 3]



# ------------------------------------------------------------------
# 조달청 공통 헬퍼 (사전규격·낙찰·계약 3개 서비스 공용)
#  - 같은 공공데이터포털 인증키(G2B_SERVICE_KEY)를 쓰되, 서비스마다 활용신청 승인은 따로 필요
#  - 주소: 사전규격 ao/HrcspSsstndrdInfoService, 낙찰 as/ScsbidInfoService, 계약 ao/CntrctInfoService
# ------------------------------------------------------------------
G2B_PRESPEC_URL = "http://apis.data.go.kr/1230000/ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoServcPPSSrch"
G2B_SCSBID_URL = "http://apis.data.go.kr/1230000/as/ScsbidInfoService/getScsbidListSttusServcPPSSrch"
G2B_CNTRCT_URL = "http://apis.data.go.kr/1230000/ao/CntrctInfoService/getCntrctInfoListServcPPSSrch"
G2B_RESULT_LOOKBACK_DAYS = 7    # 낙찰·계약 조회 기간(일) — 매일 실행되므로 7일이면 누락 없음


_G2B_DEAD = set()


def _g2b_key():
    """인증키 반환. Encoding 키(%2B 등 포함)를 넣어도 자동으로 Decoding 키로 바꿔 이중 변환 오류를 막는다."""
    from urllib.parse import unquote
    key = (os.getenv("G2B_SERVICE_KEY") or "").strip()
    return unquote(key) if "%" in key else key


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
            resp = safe_get(url, params=q, timeout=20)
            data = resp.json()
        except ValueError:
            # 키 미승인·오류 시 JSON이 아니라 XML 오류문이 옴
            body_txt = resp.text if resp is not None else ""
            msg = re.search(r"<(?:returnAuthMsg|errMsg|resultMsg)>([^<]+)<", body_txt)
            print(f"[FAIL] {label}: {msg.group(1) if msg else body_txt[:120]} (활용신청 승인 여부 확인)")
            _G2B_DEAD.add(url)
            break
        except Exception as e:
            print(f"[FAIL] {label}: {e}")
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
#      postings 테이블에 '사전규격' 구분으로 함께 저장됨
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
        results.append(base_record(
            source="API", agency="조달청(사전규격)", gubun="사전규격", title=title,
            dept=it.get("orderInsttNm") or it.get("rlDminsttNm") or "",
            manager=it.get("ofclNm", ""),
            reg_date=normalize_date(it.get("rgstDt") or it.get("rcptDt")),
            due_date=normalize_date(it.get("opninRgstClseDt")),   # 의견등록 마감일
            budget=_won(it.get("asignBdgtAmt")),
            url=it.get("specDocFileUrl1") or "https://www.g2b.go.kr",
            content=(f"{title} / 실수요기관: {it.get('rlDminsttNm', '')} / "
                     f"사전규격번호: {it.get('bfSpecRgstNo', '')} / SW사업: {it.get('swBizObjYn', '')}"),
        ))
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
        "url": (f"https://www.g2b.go.kr/link/PNPE027_01/simple?bidPbancNo={no}&bidPbancOrd={it.get('bidNtceOrd', '000')}"
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

    # ① 최근 7일 — IT 관련 전체
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

    # ② 표적 검색 — 자사 제품 관련 사업 (재발주 추적용)
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
        if not title or not any(w in title for w in MOIS_KEEP_WORDS):
            continue   # 일반 보도자료는 사업·과제가 아니므로 제외

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
        _detail_holder = {}
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
                        budget_val = _pw_detail_budget(browser, detail_url, _detail_holder, "KERIS")

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
        _detail_holder = {}
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
                        budget_val = _pw_detail_budget(browser, detail_url, _detail_holder, "AIHub")

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
        _detail_holder = {}
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
                budget_val = _pw_detail_budget(browser, detail_url, _detail_holder, "IRIS")

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
        _detail_holder = {}
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
                    budget_val = _pw_detail_budget(browser, detail_url, _detail_holder, "NTIS")

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
    "조달청(사전규격)": fetch_g2b_prespec,
    "IRIS": fetch_iris,
    "NTIS": fetch_ntis,
    "TIPA": fetch_tipa,
    "KIAT": fetch_kiat,
    "INNOPOLIS": fetch_innopolis,
    "KISA": fetch_kisa_bid,
    "IITP": fetch_iitp,
}


PLAYWRIGHT_COLLECTORS = ("KERIS", "AIHub", "NTIS")   # 크롬을 띄우는 수집처 (IRIS는 별도)


def _run_playwright_group(limit):
    """크롬(Playwright)을 쓰는 수집처는 한 줄로 차례대로 실행 — 여러 크롬을 동시에 띄울 때의 충돌·메모리 부족 방지.
    반환: {이름: (레코드 목록 또는 None, 오류)}"""
    out = {}
    try:
        iris_all = fetch_iris(limit=max(limit * 4, 40))
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


def run_all_collectors(limit=10):
    """수집처 실행: 일반 사이트(requests)는 동시에, 크롬이 필요한 사이트는 별도 1줄로 차례대로.
    IITP는 IRIS를 한 번만(넓게) 긁어서 재사용 — 중복 수집 없음."""
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
                        print(f"[OK] {name}: {len(records)}건 수집")
                        all_results[name] = records
                continue
            name = future_map[future]
            try:
                records = future.result()
                print(f"[OK] {name}: {len(records)}건 수집")
                all_results[name] = records
            except Exception as e:
                print(f"[FAIL] {name} 수집 실패: {e}")
                traceback.print_exc()
                all_results[name] = []

    return all_results
