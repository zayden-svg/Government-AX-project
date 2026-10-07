# procurement_store.py
# 조달청 낙찰·계약 결과 저장소 — 영업기회(postings)와 분리된 '시장 정보' 테이블
#  + 경쟁사 수주 건의 '재발주 예상 시점' 계산 (계약 종료일 기준)
import hashlib
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import text, bindparam

import common  # noqa: F401  (한국시간 고정)
from common import is_competitor_match, is_solution_related
from db2 import get_engine, is_postgres

TABLE = "procurement_results"
FIELDS = ["kind", "ref_no", "title", "agency", "company", "amount", "rate", "event_date", "end_date", "end_est", "url"]
REORDER_LEAD_DAYS = 60     # 계약 종료 몇 일 전에 다음 발주가 나올지 (공공기관 통상 1~3개월 전 공고)


def ensure_table():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                uniq_key TEXT PRIMARY KEY,
                kind TEXT, ref_no TEXT, title TEXT, agency TEXT, company TEXT,
                amount TEXT, rate TEXT, event_date TEXT, end_date TEXT, end_est TEXT, url TEXT,
                created_at TEXT, updated_at TEXT
            )
        """))
        if is_postgres():
            existing = {r[0] for r in conn.execute(text(
                f"SELECT column_name FROM information_schema.columns WHERE table_name='{TABLE}'"))}
        else:
            existing = {r[1] for r in conn.execute(text(f"PRAGMA table_info({TABLE})"))}
        for col in ("end_date", "end_est"):
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {col} TEXT"))


def _key(row):
    base = f"{row.get('kind')}|{row.get('ref_no') or row.get('title')}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


def count_results():
    try:
        ensure_table()
        with get_engine().begin() as conn:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).fetchone()[0])
    except Exception:
        return 0


def save_results(rows):
    """낙찰·계약 결과를 한 번에 저장(같은 건은 갱신). 반환: 저장 건수"""
    if not rows:
        return 0
    ensure_table()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    params = {}
    for r in rows:
        p = {f: str(r.get(f) or "") for f in FIELDS}
        p["uniq_key"] = _key(r)
        p["now"] = now
        params[p["uniq_key"]] = p
    keys = list(params)
    with get_engine().begin() as conn:
        created = {}
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            stmt = text(f"SELECT uniq_key, created_at FROM {TABLE} WHERE uniq_key IN :ks").bindparams(
                bindparam("ks", expanding=True))
            created.update({k: c for k, c in conn.execute(stmt, {"ks": part}).fetchall()})
            dstmt = text(f"DELETE FROM {TABLE} WHERE uniq_key IN :ks").bindparams(bindparam("ks", expanding=True))
            conn.execute(dstmt, {"ks": part})
        for p in params.values():
            p["created"] = created.get(p["uniq_key"]) or now
        conn.execute(text(f"""
            INSERT INTO {TABLE} (uniq_key, kind, ref_no, title, agency, company, amount, rate,
                                 event_date, end_date, end_est, url, created_at, updated_at)
            VALUES (:uniq_key, :kind, :ref_no, :title, :agency, :company, :amount, :rate,
                    :event_date, :end_date, :end_est, :url, :created, :now)
        """), list(params.values()))
    return len(params)


def load_results(days=30):
    """최근 N일(낙찰·계약일 기준) 결과. 테이블이 아직 없으면 빈 표."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        ensure_table()
        df = pd.read_sql_query(
            text(f"SELECT * FROM {TABLE} WHERE event_date >= :since ORDER BY event_date DESC"),
            get_engine(), params={"since": since},
        )
    except Exception:
        return pd.DataFrame(columns=FIELDS)
    return df.fillna("")


def load_reorder_candidates(competitors=None, horizon_days=180, include_solution=True):
    """재발주 예상 목록.
    - 대상: 경쟁사가 수주한 건 (include_solution=True면 자사 제품 관련 사업도 포함)
    - 예상 발주 시점 = 계약 종료일 - 60일, 오늘 기준 -30일 ~ +horizon_days 사이만
    반환 컬럼: 구분표시, title, agency, company, amount, end_date, end_est, expected, d_day, url, kind"""
    cols = ["표시", "title", "agency", "company", "amount", "end_date", "end_est", "expected", "d_day", "url", "kind"]
    try:
        ensure_table()
        df = pd.read_sql_query(text(f"SELECT * FROM {TABLE}"), get_engine())
    except Exception:
        return pd.DataFrame(columns=cols)
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = df.fillna("")
    df["_comp"] = df["company"].map(lambda c: is_competitor_match(c, competitors))
    df["_sol"] = df["title"].map(is_solution_related)
    df = df[df["_comp"] | (df["_sol"] if include_solution else False)].copy()
    if df.empty:
        return pd.DataFrame(columns=cols)

    # 같은 사업이 낙찰·계약 두 번 잡히면 계약(실제 종료일 있음)을 우선
    df["_norm"] = df["agency"].astype(str) + "|" + df["title"].astype(str).str.replace(r"\s+", "", regex=True)
    df["_prio"] = df["kind"].map({"계약": 0, "낙찰": 1}).fillna(2)
    df = df.sort_values(["_prio", "event_date"], ascending=[True, False]).drop_duplicates("_norm")

    end = pd.to_datetime(df["end_date"], errors="coerce")
    df = df[end.notna()].copy()
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["_end"] = pd.to_datetime(df["end_date"], errors="coerce")
    df["_expected"] = df["_end"] - pd.Timedelta(days=REORDER_LEAD_DAYS)
    today = pd.Timestamp(datetime.now().date())
    df["d_day"] = (df["_expected"] - today).dt.days
    df = df[(df["d_day"] >= -30) & (df["d_day"] <= horizon_days)].copy()
    df["expected"] = df["_expected"].dt.strftime("%Y-%m")
    df["표시"] = ["🔴 경쟁사" if c else "🟢 자사관련" for c in df["_comp"]]
    # 앞으로 다가올 건(D-0 이상)을 가까운 순으로 먼저, 이미 지난 건은 맨 뒤
    df["_passed"] = df["d_day"] < 0
    return df.sort_values(["_passed", "d_day"])[cols].reset_index(drop=True)
