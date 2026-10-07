# store.py
# 작은 보조 테이블 2개 (Supabase·SQLite 겸용)
#   app_cache          : 아침 배치가 미리 만들어 둔 뉴스·헤드라인·이슈카드 등 (대시보드는 읽기만)
#   alert_subscribers  : 메일 알림 받는 사람 목록
import json
from datetime import datetime

from sqlalchemy import text, bindparam

import common  # noqa: F401  (한국시간 고정)
from common import DEFAULT_SUBSCRIBER, MY_REGIONS_DEFAULT, ALERT_MIN_SCORE_DEFAULT
from db2 import get_engine

CACHE_TABLE = "app_cache"
SUB_TABLE = "alert_subscribers"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------
# app_cache
# ------------------------------------------------------------
def ensure_cache_table():
    with get_engine().begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {CACHE_TABLE} (
                cache_key TEXT PRIMARY KEY,
                payload TEXT,
                updated_at TEXT
            )
        """))


def save_cache(key, obj):
    ensure_cache_table()
    payload = json.dumps(obj, ensure_ascii=False, default=str)
    with get_engine().begin() as conn:
        conn.execute(text(f"DELETE FROM {CACHE_TABLE} WHERE cache_key = :k"), {"k": key})
        conn.execute(text(f"INSERT INTO {CACHE_TABLE} (cache_key, payload, updated_at) VALUES (:k, :p, :u)"),
                     {"k": key, "p": payload, "u": _now()})


def load_cache(key):
    """반환: (값, 저장시각 문자열). 없거나 오류면 (None, None)"""
    try:
        ensure_cache_table()
        with get_engine().begin() as conn:
            row = conn.execute(text(f"SELECT payload, updated_at FROM {CACHE_TABLE} WHERE cache_key = :k"),
                               {"k": key}).fetchone()
        if not row or row[0] is None:
            return None, None
        return json.loads(row[0]), row[1]
    except Exception:
        return None, None


def load_cache_many(keys):
    """여러 키를 한 번에 읽기. 반환: {key: (값, 저장시각)}"""
    out = {k: (None, None) for k in keys}
    try:
        ensure_cache_table()
        stmt = text(f"SELECT cache_key, payload, updated_at FROM {CACHE_TABLE} WHERE cache_key IN :ks").bindparams(
            bindparam("ks", expanding=True))
        with get_engine().begin() as conn:
            rows = conn.execute(stmt, {"ks": list(keys)}).fetchall()
        for k, p, u in rows:
            try:
                out[k] = (json.loads(p), u)
            except Exception:
                pass
    except Exception:
        pass
    return out


# ------------------------------------------------------------
# alert_subscribers
# ------------------------------------------------------------
def ensure_sub_table():
    from sqlalchemy import inspect as sa_inspect
    engine = get_engine()
    is_new = not sa_inspect(engine).has_table(SUB_TABLE)
    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {SUB_TABLE} (
                email TEXT PRIMARY KEY,
                regions TEXT,
                include_national INTEGER,
                min_score INTEGER,
                active INTEGER,
                created_at TEXT,
                updated_at TEXT
            )
        """))
        if is_new:   # 테이블을 처음 만들 때 한 번만 기본 수신자 등록 (해제하면 다시 생기지 않음)
            conn.execute(text(f"""
                INSERT INTO {SUB_TABLE} (email, regions, include_national, min_score, active, created_at, updated_at)
                VALUES (:e, :r, 1, :m, 1, :n, :n)
            """), {"e": DEFAULT_SUBSCRIBER, "r": json.dumps(MY_REGIONS_DEFAULT, ensure_ascii=False),
                   "m": ALERT_MIN_SCORE_DEFAULT, "n": _now()})


def _row_to_sub(row):
    try:
        regions = json.loads(row[1]) if row[1] else []
    except Exception:
        regions = []
    return {
        "email": row[0], "regions": regions, "include_national": bool(row[2]),
        "min_score": int(row[3] if row[3] is not None else ALERT_MIN_SCORE_DEFAULT),
        "active": bool(row[4]), "created_at": row[5], "updated_at": row[6],
    }


def upsert_subscriber(email, regions, include_national=True, min_score=ALERT_MIN_SCORE_DEFAULT):
    ensure_sub_table()
    now = _now()
    with get_engine().begin() as conn:
        old = conn.execute(text(f"SELECT created_at FROM {SUB_TABLE} WHERE email = :e"), {"e": email}).fetchone()
        conn.execute(text(f"DELETE FROM {SUB_TABLE} WHERE email = :e"), {"e": email})
        conn.execute(text(f"""
            INSERT INTO {SUB_TABLE} (email, regions, include_national, min_score, active, created_at, updated_at)
            VALUES (:e, :r, :i, :m, 1, :c, :n)
        """), {"e": email, "r": json.dumps(list(regions or []), ensure_ascii=False),
               "i": 1 if include_national else 0, "m": int(min_score),
               "c": old[0] if old else now, "n": now})
    return "updated" if old else "created"


def delete_subscriber(email):
    ensure_sub_table()
    with get_engine().begin() as conn:
        existed = conn.execute(text(f"SELECT 1 FROM {SUB_TABLE} WHERE email = :e"), {"e": email}).fetchone()
        conn.execute(text(f"DELETE FROM {SUB_TABLE} WHERE email = :e"), {"e": email})
    return bool(existed)


def get_subscriber(email):
    ensure_sub_table()
    with get_engine().begin() as conn:
        row = conn.execute(text(f"SELECT email, regions, include_national, min_score, active, created_at, updated_at "
                                f"FROM {SUB_TABLE} WHERE email = :e"), {"e": email}).fetchone()
    return _row_to_sub(row) if row else None


def list_subscribers(active_only=True):
    ensure_sub_table()
    sql = f"SELECT email, regions, include_national, min_score, active, created_at, updated_at FROM {SUB_TABLE}"
    if active_only:
        sql += " WHERE active = 1"
    with get_engine().begin() as conn:
        rows = conn.execute(text(sql)).fetchall()
    return [_row_to_sub(r) for r in rows]


def count_subscribers():
    try:
        ensure_sub_table()
        with get_engine().begin() as conn:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {SUB_TABLE} WHERE active = 1")).fetchone()[0])
    except Exception:
        return 0
