# trend_store.py
from datetime import date
import json
import pandas as pd
from sqlalchemy import text
from db2 import get_engine, is_postgres

TREND_TABLE = "trend_keywords"


def _create_sql():
    if is_postgres():
        return f"""
        CREATE TABLE IF NOT EXISTS {TREND_TABLE} (
            id SERIAL PRIMARY KEY,
            snapshot_date DATE NOT NULL,
            keyword VARCHAR(100) NOT NULL,
            count INT,
            importance INT,
            reason TEXT,
            sample_titles TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    return f"""
    CREATE TABLE IF NOT EXISTS {TREND_TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_date DATE NOT NULL,
        keyword VARCHAR(100) NOT NULL,
        count INT,
        importance INT,
        reason TEXT,
        sample_titles TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """


def ensure_table():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(_create_sql()))


def save_trend_snapshot(keywords, snapshot_date=None):
    if not keywords:
        return
    snapshot_date = snapshot_date or date.today()
    ensure_table()
    engine = get_engine()
    rows = [{
        "snapshot_date": snapshot_date,
        "keyword": kw["keyword"],
        "count": kw.get("count", 0),
        "importance": kw.get("importance", 0),
        "reason": kw.get("reason", ""),
        "sample_titles": json.dumps(kw.get("sample_titles", []), ensure_ascii=False),
    } for kw in keywords]
    pd.DataFrame(rows).to_sql(TREND_TABLE, engine, if_exists="append", index=False)


def load_latest_trend():
    ensure_table()
    engine = get_engine()
    df = pd.read_sql_query(
        f"SELECT * FROM {TREND_TABLE} WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM {TREND_TABLE})",
        engine,
    )
    if not df.empty and "sample_titles" in df.columns:
        df["sample_titles"] = df["sample_titles"].apply(lambda x: json.loads(x) if x else [])
    return df


def load_trend_history(days=14):
    ensure_table()
    engine = get_engine()
    if is_postgres():
        query = (
            f"SELECT snapshot_date, keyword, importance, count FROM {TREND_TABLE} "
            f"WHERE snapshot_date >= CURRENT_DATE - INTERVAL '{days} days'"
        )
    else:
        query = (
            f"SELECT snapshot_date, keyword, importance, count FROM {TREND_TABLE} "
            f"WHERE snapshot_date >= date('now', '-{days} days')"
        )
    return pd.read_sql_query(query, engine)
