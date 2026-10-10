# univ_crawl.py
# 사립대·전문대 홈페이지 "입찰공고 / 입찰결과" 게시판 자동 수집
#   1) data/univ_master.csv (대학알리미 학교개황, 2024.10 기준)의 학교 홈페이지에서 입찰 게시판을 자동으로 찾음
#      - 첫 화면 링크 중 '입찰·구매·조달·계약' 글자가 들어간 것 → 없으면 '알림·공지·대학소개·정보공개' 메뉴 한 단계 더
#      - data/univ_boards.json 에 직접 적은 주소가 있으면 그걸 우선 (자동으로 못 찾은 학교 보완용)
#   2) 게시판 목록에서 글 제목·날짜·링크를 뽑고, 2~3쪽까지 넘김
#   3) IT 관련 글만 남기고, '낙찰·결과·선정·계약' 글은 본문을 열어 업체명·금액을 찾음
#   결과: univ_bids.json + 학교별 수집 결과(univ_crawl_report.json) → DB(univ_bids) 저장
#
# 실행: python univ_crawl.py [--limit 20] [--only 가천대학교]
import argparse
import asyncio
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

BOARD_WORDS = re.compile(r"(입찰\s*공고|입찰\s*정보|입찰|구매\s*입찰|구매|조달|계약\s*정보|계약\s*현황|낙찰|공개\s*입찰)")
BOARD_BAD = re.compile(r"(채용|입학|모집|장학|수강|학사|논문|도서|기숙사|식단|강의)")
MENU_WORDS = re.compile(r"(알림|공지|소식|뉴스|대학\s*소개|학교\s*소개|대학\s*안내|정보\s*공개|행정|총무|대학\s*생활|캠퍼스\s*생활|커뮤니티|소통|열린|홍보|사이트\s*맵|sitemap|site\s*map|전체\s*메뉴)", re.I)
SITEMAP_PATHS = ["/sitemap.do", "/kor/sitemap.do", "/ko/sitemap.do", "/kr/sitemap.do", "/main/sitemap.do", "/sitemap/sitemap.do",
                 "/sitemap.jsp", "/sitemap.html", "/sitemap.php", "/kor/etc/sitemap.do", "/user/sitemap.do", "/home/sitemap.do"]
HTML_LINK_RE = re.compile(r"""href\s*=\s*["']([^"'#][^"']*)["'][^>]*>(?:\s*<[^>]+>)*\s*([^<]{0,40}?(?:입찰|구매\s*입찰|구매/입찰|구매·입찰)[^<]{0,20})""")
JSON_LINK_RE = re.compile(r'"(?:url|link|href|menuUrl|linkUrl)"\s*:\s*"([^"]+)"[^{}]{0,300}?"(?:name|title|menuNm|menuName|text)"\s*:\s*"([^"]*입찰[^"]*)"'
                          r'|"(?:name|title|menuNm|menuName|text)"\s*:\s*"([^"]*입찰[^"]*)"[^{}]{0,300}?"(?:url|link|href|menuUrl|linkUrl)"\s*:\s*"([^"]+)"')
RESULT_WORDS = re.compile(r"(낙찰|개찰\s*결과|입찰\s*결과|선정\s*결과|계약\s*체결|우선\s*협상|결과\s*공고|업체\s*선정|최종\s*선정)")
DATE_RE = re.compile(r"(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})")
DATE_SHORT_RE = re.compile(r"(?<!\d)(2\d)[.\-/](\d{2})[.\-/](\d{2})(?!\d)")
WINNER_RE = re.compile(r"(낙\s*찰\s*자|낙찰\s*업체|낙찰\s*예정자|계약\s*상대자|계약\s*업체|선정\s*업체|우선\s*협상\s*대상자|업\s*체\s*명|상\s*호)\s*[:：\-]?\s*"
                       r"([\(（]?[주유재사]?[\)）]?\s*[가-힣A-Za-z0-9&\.\(\)（）·\s]{2,40}?)(?=\s{2,}|\n|\s*\(|\s*대표|\s*사업자|\s*/|\s*,|$)")
AMOUNT_RE = re.compile(r"(낙찰\s*금액|계약\s*금액|낙찰가|투찰\s*금액)[^\d]{0,20}([\d,]{6,})\s*원?")
CONTRACT_WORDS = re.compile(r"(낙찰|입찰\s*결과|개찰|계약\s*정보|계약\s*현황|수의\s*계약|계약\s*공개|계약\s*내역|계약\s*체결\s*현황)")
LIST_POST_RE = re.compile(r"(계약\s*(현황|내역|정보|체결\s*현황)|수의\s*계약|계약\s*공개|입찰\s*결과\s*공개|낙찰\s*현황|입찰\s*현황|계약\s*대장)")
BID_TITLE_RE = re.compile(r"(입찰|견적|구매|용역|낙찰|선정|계약|개찰|공모|제안)")
SHEET_EXT_RE = re.compile(r"\.(xlsx|xls|csv)\b", re.I)
MAX_LIST_POSTS = 24      # 학교마다 '수의계약 현황' 같은 월별 목록 글을 열어 볼 최대 개수
KEEP_YEARS = 3
MAX_PAGES = 6
CONCURRENCY = 6
MAX_VISIT = 22            # 게시판 찾기: 학교마다 최대 몇 쪽까지 둘러볼지
MAX_DETAIL = 45           # 학교마다 상세 글을 열어 볼 최대 개수 (IT 글만)

MONEY_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(억\s*원|억|천\s*만\s*원|백\s*만\s*원|만\s*원|천\s*원|원)")
BUDGET_LBL = re.compile(r"(추정\s*가격|추정\s*금액|사업\s*규모|구매\s*예산|예정\s*금액|기초\s*가격|총\s*예산|기초\s*금액|사업\s*예산|소요\s*예산|배정\s*예산|예정\s*가격|사업\s*비|사업\s*금액|총\s*사업비|예산\s*액|예\s*산|구매\s*예정\s*금액|계약\s*예정\s*금액)")
AWARD_LBL = re.compile(r"(낙찰\s*금액|계약\s*금액|낙찰\s*가|투찰\s*금액|계약\s*액)")
PERIOD_LBL = re.compile(r"(계약\s*기간|사업\s*기간|용역\s*기간|수행\s*기간|과업\s*기간|납품\s*기한|납품\s*기간|구축\s*기간|이행\s*기간)")
DEADLINE_LBL = re.compile(r"(입찰\s*마감|제출\s*마감|접수\s*마감|마감\s*일시|제출\s*기한|투찰\s*마감|접수\s*기간|제출\s*기간|입찰서\s*제출|전자\s*입찰\s*기간)")
REL_RE = re.compile(r"(계약\s*일|착수\s*일|계약\s*체결\s*일)[^\d]{0,12}(?:로부터|부터|후)?\s*(\d{1,4})\s*(개월|일|년)")
CORP_LIKE = re.compile(r"(주식회사|\(주\)|㈜|유한|회사|시스템|정보|기술|테크|텍|소프트|솔루션|네트웍|네트워크|컴퍼니|커뮤니케이션|"
                       r"아이티|IT|디지털|데이타|데이터|산업|전자|통신|엔지니어링|corp|inc|co\.)", re.I)


def _won(num, unit):
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return ""
    u = re.sub(r"\s+", "", unit)
    mul = {"억원": 1e8, "억": 1e8, "천만원": 1e7, "백만원": 1e6, "만원": 1e4, "천원": 1e3, "원": 1}.get(u, 1)
    v *= mul
    return str(int(v)) if v >= 100000 else ""


def _after(t, end, span=110):
    """라벨 바로 뒤 값: 같은 줄(값이 비어 있으면 다음 줄)"""
    seg = t[end:end + span]
    nl = seg.find("\n")
    if nl != -1:
        first = seg[:nl]
        if len(first.strip(" :：-·)]")) < 3:
            rest = seg[nl + 1:]
            nl2 = rest.find("\n")
            first = rest[:nl2] if nl2 != -1 else rest
        seg = first
    return seg


def _money_after(text, lbl_re):
    for m in lbl_re.finditer(text):
        mm = MONEY_RE.search(_after(text, m.end()))
        if mm:
            v = _won(mm.group(1), mm.group(2))
            if v:
                return v
    return ""


def _all_dates(seg):
    out = []
    for m in DATE_RE.finditer(seg):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            out.append(f"{y:04d}-{mo:02d}-{d:02d}")
    return out


WINNER_LBL = re.compile(r"(낙\s*찰\s*자|낙찰\s*업체|낙찰\s*예정자|계약\s*상대자|계약\s*업체|선정\s*업체|우선\s*협상\s*대상자|업\s*체\s*명|상\s*호)")
WINNER_CUT = re.compile(r"(\s{2,}|\s*대표|\s*사업자|\s*/|,|\s*낙찰|\s*계약|\s*금액|\s*주소|\s*투찰|\s*\||\s*\d{3,}|\s*입니다|\s*으로|\s*로\s)")
STRONG_CORP = re.compile(r"(주식회사|\(주\)|㈜|\(유\)|유한회사|\(사\)|협동조합)")


def find_winner(t):
    for m in WINNER_LBL.finditer(t):
        seg = _after(t, m.end(), 70).lstrip(" :：-·\t")
        c = WINNER_CUT.search(seg)
        cand = (seg[:c.start()] if c else seg).strip(" :：-·")
        if not (2 <= len(cand) <= 40) or cand.startswith(("는", "은", "을", "의", "가", "이", "및")):
            continue
        if STRONG_CORP.search(cand) or (CORP_LIKE.search(cand) and len(cand) <= 20 and cand.count(" ") <= 1):
            return cand
    return ""


def parse_detail(body, post_date=""):
    """공고·결과 본문에서 예산·낙찰(계약)금액·마감일·사업 종료일·낙찰업체"""
    t = re.sub(r"[ \t\u00a0]+", " ", body or "")
    info = {"budget": _money_after(t, BUDGET_LBL), "amount": _money_after(t, AWARD_LBL), "deadline": "", "period_end": "",
            "winner": find_winner(t)}
    for m in DEADLINE_LBL.finditer(t):
        ds = _all_dates(_after(t, m.end()))
        if ds:
            info["deadline"] = ds[-1]
            break
    for m in PERIOD_LBL.finditer(t):
        seg = _after(t, m.end())
        ds = _all_dates(seg)
        if ds:
            info["period_end"] = max(ds)
            break
        r = REL_RE.search(seg) or re.search(r"(\d{1,4})\s*(개월|일|년)\s*(?:이내|간|까지)?", seg)
        if r and post_date:
            n, unit = int(r.groups()[-2]), r.groups()[-1]
            base = datetime.strptime(post_date, "%Y-%m-%d")
            days = n * (30 if unit == "개월" else 365 if unit == "년" else 1)
            if 7 <= days <= 365 * 5:
                info["period_end"] = (base + timedelta(days=days + 14)).strftime("%Y-%m-%d")   # 계약까지 2주 가정
            break
    return info


JS_TABLES = """() => Array.from(document.querySelectorAll('table')).map(tb => {
  const head = Array.from(tb.querySelectorAll('thead th, tr:first-child th, tr:first-child td')).map(x => (x.textContent||'').replace(/\\s+/g,' ').trim());
  const rows = Array.from(tb.querySelectorAll('tbody tr')).slice(0, 80).map(tr => {
    const cells = Array.from(tr.querySelectorAll('td,th')).map(x => (x.textContent||'').replace(/\\s+/g,' ').trim());
    const a = tr.querySelector('a');
    return {cells, href: a ? (a.href||'') : ''};
  });
  return {head, rows};
})"""
COL_RULES = {"title": r"(계약명|건명|사업명|용역명|공사명|품명|제목|입찰명|과제명)",
             "vendor": r"(업체|상대자|상대방|계약자|낙찰자|상호|거래처|대상자)",
             "amount": r"(금액|계약액|낙찰가|낙찰액)", "date": r"(계약일|체결일|일자|등록일|낙찰일|개찰일|시작일|착수일)",
             "period": r"(기간|완료|준공|납품기한|만료|종료)"}


def parse_contract_tables(tables):
    """정보공개 '계약현황·수의계약현황·입찰결과' 표 → 계약 행 (사업명·업체·금액·일자·기간)"""
    out = []
    for tb in tables:
        head = list(tb.get("head") or [])
        rows_ = list(tb.get("rows") or [])
        if rows_:                                   # 머리줄이 두 줄(계약상대자 → 업체명·대표자…)이면 아랫줄로 보완
            sub = rows_[0].get("cells") or []
            hits = sum(1 for c in sub if any(re.search(rx, c) for rx in COL_RULES.values()) and not re.search(r"\d{3,}", c))
            if hits >= 2:
                head = [(sub[i] if i < len(sub) and sub[i] else (head[i] if i < len(head) else "")) for i in range(max(len(head), len(sub)))]
                rows_ = rows_[1:]
        tb = {"head": head, "rows": rows_}
        idx = {}
        for k, rx in COL_RULES.items():
            for i, h in enumerate(head):
                if re.search(rx, h) and i not in idx.values():
                    idx[k] = i
                    break
        if "title" not in idx or ("vendor" not in idx and "amount" not in idx):
            continue
        if os.environ.get("UNIV_DEBUG"):
            print("[표 머리줄]", head[:12], "→", idx, "| 첫 줄", (tb.get("rows") or [{}])[0].get("cells", [])[:12], flush=True)
        for r in tb.get("rows") or []:
            cells = r.get("cells") or []
            if len(cells) <= idx["title"]:
                continue

            def g(k):
                i = idx.get(k)
                return cells[i] if i is not None and i < len(cells) else ""
            title = g("title")
            if len(title) < 4:
                continue
            am = MONEY_RE.search(g("amount") + "원") if g("amount") else None
            ds = _all_dates(g("period"))
            out.append({"title": title, "date": _date(g("date")) or _date(" ".join(cells)), "winner": g("vendor"),
                        "amount": _won(am.group(1), am.group(2)) if am else "", "period_end": max(ds) if ds else "",
                        "href": r.get("href", "")})
    return out



def load_master(path="data/univ_master.csv"):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    seen, out = set(), []
    for r in rows:
        hp = (r["학교홈페이지"] or "").strip()
        if not hp:
            continue
        if not hp.startswith("http"):
            hp = "https://" + hp
        pu = urlparse(hp)
        host = pu.netloc.lower().replace("www.", "") + pu.path.rstrip("/").lower()   # 같은 도메인의 분교(/wj 등)는 따로
        if host in seen:
            continue
        seen.add(host)
        out.append({"school": r["학교명"], "campus": r["본분교"], "type": r["학제"], "region": r["지역"],
                    "found": r["설립구분"], "home": hp})
    return out


def _date(txt):
    m = DATE_RE.search(txt or "")
    if not m:
        m2 = DATE_SHORT_RE.search(txt or "")
        if not m2:
            return ""
        y, mo, d = 2000 + int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}" if 1 <= mo <= 12 and 1 <= d <= 31 else ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


JS_LINKS = """() => Array.from(document.querySelectorAll('a')).map(a => ({t: ((a.textContent||'').replace(/\\s+/g,' ').trim() || a.title || (a.querySelector('img')||{}).alt || '').slice(0,60), h: a.href||'', o: a.getAttribute('onclick')||''}))"""
JS_ROWS = """() => {
  const out = [];
  const rows = document.querySelectorAll('table tr, ul li, ol li, div[class*=list] > div, div[class*=List] > div, div[class*=row], dl');
  rows.forEach(r => {
    const as = Array.from(r.querySelectorAll('a'));
    if (!as.length) return;
    const txt = x => (x.textContent || x.title || '').replace(/\\s+/g,' ').trim();
    const a = as.reduce((b, x) => txt(x).length > txt(b).length ? x : b, as[0]);   // 제목 링크 = 글자가 가장 긴 링크
    const t = txt(a);
    if (t.length < 6) return;
    out.push({title: t.slice(0,200), href: a.href || '', onclick: a.getAttribute('onclick') || '', text: (r.textContent||'').replace(/\\s+/g,' ').slice(0,400)});
  });
  return out;
}"""


async def eval_all(page, js):
    """메인 문서 + 모든 프레임(iframe/frameset)에서 같은 스크립트 실행"""
    out = []
    for fr in page.frames:
        try:
            out += await fr.evaluate(js)
        except Exception:
            pass
    return out


async def find_board(page, home):
    """홈페이지를 최대 MAX_VISIT쪽 둘러보며 ① 입찰공고 게시판 ② 입찰결과·계약정보 공개 페이지 후보를 점수 순으로"""
    bid, res = {}, {}
    dom = urlparse(home).netloc.replace("www.", "")

    def score_bid(t, h):
        s_ = 0
        if re.search(r"입찰\s*공고", t):
            s_ += 10
        elif re.search(r"입찰", t):
            s_ += 7
        elif re.search(r"구매|조달", t):
            s_ += 4
        if BOARD_BAD.search(t):
            s_ -= 8
        if re.search(r"bid|ipchal|purchase|buying", h, re.I):
            s_ += 3
        return s_

    async def collect(url):
        try:
            await page.goto(url, timeout=40000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2200)
        except Exception:
            return []
        links = await eval_all(page, JS_LINKS)
        base = page.url
        for fr in page.frames:
            try:
                html = await fr.content()
            except Exception:
                continue
            for h, t in HTML_LINK_RE.findall(html):
                links.append({"t": t.strip(), "h": urljoin(fr.url or base, h), "o": ""})
            for m in JSON_LINK_RE.findall(html):
                h, t = (m[0], m[1]) if m[0] else (m[3], m[2])
                links.append({"t": t, "h": urljoin(fr.url or base, h.replace("\\/", "/")), "o": ""})
        return links

    queue, seen = [home], set()
    visited = 0
    root = ""
    while queue and visited < MAX_VISIT:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        links = await collect(u)
        visited += 1
        if not root:
            pu = urlparse(page.url or home)
            root = f"{pu.scheme}://{pu.netloc}"
            queue = [root + p_ for p_ in SITEMAP_PATHS[:6]] + queue      # 사이트맵 먼저
        menus = []
        for l in links:
            t, h = l["t"], l["h"]
            if not h.startswith("http") or "javascript" in h:
                continue
            same = urlparse(h).netloc.replace("www.", "").endswith(dom) or dom.endswith(urlparse(h).netloc.replace("www.", ""))
            sb = score_bid(t, h)
            if sb > 0 and BOARD_WORDS.search(t + " " + h):
                bid[h] = max(bid.get(h, 0), sb)
            if CONTRACT_WORDS.search(t) and not BOARD_BAD.search(t):
                res[h] = max(res.get(h, 0), 5 + (3 if re.search(r"계약\s*(정보|현황)|수의", t) else 0))
            if same and (MENU_WORDS.search(t) or re.search(r"(정보\s*공개|행정\s*정보|청렴|투명|재정|예산|회계)", t)):
                menus.append(h)
        queue += [m for m in dict.fromkeys(menus) if m not in seen][:15]
        if bid and res and visited >= 4:
            break
    rank = lambda d_: [h for h, _ in sorted(d_.items(), key=lambda x: -x[1])]
    return rank(bid)[:3], rank(res)[:2]


async def read_board(page, url):
    """게시판 1~MAX_PAGES쪽 글 목록"""
    posts = []
    try:
        await page.goto(url, timeout=25000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
    except Exception:
        return posts
    for pno in range(1, MAX_PAGES + 1):
        rows = await eval_all(page, JS_ROWS)
        if pno == 1 and not any(_date(r["text"]) for r in rows):     # 늦게 뜨는 게시판: 더 기다렸다 다시
            for _ in range(2):
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                await page.wait_for_timeout(3000)
                rows = await eval_all(page, JS_ROWS)
                if any(_date(r["text"]) for r in rows):
                    break
        for r in rows:
            d = _date(r["text"])
            if not d:
                continue
            posts.append({"title": r["title"], "date": d, "href": r["href"] if r["href"].startswith("http") and "javascript" not in r["href"] else "",
                          "onclick": r["onclick"], "list_url": page.url})
        if pno == MAX_PAGES:
            break
        nxt = page.locator(f"a:text-is('{pno + 1}')").first
        try:
            if await nxt.count() == 0:
                break
            await nxt.click(timeout=5000)
            await page.wait_for_timeout(1800)
        except Exception:
            break
    uniq = {}
    for p in posts:
        uniq[(p["title"], p["date"])] = p
    return list(uniq.values())


def _sheet_tables(data, name):
    """엑셀·CSV 첨부 → 표 [{head, rows}] (머리줄은 '계약명/업체/금액' 단어가 있는 줄)"""
    import io
    import pandas as pd
    try:
        if name.lower().endswith(".csv"):
            sheets = {"csv": pd.read_csv(io.BytesIO(data), header=None, dtype=str, encoding_errors="ignore")}
        else:
            sheets = pd.read_excel(io.BytesIO(data), header=None, dtype=str, sheet_name=None)
    except Exception:
        return []
    out = []
    for df in sheets.values():
        df = df.fillna("")
        vals = [[str(x).strip() for x in row] for row in df.values.tolist()]
        for i, row in enumerate(vals[:15]):
            line = " ".join(row)
            if re.search(COL_RULES["title"], line) and (re.search(COL_RULES["vendor"], line) or re.search(COL_RULES["amount"], line)):
                out.append({"head": row, "rows": [{"cells": r, "href": ""} for r in vals[i + 1:i + 400]]})
                break
    return out


async def read_detail(page, post, want_tables=False):
    """글 본문(+필요하면 첨부 공고문)에서 예산·금액·마감·종료일·낙찰업체.
    want_tables: '수의계약 현황' 같은 목록 글이면 본문 표·엑셀 첨부의 계약 행도 함께"""
    crows = []
    try:
        if post["href"]:
            await page.goto(post["href"], timeout=25000, wait_until="domcontentloaded")
        else:
            await page.goto(post["list_url"], timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            await page.get_by_text(post["title"][:30], exact=False).first.click(timeout=6000)
        await page.wait_for_timeout(1500)
        body = ""
        for fr in page.frames:
            try:
                body += "\n" + await fr.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                pass
        url = page.url
        html = await page.content()
    except Exception:
        return {}, post.get("href", ""), crows
    info = parse_detail(body, post.get("date", ""))
    if want_tables:
        try:
            crows = parse_contract_tables(await eval_all(page, JS_TABLES))
        except Exception:
            crows = []
        if not crows:                                   # 본문에 표가 없으면 엑셀 첨부
            try:
                from bs4 import BeautifulSoup
                for a_ in BeautifulSoup(html, "html.parser").find_all("a"):
                    nm = a_.get_text(" ", strip=True)
                    hr = (a_.get("href") or "").strip()
                    if SHEET_EXT_RE.search(nm) and hr and not hr.startswith(("#", "javascript")):
                        resp = await page.request.get(urljoin(url, hr), timeout=30000)
                        if resp.ok:
                            crows = parse_contract_tables(_sheet_tables(await resp.body(), nm))
                        break
            except Exception:
                pass
        return info, url, crows
    if not (info["budget"] or info["amount"]) or not info["period_end"]:
        try:                                         # 본문에 없으면 첨부 공고문(hwp/hwpx/pdf) 중 공고문다운 것 1개
            from collectors import attachments_from_html
            from doc_text import bytes_to_text, rank_attachments
            atts = rank_attachments(attachments_from_html(html, url))
            if atts:
                name, aurl = atts[0]
                resp = await page.request.get(aurl, timeout=30000)
                if resp.ok:
                    txt = bytes_to_text(await resp.body(), name) or ""
                    extra = parse_detail(txt, post.get("date", ""))
                    for k, v in extra.items():
                        if v and not info.get(k):
                            info[k] = v
        except Exception:
            pass
    return info, url, crows


SCHOOL_TIMEOUT = 720      # 한 학교 최대 12분 (넘으면 그때까지 모은 것만)


async def crawl_school(browser, sch, overrides, it_reason, sem):
    async with sem:
        rep = {"school": sch["school"], "home": sch["home"], "board": "", "result_board": "", "posts": 0, "it_posts": 0,
               "contracts": 0, "status": ""}
        out = []
        try:
            await asyncio.wait_for(_crawl_school(browser, sch, overrides, it_reason, out, rep), SCHOOL_TIMEOUT)
        except asyncio.TimeoutError:
            rep["status"] = "성공" if out else "시간 초과"
            rep["it_posts"] = len(out)
            print(f"[시간 초과] {sch['school']} · 모은 IT {len(out)}", flush=True)
        return out, rep


async def _crawl_school(browser, sch, overrides, it_reason, out, rep):
    if True:
        ctx = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36",
                                        ignore_https_errors=True, locale="ko-KR")
        page = await ctx.new_page()
        try:
            ov = overrides.get(sch["school"])
            if not ov:                                # 홈페이지 자체가 안 열리면(해외 접속 차단 등) 바로 기록
                try:
                    await page.goto(sch["home"], timeout=35000, wait_until="domcontentloaded")
                except Exception as e:
                    msg = str(e)
                    why = ("주소 없음(DNS)" if "NAME_NOT_RESOLVED" in msg else "응답 없음" if "Timeout" in msg
                           else "연결 거부" if ("REFUSED" in msg or "RESET" in msg or "chrome-error" in msg) else type(e).__name__)
                    rep["status"] = f"접속 불가({why})"
                    return
            notice_board = isinstance(ov, dict) and ov.get("notice")
            res_boards = []
            if isinstance(ov, dict):
                res_boards = ov.get("result") or []
                ov = ov.get("url")
            if isinstance(ov, str):
                ov = [ov]
            ov = [x for x in (ov or []) if x]        # 결과 주소만 적은 학교는 입찰 게시판은 자동으로 찾기
            if isinstance(res_boards, str):
                res_boards = [res_boards]
            if ov:
                boards = ov
                if not res_boards:
                    _, res_boards = await find_board(page, sch["home"])
            else:
                boards, found_res = await find_board(page, sch["home"])
                res_boards = res_boards or found_res
            floor = (datetime.now() - timedelta(days=365 * KEEP_YEARS)).strftime("%Y-%m-%d")
            posts = []
            for b in (boards if ov else boards[:2]):
                ps = await read_board(page, b)
                if ps:
                    posts += ps
                    rep["board"] = (rep["board"] + " , " if rep["board"] else "") + b
                    if not ov and len(ps) >= 3:
                        break
            # 입찰결과·계약정보 공개: 표(계약명·업체·금액)면 바로 계약 행, 게시판이면 글 목록
            contract_rows = []
            for b in res_boards[:2]:
                try:
                    await page.goto(b, timeout=30000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2500)
                    tables = await eval_all(page, JS_TABLES)
                except Exception:
                    tables = []
                cr = parse_contract_tables(tables)
                if cr:
                    contract_rows += cr
                    rep["result_board"] = b
                else:
                    ps = await read_board(page, b)
                    for p_ in ps:
                        p_["_result"] = True
                    if ps:
                        posts += ps
                        rep["result_board"] = b
            if not posts and not contract_rows:
                rep["status"] = "게시판 못 찾음" if not boards and not res_boards else "게시판 글 못 읽음"
                rep["board"] = rep["board"] or (boards[0] if boards else "")
                return
            posts = [p for p in posts if p["date"] >= floor]
            rep["posts"] = len(posts)
            n_detail, n_list = 0, 0
            for p in posts:
                if LIST_POST_RE.search(p["title"]) and n_list < MAX_LIST_POSTS:    # 월별 '수의계약 현황' 등 → 표 안의 계약들
                    n_list += 1
                    _, durl, crows = await read_detail(page, p, want_tables=True)
                    for cr in crows:
                        cr["date"] = cr["date"] or p["date"]
                        cr["href"] = cr["href"] or durl
                    contract_rows += crows
                    continue
                if re.search(r"(취소\s*공고|공고\s*취소|입찰\s*취소)", p["title"]):
                    continue
                if (notice_board or p.get("_result")) and not BID_TITLE_RE.search(p["title"]):
                    continue                        # 공지사항·정보공개 게시판이면 입찰·계약 글만
                reason = it_reason({"cntrctNm": p["title"]})
                if not reason:
                    continue
                kind = "결과" if (p.get("_result") or RESULT_WORDS.search(p["title"])) else "공고"
                info, durl = {}, p["href"]
                if n_detail < MAX_DETAIL:
                    info, durl, _ = await read_detail(page, p)
                    n_detail += 1
                rep["it_posts"] = len(out) + 1
                out.append({**sch, "title": p["title"], "date": p["date"], "kind": kind, "winner": info.get("winner", ""),
                            "amount": info.get("amount", ""), "budget": info.get("budget", ""), "deadline": info.get("deadline", ""),
                            "period_end": info.get("period_end", ""), "url": durl or p["href"] or p["list_url"],
                            "it_reason": reason, "board": rep["board"]})
            for cr in contract_rows:
                if cr["date"] and cr["date"] < floor:
                    continue
                reason = it_reason({"cntrctNm": cr["title"]})
                if not reason:
                    continue
                out.append({**sch, "title": cr["title"], "date": cr["date"], "kind": "계약공개", "winner": cr["winner"],
                            "amount": cr["amount"], "budget": "", "deadline": "", "period_end": cr["period_end"],
                            "url": cr["href"] or rep["result_board"], "it_reason": reason, "board": rep["result_board"]})
                rep["contracts"] += 1
            seen_ = set()                              # 같은 계약이 여러 달 목록에 겹치면 하나만
            out[:] = [r for r in out if not ((r["title"], r["winner"], r["amount"]) in seen_ or seen_.add((r["title"], r["winner"], r["amount"])))]
            rep["it_posts"] = len(out)
            rep["status"] = "성공"
        except Exception as e:
            rep["status"] = f"오류: {type(e).__name__}"
        finally:
            await ctx.close()
            print(f"[{rep['status']}] {sch['school']} · 글 {rep['posts']} · IT {rep['it_posts']} · 계약공개 {rep['contracts']} · "
                  f"{rep['board'][:70]} | {rep['result_board'][:60]}", flush=True)


def _order(s_):
    t = s_["type"]
    return 0 if t in ("대학교", "교육대학", "산업대학") else 1 if t == "전문대학" else 2


def finish(rows, report):
    """수집 결과 저장(파일 + DB) — 이비즈포유 공고도 함께"""
    import contract_export as ce
    rows = rows + collect_ebiz4u(ce.it_reason)
    json.dump(rows, open("univ_bids.json", "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(report, open("univ_crawl_report.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = sum(1 for r in report if r["status"] == "성공")
    print(f"[OK] 대학 홈페이지 수집: 학교 {len(report)}곳 중 성공 {ok}곳 · IT 관련 {len(rows)}건 "
          f"(결과 {sum(1 for r in rows if r['kind'] == '결과')} · 계약공개 {sum(1 for r in rows if r['kind'] == '계약공개')} · "
          f"업체명 {sum(1 for r in rows if r.get('winner'))} · 예산/금액 {sum(1 for r in rows if r.get('budget') or r.get('amount'))} · "
          f"종료일 {sum(1 for r in rows if r.get('period_end'))})")
    try:
        save_db(rows, report)
    except Exception as e:
        print(f"[WARN] DB 저장 실패: {type(e).__name__}")


async def main_async(a):
    from playwright.async_api import async_playwright
    import contract_export as ce
    schools = sorted(load_master(), key=_order)
    try:
        overrides = json.load(open("data/univ_boards.json", encoding="utf-8"))
    except Exception:
        overrides = {}
    if a.only == "직접지정":                    # data/univ_boards.json 에 주소를 적은 학교만 다시
        schools = [s for s in schools if s["school"] in overrides]
    elif a.only:                               # 쉼표로 여러 학교
        names = [x.strip() for x in a.only.split(",") if x.strip()]
        schools = [s for s in schools if any(n in s["school"] for n in names)]
    if a.shard:                                # "0/4" → 4대 중 0번 컴퓨터 몫
        i, n = map(int, a.shard.split("/"))
        schools = [s for k, s in enumerate(schools) if k % n == i]
    if a.limit:
        schools = schools[:a.limit]
    print(f"대상 학교 {len(schools)}곳", flush=True)
    sem = asyncio.Semaphore(CONCURRENCY)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        res = await asyncio.gather(*[crawl_school(browser, s, overrides, ce.it_reason, sem) for s in schools])
        await browser.close()
    rows = [r for o, _ in res for r in o]
    report = [r for _, r in res]
    if a.out:                                  # 나눠 돌리는 중: 파일만 남기고 합치기 단계에서 저장
        json.dump({"rows": rows, "report": report}, open(a.out, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"[OK] {a.out}: 학교 {len(report)} · 성공 {sum(1 for r in report if r['status'] == '성공')} · IT {len(rows)}")
        return
    finish(rows, report)


EBIZ4U_LIST = "https://www.ebiz4u.co.kr/home.do?cmd=private&subcmd=searchBiddingList&srchServiceType=ebiz4u&srchPrType={t}&srchText="


def _ebiz_budget(b):
    for k, v in b.items():
        if re.search(r"(amt|price|budget|money)", k, re.I) and str(v).replace(",", "").replace(".", "").isdigit():
            v = str(int(float(str(v).replace(",", ""))))
            if int(v) >= 100000:
                return v
    return ""


def _ebiz_deadline(b):
    for k, v in b.items():
        if re.search(r"(bid_expire|end|clos|dead|fin)", k, re.I) and v:
            try:
                if str(v).isdigit() and len(str(v)) >= 12:
                    return datetime.fromtimestamp(int(v) / 1000).strftime("%Y-%m-%d")
                d = _date(str(v))
                if d:
                    return d
            except Exception:
                pass
    return ""


def collect_ebiz4u(it_reason):
    """이비즈포유(대학 전자입찰 플랫폼: 성균관대·연세대·이화여대·국민대·숭실대·홍익대 등)의 현재 공고 전체.
    공고는 마감 전까지만 보이므로 매일 받아 쌓음"""
    import requests
    import csv
    master = {re.sub(r"\s+", "", r["학교명"]): r for r in csv.DictReader(open("data/univ_master.csv", encoding="utf-8-sig"))}
    keys = sorted(master, key=len, reverse=True)
    out, seen = [], set()
    for t in ("ALL", "W", "G", "C"):
        try:
            r = requests.get(EBIZ4U_LIST.format(t=t), headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
            lst = (r.json() or {}).get("biddingList") or []
        except Exception as e:
            print(f"[WARN] 이비즈포유 {t}: {type(e).__name__}")
            continue
        if lst and t == "ALL":
            print("이비즈포유 항목 칸:", ", ".join(sorted(lst[0].keys()))[:600])
        for b in lst:
            k = b.get("rfq_no")
            if not k or k in seen:
                continue
            seen.add(k)
            title = str(b.get("ttl") or "").strip()
            reason = it_reason({"cntrctNm": title})
            if not reason:
                continue
            org = str(b.get("org_nm") or "").strip()
            nm = re.sub(r"\s+", "", org)
            m = next((master[x] for x in keys if x in nm), None)
            reg = datetime.fromtimestamp(int(b.get("reg_dt") or 0) / 1000).strftime("%Y-%m-%d") if b.get("reg_dt") else ""
            out.append({"school": org, "campus": "", "type": (m or {}).get("학제", ""), "region": (m or {}).get("지역", ""),
                        "found": (m or {}).get("설립구분", ""), "home": "https://www.ebiz4u.co.kr", "title": title, "date": reg,
                        "kind": "결과" if RESULT_WORDS.search(title) else "공고", "winner": "", "amount": "",
                        "budget": _ebiz_budget(b), "deadline": _ebiz_deadline(b),
                        "url": f"https://www.ebiz4u.co.kr/bid/bidding.do?cmd=viewPublic&subcmd=login&aspId={b.get('asp_id', 'u.ebiz4u')}&rfqNo={k}",
                        "it_reason": reason, "board": "이비즈포유"})
    print(f"[OK] 이비즈포유: 현재 공고 {len(seen)}건 중 IT 관련 {len(out)}건")
    return out


def save_db(rows, report):
    from sqlalchemy import text
    from db2 import get_engine
    import store
    cols = ["uniq_key", "school", "campus", "type", "region", "found", "title", "date", "kind", "winner", "amount", "url", "it_reason", "board",
            "budget", "deadline", "period_end", "updated_at"]
    with get_engine().begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS univ_bids (uniq_key TEXT PRIMARY KEY, " + ", ".join(f"{c} TEXT" for c in cols[1:]) + ")"))
    for col in ("budget", "deadline", "period_end"):           # 예전 표에 새 칸 추가
        try:
            with get_engine().begin() as conn:
                conn.execute(text(f"ALTER TABLE univ_bids ADD COLUMN {col} TEXT"))
        except Exception:
            pass
    with get_engine().begin() as conn:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        data = []
        for r in rows:
            d = {c: str(r.get(c, "") or "") for c in cols}
            d["uniq_key"] = f"{r['school']}|{r['date']}|{r['title'][:80]}"
            d["updated_at"] = now
            data.append(d)
        if data:
            upd = ", ".join(f"{c}=excluded.{c}" for c in cols[1:])
            conn.execute(text(f"INSERT INTO univ_bids ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)}) "
                              f"ON CONFLICT (uniq_key) DO UPDATE SET {upd}"), data)
    if report is not None:                     # 일부 학교만 다시 돈 경우 기존 결과에 덮어씀
        old, _ = store.load_cache("univ_crawl_report")
        merged = {r["school"]: r for r in (old or [])}
        merged.update({r["school"]: r for r in report})
        store.save_cache("univ_crawl_report", list(merged.values()))


async def probe(urls):
    """조사용: 주소를 열어 프레임별 글자·링크를 출력"""
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await (await b.new_context(ignore_https_errors=True, locale="ko-KR")).new_page()
        xhr = []

        async def on_resp(r):
            try:
                if r.request.resource_type in ("xhr", "fetch", "document"):
                    body = ""
                    try:
                        body = (await r.text())[:600]
                    except Exception:
                        pass
                    xhr.append((r.request.method, r.url, (r.request.post_data or "")[:400], body))
            except Exception:
                pass
        pg.on("response", lambda r: asyncio.ensure_future(on_resp(r)))
        for u in urls:
            click = ""
            if " >> " in u:                      # "주소 >> 누를 글자"
                u, click = u.split(" >> ", 1)
            try:
                await pg.goto(u, timeout=30000, wait_until="domcontentloaded")
                await pg.wait_for_timeout(5000)
            except Exception as e:
                print("ERR", u, e)
                continue
            if click:
                for t in click.split(" > "):
                    try:
                        await pg.get_by_text(t, exact=True).first.click(timeout=6000)
                        await pg.wait_for_timeout(4000)
                    except Exception as e:
                        print("click fail", t, type(e).__name__)
            print("=====", u, "→", pg.url, "frames", len(pg.frames))
            for m, xu, pd_, body in xhr:
                print("  XHR", m, xu[:200], "| post:", pd_[:300], "| body:", body[:300].replace("\n", " "))
            xhr.clear()
            for fr in pg.frames:
                try:
                    txt = await fr.evaluate("() => (document.body ? document.body.innerText : '').replace(/\\s+/g,' ').slice(0,1500)")
                    links = await fr.evaluate(JS_LINKS)
                except Exception:
                    continue
                print("--- frame", fr.url[:150]); print(txt)
                for l in links[:80]:
                    if l["t"]:
                        print("   ", l["t"][:40], "|", (l["h"] or l["o"])[:150])
        await b.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--probe", default="")
    ap.add_argument("--ebiz4u-only", action="store_true", help="이비즈포유 공고만 (매일)")
    ap.add_argument("--shard", default="", help="나눠 돌리기 i/n")
    ap.add_argument("--out", default="", help="나눠 돌린 결과 파일")
    ap.add_argument("--merge", nargs="*", default=None, help="나눠 돌린 결과 파일들을 합쳐 저장")
    a = ap.parse_args()
    if a.merge is not None:
        rows, report = [], []
        for f in a.merge:
            try:
                d = json.load(open(f, encoding="utf-8"))
            except Exception:
                print(f"[WARN] {f} 읽기 실패")
                continue
            rows += d["rows"]
            report += d["report"]
        finish(rows, report)
        return 0
    if a.probe:
        asyncio.run(probe([x for x in a.probe.split(",,") if x]))
        return 0
    if a.ebiz4u_only:
        import contract_export as ce
        rows = collect_ebiz4u(ce.it_reason)
        save_db(rows, None)
        return 0
    asyncio.run(main_async(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
