# award_export.py
# 조달청 낙찰 결과 전체 수집 (IT 관련만 저장) — "누가 어떤 입찰을 따냈나"
#   · 사립대 산학협력단 등은 나라장터로 입찰만 하고 계약은 나라장터에 등록하지 않는 경우가 많아,
#     낙찰 자료가 있어야 대학 사업 수주 업체를 놓치지 않음
#   · 계약 수집(contract_export.py)과 같은 방식: 하루 단위로 최근 → 과거, 이어하기, 하루 호출 한도 보호
#   · API: 조달청_나라장터 낙찰정보서비스 (계약정보서비스와 호출 한도가 따로)
#
# 실행: python award_export.py
import os
import sys
from datetime import datetime

from sqlalchemy import text

import common  # noqa: F401  (한국시간 고정)
import contract_export as ce
from db2 import get_engine

TABLE = "g2b_awards"
K_PROGRESS = "award_crawl_progress"
OPS = {
    "용역": "https://apis.data.go.kr/1230000/as/ScsbidInfoService/getScsbidListSttusServcPPSSrch",
    "물품": "https://apis.data.go.kr/1230000/as/ScsbidInfoService/getScsbidListSttusThngPPSSrch",
}
YEARS_BY_TYPE = {"용역": 3, "물품": 1}
CALL_BUDGET = int(os.getenv("AWARD_CALL_BUDGET", "950"))

FIELDS = [  # (저장 칼럼, API 필드 후보들)
    ("bid_no", ("bidNtceNo",)), ("bid_ord", ("bidNtceOrd",)), ("title", ("bidNtceNm",)),
    ("ntce_instt", ("ntceInsttNm",)), ("dminstt", ("dminsttNm",)), ("dminstt_cd", ("dminsttCd",)),
    ("winner", ("bidwinnrNm",)), ("winner_bizno", ("bidwinnrBizno",)), ("winner_ceo", ("bidwinnrCeoNm",)),
    ("winner_addr", ("bidwinnrAdrs",)), ("winner_tel", ("bidwinnrTelNo",)),
    ("amount", ("sucsfbidAmt",)), ("rate", ("sucsfbidRate",)), ("bidders", ("prtcptCnum",)),
    ("open_dt", ("rlOpengDt", "opengDt")), ("final_date", ("fnlSucsfDate",)), ("rgst_dt", ("rgstDt",)),
]
COLS = ["uniq_key", "biz_type", "it_reason", "event_date", "end_date", "end_est", "url"] + [c for c, _ in FIELDS] + ["updated_at"]


def ensure_table():
    with get_engine().begin() as conn:
        cols = ", ".join(f"{c} TEXT" for c in COLS if c != "uniq_key")
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS {TABLE} (uniq_key TEXT PRIMARY KEY, {cols})"))


def save_rows(rows):
    if not rows:
        return 0
    ensure_table()
    dedup = {r["uniq_key"]: r for r in rows}
    cols = ", ".join(COLS)
    vals = ", ".join(f":{c}" for c in COLS)
    upd = ", ".join(f"{c}=excluded.{c}" for c in COLS if c != "uniq_key")
    stmt = text(f"INSERT INTO {TABLE} ({cols}) VALUES ({vals}) ON CONFLICT (uniq_key) DO UPDATE SET {upd}")
    data = [{c: str(r.get(c) or "") for c in COLS} for r in dedup.values()]
    with get_engine().begin() as conn:
        for i in range(0, len(data), 1000):
            conn.execute(stmt, data[i:i + 1000])
    return len(data)


def to_row(it, biz_type):
    title = str(it.get("bidNtceNm") or "").strip()
    reason = ce.it_reason({"cntrctNm": title})
    if not reason:
        return None
    r = {}
    for c, keys in FIELDS:
        r[c] = next((str(it.get(k)).strip() for k in keys if it.get(k) not in (None, "")), "")
    ev = ce._date(r["final_date"] or r["open_dt"])
    r.update({
        "biz_type": biz_type, "it_reason": reason, "event_date": ev,
        # 낙찰 정보엔 계약기간이 없어 '낙찰일 + 1년'을 종료 추정으로 둠 (계약 자료가 있으면 통합 때 계약 쪽을 우선)
        "end_date": _plus_year(ev),
        "end_est": "Y",
        "url": (f"https://www.g2b.go.kr/link/PNPE027_01/single/?bidPbancNo={r['bid_no']}&bidPbancOrd={r['bid_ord'] or '000'}"
                if r["bid_no"] else ""),
        "uniq_key": f"{biz_type}|{r['bid_no']}|{r['bid_ord']}|{r['winner_bizno'] or r['winner']}",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    return r


def _plus_year(d):
    try:
        x = datetime.strptime(d, "%Y-%m-%d")
        try:
            return x.replace(year=x.year + 1).strftime("%Y-%m-%d")
        except ValueError:                      # 2월 29일
            return x.replace(year=x.year + 1, day=28).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def make_crawler():
    cr = ce.Crawler(call_budget=CALL_BUDGET)
    cr.ops = OPS
    cr.row_fn = to_row
    cr.date_params = lambda day: {"inqryDiv": "2", "inqryBgnDt": day + "0000", "inqryEndDt": day + "2359"}
    return cr


def main():
    ce.crawl(cr=make_crawler(), progress_key=K_PROGRESS, save_fn=save_rows, years_by_type=YEARS_BY_TYPE, label="낙찰")
    return 0


if __name__ == "__main__":
    sys.exit(main())
