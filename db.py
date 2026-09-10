# db.py
import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_FILE = "gov_tracker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    collected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    agency TEXT,
    gubun TEXT,
    post_type TEXT,              -- 'RND' 또는 'BID'
    title TEXT NOT NULL,
    title_normalized TEXT NOT NULL,
    dept TEXT,
    manager TEXT,
    reg_date TEXT,
    due_date TEXT,
    budget TEXT,
    attach TEXT,
    views TEXT,
    url TEXT,
    grade TEXT,                  -- 상/중/하
    category TEXT,
    matched_keywords TEXT,
    recommended_solution TEXT,
    status TEXT DEFAULT '진행중',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source, title_normalized)
);

CREATE INDEX IF NOT EXISTS idx_post_type ON postings(post_type);
CREATE INDEX IF NOT EXISTS idx_grade ON postings(grade);
CREATE INDEX IF NOT EXISTS idx_due_date ON postings(due_date);
"""


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def save_raw(source: str, item: dict):
    conn = get_conn()
    conn.execute(
        "INSERT INTO raw_postings (source, raw_json, collected_at) VALUES (?, ?, ?)",
        (source, json.dumps(item, ensure_ascii=False), datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def upsert_posting(item: dict) -> str:
    """item에는 이미 classifier가 채운 post_type/grade/category 등이 들어있어야 함.
    반환값: 'new' 또는 'updated'"""
    conn = get_conn()
    now = datetime.now().isoformat()
    row = conn.execute(
        "SELECT id, due_date, url FROM postings WHERE source=? AND title_normalized=?",
        (item["source"], item["title_normalized"]),
    ).fetchone()

    if row:
        conn.execute(
            """UPDATE postings SET due_date=?, url=?, grade=?, category=?,
               matched_keywords=?, recommended_solution=?, post_type=?, updated_at=?
               WHERE id=?""",
            (item.get("due_date") or row["due_date"], item.get("url") or row["url"],
             item.get("grade"), item.get("category"), item.get("matched_keywords"),
             item.get("recommended_solution"), item.get("post_type"), now, row["id"]),
        )
        conn.commit(); conn.close()
        return "updated"

    conn.execute(
        """INSERT INTO postings
           (source, agency, gubun, post_type, title, title_normalized, dept, manager,
            reg_date, due_date, budget, attach, views, url, grade, category,
            matched_keywords, recommended_solution, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (item.get("source"), item.get("agency"), item.get("gubun"), item.get("post_type"),
         item.get("title"), item.get("title_normalized"), item.get("dept"), item.get("manager"),
         item.get("reg_date"), item.get("due_date"), item.get("budget"), item.get("attach"),
         item.get("views"), item.get("url"), item.get("grade"), item.get("category"),
         item.get("matched_keywords"), item.get("recommended_solution"), "진행중", now, now),
    )
    conn.commit(); conn.close()
    return "new"


def mark_expired():
    """마감일이 지난 공고는 status를 '마감'으로 변경 (삭제하지 않고 보존)"""
    conn = get_conn()
    conn.execute(
        "UPDATE postings SET status='마감' WHERE due_date IS NOT NULL AND due_date != '' "
        "AND date(due_date) < date('now') AND status != '마감'"
    )
    conn.commit(); conn.close()


def fetch_by_type(post_type: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM postings WHERE post_type=? AND status='진행중' "
        "ORDER BY CASE grade WHEN '상' THEN 0 WHEN '중' THEN 1 ELSE 2 END, due_date ASC",
        (post_type,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
