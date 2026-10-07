# briefing_batch.py
# 매일 아침 자동수집 직후 실행 — 대시보드가 열릴 때마다 하던 무거운 작업을 미리 해 둔다.
#   뉴스 수집·AI 점수, 쉬운말 제목, 추천 키워드, 오늘의 헤드라인, 핵심 이슈 카드, 뉴스 요약, 제품별 AI 매칭
# 결과는 app_cache 테이블에 저장 → 대시보드는 읽기만 하므로 첫 화면이 수 초 안에 뜬다.
import re
import sys
import time
import traceback
from datetime import datetime, timedelta

import pandas as pd

import common  # noqa: F401  (한국시간 고정)
from common import (
    PRODUCT_KEYWORDS, INTEGRATED_RND_DOMAINS, PRODUCT_TO_DOMAIN,
    DEFAULT_NEWS_KEYWORDS, SOLUTION_NEWS_KEYWORDS,
)
from ai_utils import (
    is_ai_ready, recommend_keywords, generate_headline, generate_key_issues,
    generate_news_digest, simplify_news_titles, match_titles_to_product, generate_product_guide,
    generate_track_brief,
)
from product_match import PRODUCT_CODES, PRODUCT_TITLES, match_product, item_line
from news_pool import build_pool, fetch_keyword_news, SOURCES
from postings_data import load_active_postings
from store import save_cache, load_cache
from procurement_store import load_reorder_candidates
from trend_store import load_latest_trend

# 대시보드(app.py)가 읽는 키 이름 — 바꾸면 app.py도 같이 바꿔야 함
K_NEWS_DEFAULT = "news_pool_default"
K_NEWS_SRC10 = "news_src10_default"
K_NEWS_SOLUTION = "news_pool_solution"
K_NEWS_SIMPLE = "news_simple"
K_REC_KEYWORDS = "rec_keywords"
K_HEADLINE = "headline"
K_ISSUES = "issues"
K_DIGEST = "news_digest"
K_PRODUCT_AI = "product_ai_match"
K_PRODUCT_GUIDE = "product_guide"
K_META = "briefing_meta"
K_TRACK_BRIEF = "track_brief"          # 사업부·R&D별 헤드라인·동향·Action Item (PDF 요약본용)


def _title_key(t):
    return re.sub(r"\s+", "", str(t or ""))[:40]


def _step(name, fn, results):
    t0 = time.time()
    try:
        out = fn()
        results[name] = f"OK ({time.time() - t0:.0f}초)"
        return out
    except Exception as e:
        traceback.print_exc()
        results[name] = f"실패: {type(e).__name__}: {e}"[:200]
        return None


def _match_rows(sub, keywords):
    if sub.empty or not keywords:
        return sub.iloc[0:0]
    pat = "|".join(re.escape(k) for k in keywords)
    mask = sub["title"].str.contains(pat, case=False, na=False) | \
        sub["matched_keywords"].str.contains(pat, case=False, na=False)
    return sub[mask]


def run():
    results = {}
    started = datetime.now()
    ai_ok = is_ai_ready()
    print(f"[브리핑] 시작 {started:%Y-%m-%d %H:%M} · Claude API {'사용' if ai_ok else '미설정(뉴스 수집만 진행)'}")

    hl = iss = gd = tbs = None
    df = _step("공고 불러오기", load_active_postings, results)
    if df is None:
        df = pd.DataFrame()

    # 1) 뉴스 — 기본 키워드 묶음 / 자사 솔루션 묶음 / 수집처별 10건
    pool_default = _step("뉴스(기본)", lambda: build_pool(DEFAULT_NEWS_KEYWORDS), results) or []
    if pool_default:
        save_cache(K_NEWS_DEFAULT, pool_default)

    src10 = _step("뉴스(수집처별)", lambda: fetch_keyword_news(DEFAULT_NEWS_KEYWORDS[0], per_source=10), results) or {}
    if src10:
        save_cache(K_NEWS_SRC10, src10)

    pool_solution = _step("뉴스(자사 솔루션)", lambda: build_pool(SOLUTION_NEWS_KEYWORDS), results) or []
    if pool_solution:
        save_cache(K_NEWS_SOLUTION, pool_solution)

    if ai_ok:
        # 2) 쉬운말 제목 (TOP10 + 수집처별 10건)
        def _simple():
            ranked = sorted(pool_default, key=lambda x: -x.get("_score", -1))[:10]
            batch = list(ranked)
            for s in SOURCES:
                batch.extend((src10.get(s) or [])[:10])
            seen, items = set(), []
            for it in batch:
                k = _title_key(it.get("title"))
                if k and k not in seen:
                    seen.add(k)
                    items.append({"title": it.get("title", "")})
            out = {}
            for i in range(0, len(items), 25):
                res, err = simplify_news_titles(items[i:i + 25])
                for r in res or []:
                    if isinstance(r, dict) and r.get("simple"):
                        out[_title_key(r.get("title"))] = r["simple"]
            return out
        simple_map = _step("쉬운말 제목", _simple, results)
        if simple_map:
            save_cache(K_NEWS_SIMPLE, simple_map)

        # 3) 추천 키워드 (상단 검색칩 + 뉴스탭 추천)
        def _rec():
            titles = df["title"].head(80).tolist() if not df.empty else []
            trend = load_latest_trend()
            if not trend.empty and "keyword" in trend.columns:
                titles += trend["keyword"].dropna().astype(str).head(20).tolist()
            titles += [it.get("title", "") for it in pool_default[:30]]
            rec, err = recommend_keywords(titles)
            if err:
                raise RuntimeError(err)
            return rec
        rec = _step("추천 키워드", _rec, results)
        if rec:
            save_cache(K_REC_KEYWORDS, rec)

        if not df.empty:
            today = pd.Timestamp(datetime.now().date())

            # 4) 오늘의 헤드라인 — 공고 한 줄 요약(기관·예산·마감·연관도) + 주요 뉴스 제목
            def _kind(r):
                return "R&D" if str(r.get("_track")) == "RND" else "사업"

            def _headline():
                new_df = df[df["_created"].dt.date == today.date()].sort_values("_score", ascending=False)
                soon = df[df["_due"].notna() & (df["_due"] <= today + timedelta(days=7))].sort_values("_score", ascending=False)
                top = df.sort_values("_score", ascending=False)
                picked = pd.concat([new_df.head(10), soon.head(6), top.head(10)]).drop_duplicates("uniq_key")
                lines = [item_line(r, _kind(r)) for r in picked.head(24).to_dict("records")]
                lines += [f"[뉴스] {it.get('title', '')}" for it in sorted(pool_default, key=lambda x: -x.get("_score", -1))[:12]]
                obj, err = generate_headline(lines)
                if err or not obj:
                    raise RuntimeError(err or "빈 응답")
                return obj
            hl = _step("헤드라인", _headline, results)
            if hl:
                save_cache(K_HEADLINE, hl)

            # 5) 핵심 이슈 카드 — 테마 · 영향도 · 건수 · 사실 요약 · 우리 사업 영향 · 관련 제품
            def _issues():
                top = df[df["_score"] >= 50].sort_values("_score", ascending=False).head(30)
                if len(top) < 10:
                    top = df.sort_values("_score", ascending=False).head(30)
                lines = [item_line(r, _kind(r)) for r in top.to_dict("records")]
                lines += [f"[뉴스] {it.get('title', '')}" for it in sorted(pool_default, key=lambda x: -x.get("_score", -1))[:10]]
                issues, err = generate_key_issues("\n".join(f"- {x}" for x in lines), n=4)
                if err:
                    raise RuntimeError(err)
                return [i for i in (issues or []) if isinstance(i, dict)]
            iss = _step("이슈 카드", _issues, results)
            if iss:
                save_cache(K_ISSUES, iss)

            # 6) 제품별 대응 가이드 — 키워드로 0건일 때만 AI가 문맥으로 재판단
            def _product_ai():
                out = {}
                biz = df[df["_track"] == "BIZ"]
                rnd = df[df["_track"] == "RND"]
                for pname, pinfo in PRODUCT_KEYWORDS.items():
                    dom = []
                    for d in PRODUCT_TO_DOMAIN.get(pname, []):
                        dom.extend(INTEGRATED_RND_DOMAINS.get(d, {}).get("keywords", []))
                    for label, sub, kws in (("biz", biz, pinfo["keywords"]), ("rnd", rnd, dom)):
                        if sub.empty or not _match_rows(sub, kws).empty:
                            continue
                        titles, err = match_titles_to_product(pname, pinfo["desc"], sub["title"].tolist()[:150])
                        if not err and titles:
                            out[f"{label}_{pname}"] = titles
                return out
            pai = _step("제품별 AI 매칭", _product_ai, results)
            if pai is not None:
                save_cache(K_PRODUCT_AI, pai)

            # 7) 제품별 대응 가이드 — 화면과 같은 규칙으로 고른 사업·과제·뉴스를 보고 관련 이슈/영향/할 일 작성
            def _guide():
                biz = df[df["_track"] == "BIZ"]
                rnd = df[df["_track"] == "RND"]
                news = list(pool_solution) + list(pool_default)
                blocks = {}
                for pname in PRODUCT_KEYWORDS:
                    code = PRODUCT_CODES[pname]
                    mt = match_product(pname, biz, rnd, news, ai_titles=pai or {})
                    items = [item_line(r, "사업") for r in mt["biz"].head(5).to_dict("records")]
                    items += [item_line(r, "R&D") for r in mt["rnd"].head(4).to_dict("records")]
                    items += [f"[뉴스] {it.get('title', '')}" for it in mt["news"][:4]]
                    blocks[code] = {"name": PRODUCT_TITLES[code][0], "desc": PRODUCT_TITLES[code][1], "items": items}
                guide, err = generate_product_guide(blocks)
                if err:
                    raise RuntimeError(err)
                return guide
            gd = _step("제품별 대응 가이드", _guide, results)
            if gd:
                save_cache(K_PRODUCT_GUIDE, gd)

            # 7-2) 사업부 / R&D별 요약 — PDF 요약본의 헤드라인 · 동향 · Action Item
            def _track_briefs():
                out = {}
                guide = gd or load_cache(K_PRODUCT_GUIDE)[0] or {}
                guide_lines = [f"{c}: {g.get('issue', '')} / 할 일: {'; '.join(g.get('actions') or [])}"
                               for c, g in guide.items() if isinstance(g, dict) and not g.get("none")]
                news_titles = [it.get("title", "") for it in sorted(pool_default, key=lambda x: -x.get("_score", -1))[:12]]
                for track, label in (("BIZ", "사업부"), ("RND", "R&D")):
                    sub = df[df["_track"] == track]
                    if sub.empty:
                        continue
                    soon = sub[sub["_due"].notna() & (sub["_due"] <= today + timedelta(days=14))].sort_values("_score", ascending=False)
                    picked = pd.concat([sub.sort_values("_score", ascending=False).head(18), soon.head(10)]).drop_duplicates("uniq_key")
                    lines = [item_line(r, "사업" if track == "BIZ" else "R&D") for r in picked.to_dict("records")]
                    obj, err = generate_track_brief(label, lines, news_titles, guide_lines)
                    if obj:
                        out[track] = obj
                    elif err:
                        print(f"  [경고] {label} 요약 실패: {err}")
                return out
            tbs = _step("사업부·R&D 요약", _track_briefs, results)
            if tbs:
                save_cache(K_TRACK_BRIEF, tbs)

        # 8) 오늘의 뉴스 종합분석
        def _digest():
            titles = [it.get("title", "") for it in sorted(pool_default, key=lambda x: -x.get("_score", -1))[:40]]
            if not titles:
                return None
            txt, err = generate_news_digest(titles)
            if err:
                raise RuntimeError(err)
            return txt
        dg = _step("뉴스 요약", _digest, results)
        if dg:
            save_cache(K_DIGEST, dg)

    # 9) 사업부 / R&D 브리핑 PDF — 크롬으로 만들어 저장 (대시보드 'PDF' 버튼에서 내려받기)
    if df is not None and not df.empty:
        def _pdfs():
            from briefing_pdf import build_pdfs, K_PDF
            reorder = load_reorder_candidates(horizon_days=180, include_solution=True)
            res = build_pdfs(df, tbs or load_cache(K_TRACK_BRIEF)[0], gd or load_cache(K_PRODUCT_GUIDE)[0],
                             iss or load_cache(K_ISSUES)[0], hl or load_cache(K_HEADLINE)[0], reorder)
            for track, v in res.items():
                save_cache(K_PDF[track], v)
            return {k: v["size"] for k, v in res.items()}
        _step("브리핑 PDF", _pdfs, results)

    finished = datetime.now()
    save_cache(K_META, {"started": started.strftime("%Y-%m-%d %H:%M"),
                        "finished": finished.strftime("%Y-%m-%d %H:%M"), "steps": results})
    print("\n[브리핑] 단계별 결과")
    for k, v in results.items():
        print(f"  - {k}: {v}")
    print(f"[브리핑] 완료 ({(finished - started).seconds}초)")
    return results


if __name__ == "__main__":
    res = run()
    # 저장소 연결 자체가 안 되는 등 치명적 문제일 때만 실패로 표시
    sys.exit(0)
