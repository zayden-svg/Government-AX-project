import sqlite3

DB_PATH = "gov_tracker.db"  # 폴더 안의 실제 .db 파일 이름으로 바꿔주세요

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

print("=== 테이블 목록 ===")
cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
for row in cur.fetchall():
    print(row[0])

print("\n=== postings 테이블 컬럼 목록 ===")
cur.execute("PRAGMA table_info(postings);")
for row in cur.fetchall():
    print(row)

conn.close()
