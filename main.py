# main.py — 매일 아침 자동수집: 수집 → 중복 정리 → 예산·마감 보강 → AI 분석 → 저장 → 낙찰·계약 결과
import os
import sys
import traceback

import common  # noqa: F401  (한국시간 고정 — 다른 모듈보다 먼저)
import asyncio
import re
from datetime import datetime, date, timedelta

import pandas as pd
from sqlalchemy import text
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from collectors import run_all_collectors, fetch_g2b_results, refresh_records, refreshable_url
from common import is_mois_noise, posting_key, family_key, full_agency, is_closed, series_title, is_list_url
from store import load_cache, save_cache
from procurement_store import save_results, count_results
from biz_classifier import classify_and_score as biz_classify_and_score
from ai_utils import is_ai_ready, analyze_postings_batch, AI_MAX_CONCURRENCY, AI_RPM_LIMIT, MODEL_NAME
from db2 import get_engine, is_postgres

EXCEL_OUTPUT = "Gov-Tracker_결과.xlsx"
COLLECT_LIMIT = int(os.getenv("COLLECT_LIMIT", "20"))   # 수집처당 최대 건수
REFRESH_LIMIT = int(os.getenv("REFRESH_LIMIT", "60"))   # 예산·마감이 빈 기존 공고를 하루에 다시 읽는 최대 건수
KEEP_DAYS = 400                                          # 이보다 오래된 공고는 DB에서 정리
MERGE_WINDOW_DAYS = 120                                  # 같은 사업명이라도 등록일이 이만큼 떨어지면 다른 회차로 봄

RND_TYPE_KEYWORDS = ["과제공고", "지원사업", "R&D", "연구개발", "공모전", "지원과제", "사업 공모", "공모", "수요조사"]
BID_TYPE_KEYWORDS = ["입찰", "전자입찰", "구매", "용역", "발주", "제안서", "제안", "적격심사", "사전규격"]
TRACK_LABELS = {"RND": "R&D", "BIZ": "사업부", "": "미분류"}

# AI 분석이 실패했을 때만 쓰는 최후의 안전장치 (평소엔 사용 안 함)
AGENCY_TRACK_FALLBACK = {
    "범부처통합연구지원시스템(IRIS)": "RND", "한국교육학술정보원": "BIZ", "국가과학기술지식정보서비스(NTIS)": "RND",
    "중소기업기술정보진흥원": "RND", "한국산업기술진흥원": "RND", "연구개발특구진흥재단": "RND",
    "국가AI전략위원회": "RND", "한국지능정보사회진흥원(AIHub)": "RND", "정보통신기획평가원(IITP)": "RND",
    "정보통신산업진흥원": "BIZ", "조달청": "BIZ", "조달청(사전규격)": "BIZ", "행정안전부": "BIZ", "한국인터넷진흥원": "BIZ",
}

# postings 테이블 컬럼 (순서 = 저장 순서)
COLUMNS = [
    "uniq_key", "source", "agency", "gubun", "post_type", "title", "dept", "manager",
    "reg_date", "due_date", "budget", "budget_label", "period_end", "ref_no",
    "attach", "views", "url", "grade", "category", "matched_keywords", "recommended_solution", "status",
    "track", "track_reason", "ai_priority_score", "ai_priority_reason", "dedup_hash",
    "ai_oneline", "ai_summary", "content", "created_at", "updated_at",
]
AI_FIELDS = ["track", "track_reason", "ai_priority_score", "ai_priority_reason", "ai_oneline", "ai_summary"]
SCRAPED_FIELDS = ["source", "gubun", "title", "dept", "manager", "reg_date", "due_date", "budget", "budget_label",
                  "period_end", "ref_no", "attach", "views", "url", "content"]


def classify_post_type(title: str) -> str:
    if not title:
        return "ETC"
    if any(kw in title for kw in BID_TYPE_KEYWORDS):
        return "BID"
    if any(kw in title for kw in RND_TYPE_KEYWORDS):
        return "RND"
    return "ETC"


AGENCY_SUFFIXES = ["센터", "재단", "진흥원", "위원회", "연구원", "정보원", "공사", "청", "부", "원", "팀", "실", "국", "협회", "본부"]


def clean_manager_name(raw) -> str:
    name = str(raw or "").strip()
    if not name or re.search(r"\d", name) or any(name.endswith(s) for s in AGENCY_SUFFIXES):
        return ""
    if re.fullmatch(r"[가-힣]{2,4}", name) or re.fullmatch(r"[A-Za-z]{2,20}(\s[A-Za-z]{2,20})?", name):
        return name
    return ""


# ------------------------------------------------------------
# DB 초기화 / 마이그레이션 (Supabase-Postgres, 로컬-SQLite 겸용)
# ------------------------------------------------------------
def init_db():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS postings (
                uniq_key TEXT PRIMARY KEY,
                source TEXT, agency TEXT, gubun TEXT, post_type TEXT, title TEXT,
                dept TEXT, manager TEXT, reg_date TEXT, due_date TEXT, budget TEXT,
                attach TEXT, views TEXT, url TEXT, grade TEXT, category TEXT,
                matched_keywords TEXT, recommended_solution TEXT, status TEXT,
                track TEXT, track_reason TEXT,
                ai_priority_score INTEGER, ai_priority_reason TEXT,
                dedup_hash TEXT, ai_oneline TEXT, ai_summary TEXT, content TEXT,
                created_at TEXT, updated_at TEXT
            )
        """))
    ensure_columns(engine)
    return engine


def ensure_columns(engine):
    """예전 버전 DB에 새 컬럼(budget_label, period_end, ref_no 등)이 없으면 추가"""
    needed = {c: "TEXT" for c in COLUMNS if c != "ai_priority_score"}
    needed["ai_priority_score"] = "INTEGER"
    with engine.begin() as conn:
        if is_postgres():
            existing = {row[0] for row in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name='postings'"))}
        else:
            existing = {row[1] for row in conn.execute(text("PRAGMA table_info(postings)"))}
        for col, coltype in needed.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE postings ADD COLUMN {col} {coltype}"))


def load_rows(engine):
    with engine.begin() as conn:
        res = conn.execute(text("SELECT * FROM postings"))
        cols = list(res.keys())
        rows = [dict(zip(cols, r)) for r in res.fetchall()]
    for r in rows:
        for c in COLUMNS:
            if c == "ai_priority_score":
                r[c] = None if r.get(c) in (None, "") else int(r[c])
            else:
                r[c] = "" if r.get(c) is None else str(r.get(c))
    return rows


def write_rows(engine, rows):
    """전체를 트랜잭션 1번으로 다시 저장 (중간에 실패하면 원래 데이터 그대로 유지됨)"""
    cols = ", ".join(COLUMNS)
    vals = ", ".join(f":{c}" for c in COLUMNS)
    payload = []
    for r in rows:
        p = {c: (r.get(c) if c == "ai_priority_score" else str(r.get(c) or "")) for c in COLUMNS}
        p["content"] = p["content"][:4000]
        payload.append(p)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM postings"))
        if payload:
            conn.execute(text(f"INSERT INTO postings ({cols}) VALUES ({vals})"), payload)


# ------------------------------------------------------------
# 중복 정리 — 같은 공고(고유번호) / 같은 입찰공고번호 / 같은 사업의 연장·재공고·정정을 1건으로
# ------------------------------------------------------------
def _d(v):
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _quality(r):
    """같은 공고 여러 행 중 남길 행: ① 공고 1건 원문 주소 ② 최근 등록(연장·재공고) ③ 최근 갱신(오늘 수집)
    ④ IRIS 대신 실제 주관기관 표기 ⑤ 마감·예산·AI분석·본문이 있는 행"""
    return (not is_list_url(r.get("url")), _d(r.get("reg_date")) or date.min, str(r.get("updated_at") or ""),
            "IRIS" not in str(r.get("agency") or ""), bool(r.get("due_date")), bool(r.get("budget")),
            r.get("ai_priority_score") is not None, len(r.get("content") or ""))


_JUNK_TITLES = {"전체", "공지", "검색", "목록"}
_NEW_BADGE_RE = re.compile(r"^\s*N\s*(?=[가-힣\[\(「『<〈'\"‘“])")


def clean_title(agency, title):
    """TIPA 목록의 새 글 배지 'N'이 제목 앞에 붙은 경우 제거 (같은 공고가 2건으로 보이던 원인)"""
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    if "중소기업기술정보진흥원" in str(agency or "") or str(agency or "") == "TIPA":
        t = _NEW_BADGE_RE.sub("", t)
    return t


def is_junk_row(r):
    """예전 수집기가 화면 메뉴·조회수 등을 공고로 잘못 저장한 행 / 목록 페이지 주소만 가진 행"""
    reg = str(r.get("reg_date") or "").strip()
    if reg and not _d(reg):
        return True                      # 등록일 칸에 '7,663'(조회수) · '접수중' · 부처명 등이 들어간 행
    if str(r.get("title") or "").strip() in _JUNK_TITLES:
        return True
    return is_list_url(r.get("url"))


def merge_duplicates(rows):
    """반환: (남길 행 목록, 합쳐서 없앤 건수). 남는 행 = 가장 최근 공고(연장·재공고), 빈 칸은 형제 행에서 채움"""
    n = len(rows)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    by_key, by_ref, by_url, by_fam = {}, {}, {}, {}
    for i, r in enumerate(rows):
        url_k = "" if is_list_url(r.get("url")) else str(r.get("url")).strip().rstrip("/")   # 같은 원문 주소 = 같은 공고
        for idx, k in ((by_key, r["uniq_key"]), (by_ref, r.get("ref_no") or ""), (by_url, url_k)):
            if k:
                if k in idx:
                    union(idx[k], i)
                else:
                    idx[k] = i
        fam = family_key(r.get("agency"), r.get("dept"), r.get("title"))
        if len(fam.split("|", 1)[-1]) < 6:
            continue
        by_fam.setdefault(fam, []).append(i)
    for idxs in by_fam.values():
        for a_pos, a in enumerate(idxs):
            for b in idxs[a_pos + 1:]:
                da, db = _d(rows[a].get("reg_date")), _d(rows[b].get("reg_date"))
                if not da or not db or abs((da - db).days) <= MERGE_WINDOW_DAYS:
                    union(a, b)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(rows[i])
    kept, removed = [], 0
    for members in groups.values():
        if len(members) == 1:
            kept.append(members[0])
            continue
        members.sort(key=_quality, reverse=True)
        keep = dict(members[0])
        same_series = [m for m in members[1:] if series_title(m.get("title")) == series_title(keep.get("title"))]
        for m in members[1:]:
            for f in ("budget", "budget_label", "period_end", "ref_no", "dept", "manager"):
                if not keep.get(f) and m.get(f):
                    keep[f] = m[f]
            if len(m.get("content") or "") > len(keep.get("content") or ""):
                keep["content"] = m["content"]
            if m.get("created_at") and (not keep.get("created_at") or m["created_at"] < keep["created_at"]):
                keep["created_at"] = m["created_at"]
        if keep.get("ai_priority_score") is None:          # 같은 사업의 이전 분석 결과 재사용 (AI 비용 절감)
            for m in same_series:
                if m.get("ai_priority_score") is not None:
                    for f in AI_FIELDS:
                        keep[f] = m.get(f)
                    break
        kept.append(keep)
        removed += len(members) - 1
    return kept, removed


# ------------------------------------------------------------
def mark_status(rows):
    today = date.today()
    for r in rows:
        r["status"] = "마감" if is_closed(r.get("due_date"), r.get("reg_date"), r.get("period_end"), today) else "진행중"


def _apply_grade_style(path):
    wb = load_workbook(path)
    ws = wb["전체"]
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    orange_fill = PatternFill(start_color="FFE699", end_color="FFE699", fill_type="solid")
    headers = [cell.value for cell in ws[1]]
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    if "등급" in headers:
        grade_col = headers.index("등급") + 1
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            v = row[grade_col - 1].value
            if v in ("상", "중"):
                for c in row:
                    c.fill = red_fill if v == "상" else orange_fill
    if "제목" in headers and "원문링크" in headers:
        title_col = headers.index("제목") + 1
        url_col = headers.index("원문링크") + 1
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            if row[url_col - 1].value:
                row[title_col - 1].hyperlink = row[url_col - 1].value
                row[title_col - 1].font = Font(color="0563C1", underline="single")
        ws.delete_cols(url_col)
    for col_cells in ws.columns:
        max_len = max((len(str(c.value)) if c.value else 0) for c in col_cells)
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = min(max(max_len + 2, 10), 60)
    ws.freeze_panes = "A2"
    wb.save(path)


def export_to_excel(engine):
    df = pd.read_sql_query("""
        SELECT agency AS 기관, track AS 분류, track_reason AS AI구분근거,
               post_type AS 공고유형, gubun AS 세부구분, title AS 제목,
               dept AS 담당부서, manager AS 담당자, reg_date AS 등록일, due_date AS 마감일,
               budget AS 예산, budget_label AS 예산구분, period_end AS 사업종료일, ai_oneline AS 한줄요약,
               grade AS 등급, category AS 카테고리, recommended_solution AS 추천솔루션,
               matched_keywords AS 매칭키워드, ai_priority_score AS AI연관도점수,
               ai_priority_reason AS AI연관도근거, status AS 상태, url AS 원문링크
        FROM postings ORDER BY reg_date DESC
    """, engine).fillna("")
    df["분류"] = df["분류"].map(lambda v: TRACK_LABELS.get(v, v) if v else "미분류")
    with pd.ExcelWriter(EXCEL_OUTPUT, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="전체", index=False)
    _apply_grade_style(EXCEL_OUTPUT)
    print(f"[완료] 결과 파일 저장: {EXCEL_OUTPUT} ({len(df)}건)")


def _build_ai_info_block(rec):
    return (
        f"제목: {rec.get('title', '')}\n"
        f"기관: {rec.get('agency', '')} / 수요·주관기관: {rec.get('dept', '')}\n"
        f"공고유형(원본 구분값): {rec.get('gubun', '')}\n"
        f"접수 마감일: {rec.get('due_date', '') or '미표기'}\n"
        f"예산(원): {rec.get('budget', '') or '미표기'} ({rec.get('budget_label', '')})\n"
        f"사업 종료일: {rec.get('period_end', '') or '미표기'}\n"
        f"본문 일부: {str(rec.get('content', ''))[:2500]}"
    )


def _prepare_new(rec):
    rec["agency"] = full_agency(rec.get("agency"))
    rec["title"] = clean_title(rec["agency"], rec.get("title"))
    rec["manager"] = clean_manager_name(rec.get("manager", ""))
    rec["post_type"] = classify_post_type(rec["title"])
    info = biz_classify_and_score(rec["title"], rec.get("content", ""))
    rec["grade"], rec["category"] = info["등급"], info["카테고리"]
    rec["recommended_solution"], rec["matched_keywords"] = info["추천솔루션"], info["매칭키워드"]
    rec["uniq_key"] = posting_key(rec["agency"], rec["title"], rec.get("reg_date"))
    rec["dedup_hash"] = family_key(rec["agency"], rec.get("dept"), rec["title"])
    return rec


def collect_and_process():
    engine = init_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = date.today()

    # [0단계] 기존 데이터 정리 — 기관명 통일, 고유번호 재계산, 연장·재공고·중복 합치기, 오래된 공고 정리
    rows = load_rows(engine)
    before = len(rows)
    for r in rows:
        r["agency"] = full_agency(r["agency"])
        r["title"] = clean_title(r["agency"], r["title"])
        r["uniq_key"] = posting_key(r["agency"], r["title"], r["reg_date"])
        r["dedup_hash"] = family_key(r["agency"], r.get("dept"), r["title"])
    cutoff = today - timedelta(days=KEEP_DAYS)
    junk = sum(1 for r in rows if is_junk_row(r))
    rows = [r for r in rows if not is_junk_row(r) and (not _d(r["reg_date"]) or _d(r["reg_date"]) >= cutoff)]
    rows, merged0 = merge_duplicates(rows)
    print(f"[0단계] 기존 {before}건 정리 → {len(rows)}건 (잘못 저장된 행·목록 주소 삭제 {junk}건, "
          f"중복·연장공고 합침 {merged0}건)")

    # [1단계] 수집
    results_by_agency = run_all_collectors(limit=COLLECT_LIMIT)
    total_records = sum(len(v) for v in results_by_agency.values())
    print(f"\n[1단계] 총 {total_records}건 수집 완료. 중복확인·보강 진행 중...\n")

    by_key = {r["uniq_key"]: r for r in rows}
    needs_ai, touched = set(), set()
    new_count = updated_count = skipped_count = 0
    for agency, records in results_by_agency.items():
        for rec in records:
            if not rec.get("title") or is_mois_noise(rec.get("agency", agency), rec["title"]):
                skipped_count += 1
                continue
            rec = _prepare_new(rec)
            if is_junk_row(rec):
                print(f"[WARN] 원문 주소 확인 불가로 제외: {agency} · {rec['title'][:40]} ({str(rec.get('url'))[:60]})")
                skipped_count += 1
                continue
            if is_closed(rec.get("due_date"), rec.get("reg_date"), rec.get("period_end"), today) \
                    and rec["uniq_key"] not in by_key:
                skipped_count += 1          # 이미 마감된 새 공고는 저장·AI 분석하지 않음 (비용 절감)
                continue
            old = by_key.get(rec["uniq_key"])
            if old:
                old_len = len(old.get("content") or "")
                for f in SCRAPED_FIELDS + ["agency", "post_type", "grade", "category", "recommended_solution",
                                           "matched_keywords", "dedup_hash"]:
                    v = rec.get(f)
                    if v not in (None, ""):
                        old[f] = v
                if old.get("ai_priority_score") is None or not old.get("ai_summary") or \
                        (old_len < 300 and len(old.get("content") or "") >= 600):
                    needs_ai.add(old["uniq_key"])       # 본문이 새로 확보되면 더 정확하게 다시 분석
                old["updated_at"] = now
                touched.add(old["uniq_key"])
                updated_count += 1
            else:
                rec["created_at"] = now
                rec["updated_at"] = now
                rec["ai_priority_score"] = None
                by_key[rec["uniq_key"]] = rec
                needs_ai.add(rec["uniq_key"])
                touched.add(rec["uniq_key"])
                new_count += 1

    rows, merged1 = merge_duplicates(list(by_key.values()))
    print(f"[1단계] 신규 {new_count}건 · 갱신 {updated_count}건 · 제외 {skipped_count}건 · 중복 합침 {merged1}건")

    # [2단계] 예산·마감이 빈 진행 중 공고는 원문을 다시 읽어 보강 (오늘 이미 읽은 건 제외)
    targets = [r for r in rows
               if r["uniq_key"] not in touched and (not r.get("budget") or not r.get("due_date"))
               and not is_closed(r.get("due_date"), r.get("reg_date"), r.get("period_end"), today)
               and refreshable_url(r.get("url"))]
    targets.sort(key=lambda r: r.get("reg_date") or "", reverse=True)
    targets = targets[:REFRESH_LIMIT]
    if targets:
        before_len = {r["uniq_key"]: len(r.get("content") or "") for r in targets}
        refresh_records(targets)
        filled = sum(1 for r in targets if r.get("budget") or r.get("due_date"))
        for r in targets:
            r["updated_at"] = now
            if before_len[r["uniq_key"]] < 300 and len(r.get("content") or "") >= 600:
                needs_ai.add(r["uniq_key"])
        print(f"[2단계] 기존 공고 원문 재확인 {len(targets)}건 → 예산 또는 마감 확보 {filled}건")

    # [3단계] AI 분석 — 신규 + 본문이 새로 확보된 공고 (마감된 공고는 제외)
    row_by_key = {r["uniq_key"]: r for r in rows}
    pending = [(k, _build_ai_info_block(row_by_key[k])) for k in needs_ai
               if k in row_by_key and not is_closed(row_by_key[k].get("due_date"), row_by_key[k].get("reg_date"),
                                                    row_by_key[k].get("period_end"), today)]
    print(f"\n[3단계] AI 분석 {len(pending)}건 (모델 {MODEL_NAME}, 동시 {AI_MAX_CONCURRENCY}건, 분당 {AI_RPM_LIMIT}건)\n")
    ai_results = {}
    if pending and is_ai_ready():
        done = {"n": 0}

        def _progress(k, result):
            done["n"] += 1
            title = row_by_key[k].get("title", "")[:40]
            if result and not result.get("error"):
                print(f"  [{done['n']}/{len(pending)}] {result['track']} / {result['score']}점 - {title}")
            else:
                print(f"  [{done['n']}/{len(pending)}] 실패: {(result or {}).get('error', '알수없음')} - {title}")

        ai_results = asyncio.run(analyze_postings_batch(pending, progress_cb=_progress))
    elif pending:
        print("  [경고] ANTHROPIC_API_KEY 미설정 → AI 분석 생략, 기관 기본값으로 임시 분류")
    for k, _ in pending:
        r, res = row_by_key[k], ai_results.get(k)
        if res and not res.get("error"):
            r["track"], r["track_reason"] = res["track"], res["track_reason"]
            r["ai_priority_score"], r["ai_priority_reason"] = res["score"], res["score_reason"]
            r["ai_oneline"], r["ai_summary"] = res["oneline"], res["summary"]
        elif r.get("ai_priority_score") is None:
            r["track"] = AGENCY_TRACK_FALLBACK.get(r.get("agency", ""), "BIZ")
            r["track_reason"] = f"AI 분석 실패({(res or {}).get('error', 'API 키 미설정')})로 기관 기본값으로 임시 분류됨"
            r["ai_priority_reason"] = "AI 분석 대기 중"

    # [4단계] 저장
    mark_status(rows)
    for r in rows:
        r.setdefault("created_at", now)
        r["updated_at"] = r.get("updated_at") or now
    write_rows(engine, rows)
    open_n = sum(1 for r in rows if r["status"] == "진행중")
    with_budget = sum(1 for r in rows if r["status"] == "진행중" and r.get("budget"))
    with_due = sum(1 for r in rows if r["status"] == "진행중" and r.get("due_date"))
    print(f"\n[4단계] DB 저장 {len(rows)}건 (진행 중 {open_n}건: 예산 확보 {with_budget}건 · 마감일 확보 {with_due}건)")

    try:
        export_to_excel(engine)
    except Exception as e:                     # 엑셀은 부가 산출물 — 실패해도 수집 결과에는 영향 없음
        print(f"[경고] 엑셀 저장 실패(건너뜀): {e}")

    # [5단계] 조달청 낙찰·계약 결과 (시장 정보) — 실패해도 위 작업에는 영향 없음
    try:
        done_flag, _ = load_cache("procurement_backfill_done")
        backfill = 365 if (not done_flag and count_results() < 50) else 0
        if backfill:
            print("[5단계] 낙찰·계약 최초 실행 — 최근 1년치 자사 관련 사업을 함께 조회합니다.")
        saved = save_results(fetch_g2b_results(backfill_days=backfill))
        print(f"[5단계] 낙찰·계약 결과 {saved}건 저장")
        if backfill and saved:
            save_cache("procurement_backfill_done", {"date": today.isoformat(), "saved": saved})
    except Exception as e:
        traceback.print_exc()
        print(f"[5단계] 낙찰·계약 수집 실패(건너뜀): {e}")

    print(f"\n신규 {new_count}건 / 갱신 {updated_count}건 / 제외 {skipped_count}건 / "
          f"중복 합침 {merged0 + merged1}건 / AI 분석 {len(pending)}건 처리 완료")


if __name__ == "__main__":
    collect_and_process()
    sys.exit(0)
