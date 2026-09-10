# main.py
import hashlib
import re
import sqlite3
from datetime import datetime, date

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from collectors import run_all_collectors

DB_PATH = "gov_tracker.db"
EXCEL_OUTPUT = "Gov-Tracker_결과.xlsx"

# ------------------------------------------------------------
# 공고 유형(입찰/RND/기타) 분류 - 기존 로직 유지
# ------------------------------------------------------------
RND_KEYWORDS = ["과제공고", "지원사업", "R&D", "연구개발", "공모전", "지원과제", "사업 공모", "공모","수요조사"]
BID_KEYWORDS = ["입찰", "전자입찰", "구매", "용역", "발주", "제안서","제안" "적격심사", "사전규격"]

# 키워드 -> (등급, 카테고리, 추천솔루션)
GRADE_RULES = [
    (["통합관리시스템", "예측·지원", "재해복구", "고도화", "구축"], "상", "시스템구축·전환", "NetFUNNEL"),
    (["감리", "컨설팅", "ISMS"], "중", "컨설팅·감리", "검토 필요"),
]

# ------------------------------------------------------------
# R&D 과제 / 사업부(매출 연계) 과제 구분
# ------------------------------------------------------------
TRACK_RND_KEYWORDS = ["연구개발", "R&D", "기술개발", "실증", "과제", "연구과제", "기초연구", "산학협력"]
TRACK_BIZ_KEYWORDS = ["입찰", "용역", "구매", "발주", "제안서", "사업자 선정", "위탁", "공급"]

AGENCY_TRACK_DEFAULT = {
    "IRIS": "RND",
    "KERIS": "RND",
    "NTIS": "RND",
    "TIPA": "RND",
    "KIAT": "RND",
    "INNOPOLIS": "RND",
    "NIPA": "BIZ",
    "조달청": "BIZ",
    "국가AI전략위원회": "BIZ",
}

TRACK_LABELS = {"RND": "R&D", "BIZ": "사업부", "": "미분류"}


def classify_post_type(title: str) -> str:
    if not title:
        return "ETC"
    for kw in BID_KEYWORDS:
        if kw in title:
            return "BID"
    for kw in RND_KEYWORDS:
        if kw in title:
            return "RND"
    return "ETC"


def classify_track(agency: str, title: str, content: str = "") -> str:
    """R&D 과제인지 사업부(매출 연계) 과제인지 구분"""
    text = f"{title} {content}"
    for kw in TRACK_BIZ_KEYWORDS:
        if kw in text:
            return "BIZ"
    for kw in TRACK_RND_KEYWORDS:
        if kw in text:
            return "RND"
    return AGENCY_TRACK_DEFAULT.get(agency, "BIZ")


def classify_and_score(title: str, content: str = ""):
    text = f"{title} {content}"
    for keywords, grade, category, solution in GRADE_RULES:
        matched = [kw for kw in keywords if kw in text]
        if matched:
            return {
                "grade": grade,
                "category": category,
                "recommended_solution": solution,
                "matched_keywords": ", ".join(matched),
            }
    return {"grade": "하", "category": "-", "recommended_solution": "-", "matched_keywords": "-"}


# ------------------------------------------------------------
# 담당자명 정제 - 실제 사람 이름만 남기고 기관명/숫자는 제거
# ------------------------------------------------------------
AGENCY_SUFFIXES = [
    "센터", "재단", "진흥원", "위원회", "연구원", "정보원",
    "공사", "청", "부", "원", "팀", "실", "국", "협회", "본부",
]


def clean_manager_name(raw) -> str:
    if raw is None:
        return ""
    name = str(raw).strip()
    if not name:
        return ""

    # 숫자(전화번호, 내선번호 등)가 포함되면 제외
    if re.search(r"\d", name):
        return ""

    # 기관명 접미사로 끝나면 제외 (예: "정보통신기획평가원", "조달청" 등)
    for suffix in AGENCY_SUFFIXES:
        if name.endswith(suffix):
            return ""

    # 한글 이름(2~4자) 허용
    if re.fullmatch(r"[가-힣]{2,4}", name):
        return name

    # 영문 이름(성/이름 형태) 허용
    if re.fullmatch(r"[A-Za-z]{2,20}(\s[A-Za-z]{2,20})?", name):
        return name

    return ""


# ------------------------------------------------------------
# 중복 판별용 해시 - 제목/등록일/마감일을 정규화하여 생성
# 다른 기관에서 수집되었더라도 같은 과제면 동일 해시가 나옴
# ------------------------------------------------------------
def normalize_for_dedup(text) -> str:
    if not text:
        return ""
    text = str(text)
    text = re.sub(r"\s+", "", text)          # 공백 제거
    text = re.sub(r"[^\w가-힣]", "", text)     # 특수문자 제거
    return text.lower()


def build_dedup_hash(title: str, reg_date: str, due_date: str) -> str:
    norm_title = normalize_for_dedup(title)
    base = f"{norm_title}|{reg_date or ''}|{due_date or ''}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


# ------------------------------------------------------------
# DB 초기화 / 마이그레이션
# ------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS postings (
            uniq_key TEXT PRIMARY KEY,
            source TEXT, agency TEXT, gubun TEXT, post_type TEXT, title TEXT,
            dept TEXT, manager TEXT, reg_date TEXT, due_date TEXT, budget TEXT,
            attach TEXT, views TEXT, url TEXT, grade TEXT, category TEXT,
            matched_keywords TEXT, recommended_solution TEXT, status TEXT,
            track TEXT, dedup_hash TEXT,
            created_at TEXT, updated_at TEXT
        )
    """)
    ensure_columns(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dedup_hash ON postings(dedup_hash)")
    conn.commit()
    return conn


def ensure_columns(conn):
    """기존 DB(구버전)에 track / dedup_hash 컬럼이 없으면 추가"""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(postings)").fetchall()]
    if "track" not in cols:
        conn.execute("ALTER TABLE postings ADD COLUMN track TEXT")
    if "dedup_hash" not in cols:
        conn.execute("ALTER TABLE postings ADD COLUMN dedup_hash TEXT")
    conn.commit()


def make_uniq_key(rec: dict) -> str:
    base = rec.get("url") or f"{rec.get('agency')}|{rec.get('title')}|{rec.get('reg_date')}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


def find_existing_by_dedup_hash(conn, dedup_hash: str):
    """동일한 dedup_hash를 가진 기존 레코드(uniq_key, agency) 조회"""
    if not dedup_hash:
        return None
    return conn.execute(
        "SELECT uniq_key, agency FROM postings WHERE dedup_hash=? LIMIT 1",
        (dedup_hash,),
    ).fetchone()


def upsert_posting(conn, rec: dict) -> str:
    uniq_key = make_uniq_key(rec)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    exists = conn.execute("SELECT 1 FROM postings WHERE uniq_key=?", (uniq_key,)).fetchone()

    if exists:
        conn.execute("""
            UPDATE postings SET
                source=?, agency=?, gubun=?, post_type=?, title=?, dept=?, manager=?,
                reg_date=?, due_date=?, budget=?, attach=?, views=?, url=?,
                grade=?, category=?, matched_keywords=?, recommended_solution=?,
                track=?, dedup_hash=?,
                status='진행중', updated_at=?
            WHERE uniq_key=?
        """, (
            rec.get("source", ""), rec.get("agency", ""), rec.get("gubun", ""),
            rec.get("post_type", ""), rec.get("title", ""), rec.get("dept", ""),
            rec.get("manager", ""), rec.get("reg_date", ""), rec.get("due_date", ""),
            rec.get("budget", ""), rec.get("attach", ""), rec.get("views", ""),
            rec.get("url", ""), rec.get("grade", ""), rec.get("category", ""),
            rec.get("matched_keywords", ""), rec.get("recommended_solution", ""),
            rec.get("track", ""), rec.get("dedup_hash", ""),
            now, uniq_key,
        ))
        return "updated"
    else:
        conn.execute("""
            INSERT INTO postings (
                uniq_key, source, agency, gubun, post_type, title, dept, manager,
                reg_date, due_date, budget, attach, views, url,
                grade, category, matched_keywords, recommended_solution,
                track, dedup_hash,
                status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            uniq_key, rec.get("source", ""), rec.get("agency", ""), rec.get("gubun", ""),
            rec.get("post_type", ""), rec.get("title", ""), rec.get("dept", ""),
            rec.get("manager", ""), rec.get("reg_date", ""), rec.get("due_date", ""),
            rec.get("budget", ""), rec.get("attach", ""), rec.get("views", ""),
            rec.get("url", ""), rec.get("grade", ""), rec.get("category", ""),
            rec.get("matched_keywords", ""), rec.get("recommended_solution", ""),
            rec.get("track", ""), rec.get("dedup_hash", ""),
            "진행중", now, now,
        ))
        return "new"


def mark_expired(conn):
    today = date.today().strftime("%Y-%m-%d")
    conn.execute("""
        UPDATE postings SET status='마감'
        WHERE due_date != '' AND due_date < ? AND status != '마감'
    """, (today,))
    conn.commit()


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
            grade_cell = row[grade_col - 1]
            if grade_cell.value == "상":
                for c in row:
                    c.fill = red_fill
            elif grade_cell.value == "중":
                for c in row:
                    c.fill = orange_fill

    # 제목 클릭 시 원문으로 이동하도록 하이퍼링크 적용
    if "제목" in headers and "원문링크" in headers:
        title_col = headers.index("제목") + 1
        url_col = headers.index("원문링크") + 1
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            title_cell = row[title_col - 1]
            url_cell = row[url_col - 1]
            if url_cell.value:
                title_cell.hyperlink = url_cell.value
                title_cell.font = Font(color="0563C1", underline="single")

    for col_cells in ws.columns:
        max_len = max((len(str(c.value)) if c.value else 0) for c in col_cells)
        col_letter = get_column_letter(col_cells[0].column)
        ws.column_dimensions[col_letter].width = min(max(max_len + 2, 10), 60)

    ws.freeze_panes = "A2"
    wb.save(path)


def export_to_excel(conn):
    df = pd.read_sql_query("""
        SELECT
            agency AS 기관, track AS 분류, post_type AS 공고유형, gubun AS 세부구분, title AS 제목,
            dept AS 담당부서, manager AS 담당자, reg_date AS 등록일, due_date AS 마감일,
            grade AS 등급, category AS 카테고리, recommended_solution AS 추천솔루션,
            matched_keywords AS 매칭키워드, status AS 상태, url AS 원문링크
        FROM postings
        ORDER BY reg_date DESC
    """, conn)

    df = df.fillna("")
    df["분류"] = df["분류"].map(lambda v: TRACK_LABELS.get(v, v) if v else "미분류")

    with pd.ExcelWriter(EXCEL_OUTPUT, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="전체", index=False)

    _apply_grade_style(EXCEL_OUTPUT)
    print(f"[완료] 결과 파일 저장: {EXCEL_OUTPUT} ({len(df)}건)")


def collect_and_process():
    conn = init_db()
    new_count = updated_count = skipped_count = duplicate_count = 0

    results_by_agency = run_all_collectors(limit=10)

    for agency, records in results_by_agency.items():
        for rec in records:
            if not rec.get("title"):
                skipped_count += 1
                continue

            # 담당자명 정제 (기관명/숫자 제거, 실제 이름만 유지)
            rec["manager"] = clean_manager_name(rec.get("manager", ""))

            rec["post_type"] = classify_post_type(rec["title"])
            rec["track"] = classify_track(
                rec.get("agency", agency), rec["title"], rec.get("content", "")
            )
            rec.update(classify_and_score(rec["title"], rec.get("content", "")))

            # 기관 간 중복 과제 제외 처리
            dedup_hash = build_dedup_hash(
                rec.get("title", ""), rec.get("reg_date", ""), rec.get("due_date", "")
            )
            rec["dedup_hash"] = dedup_hash
            current_uniq_key = make_uniq_key(rec)

            existing = find_existing_by_dedup_hash(conn, dedup_hash)
            if existing and existing[0] != current_uniq_key:
                # 다른 기관/다른 공고로 이미 동일한 내용이 수집되어 있음 -> 건너뜀
                duplicate_count += 1
                continue

            status = upsert_posting(conn, rec)
            if status == "new":
                new_count += 1
            elif status == "updated":
                updated_count += 1

    conn.commit()
    mark_expired(conn)
    export_to_excel(conn)
    conn.close()

    print(
        f"\n신규 {new_count}건 / 갱신 {updated_count}건 / "
        f"제목없음 제외 {skipped_count}건 / 중복 제외 {duplicate_count}건 처리 완료"
    )


if __name__ == "__main__":
    collect_and_process()
