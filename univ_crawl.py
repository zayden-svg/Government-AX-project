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
KEEP_YEARS = 3
MAX_PAGES = 3
CONCURRENCY = 5


def load_master(path="data/univ_master.csv"):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    seen, out = set(), []
    for r in rows:
        if r["설립구분"] not in ("사립", "공립"):
            continue
        hp = (r["학교홈페이지"] or "").strip()
        if not hp:
            continue
        if not hp.startswith("http"):
            hp = "https://" + hp
        host = urlparse(hp).netloc.lower().replace("www.", "")
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
    const a = r.querySelector('a');
    if (!a) return;
    const t = (a.textContent || a.title || '').replace(/\\s+/g,' ').trim();
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
    """홈페이지에서 입찰 게시판 주소 후보를 점수 순으로"""
    cands = {}

    def score(t, h):
        s = 0
        if re.search(r"입찰\s*공고", t):
            s += 10
        elif re.search(r"입찰", t):
            s += 7
        elif re.search(r"구매|조달", t):
            s += 4
        elif re.search(r"계약\s*정보|계약\s*현황", t):
            s += 3
        if BOARD_BAD.search(t):
            s -= 8
        if re.search(r"bid|ipchal|purchase|buying|contract", h, re.I):
            s += 3
        return s

    async def collect(url):
        """링크 목록 + 화면 HTML 속 숨은 메뉴(JSON·스크립트)에서 '입찰' 링크까지"""
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
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

    def take(links, bonus=0):
        menus = []
        for l in links:
            t, h = l["t"], l["h"]
            if not h.startswith("http"):
                continue
            sc = score(t, h)
            if sc > 0 and BOARD_WORDS.search(t + " " + h):
                cands[h] = max(cands.get(h, 0), sc + bonus)
            elif MENU_WORDS.search(t) and urlparse(h).netloc.replace("www.", "") == urlparse(home).netloc.replace("www.", ""):
                menus.append(h)
        return menus

    menus = take(await collect(home))
    root = f"{urlparse(page.url or home).scheme}://{urlparse(page.url or home).netloc}"
    if not cands:                              # 사이트맵 먼저 (메뉴 전체가 한 화면에 있음)
        sm = [m for m in menus if re.search(r"sitemap|사이트", m, re.I)] + [root + p_ for p_ in SITEMAP_PATHS]
        for m in sm[:8]:
            take(await collect(m), -1)
            if cands:
                break
    if not cands:
        for m in list(dict.fromkeys(menus))[:12]:
            take(await collect(m), -1)
            if cands:
                break
    return [h for h, _ in sorted(cands.items(), key=lambda x: -x[1])][:3]


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


async def read_detail(page, post):
    """결과 글 본문에서 업체명·금액 찾기"""
    try:
        if post["href"]:
            await page.goto(post["href"], timeout=25000, wait_until="domcontentloaded")
        else:
            await page.goto(post["list_url"], timeout=25000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            await page.get_by_text(post["title"][:30], exact=False).first.click(timeout=6000)
        await page.wait_for_timeout(1500)
        body = await page.evaluate("() => document.body.innerText")
        url = page.url
    except Exception:
        return "", "", post.get("href", "")
    w = WINNER_RE.search(body)
    a = AMOUNT_RE.search(body)
    winner = re.sub(r"\s+", " ", w.group(2)).strip(" :：-") if w else ""
    # 회사 이름처럼 보이지 않으면 버림 (예: '가 소정 기일')
    if winner and not re.search(r"(주식회사|\(주\)|㈜|유한|회사|시스템|정보|테크|텍|소프트|솔루션|네트웍|네트워크|컴퍼니|커뮤니케이션|"
                                r"아이티|IT|디지털|데이타|데이터|산업|전자|통신|엔지니어링|corp|inc|co\.)", winner, re.I):
        winner = ""
    return winner, (a.group(2).replace(",", "") if a else ""), url


async def crawl_school(browser, sch, overrides, it_reason, sem):
    async with sem:
        ctx = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36",
                                        ignore_https_errors=True, locale="ko-KR")
        page = await ctx.new_page()
        rep = {"school": sch["school"], "home": sch["home"], "board": "", "posts": 0, "it_posts": 0, "status": ""}
        out = []
        try:
            ov = overrides.get(sch["school"])
            notice_board = isinstance(ov, dict) and ov.get("notice")
            if isinstance(ov, dict):
                ov = ov.get("url")
            boards = ov or await find_board(page, sch["home"])
            if isinstance(boards, str):
                boards = [boards]
            if not boards:
                rep["status"] = "게시판 못 찾음"
                return out, rep
            floor = (datetime.now() - timedelta(days=365 * KEEP_YEARS)).strftime("%Y-%m-%d")
            posts = []
            if ov:                                 # 직접 적은 게시판은 여러 개 모두 읽음 (예: 입찰공고 + 입찰결과)
                for b in boards:
                    posts += await read_board(page, b)
                rep["board"] = " , ".join(boards)
            else:
                for b in boards[:2]:
                    ps = await read_board(page, b)
                    if len(ps) >= 3:
                        posts, rep["board"] = ps, b
                        break
            if not posts:
                rep["status"] = "게시판 글 못 읽음"
                rep["board"] = boards[0]
                return out, rep
            posts = [p for p in posts if p["date"] >= floor]
            rep["posts"] = len(posts)
            for p in posts:
                if notice_board and not re.search(r"(입찰|견적|구매|용역|낙찰|선정|계약)", p["title"]):
                    continue                        # 공지사항 게시판이면 입찰 글만
                reason = it_reason({"cntrctNm": p["title"]})
                if not reason:
                    continue
                kind = "결과" if RESULT_WORDS.search(p["title"]) else "공고"
                winner, amount, durl = ("", "", p["href"])
                if kind == "결과":
                    winner, amount, durl = await read_detail(page, p)
                out.append({**sch, "title": p["title"], "date": p["date"], "kind": kind, "winner": winner, "amount": amount,
                            "url": durl or p["href"] or p["list_url"], "it_reason": reason, "board": rep["board"]})
            rep["it_posts"] = len(out)
            rep["status"] = "성공"
        except Exception as e:
            rep["status"] = f"오류: {type(e).__name__}"
        finally:
            await ctx.close()
            print(f"[{rep['status']}] {sch['school']} · 글 {rep['posts']} · IT {rep['it_posts']} · {rep['board'][:80]}", flush=True)
        return out, rep


async def main_async(a):
    from playwright.async_api import async_playwright
    import contract_export as ce
    schools = load_master()
    try:
        overrides = json.load(open("data/univ_boards.json", encoding="utf-8"))
    except Exception:
        overrides = {}
    if a.only == "직접지정":                    # data/univ_boards.json 에 주소를 적은 학교만 다시
        schools = [s for s in schools if s["school"] in overrides]
    elif a.only:
        schools = [s for s in schools if a.only in s["school"]]
    if a.limit:
        schools = schools[:a.limit]
    sem = asyncio.Semaphore(CONCURRENCY)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        res = await asyncio.gather(*[crawl_school(browser, s, overrides, ce.it_reason, sem) for s in schools])
        await browser.close()
    rows = [r for o, _ in res for r in o]
    report = [r for _, r in res]
    rows += collect_ebiz4u(ce.it_reason)
    json.dump(rows, open("univ_bids.json", "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(report, open("univ_crawl_report.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = sum(1 for r in report if r["status"] == "성공")
    print(f"[OK] 대학 홈페이지 수집: 학교 {len(report)}곳 중 성공 {ok}곳 · IT 관련 글 {len(rows)}건 "
          f"(결과 {sum(1 for r in rows if r['kind'] == '결과')} · 업체명 찾음 {sum(1 for r in rows if r['winner'])})")
    try:
        save_db(rows, report)
    except Exception as e:
        print(f"[WARN] DB 저장 실패: {type(e).__name__}")


EBIZ4U_LIST = "https://www.ebiz4u.co.kr/home.do?cmd=private&subcmd=searchBiddingList&srchServiceType=ebiz4u&srchPrType={t}&srchText="


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
                        "url": f"https://www.ebiz4u.co.kr/bid/bidding.do?cmd=viewPublic&subcmd=login&aspId={b.get('asp_id', 'u.ebiz4u')}&rfqNo={k}",
                        "it_reason": reason, "board": "이비즈포유"})
    print(f"[OK] 이비즈포유: 현재 공고 {len(seen)}건 중 IT 관련 {len(out)}건")
    return out


def save_db(rows, report):
    from sqlalchemy import text
    from db2 import get_engine
    import store
    cols = ["uniq_key", "school", "campus", "type", "region", "found", "title", "date", "kind", "winner", "amount", "url", "it_reason", "board", "updated_at"]
    with get_engine().begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS univ_bids (uniq_key TEXT PRIMARY KEY, " + ", ".join(f"{c} TEXT" for c in cols[1:]) + ")"))
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
    a = ap.parse_args()
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
