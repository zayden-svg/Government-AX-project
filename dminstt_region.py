# dminstt_region.py
# 조달청 수요기관 정보(주소) → 기관코드별 시·도 표 (dminstt_region) — 통합 엑셀의 '지역' 칸 보완용
#   API: 조달청_나라장터 사용자정보 서비스 / getDminsttInfo02 (공공데이터포털 활용신청 필요, 자동승인)
#   등록일 기준으로 기간을 나눠 전체 기관을 받음 (하루 한도 안에서 이어서)
import json
import os
import re
import sys
from datetime import datetime, timedelta

from sqlalchemy import text

import common  # noqa: F401  (한국시간 고정)
import store
from db2 import get_engine

URL = "https://apis.data.go.kr/1230000/ao/UsrInfoService02/getDminsttInfo02"
K_PROGRESS = "dminstt_region_progress"
BUDGET = int(os.getenv("DMINSTT_CALL_BUDGET", "600"))
SIDO = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충청북", "충청남", "전라북", "전북", "전라남",
        "경상북", "경상남", "제주", "충북", "충남", "전남", "경북", "경남"]
SHORT = {"충청북": "충북", "충청남": "충남", "전라북": "전북", "전라남": "전남", "경상북": "경북", "경상남": "경남"}


def sido_of(text_):
    t = str(text_ or "")
    for s in SIDO:
        if t.startswith(s) or re.search(rf"(^|\s){s}", t):
            return SHORT.get(s, s)
    return ""


def main():
    from collectors import _g2b_key, _g2b_get, mask_secret
    key = _g2b_key()
    if not key:
        print("[SKIP] 인증키 없음")
        return 0
    with get_engine().begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS dminstt_region (code TEXT PRIMARY KEY, name TEXT, region TEXT, addr TEXT, raw TEXT)"))
    prog, _ = store.load_cache(K_PROGRESS)
    prog = prog if isinstance(prog, dict) else {}
    cur = datetime.strptime(prog.get("next", "20000101"), "%Y%m%d")
    end = datetime.now()
    calls, saved = 0, 0
    step = timedelta(days=int(prog.get("step", 31)))
    while cur <= end and calls < BUDGET:
        nxt = min(cur + step, end)
        page, total = 1, None
        while True:
            q = {"serviceKey": key, "pageNo": str(page), "numOfRows": "999", "type": "json", "inqryDiv": "1",
                 "inqryBgnDt": cur.strftime("%Y%m%d") + "0000", "inqryEndDt": nxt.strftime("%Y%m%d") + "2359"}
            calls += 1
            try:
                r = _g2b_get(URL, q)
                txt = r.text or ""
                if "SERVICE_KEY_IS_NOT_REGISTERED" in txt:
                    print("[FAIL] '조달청_나라장터 사용자정보 서비스' 활용신청이 필요합니다")
                    return 1
                data = r.json()
            except Exception as e:
                print(f"[FAIL] {type(e).__name__}: {mask_secret(e)[:200]}")
                store.save_cache(K_PROGRESS, {**prog, "next": cur.strftime("%Y%m%d")})
                return 1
            err = data.get("nkoneps.com.response.ResponseError")
            if err:
                h = err.get("header") or {}
                if str(h.get("resultCode")) == "07" and step.days > 7:     # 기간이 너무 길면 줄임
                    step = timedelta(days=7)
                    nxt = min(cur + step, end)
                    continue
                print(f"[FAIL] {h.get('resultCode')} {h.get('resultMsg')}")
                return 1
            body = (data.get("response") or {}).get("body") or {}
            items = body.get("items") or []
            if isinstance(items, dict):
                items = items.get("item") or []
            if isinstance(items, dict):
                items = [items]
            total = int(body.get("totalCount") or 0)
            rows = []
            for it in items:
                code = it.get("dminsttCd") or it.get("insttCd") or ""
                if not code:
                    continue
                addr = " ".join(str(it.get(k) or "") for k in it if re.search(r"(adrs|addr|Adrs|rgn|Rgn)", k))
                rows.append({"code": code, "name": it.get("dminsttNm") or it.get("insttNm") or "",
                             "region": sido_of(addr) or sido_of(it.get("dminsttNm")), "addr": addr[:200],
                             "raw": json.dumps(it, ensure_ascii=False)[:2000]})
            if rows:
                with get_engine().begin() as conn:
                    conn.execute(text("INSERT INTO dminstt_region (code, name, region, addr, raw) VALUES (:code, :name, :region, :addr, :raw) "
                                      "ON CONFLICT (code) DO UPDATE SET name=excluded.name, region=excluded.region, addr=excluded.addr, raw=excluded.raw"), rows)
                saved += len(rows)
            if page == 1 and calls <= 2:
                print("예시:", json.dumps(items[:1], ensure_ascii=False)[:600])
            if page * 999 >= total or not items:
                break
            page += 1
        cur = nxt + timedelta(days=1)
        prog = {"next": cur.strftime("%Y%m%d"), "step": step.days, "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        store.save_cache(K_PROGRESS, prog)
    print(f"[OK] 수요기관 지역: API {calls}회 · {saved}곳 저장 · 다음 시작 {prog.get('next')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
