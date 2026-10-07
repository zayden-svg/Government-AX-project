# postings_data.py
# 배치(briefing_batch.py·alert_mailer.py)용 공고 데이터 로더 — 대시보드와 같은 기준으로 정리
from datetime import datetime

import pandas as pd

import common  # noqa: F401  (한국시간 고정)
from common import is_mois_noise, detect_regions
from db2 import get_engine


def load_active_postings():
    """진행 중(마감 전 또는 마감일 미표기)이고 일반 보도자료가 아닌 공고. 지역·점수 컬럼 포함."""
    try:
        df = pd.read_sql_query("SELECT * FROM postings", get_engine())
    except Exception as e:
        print(f"[WARN] postings 읽기 실패: {e}")
        return pd.DataFrame()
    if df.empty:
        return df
    for c in ["title", "agency", "dept", "content", "track", "due_date", "reg_date", "created_at",
              "ai_oneline", "ai_summary", "budget", "url", "matched_keywords"]:
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].fillna("").astype(str)
    df["_due"] = pd.to_datetime(df["due_date"], errors="coerce")
    df["_reg"] = pd.to_datetime(df["reg_date"], errors="coerce")
    df["_created"] = pd.to_datetime(df["created_at"], errors="coerce")
    if "ai_priority_score" not in df.columns:
        df["ai_priority_score"] = None
    df["_score"] = pd.to_numeric(df["ai_priority_score"], errors="coerce").fillna(-1).astype(int)
    today = pd.Timestamp(datetime.now().date())
    df = df[df["_due"].isna() | (df["_due"] >= today)]
    noise = [is_mois_noise(a, t) for a, t in zip(df["agency"], df["title"])]
    df = df[[not n for n in noise]].copy()
    df["_regions"] = [detect_regions(a, d, t, c[:300]) for a, d, t, c in
                      zip(df["agency"], df["dept"], df["title"], df["content"])]
    df["_track"] = df["track"].str.upper().str.strip()
    return df.reset_index(drop=True)
