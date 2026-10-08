# dlvr_export.py
# 조달청 쇼핑몰(디지털서비스몰·종합쇼핑몰) 납품요구 수집 → "어느 기관이 언제 무엇을 샀나" 엑셀
#   · 자사(NetFUNNEL·NFA·MBUSTER)·경쟁사(xQueue·DynaPath·에버세이프 등)·비교군 제품의 기관 구매 이력
#   · API: 조달청_나라장터쇼핑몰 품목정보 서비스 / getDlvrReqDtlInfoList (납품요구 상세)
#   · 공공데이터포털 활용신청 필요(자동승인). 하루 1,000회 한도 — 이 수집은 수십~수백 회면 끝남
#
# 실행: python dlvr_export.py --out dlvr.xlsx [--years 4]
import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import unquote

import pandas as pd
import requests

BASE = "https://apis.data.go.kr/1230000/at/ShoppingMallPrdctInfoService"
OP_DTL = f"{BASE}/getDlvrReqDtlInfoList"
ROWS = 999

# 디지털서비스몰 계약상품정보(2026-03 추출본)에서 찾은 계약번호 — (계약번호, 구분, 제품군, 제조사, 제품)
TARGET_CONTRACTS = [
    ("002150153", "자사", "NF", "에스티씨랩", "NetFUNNEL v3.0"),
    ("002460509", "자사", "NF", "에스티씨랩", "NetFUNNEL v3.0"),
    ("R26TA01343941", "자사", "NF", "에스티씨랩", "NetFUNNEL v4.0"),
    ("R26TA01375633", "자사", "NFA", "에스티씨랩(에티버스이피에이 판매)", "API NetFUNNEL v4.0"),
    ("002460506", "자사", "BM/MB", "에스티씨랩", "MBUSTER v2.0"),
    ("002461613", "경쟁사", "대기열·유량제어", "소프트베이스", "xQueue v1.0"),
    ("002461139", "경쟁사", "봇·매크로 차단", "스크립터스", "DynaPath 3.1"),
    ("R25TA00251499", "경쟁사", "봇·매크로 차단", "에버스핀", "에버세이프 웹 보안솔루션 V4.2.1"),
    ("R25TA00145277", "경쟁사", "앱 위변조 방지", "에버스핀", "에버세이프 v2.0"),
    ("002161010", "경쟁사", "앱 위변조 방지", "에버스핀(가온아이 판매)", "에버세이프 v2.0"),
    ("R25TA00466390", "비교군", "봇·매크로 차단", "네스토리", "BotfenderAI V2.0"),
    ("002162591", "비교군", "API 게이트웨이", "메타빌드", "MESIM APIG v1.5"),
    ("002462107", "비교군", "API 게이트웨이", "메타빌드", "MESIM APIG v1.5"),
    ("002261452", "비교군", "API 게이트웨이", "이데아텍", "i-ONE API Gateway v1.2"),
    ("R25TA01041570", "비교군", "API 게이트웨이", "메가투스", "Megaapim V1.0"),
]
# 계약번호로 못 잡는 예전·다른 계약을 보완: 물품규격명(제품명) 검색
TARGET_NAMES = [
    ("NetFUNNEL", "자사", "NF"), ("넷퍼넬", "자사", "NF"), ("MBUSTER", "자사", "BM/MB"), ("엠버스터", "자사", "BM/MB"),
    ("xQueue", "경쟁사", "대기열·유량제어"), ("DynaPath", "경쟁사", "봇·매크로 차단"), ("에버세이프", "경쟁사", "봇·앱 보안"),
    ("BotfenderAI", "비교군", "봇·매크로 차단"),
]
MY_REGIONS = ["서울", "인천", "강원", "전북", "전남", "광주", "제주"]   # 담당 지역 표시용


def _key():
    k = (os.getenv("G2B_SERVICE_KEY") or "").strip()
    return unquote(k) if "%" in k else k


def mask(s):
    return re.sub(r"(serviceKey|key)=[^&\s'\"]+", r"\1=***", str(s))


class ApiError(Exception):
    pass


CALLS = {"n": 0}


def call(params):
    q = {"serviceKey": _key(), "type": "json", "numOfRows": str(ROWS), **params}
    last = "429 반복"
    for attempt in range(4):
        CALLS["n"] += 1
        try:
            r = requests.get(OP_DTL, params=q, headers={"User-Agent": "Mozilla/5.0"}, timeout=(15, 60))
        except requests.RequestException as e:
            time.sleep(5 * (attempt + 1))
            last = e
            continue
        if r.status_code == 429:
            time.sleep(30 * (attempt + 1))
            continue
        txt = r.text or ""
        if "SERVICE_KEY_IS_NOT_REGISTERED" in txt:
            raise ApiError("활용신청이 아직 승인되지 않음 (조달청_나라장터쇼핑몰 품목정보 서비스)")
        if "LIMITED_NUMBER" in txt:
            raise ApiError("하루 호출 한도 초과")
        try:
            data = r.json()
        except ValueError:
            raise ApiError(mask(txt[:200]))
        root = data.get("response") or {}
        head = root.get("header") or {}
        if str(head.get("resultCode", "00")) not in ("00", "0"):
            raise ApiError(f"{head.get('resultCode')} {head.get('resultMsg')}")
        body = root.get("body") or {}
        items = body.get("items") or []
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        return items, int(body.get("totalCount") or 0)
    raise ApiError(f"접속 실패: {mask(last)}")


def fetch_all(params):
    out, page = [], 1
    while True:
        items, total = call({**params, "pageNo": str(page)})
        out.extend(items)
        if not items or len(out) >= total or len(items) < ROWS:
            return out
        page += 1


def year_chunks(years):
    end = datetime.now()
    cur = end - timedelta(days=365 * years)
    while cur < end:
        nxt = min(cur + timedelta(days=364), end)
        yield cur.strftime("%Y%m%d"), nxt.strftime("%Y%m%d")
        cur = nxt + timedelta(days=1)


def collect(years=4):
    rows = []
    log = []
    for no, kind, fam, maker, prod in TARGET_CONTRACTS:
        n0 = len(rows)
        for b, e in year_chunks(years):
            for it in fetch_all({"inqryDiv": "1", "inqryBgnDate": b, "inqryEndDate": e, "cntrctNo": no}):
                rows.append({**it, "_구분": kind, "_제품군": fam, "_제조사": maker, "_대상제품": prod, "_찾은방법": f"계약번호 {no}"})
        log.append(f"계약 {no} {prod}: {len(rows) - n0}건")
    for nm, kind, fam in TARGET_NAMES:
        n0 = len(rows)
        for b, e in year_chunks(years):
            for it in fetch_all({"inqryDiv": "1", "inqryBgnDate": b, "inqryEndDate": e, "prdctIdntNoNm": nm}):
                rows.append({**it, "_구분": kind, "_제품군": fam, "_제조사": "", "_대상제품": nm, "_찾은방법": f"제품명 '{nm}'"})
        log.append(f"제품명 {nm}: {len(rows) - n0}건")
    print("\n".join(log))
    return rows


def _num(v):
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _date(v):
    s = re.sub(r"[^0-9]", "", str(v or ""))[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else ""


def build(rows):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    df = pd.DataFrame(rows).fillna("")
    if df.empty:
        raise SystemExit("[SKIP] 납품요구 결과 0건")
    # 같은 납품요구·물품은 최신 변경차수만
    df["_chg"] = pd.to_numeric(df.get("dlvrReqChgOrd", 0), errors="coerce").fillna(0)
    df = (df.sort_values("_chg", ascending=False)
            .drop_duplicates(subset=["dlvrReqNo", "prdctSno", "prdctIdntNo"], keep="first"))
    df["납품요구일"] = df["dlvrReqRcptDate"].map(_date)
    df["금액"] = df["prdctAmt"].map(_num)
    df["유지보수·재구매 예상"] = (pd.to_datetime(df["납품요구일"], errors="coerce") + pd.Timedelta(days=365)).dt.strftime("%Y-%m")
    df["담당지역"] = df["dminsttRgnNm"].map(lambda r: "Y" if any(k in str(r) for k in MY_REGIONS) else "")
    order = {"자사": 0, "경쟁사": 1, "비교군": 2}
    df = df.assign(_o=df["_구분"].map(order)).sort_values(["_o", "납품요구일"], ascending=[True, False])

    cols = [("구분", "_구분", 7), ("제품군", "_제품군", 14), ("납품요구일", "납품요구일", 11), ("수요기관", "dminsttNm", 30),
            ("기관구분", "dmndInsttDivNm", 10), ("지역", "dminsttRgnNm", 14), ("담당지역", "담당지역", 7),
            ("납품요구건명", "dlvrReqNm", 40), ("물품규격(제품)", "prdctIdntNoNm", 50), ("수량", "prdctQty", 6),
            ("금액(원)", "금액", 13), ("판매업체", "corpNm", 20), ("납품기한", "dlvrTmlmtDate", 11),
            ("유지보수·재구매 예상", "유지보수·재구매 예상", 12), ("납품요구번호", "dlvrReqNo", 14), ("계약번호", "cntrctNo", 14),
            ("찾은 방법", "_찾은방법", 18)]
    wb = Workbook()
    navy = PatternFill("solid", fgColor="1B2A4A")
    hf = Font(bold=True, color="FFFFFF")

    def sheet(ws, d, cdefs):
        ws.append([t for t, _, _ in cdefs])
        for i, (_, _, w) in enumerate(cdefs, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
            ws.cell(1, i).font, ws.cell(1, i).fill = hf, navy
        for r in d.to_dict("records"):
            ws.append([(_date(r.get(c)) if c == "dlvrTmlmtDate" else r.get(c, "")) for _, c, _ in cdefs])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cdefs))}{len(d) + 1}"
        amt_i = [t for t, _, _ in cdefs].index("금액(원)") + 1 if "금액(원)" in [t for t, _, _ in cdefs] else None
        if amt_i:
            for row in ws.iter_rows(min_row=2, min_col=amt_i, max_col=amt_i):
                for c in row:
                    c.number_format = "#,##0"

    # 기관별 요약
    g = df.groupby(["_구분", "dminsttNm"])
    summ = pd.DataFrame({
        "제품": g["_대상제품"].agg(lambda s: ", ".join(dict.fromkeys(s))),
        "구매 건수": g["dlvrReqNo"].nunique(),
        "금액 합계(원)": g["금액"].sum(min_count=1),
        "최근 구매일": g["납품요구일"].max(),
        "지역": g["dminsttRgnNm"].first(),
        "담당지역": g["담당지역"].first(),
        "판매업체": g["corpNm"].agg(lambda s: ", ".join(dict.fromkeys(s))),
    }).reset_index().rename(columns={"_구분": "구분", "dminsttNm": "수요기관"})
    summ["다음 접촉 시점"] = (pd.to_datetime(summ["최근 구매일"], errors="coerce") + pd.Timedelta(days=300)).dt.strftime("%Y-%m")
    summ = summ.assign(_o=summ["구분"].map(order)).sort_values(["_o", "최근 구매일"], ascending=[True, False]).drop(columns="_o")

    ws = wb.active
    ws.title = "안내"
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 22, 100
    info = [
        ("쇼핑몰 납품요구 현황 (자사·경쟁사 제품)", ""),
        ("기준일", datetime.now().strftime("%Y-%m-%d")),
        ("출처", "조달청_나라장터쇼핑몰 품목정보 서비스 · 납품요구 상세 (공공데이터포털 OpenAPI)"),
        ("대상", "디지털서비스몰에 등록된 자사·경쟁사·비교군 제품의 계약번호 + 제품명 검색"),
        ("", ""),
        ("시트", "내용"),
        ("기관별 요약", "기관마다 산 제품·건수·금액·최근 구매일 → 다음 접촉 시점(최근 구매 + 10개월)"),
        ("경쟁사 고객", "경쟁사·비교군 제품을 산 기관 = 교체 영업 대상"),
        ("자사 고객", "자사 제품을 산 기관 = 유지보수·추가 구매 대상"),
        ("전체 내역", "납품요구 한 건 한 건 (변경차수는 최신만)"),
        ("", ""),
        ("참고", "유지보수·재구매 예상 = 구매일 + 1년 (추정). 담당지역 = 서울·인천·강원·전라·광주·제주"),
    ]
    for a, b in info:
        ws.append([a, b])
    ws["A1"].font = Font(bold=True, size=15)
    for c in ("A6", "B6"):
        ws[c].font, ws[c].fill = hf, navy

    scols = [("구분", "구분", 7), ("수요기관", "수요기관", 32), ("제품", "제품", 36), ("구매 건수", "구매 건수", 8),
             ("금액(원)", "금액 합계(원)", 14), ("최근 구매일", "최근 구매일", 11), ("다음 접촉 시점", "다음 접촉 시점", 12),
             ("지역", "지역", 14), ("담당지역", "담당지역", 7), ("판매업체", "판매업체", 30)]
    sheet(wb.create_sheet("기관별 요약"), summ, scols)
    sheet(wb.create_sheet("경쟁사 고객"), summ[summ["구분"] != "자사"], scols)
    sheet(wb.create_sheet("자사 고객"), summ[summ["구분"] == "자사"], scols)
    sheet(wb.create_sheet("전체 내역"), df, cols)
    buf = io.BytesIO()
    wb.save(buf)
    meta = {"rows": len(df), "insttn": int(summ["수요기관"].nunique()),
            "own": int((summ["구분"] == "자사").sum()), "comp": int((summ["구분"] != "자사").sum())}
    return buf.getvalue(), meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dlvr.xlsx")
    ap.add_argument("--years", type=int, default=4)
    a = ap.parse_args()
    try:
        rows = collect(a.years)
    except ApiError as e:
        print(f"[FAIL] {e} · API {CALLS['n']}회")
        return 1
    data, meta = build(rows)
    open(a.out, "wb").write(data)
    print(f"[OK] 납품요구 엑셀: API {CALLS['n']}회 · {json.dumps(meta, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
