# briefing_pdf.py
# 사업부 / R&D 데일리 브리핑 PDF — '공공 IT 데일리 브리핑' 양식(머리말 · 헤드라인 카드 · 동향 카드 · 표 · 제품 카드 · 워치리스트)
#   아침 배치(briefing_batch.py)가 HTML을 만들고 크롬(Playwright)으로 PDF로 바꿔 app_cache에 저장 → 대시보드는 내려받기만 함
#   글꼴: 대시보드와 같은 Pretendard (assets/fonts)
import base64
import os
import re
import tempfile
from datetime import datetime
from html import escape

import pandas as pd

from common import PRODUCT_KEYWORDS, owner_org
from product_match import PRODUCT_CODES, PRODUCT_TITLES

K_PDF = {"BIZ": "briefing_pdf_biz", "RND": "briefing_pdf_rnd"}      # app_cache 키
TRACK_LABEL = {"BIZ": "사업부", "RND": "R&D"}
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
NF_HIGH_SCORE = 60


# ------------------------------------------------------------
# 표기 도우미
# ------------------------------------------------------------
def _eok(v):
    try:
        n = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n >= 1e8:
        return f"{n / 1e8:,.1f}".rstrip("0").rstrip(".") + "억원"
    return f"{n / 1e4:,.0f}만원"


def _md_bold(txt):
    """AI 문장의 **굵게** → <b>, 나머지는 이스케이프"""
    h = escape(str(txt or ""))
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", h)


def _est(txt):
    """문장 끝 '(추정)' → 작은 '추정' 표시"""
    h = _md_bold(txt)
    return re.sub(r"\s*\(추정\)\s*\.?$", '<span class="est">추정</span>', h)


def _due_info(due_raw, today):
    d = pd.to_datetime(str(due_raw or "")[:10], errors="coerce")
    if pd.isna(d):
        return None, None
    return d, (d.normalize() - pd.Timestamp(today)).days


def _dday_txt(left):
    if left is None:
        return "마감 미정"
    return "D-DAY" if left == 0 else (f"D-{left}" if left > 0 else "마감")


def _md(d):
    return f"{d.month}/{d.day}({WEEKDAY_KO[d.weekday()]})"


def products_for(row):
    """공고 1건과 연관된 제품 약어 — 제목·매칭 키워드에 제품 단어가 있으면, NF는 연관도 60점 이상도 포함"""
    text = f"{row.get('title', '')} {row.get('matched_keywords', '')}".lower()
    codes = [PRODUCT_CODES[p] for p, info in PRODUCT_KEYWORDS.items()
             if any(k.lower() in text for k in info["keywords"])]
    try:
        if int(row.get("_score", -1)) >= NF_HIGH_SCORE and "NF" not in codes:
            codes.insert(0, "NF")
    except (TypeError, ValueError):
        pass
    return codes


# ------------------------------------------------------------
# 1) 내용 고르기 — 사업부 / R&D
# ------------------------------------------------------------
def build_context(df, track, track_brief=None, product_guide=None, issues_global=None,
                  headline_global=None, reorder=None, now=None):
    now = now or datetime.now()
    today = now.date()
    label = TRACK_LABEL[track]
    sub = df[df["_track"] == track].copy() if df is not None and not df.empty else pd.DataFrame()
    tb = track_brief if isinstance(track_brief, dict) else {}

    # 헤드라인
    points = tb.get("points") or (headline_global or {}).get("points") or []
    headline = tb.get("headline") or (headline_global or {}).get("headline") or ""

    # 동향 카드
    issues = tb.get("issues") or []
    if not issues and issues_global:
        pri_map = {"매우높음": "red", "높음": "orange", "보통": "amber", "낮음": "green"}
        issues = [{"tag": f"동향 — 관련 {i.get('count', '')}건", "title": i.get("theme", ""),
                   "summary": i.get("summary", ""), "impact": i.get("biz_impact", ""),
                   "products": i.get("products") or [], "pri": pri_map.get(i.get("impact"), "amber")}
                  for i in issues_global if isinstance(i, dict)][:4]

    # III. 주요사업·예정사업 / 주요과제
    pipeline = []
    if not sub.empty:
        sub = sub.sort_values("_score", ascending=False)
        if track == "BIZ":
            ongoing = sub[~sub["agency"].astype(str).str.contains("사전규격")].head(6)
            prespec = sub[sub["agency"].astype(str).str.contains("사전규격")].head(3)
            for r in ongoing.to_dict("records"):
                d, left = _due_info(r.get("due_date"), today)
                stage = f"입찰 진행 · 마감 {_md(d)} {_dday_txt(left)}" if d is not None else "공고 중 · 마감 미정"
                pipeline.append(dict(title=r["title"], org=owner_org(r.get("agency"), r.get("dept")),
                                     size=_eok(r.get("budget")) or "미정", stage=stage, products=products_for(r),
                                     note=r.get("ai_oneline") or "", url=r.get("url") or "", kind="진행"))
            for r in prespec.to_dict("records"):
                d, left = _due_info(r.get("due_date"), today)
                stage = (f"예정 · 사전규격 공개, 의견 마감 {_md(d)} {_dday_txt(left)}" if d is not None
                         else "예정 · 사전규격 공개")
                pipeline.append(dict(title=r["title"], org=owner_org(r.get("agency"), r.get("dept")),
                                     size=_eok(r.get("budget")) or "미정", stage=stage, products=products_for(r),
                                     note=r.get("ai_oneline") or "", url=r.get("url") or "", kind="예정"))
        else:
            for r in sub.head(8).to_dict("records"):
                d, left = _due_info(r.get("due_date"), today)
                size = _eok(r.get("budget"))
                lbl = str(r.get("budget_label") or "")
                pipeline.append(dict(title=r["title"], org=owner_org(r.get("agency"), r.get("dept")),
                                     size=(f"{size} ({lbl})" if size and lbl and lbl not in ("사업예산", "사업금액") else size) or "미정",
                                     stage=(f"접수 마감 {_md(d)} {_dday_txt(left)}" if d is not None else "마감 미정"),
                                     products=products_for(r), note=r.get("ai_oneline") or "",
                                     url=r.get("url") or "", kind="과제"))
    if track == "BIZ" and reorder is not None and not reorder.empty:
        for r in reorder[reorder["d_day"] >= 0].head(3).to_dict("records"):
            pipeline.append(dict(title=r.get("title", ""), org=r.get("agency", ""),
                                 size=_eok(r.get("amount")) or "미정",
                                 stage=f"예정 · 재발주 예상 {r.get('expected', '')} (계약 종료 {r.get('end_date', '')}"
                                       f"{' 추정' if r.get('end_est') == 'Y' else ''}, 현 수주 {r.get('company', '')})",
                                 products=products_for({"title": r.get("title", ""), "_score": -1}),
                                 note="", url=r.get("url") or "", kind="예정"))

    # IV. 제품별 대응 가이드
    products = []
    for pname in PRODUCT_KEYWORDS:
        code = PRODUCT_CODES[pname]
        g = (product_guide or {}).get(code) or {}
        quiet = bool(g.get("none")) or not g.get("issue")
        chip = "green" if quiet else ("red" if code in ("NF", "BM") and len(g.get("actions") or []) >= 2 else "orange")
        products.append(dict(code=code, name=" · ".join(PRODUCT_TITLES[code]), quiet=quiet, chip=chip,
                             issue=g.get("issue", ""), impact=g.get("impact", ""),
                             actions=[a for a in (g.get("actions") or []) if a]))

    # V. Action Item
    actions = tb.get("actions") or []
    if not actions:
        for p in products:
            for a in p["actions"][:1]:
                actions.append({"action": f"[{p['code']}] {a}", "owner": label, "due": "상시"})

    # VI. 마감 임박 워치리스트 (14일 이내)
    watch = []
    if not sub.empty:
        w = sub[sub["_due"].notna()].copy()
        w["_left"] = (w["_due"].dt.normalize() - pd.Timestamp(today)).dt.days
        w = w[(w["_left"] >= 0) & (w["_left"] <= 14)].sort_values(["_left", "_score"], ascending=[True, False]).head(8)
        for r in w.to_dict("records"):
            parts = [owner_org(r.get("agency"), r.get("dept"))]
            if _eok(r.get("budget")):
                parts.append(_eok(r.get("budget")))
            parts.append(f"마감 {_md(r['_due'])}")
            watch.append(dict(dday=_dday_txt(int(r["_left"])), title=r["title"], desc=" · ".join(parts),
                              url=r.get("url") or ""))

    new_n = int((sub["_created"].dt.date == today).sum()) if not sub.empty and "_created" in sub.columns else 0
    return dict(track=track, label=label, now=now, headline=headline, points=points[:3], issues=issues[:4],
                pipeline=pipeline, products=products, actions=actions[:5], watch=watch,
                total=len(sub), new_n=new_n)


# ------------------------------------------------------------
# 2) HTML — 참고 양식과 같은 배치·카드 (색상 토큰 · 간격 · 표 구조 동일), 글꼴만 Pretendard
# ------------------------------------------------------------
_CSS = """
@font-face { font-family: 'Pretendard'; font-weight: 400; src: url('{font_dir}/Pretendard-Regular.ttf'); }
@font-face { font-family: 'Pretendard'; font-weight: 600 900; src: url('{font_dir}/Pretendard-Bold.ttf'); }
@page { size: A4; margin: 14mm 13mm 16mm; }
:root{
  --paper:#F3F5F7; --paper-raised:#FFFFFF; --ink:#1A2233; --ink-soft:#5B6472; --ink-faint:#8B93A1;
  --line:#DBE0E6; --line-strong:#C3CAD3; --accent:#1B4B6B; --accent-soft:#DCE7EE; --accent-ink:#0E3350;
  --red:#B8402C; --red-soft:#F6E4E0; --orange:#B06A1E; --orange-soft:#F5EADA; --amber:#96791E; --amber-soft:#F2EDD9;
  --green:#2F6B4F; --green-soft:#DEEBE3;
  --shadow: 0 1px 2px rgba(20,30,45,.06), 0 6px 16px rgba(20,30,45,.05);
}
*{box-sizing:border-box;}
html{-webkit-print-color-adjust:exact; print-color-adjust:exact;}
body{background:var(--paper); color:var(--ink); margin:0;
  font-family:'Pretendard','Pretendard Variable',-apple-system,'Malgun Gothic','Noto Sans CJK KR',sans-serif;
  font-variant-numeric:tabular-nums; padding:22px 20px 28px;}
.sheet{max-width:760px; margin:0 auto; display:flex; flex-direction:column; gap:26px;}
.masthead{display:flex; flex-direction:column; gap:14px; padding-bottom:18px; border-bottom:2px solid var(--ink);}
.masthead-top{display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap;}
.doc-no{font-size:12.5px; letter-spacing:.03em; color:var(--ink-faint);}
.doc-status{font-size:12px; letter-spacing:.04em; color:var(--accent-ink); background:var(--accent-soft); padding:3px 9px; border-radius:3px;}
h1.title{font-weight:800; font-size:34px; line-height:1.15; letter-spacing:-.01em; margin:0;}
h1.title .trk{color:var(--accent);}
.masthead-meta{display:flex; flex-wrap:wrap; gap:6px 24px; font-size:13px; color:var(--ink-soft); margin:0;}
.masthead-meta dt, .masthead-meta dd{white-space:nowrap;}
.masthead-meta dt{color:var(--ink-faint); display:inline;}
.masthead-meta dd{display:inline; margin:0; color:var(--ink); font-weight:600;}
.masthead-meta .row{display:flex; gap:6px;}
.masthead-note{font-size:12px; color:var(--ink-faint); line-height:1.6;}
.headline{background:var(--paper-raised); border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); padding:20px 22px; break-inside:avoid;}
.headline h2{font-size:14px; font-weight:800; letter-spacing:.08em; margin:0 0 6px; color:var(--accent-ink);}
.headline .lead{font-size:18px; font-weight:800; margin:0 0 12px; line-height:1.4;}
.headline ol{margin:0; padding-left:0; list-style:none; display:flex; flex-direction:column; gap:10px;}
.headline li{display:flex; gap:12px; font-size:15.5px; line-height:1.55;}
.headline li .n{color:var(--ink-faint); font-size:13px; padding-top:3px; flex:0 0 auto;}
section.block{display:flex; flex-direction:column; gap:12px;}
.block-head{display:flex; align-items:baseline; gap:10px; break-after:avoid;}
.block-head .num{font-weight:800; color:var(--accent); font-size:15px;}
.block-head h2{font-size:15px; font-weight:700; margin:0;}
.block-head .sub{font-size:12.5px; color:var(--ink-faint);}
.issue{background:var(--paper-raised); border:1px solid var(--line); border-left:4px solid var(--pri, var(--line-strong));
  border-radius:6px; padding:14px 16px; display:flex; flex-direction:column; gap:8px; break-inside:avoid;}
.issue[data-pri="orange"]{--pri:var(--orange);} .issue[data-pri="amber"]{--pri:var(--amber);}
.issue[data-pri="red"]{--pri:var(--red);} .issue[data-pri="green"]{--pri:var(--green);}
.issue-tag{font-size:12px; color:var(--ink-faint);}
.issue h3{font-size:15.5px; font-weight:700; margin:0; line-height:1.4;}
.issue p{margin:0; font-size:14px; line-height:1.6; color:var(--ink-soft);}
.issue p b{color:var(--ink); font-weight:600;}
.prod-tags{display:flex; gap:6px; flex-wrap:wrap;}
.prod-tag{font-size:11.5px; font-weight:600; padding:2px 7px; border-radius:3px; background:var(--accent-soft); color:var(--accent-ink);}
.est{font-size:11px; color:var(--ink-faint); border:1px solid var(--line-strong); padding:1px 6px; border-radius:3px; margin-left:4px; white-space:nowrap;}
.product-grid{display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px;}
.product{background:var(--paper-raised); border:1px solid var(--line); border-radius:8px; padding:14px 15px;
  display:flex; flex-direction:column; gap:9px; break-inside:avoid;}
.product.quiet{opacity:.6;}
.product-head{display:flex; justify-content:space-between; align-items:center;}
.product-code{font-weight:700; font-size:14px; letter-spacing:.02em;}
.product-name{font-size:11px; color:var(--ink-faint);}
.pri-chip{width:10px; height:10px; border-radius:50%; flex:0 0 auto;}
.pri-chip.orange{background:var(--orange);} .pri-chip.amber{background:var(--amber);}
.pri-chip.red{background:var(--red);} .pri-chip.green{background:var(--green);}
.product-body{font-size:13px; line-height:1.6; color:var(--ink-soft);}
.product-body .lbl{display:block; font-size:10.5px; letter-spacing:.06em; color:var(--ink-faint); margin-top:8px;}
.product-body .lbl:first-child{margin-top:0;}
.product-body p{margin:2px 0 0; color:var(--ink);}
.product.quiet .product-body{color:var(--ink-faint);}
.action-table{width:100%; border-collapse:separate; border-spacing:0; background:var(--paper-raised); border:1px solid var(--line);
  border-radius:8px; overflow:hidden; font-size:13.5px; table-layout:fixed;}
.action-table thead th{white-space:nowrap;}
.action-table td.org{color:var(--ink-soft); font-size:12.5px; word-break:keep-all;}
.action-table thead th{text-align:left; font-size:11px; letter-spacing:.06em; color:var(--ink-faint); background:var(--accent-soft); padding:9px 14px; font-weight:600;}
.action-table td{padding:12px 14px; border-top:1px solid var(--line); vertical-align:top; line-height:1.55;}
.action-table tr{break-inside:avoid;}
.action-table td.owner,.action-table td.due{color:var(--ink-soft); font-size:12.5px; word-break:keep-all;}
.action-table td .note{font-size:12px; color:var(--ink-faint); margin-top:2px;}
.action-table a{color:var(--ink); text-decoration:none;}
.kind{display:inline-block; font-size:10.5px; font-weight:700; padding:1px 6px; border-radius:3px; margin-right:6px; vertical-align:1px;}
.kind.진행{background:var(--accent-soft); color:var(--accent-ink);} .kind.예정{background:var(--amber-soft); color:var(--amber);}
.kind.과제{background:var(--green-soft); color:var(--green);}
.watch-list{display:flex; flex-direction:column; gap:8px;}
.watch-row{display:flex; align-items:center; gap:14px; background:var(--paper-raised); border:1px solid var(--line); border-radius:6px; padding:11px 15px; break-inside:avoid;}
.watch-dday{font-weight:700; font-size:12.5px; color:var(--accent-ink); background:var(--accent-soft); padding:4px 9px; border-radius:4px; flex:0 0 auto; white-space:nowrap; min-width:58px; text-align:center;}
.watch-dday.hot{color:var(--red); background:var(--red-soft);}
.watch-body{flex:1; min-width:0;}
.watch-body .t{font-size:14px; font-weight:600;}
.watch-body .d{font-size:12.5px; color:var(--ink-faint); margin-top:2px;}
.empty{font-size:13px; color:var(--ink-faint); background:var(--paper-raised); border:1px dashed var(--line-strong); border-radius:6px; padding:12px 15px;}
footer{border-top:1px solid var(--line); padding-top:16px; display:flex; flex-direction:column; gap:6px; font-size:12px; color:var(--ink-faint); line-height:1.6;}
"""


def render_html(ctx, font_dir=FONT_DIR):
    now = ctx["now"]
    label = ctx["label"]
    track = ctx["track"]
    font_url = "file://" + font_dir.replace("\\", "/")
    title_txt = "공공 IT 데일리 브리핑" if track == "BIZ" else "R&D 과제 데일리 브리핑"
    doc_no = f"Gov-Tracker 브리핑 제{now:%Y}-{now:%m%d}호 · {label}"
    status = f"{now.year}년 {now.month}월 {now.day}일({WEEKDAY_KO[now.weekday()]}) · {now:%H:%M} 수집 기준"

    pts = "".join(f'<li><span class="n">{i:02d}</span><span>{_est(p)}</span></li>'
                  for i, p in enumerate(ctx["points"], 1)) or '<li><span class="n">01</span><span>오늘 요약할 신규 항목이 없습니다.</span></li>'
    lead = f'<p class="lead">{_md_bold(ctx["headline"])}</p>' if ctx["headline"] else ""

    issues_html = ""
    for it in ctx["issues"]:
        tags = "".join(f'<span class="prod-tag">{escape(str(p))}</span>' for p in (it.get("products") or []))
        issues_html += (
            f'<article class="issue" data-pri="{escape(str(it.get("pri") or "amber"))}">'
            f'<span class="issue-tag">{escape(str(it.get("tag") or ""))}</span>'
            f'<h3>{_md_bold(it.get("title"))}</h3>'
            f'<p>{_md_bold(it.get("summary"))}</p>'
            + (f'<p><b>우리 사업 영향</b> — {_est(it.get("impact"))}</p>' if it.get("impact") else "")
            + (f'<div class="prod-tags">{tags}</div>' if tags else "")
            + '</article>')
    if not issues_html:
        issues_html = '<div class="empty">오늘 새로 묶을 동향이 없습니다.</div>'

    if track == "BIZ":
        p_title, p_sub = "주요사업·예정사업 파이프라인", "— 연관도 높은 진행 사업 · 사전규격(예정) · 재발주 예상"
        p_cols = ("사업명", "발주기관", "규모", "현재 단계", "제품연관")
    else:
        p_title, p_sub = "주요과제", "— 연관도 높은 순 · 지원규모는 공고 원문 기준"
        p_cols = ("과제명", "주관기관", "지원규모", "현재 단계", "제품연관")
    rows = ""
    for r in ctx["pipeline"]:
        name = escape(str(r["title"]))
        if r.get("url"):
            name = f'<a href="{escape(str(r["url"]))}">{name}</a>'
        note = f'<div class="note">{escape(str(r["note"]))}</div>' if r.get("note") else ""
        rows += (f'<tr><td><span class="kind {r["kind"]}">{r["kind"]}</span>{name}{note}</td>'
                 f'<td class="org">{escape(str(r["org"]))}</td><td class="owner">{escape(str(r["size"]))}</td>'
                 f'<td>{escape(str(r["stage"]))}</td><td class="owner">{escape("·".join(r["products"]) or "-")}</td></tr>')
    head = "".join(f"<th>{h}</th>" for h in p_cols)
    pcols = "".join(f'<col style="width:{w}%">' for w in (37, 17, 11, 25, 10))
    pipe_html = (f'<table class="action-table"><colgroup>{pcols}</colgroup><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>' if rows
                 else '<div class="empty">진행 중인 항목이 없습니다.</div>')

    prod_html = ""
    for p in ctx["products"]:
        if p["quiet"]:
            body = '<div class="product-body"><p>오늘 신규 이슈 없음 — 지속 모니터링</p></div>'
        else:
            acts = " · ".join(escape(str(a)) for a in p["actions"][:3])
            body = ('<div class="product-body">'
                    f'<span class="lbl">관련 이슈</span><p>{_est(p["issue"])}</p>'
                    f'<span class="lbl">우리 사업 영향</span><p>{_est(p["impact"])}</p>'
                    + (f'<span class="lbl">지금 할 일</span><p>{acts}</p>' if acts else "")
                    + '</div>')
        code_name = p["name"].split(" · ", 1)
        prod_html += (f'<div class="product{" quiet" if p["quiet"] else ""}"><div class="product-head">'
                      f'<div><span class="product-code">{escape(p["code"])}</span>'
                      f'<div class="product-name">{escape(" · ".join(code_name))}</div></div>'
                      f'<span class="pri-chip {p["chip"]}"></span></div>{body}</div>')

    act_rows = "".join(
        f'<tr><td>{_md_bold(a.get("action"))}</td><td class="owner">{escape(str(a.get("owner") or label))}</td>'
        f'<td class="due">{escape(str(a.get("due") or "상시"))}</td></tr>' for a in ctx["actions"])
    act_html = (f'<table class="action-table"><colgroup><col style="width:66%"><col style="width:17%"><col style="width:17%"></colgroup>'
                f'<thead><tr><th>액션</th><th>담당</th><th>기한</th></tr></thead>'
                f'<tbody>{act_rows}</tbody></table>' if act_rows else '<div class="empty">오늘 새 Action Item이 없습니다.</div>')

    watch_html = "".join(
        f'<div class="watch-row"><span class="watch-dday{" hot" if w["dday"] in ("D-DAY", "D-1", "D-2", "D-3") else ""}">'
        f'{escape(w["dday"])}</span><div class="watch-body"><div class="t">{escape(str(w["title"]))}</div>'
        f'<div class="d">{escape(str(w["desc"]))}</div></div></div>' for w in ctx["watch"]
    ) or '<div class="empty">14일 이내 마감 항목이 없습니다.</div>'

    css = _CSS.replace("{font_dir}", font_url)
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{title_txt} · {label}</title>
<style>{css}</style></head><body><div class="sheet">
<header class="masthead">
  <div class="masthead-top"><span class="doc-no">{escape(doc_no)}</span><span class="doc-status">{escape(status)}</span></div>
  <h1 class="title">{title_txt} <span class="trk">· {escape(label)}</span></h1>
  <dl class="masthead-meta">
    <div class="row"><dt>수신&nbsp;</dt><dd>{escape(label)} {'(수주·용역)' if track == 'BIZ' else '(연구개발 과제)'}</dd></div>
    <div class="row"><dt>기안&nbsp;</dt><dd>Gov-Tracker AI 자동 분석</dd></div>
    <div class="row"><dt>조회범위&nbsp;</dt><dd>진행 중 {ctx['total']}건 · 오늘 신규 {ctx['new_n']}건</dd></div>
    <div class="row"><dt>제품범례&nbsp;</dt><dd>NF·NFA·BM·LT</dd></div>
  </dl>
  <div class="masthead-note">※ 매일 아침 8시 자동수집 결과를 AI가 분류·점수화·요약했습니다. 예산·마감은 공고 원문 기준이며, 해석 문장에는 '추정'을 표시했습니다.
  제품별 대응 가이드는 사업부·R&D 공통입니다.</div>
</header>
<div class="headline"><h2>I. 핵심 요약 — 오늘의 헤드라인</h2>{lead}<ol>{pts}</ol></div>
<section class="block"><div class="block-head"><span class="num">II</span><h2>동향</h2><span class="sub">— 오늘 {escape(label)} 공고·뉴스를 테마로 묶음</span></div>{issues_html}</section>
<section class="block"><div class="block-head"><span class="num">III</span><h2>{p_title}</h2><span class="sub">{p_sub}</span></div>{pipe_html}</section>
<section class="block"><div class="block-head"><span class="num">IV</span><h2>제품별 대응 가이드</h2></div><div class="product-grid">{prod_html}</div></section>
<section class="block"><div class="block-head"><span class="num">V</span><h2>오늘의 Action Item</h2></div>{act_html}</section>
<section class="block"><div class="block-head"><span class="num">VI</span><h2>마감 임박 워치리스트</h2><span class="sub">— 14일 이내 마감</span></div><div class="watch-list">{watch_html}</div></section>
<footer>
  <div>공고·과제는 조달청·IRIS·NTIS·기관 게시판 원문에서 수집했으며, 금액·날짜는 원문 표기를 그대로 옮겼습니다. 법·규정 판단은 확인이 필요합니다.</div>
  <div>Gov-Tracker 자동 생성 · 최종 갱신 {now:%Y-%m-%d %H:%M}</div>
</footer>
</div></body></html>"""


# ------------------------------------------------------------
# 3) PDF 변환 — 크롬(Playwright). 아침 배치(GitHub Actions)에서만 실행
# ------------------------------------------------------------
def html_to_pdf(html):
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "brief.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto("file://" + path, wait_until="load")
                page.evaluate("document.fonts.ready.then(() => true)")
                pdf = page.pdf(
                    prefer_css_page_size=True, print_background=True, display_header_footer=True,
                    header_template="<span></span>",
                    footer_template=('<div style="width:100%;font-size:8px;color:#8B93A1;text-align:center;'
                                     'font-family:sans-serif;"><span class="pageNumber"></span> / <span class="totalPages"></span></div>'),
                )
            finally:
                browser.close()
    return pdf


def build_pdfs(df, track_briefs, product_guide, issues_global, headline_global, reorder, now=None):
    """반환: {"BIZ": {"b64": ..., "size": ..., "generated": ...}, "RND": {...}}"""
    now = now or datetime.now()
    out = {}
    for track in ("BIZ", "RND"):
        ctx = build_context(df, track, (track_briefs or {}).get(track), product_guide, issues_global,
                            headline_global, reorder, now=now)
        pdf = html_to_pdf(render_html(ctx))
        out[track] = {"b64": base64.b64encode(pdf).decode("ascii"), "size": len(pdf),
                      "generated": now.strftime("%Y-%m-%d %H:%M"),
                      "filename": f"gov_tracker_{'biz' if track == 'BIZ' else 'rnd'}_briefing_{now:%Y%m%d}.pdf"}
    return out
