# postings_data.py
# 배치(briefing_batch.py·alert_mailer.py)용 공고 데이터 로더 — 대시보드와 같은 기준으로 정리
from datetime import datetime

import pandas as pd

import common  # noqa: F401  (한국시간 고정)
from common import is_mois_noise, detect_regions, is_closed, family_key, source_rank
from db2 import get_engine


def load_active_postings():
    """진행 중(마감 전, 또는 마감일 미표기지만 최근 등록)이고 일반 보도자료가 아닌 공고. 지역·점수 컬럼 포함."""
    try:
        df = pd.read_sql_query("SELECT * FROM postings", get_engine())
    except Exception as e:
        print(f"[WARN] postings 읽기 실패: {e}")
        return pd.DataFrame()
    if df.empty:
        return df
    for c in ["title", "agency", "dept", "content", "track", "due_date", "reg_date", "created_at",
              "ai_oneline", "ai_summary", "budget", "budget_label", "period_end", "url", "matched_keywords"]:
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].fillna("").astype(str)
    df["_due"] = pd.to_datetime(df["due_date"], errors="coerce")
    df["_reg"] = pd.to_datetime(df["reg_date"], errors="coerce")
    df["_created"] = pd.to_datetime(df["created_at"], errors="coerce")
    if "ai_priority_score" not in df.columns:
        df["ai_priority_score"] = None
    df["_score"] = pd.to_numeric(df["ai_priority_score"], errors="coerce").fillna(-1).astype(int)
    today = datetime.now().date()
    # 마감 지남 / 마감일 모름+사업종료일 지남 / 마감일 모름+등록 45일 경과 → 제외 (대시보드와 같은 기준)
    df = df[[not is_closed(d, r, p, today) for d, r, p in zip(df["due_date"], df["reg_date"], df["period_end"])]]
    # 같은 사업의 연장·재공고가 남아 있으면 최근 것만
    fams = [family_key(a, d, t) for a, d, t in zip(df["agency"], df["dept"], df["title"])]
    df = df.assign(_fam=[f if len(f.split("|", 1)[-1]) >= 6 else f"{f}#{i}" for i, f in enumerate(fams)])
    df = df.assign(_src_rank=[source_rank(a) for a in df["agency"]])
    df = df.sort_values(["_src_rank", "reg_date"], ascending=[False, False]).drop_duplicates("_fam", keep="first")
    noise = [is_mois_noise(a, t) for a, t in zip(df["agency"], df["title"])]
    df = df[[not n for n in noise]].copy()
    df["_regions"] = [detect_regions(a, d, t, c[:300]) for a, d, t, c in
                      zip(df["agency"], df["dept"], df["title"], df["content"])]
    df["_track"] = df["track"].str.upper().str.strip()
    return df.reset_index(drop=True)
