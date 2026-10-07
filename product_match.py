# product_match.py
# 제품별 대응 가이드 — 제품마다 '관련 사업·과제·뉴스'를 고르는 공통 규칙
#   대시보드(app.py)와 아침 배치(briefing_batch.py)가 같은 규칙을 써야 화면 목록과 AI 분석이 일치한다.
import re

from common import PRODUCT_KEYWORDS, INTEGRATED_RND_DOMAINS, PRODUCT_TO_DOMAIN, owner_org

PRODUCT_CODES = {"넷퍼넬 (NF)": "NF", "넷퍼넬API (NFA)": "NFA", "봇매니저 (BM)": "BM", "로드테스터 (LT)": "LT"}
PRODUCT_TITLES = {   # 화면 제목 (제품 약어 · 이름 · 한 줄 설명)
    "NF": ("NetFUNNEL", "웹 접속 대기열 · 트래픽 유량제어"),
    "NFA": ("NetFUNNEL API", "API 트래픽 제어 · AI 에이전트 트래픽 대응"),
    "BM": ("BotManager / MBUSTER", "매크로·봇 탐지 차단 · 부정접속 방어"),
    "LT": ("LoadTester", "웹·앱 부하테스트(성능 검증)"),
}
RND_DOMAIN_MIN_SCORE = 60     # R&D는 도메인(AI·데이터·보안 등) 단어만 걸린 경우 연관도 60점 이상만 (단어만 같은 과제 제외)


def _mask(df, keywords, cols=("title", "matched_keywords")):
    if df is None or df.empty or not keywords:
        return None
    pat = "|".join(re.escape(k) for k in keywords)
    m = None
    for c in cols:
        if c in df.columns:
            cm = df[c].astype(str).str.contains(pat, case=False, na=False)
            m = cm if m is None else (m | cm)
    return m


def match_product(pname, biz_df, rnd_df, news_items, score_col="_score", ai_titles=None):
    """반환: {"biz": DataFrame, "rnd": DataFrame, "news": [dict]} — 연관도 높은 순"""
    info = PRODUCT_KEYWORDS[pname]
    kws = info["keywords"]
    ai_titles = ai_titles or {}

    def _pick(df, label):
        if df is None or df.empty:
            return df.iloc[0:0] if df is not None else None
        m = _mask(df, kws)
        hits = df[m] if m is not None else df.iloc[0:0]
        if label == "rnd":
            dom = []
            for d in PRODUCT_TO_DOMAIN.get(pname, []):
                dom.extend(INTEGRATED_RND_DOMAINS.get(d, {}).get("keywords", []))
            dm = _mask(df, dom)
            if dm is not None and score_col in df.columns:
                extra = df[dm & (df[score_col] >= RND_DOMAIN_MIN_SCORE)]
                hits = df[df.index.isin(hits.index) | df.index.isin(extra.index)]
        if hits.empty and ai_titles.get(f"{label}_{pname}"):     # 키워드 0건이면 아침 배치 AI가 문맥으로 고른 결과
            hits = df[df["title"].astype(str).isin([str(t) for t in ai_titles[f"{label}_{pname}"]])]
        if score_col in hits.columns:
            hits = hits.sort_values(score_col, ascending=False)
        return hits

    news = []
    seen = set()
    for it in news_items or []:
        t = str(it.get("title", ""))
        k = re.sub(r"\s+", "", t)[:40]
        if k in seen:
            continue
        if any(kw.lower() in t.lower() for kw in kws):
            seen.add(k)
            news.append(it)
    news.sort(key=lambda x: -(x.get("_score") or -1))
    return {"biz": _pick(biz_df, "biz"), "rnd": _pick(rnd_df, "rnd"), "news": news}


def _eok(v):
    try:
        n = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return f"{n / 1e8:.1f}억".replace(".0억", "억") if n >= 1e7 else f"{n / 1e4:,.0f}만원"


def item_line(row, kind, score_col="_score"):
    """AI에 넘길 한 줄 요약: [사업] 제목 · 기관 · 예산 · 마감 · 연관도"""
    parts = [f"[{kind}] {row.get('title', '')}", owner_org(row.get("agency"), row.get("dept"))]
    b = _eok(row.get("budget"))
    if b:
        parts.append(f"예산 {b}")
    if row.get("due_date"):
        parts.append(f"마감 {str(row.get('due_date'))[:10]}")
    sc = row.get(score_col)
    try:
        if sc is not None and int(sc) >= 0:
            parts.append(f"연관도 {int(sc)}")
    except (TypeError, ValueError):
        pass
    return " · ".join(p for p in parts if p)
