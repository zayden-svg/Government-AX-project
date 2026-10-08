# integrated_export.py
# 업체 중심 통합 엑셀: 낙찰 + 계약 + 쇼핑몰 납품(솔루션) 을 한 표로
#   · 업체검색: 업체명 일부 입력 → 그 업체가 진행 중·진행했던 사업/납품 전부
#   · 경쟁사 수주 현황 · API 관련 사업 수주 업체 · 대학 사업 현황(국립/사립) · 영업 파트너 후보
# 실행: python integrated_export.py --out integrated.xlsx [--dlvr-cache raw_cache.json]
import argparse
import io
import json
import re
import sys
from datetime import datetime

import pandas as pd
from sqlalchemy import text

import common  # noqa: F401  (한국시간 고정)
from common import solution_hits, is_competitor_match, detect_regions
import contract_export as ce
import store
from db2 import get_engine

MY_REGIONS = {"서울", "인천", "강원", "전북", "전남", "광주", "제주"}
API_RE = re.compile(r"(API|에이피아이|게이트웨이|gateway|오픈\s*API|MSA|마이크로서비스|트래픽\s*(제어|관리)|대기열|유량\s*제어)", re.I)
UNIV_RE = re.compile(r"(대학교|대학원|산학협력단|교육대학|한국과학기술원|광주과학기술원|대구경북과학기술원|울산과학기술원|KAIST|GIST|DGIST|UNIST|폴리텍|대학\b|대학$)")
NATIONAL_UNIV = [
    "서울대학교", "부산대학교", "경북대학교", "전남대학교", "전북대학교", "충남대학교", "충북대학교", "강원대학교", "경상국립대학교",
    "제주대학교", "부경대학교", "한국해양대학교", "목포해양대학교", "목포대학교", "순천대학교", "군산대학교", "안동대학교",
    "창원대학교", "공주대학교", "금오공과대학교", "한밭대학교", "한국교통대학교", "강릉원주대학교", "한경국립대학교", "한경대학교",
    "한국교원대학교", "한국체육대학교", "서울과학기술대학교", "한국방송통신대학교", "인천대학교", "서울시립대학교",
    "교육대학교", "한국과학기술원", "광주과학기술원", "대구경북과학기술원", "울산과학기술원", "KAIST", "GIST", "DGIST", "UNIST", "한국전통문화대학교", "한국예술종합학교", "국립",
    "한국폴리텍", "한국기술교육대학교", "한국농수산대학교", "경찰대학", "육군사관학교", "해군사관학교", "공군사관학교", "국군간호사관학교",
]
COMMON_COLS = ["출처", "구분", "상태", "남은일수", "사업명", "제품", "사업명·제품", "수요기관", "대학", "지역", "담당지역", "업체명", "대표업체",
               "금액", "일자", "종료일", "종료추정", "자사관련", "자사관련단어", "경쟁사", "API관련", "원문", "검색용업체명", "_gk", "_key"]


UNIV_EXCLUDE = re.compile(r"(병원|협의회|협회|진흥원|연구재단|장학재단|해양과학기술원|부설)")


def univ_type(name):
    n = str(name or "")
    if not UNIV_RE.search(n) or UNIV_EXCLUDE.search(n):
        return ""
    return "국립·공립" if any(k in n for k in NATIONAL_UNIV) else "사립"


def _status(end, today):
    if pd.isna(end):
        return "종료일 미상", None
    d = (end - today).days
    return ("완료" if d < 0 else "곧 완료" if d <= ce.SOON_DAYS else "진행중"), d


def _common(df, comp):
    """공통 칼럼 채우기 (각 출처에서 사업명·수요기관·업체목록·금액·일자·종료일을 만든 뒤 호출)"""
    today = pd.Timestamp(datetime.now().date())
    if "제품" not in df:
        df["제품"] = ""
    df["사업명·제품"] = (df["사업명"].astype(str) + " " + df["제품"].astype(str)).str.strip()
    end = pd.to_datetime(df["종료일"], errors="coerce")
    st = [_status(e, today) for e in end]
    df["상태"] = [s for s, _ in st]
    df["남은일수"] = [d for _, d in st]
    df["업체명"] = df["업체목록"].map(lambda xs: " / ".join(xs))
    df["대표업체"] = df["업체목록"].map(lambda xs: xs[0] if xs else "")
    df["검색용업체명"] = df["업체목록"].map(ce.search_text)
    df["_gk"] = df["대표업체"].map(ce.group_key)
    df["대학"] = df["수요기관"].map(univ_type)
    regs = df["수요기관"].map(lambda n: detect_regions(n))
    df["지역"] = regs.map(lambda r: "·".join(r))
    df["담당지역"] = regs.map(lambda r: "Y" if set(r) & MY_REGIONS else "")
    hits = df["사업명·제품"].map(solution_hits)
    df["자사관련단어"] = hits.map(lambda h: ", ".join(dict.fromkeys(h)) if h else "")
    df["자사관련"] = (df["자사관련단어"] != "").map({True: "Y", False: ""})
    df["경쟁사"] = df["업체명"].map(lambda c: "Y" if is_competitor_match(c, comp) else "")
    df["API관련"] = df["사업명·제품"].map(lambda t: "Y" if API_RE.search(str(t)) else "")
    return df


def load_contracts(comp):
    raw = ce.load_rows()
    if raw.empty:
        return pd.DataFrame(columns=COMMON_COLS), set()
    d = ce.prepare(raw, competitors=comp)
    out = pd.DataFrame({
        "출처": "계약", "구분": d["biz_type"], "사업명": d["title"], "제품": "", "수요기관": d["수요기관"],
        "업체목록": d["업체목록"], "금액": d["계약금액"], "일자": d["cntrct_date"], "종료일": d["end_date"],
        "종료추정": d["end_est"], "원문": d["url"], "_key": "계약|" + d["uniq_key"],
    })
    ntce = {str(x).split("-")[0] for x in d["ntce_no"] if str(x)}
    return _common(out, comp), ntce


def load_awards(comp, skip_bid_nos):
    try:
        with get_engine().begin() as conn:
            a = pd.read_sql(text("SELECT * FROM g2b_awards"), conn).fillna("")
    except Exception:
        return pd.DataFrame(columns=COMMON_COLS)
    if a.empty:
        return pd.DataFrame(columns=COMMON_COLS)
    # 같은 공고가 계약으로도 들어와 있으면 계약 쪽을 씀 (계약기간이 정확함)
    a = a[~a["bid_no"].isin(skip_bid_nos)]
    a = a.sort_values("updated_at", ascending=False).drop_duplicates(subset=["bid_no", "bid_ord", "winner"], keep="first")
    out = pd.DataFrame({
        "출처": "낙찰", "구분": a["biz_type"], "사업명": a["title"], "제품": "",
        "수요기관": [d or n for d, n in zip(a["dminstt"], a["ntce_instt"])],
        "업체목록": a["winner"].map(lambda w: [w] if w else []), "금액": a["amount"].map(ce._won),
        "일자": a["event_date"], "종료일": a["end_date"], "종료추정": "Y", "원문": a["url"], "_key": "낙찰|" + a["uniq_key"],
    })
    return _common(out, comp)


def load_dlvr(comp, cache_path):
    import dlvr_export as de
    import os
    if not os.path.exists(cache_path):
        return pd.DataFrame(columns=COMMON_COLS)
    de.OFFLINE = True
    de.CACHE_FILE = cache_path
    rows = de.collect()
    if not rows:
        return pd.DataFrame(columns=COMMON_COLS)
    d = pd.DataFrame(rows).fillna("")
    d["_chg"] = pd.to_numeric(d.get("dlvrReqChgOrd", 0), errors="coerce").fillna(0)
    d = d.sort_values("_chg", ascending=False).drop_duplicates(subset=["dlvrReqNo", "prdctSno", "prdctIdntNo"], keep="first")
    spec = d["prdctIdntNoNm"].astype(str)
    out = pd.DataFrame({
        "출처": "쇼핑몰 납품", "구분": "쇼핑몰(" + d["_구분"].astype(str) + ")",
        "사업명": d["dlvrReqNm"].astype(str),
        "제품": spec.map(lambda s: " · ".join([x.strip() for x in s.split(",")[1:4]]) or s),
        "수요기관": d["dminsttNm"], "업체목록": d["corpNm"].map(lambda w: [w] if w else []), "금액": d["prdctAmt"].map(ce._won),
        "일자": d["dlvrReqRcptDate"].map(ce._date),
        # 솔루션 납품은 '납품일 + 1년'을 유지보수·재구매 시점으로 봄 (추정)
        "종료일": (pd.to_datetime(d["dlvrReqRcptDate"].map(ce._date), errors="coerce") + pd.Timedelta(days=365)).dt.strftime("%Y-%m-%d"),
        "종료추정": "Y", "원문": "", "_key": "납품|" + d["dlvrReqNo"].astype(str) + "|" + d["prdctSno"].astype(str),
    })
    out = _common(out, comp)
    # 경쟁사·비교군 '제품' 납품은 판매업체가 리셀러여도 경쟁사 수주로 표시
    kind = d["_구분"].tolist()
    out["경쟁사"] = ["Y" if (c == "Y" or k in ("경쟁사",)) else "" for c, k in zip(out["경쟁사"], kind)]
    out["API관련"] = ["Y" if (a == "Y" or "API" in str(f)) else "" for a, f in zip(out["API관련"], d["_제품군"])]
    out.loc[[k == "자사" for k in kind], "자사관련"] = "Y"
    return out


def build(all_df):
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    order = {"곧 완료": 0, "진행중": 1, "종료일 미상": 2, "완료": 3}
    d = all_df.assign(_o=all_df["상태"].map(order), _dd=pd.to_datetime(all_df["일자"], errors="coerce"))
    d = d.sort_values(["_o", "_dd"], ascending=[True, False]).drop(columns=["_o", "_dd"]).reset_index(drop=True)
    wb = Workbook(write_only=True)
    navy = PatternFill("solid", fgColor="1B2A4A")
    hf = Font(bold=True, color="FFFFFF")
    bold = Font(bold=True)
    big = Font(bold=True, size=16)
    yellow = PatternFill("solid", fgColor="FFF59D")

    def c(ws, v, font=None, fill=None, fmt=None):
        x = WriteOnlyCell(ws, value=v)
        if font:
            x.font = font
        if fill:
            x.fill = fill
        if fmt:
            x.number_format = fmt
        return x

    VIEW = [("출처", "출처", 9), ("상태", "상태", 9), ("남은일수", "남은일수", 8), ("구분", "구분", 10),
            ("사업명 (누르면 원문)", "사업명", 46), ("제품", "제품", 30), ("업체명", "업체명", 26), ("수요기관", "수요기관", 26),
            ("대학", "대학", 8), ("지역", "지역", 10), ("담당지역", "담당지역", 7), ("금액(억)", "금액", 10), ("일자", "일자", 11),
            ("종료일", "종료일", 11), ("종료 추정", "종료추정", 7), ("자사관련", "자사관련", 7), ("자사관련 단어", "자사관련단어", 14),
            ("경쟁사", "경쟁사", 7), ("API 관련", "API관련", 7)]
    EOK = '#,##0.0"억"'

    def eok(v):
        return None if v is None or (isinstance(v, float) and pd.isna(v)) or v == "" else round(float(v) / 1e8, 2)

    def link(url, title):
        t = str(title or "").replace('"', '""')[:250]
        return f'=HYPERLINK("{url}","{t}")' if str(url).startswith("http") else str(title or "")

    def table(name, sub, helper=False):
        ws = wb.create_sheet(name)
        for i, (_, _, w) in enumerate(VIEW, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "F2"
        titles = [t for t, _, _ in VIEW] + (["원문 주소", "검색용 업체명(자동)", "검색키(수정 금지)"] if helper else [])
        ws.append([c(ws, t, hf, navy) for t in titles])
        scol = get_column_letter(len(VIEW) + 2)
        for rn, r in enumerate(sub.to_dict("records"), 2):
            row = []
            for t, k, _ in VIEW:
                v = r.get(k, "")
                if k == "금액":
                    row.append(c(ws, eok(v), fmt=EOK))
                    continue
                if k == "남은일수":
                    v = None if v is None or pd.isna(v) else int(v)
                if k == "사업명":
                    v = link(r.get("원문", ""), v)
                row.append(v)
            if helper:
                row += [r.get("원문", ""), r.get("검색용업체명", ""),
                        f'=IF(업체검색!$F$3="","",IF(ISNUMBER(SEARCH(업체검색!$F$3,{scol}{rn})),ROW(),""))']
            ws.append(row)
        ws.auto_filter.ref = f"A1:{get_column_letter(len(titles))}{max(len(sub) + 1, 2)}"

    def summary(sub):
        if sub.empty:
            return pd.DataFrame()
        g = sub.groupby("_gk")
        s = pd.DataFrame({
            "업체명": g["대표업체"].agg(lambda x: x.value_counts().index[0]),
            "전체 건수": g.size(),
            "진행중": g["상태"].agg(lambda x: int((x == "진행중").sum())),
            "곧 완료": g["상태"].agg(lambda x: int((x == "곧 완료").sum())),
            "대학(진행중·곧완료)": g.apply(lambda x: int(((x["대학"] != "") & x["상태"].isin(["진행중", "곧 완료"])).sum())),
            "담당지역(진행중·곧완료)": g.apply(lambda x: int(((x["담당지역"] == "Y") & x["상태"].isin(["진행중", "곧 완료"])).sum())),
            "API 관련": g["API관련"].agg(lambda x: int((x == "Y").sum())),
            "자사 관련": g["자사관련"].agg(lambda x: int((x == "Y").sum())),
            "금액 합계(억)": g["금액"].sum(min_count=1),
            "최근 일자": g["일자"].max(),
            "주요 기관": g["수요기관"].agg(lambda x: ", ".join(x.value_counts().index[:3])),
            "출처 구성": g["출처"].agg(lambda x: " · ".join(f"{k} {v}" for k, v in x.value_counts().items())),
            "경쟁사": g["경쟁사"].agg(lambda x: "Y" if (x == "Y").any() else ""),
            "다른 표기": g["대표업체"].agg(lambda x: ", ".join(list(x.value_counts().index[1:4]))),
        })
        return s.sort_values(["진행중", "전체 건수"], ascending=False)

    def summary_sheet(name, s, note=""):
        ws = wb.create_sheet(name)
        cols = list(s.columns) if not s.empty else ["업체명"]
        widths = {"업체명": 28, "주요 기관": 46, "출처 구성": 22, "다른 표기": 30, "금액 합계(억)": 12}
        for i, k in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = widths.get(k, 10)
        if note:
            ws.append([c(ws, note, bold)])
        ws.append([c(ws, k, hf, navy) for k in cols])
        for r in s.to_dict("records"):
            ws.append([c(ws, eok(r[k]), fmt=EOK) if k == "금액 합계(억)" else r[k] for k in cols])
        ws.freeze_panes = "B3" if note else "B2"

    comp_rows = d[d["경쟁사"] == "Y"]
    api_rows = d[d["API관련"] == "Y"]
    univ_rows = d[d["대학"] != ""]
    summ = summary(d)
    partner = summ[(summ["경쟁사"] == "") & ((summ["대학(진행중·곧완료)"] > 0) | (summ["담당지역(진행중·곧완료)"] > 0))].copy() if not summ.empty else summ
    if not partner.empty:
        partner = partner.sort_values(["대학(진행중·곧완료)", "담당지역(진행중·곧완료)", "API 관련", "진행중"], ascending=False).head(300)

    # 안내
    ws = wb.create_sheet("안내")
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 24, 100
    n = d["출처"].value_counts()
    rows = [
        (c(ws, "공공 IT 수주 통합 현황 (업체 중심)", big), ""), ("기준일", datetime.now().strftime("%Y-%m-%d")),
        ("출처", f"조달청 나라장터 낙찰 {int(n.get('낙찰', 0)):,}건 · 계약 {int(n.get('계약', 0)):,}건 · 쇼핑몰 납품(자사·경쟁사 솔루션) {int(n.get('쇼핑몰 납품', 0)):,}건"),
        ("범위", "IT 관련만 (정보화사업 표시 또는 사업명에 IT·자사 관련 단어). 같은 공고가 낙찰·계약 둘 다 있으면 계약만 남김"),
        ("", ""), (c(ws, "시트", hf, navy), c(ws, "내용", hf, navy)),
        ("업체검색", "B3에 업체명 일부 입력 → 그 업체의 낙찰·계약·납품 전부 (진행중 먼저). (주)·띄어쓰기·한글/영문 표기 차이 무시"),
        ("업체별 요약", "업체마다 진행중·곧 완료 건수, 대학·담당지역 사업 수, API·자사 관련, 금액, 주요 기관"),
        ("영업 파트너 후보", "경쟁사가 아니면서 대학·담당지역에서 진행 중인 사업이 많은 업체 (함께 영업하기 유리한 업체)"),
        ("경쟁사 수주", "경쟁사가 따낸 사업 + 경쟁사 제품이 납품된 기관"),
        ("API 관련", "API·게이트웨이·트래픽 제어·대기열 관련 사업과 수주 업체"),
        ("대학 사업", f"대학(국립·공립 / 사립) 수요기관 사업 {len(univ_rows):,}건 + 대학별 요약"),
        ("통합 내역", "전체 (머리글 ▼ 필터로 검색 가능)"),
        ("", ""),
        ("상태 기준", f"곧 완료 = 종료일까지 {ce.SOON_DAYS}일 이내 · 낙찰은 종료일 정보가 없어 '낙찰일 + 1년' 추정 · 쇼핑몰 납품은 '납품일 + 1년'(유지보수 시점) 추정"),
        ("한계", "사립대가 자체 입찰·계약(학교 홈페이지)으로 진행한 사업은 나라장터에 없어 빠짐 → 대학 홈페이지 입찰 게시판 수집으로 보완 예정"),
    ]
    for a, b in rows:
        ws.append([a if not isinstance(a, str) else c(ws, a, bold), b])

    # 업체검색
    ws = wb.create_sheet("업체검색")
    show = ["출처", "상태", "남은일수", "구분", "사업명 (누르면 원문)", "제품", "업체명", "수요기관", "대학", "지역", "금액(억)", "일자", "종료일", "경쟁사", "API 관련"]
    letters = {t: get_column_letter(i + 1) for i, (t, _, _) in enumerate(VIEW)}
    ucol = get_column_letter(len(VIEW) + 1)
    for i, w in enumerate([9, 9, 8, 10, 46, 30, 26, 26, 8, 10, 10, 11, 11, 7, 7], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    hcol = get_column_letter(len(VIEW) + 3)
    ws.append([c(ws, "업체 검색", big)])
    ws.append(["노란 칸(B3)에 업체명 일부를 넣으세요. (주)·주식회사·띄어쓰기·대소문자·한글/영문 표기 차이는 무시합니다. 최대 1,000건 (진행중 먼저). 사업명을 누르면 원문이 열립니다."])
    ws.append([c(ws, "업체명 →", bold), c(ws, "", fill=yellow), c(ws, "결과 건수", bold),
               f"=IF(F3=\"\",\"\",COUNT('통합 내역'!{hcol}:{hcol}))", c(ws, "인식한 검색어", bold), f'={ce.excel_norm_formula("B3")}'])
    ws.append([c(ws, t, hf, navy) for t in show])
    T = "'통합 내역'"
    for k in range(1, 1001):
        row = []
        pos = f"SMALL({T}!${hcol}:${hcol},{k})"
        for t in show:
            col = letters[t]
            if t.startswith("사업명"):
                f = f'=IFERROR(IF(INDEX({T}!{ucol}:{ucol},{pos})="",INDEX({T}!{col}:{col},{pos})&"",HYPERLINK(INDEX({T}!{ucol}:{ucol},{pos}),INDEX({T}!{col}:{col},{pos})&"")),"")'
                row.append(f)
                continue
            tail = "" if t in ("남은일수", "금액(억)") else '&""'
            f = f"=IFERROR(INDEX({T}!{col}:{col},{pos}){tail},\"\")"
            row.append(c(ws, f, fmt=EOK) if t == "금액(억)" else f)
        ws.append(row)

    summary_sheet("업체별 요약", summ)
    summary_sheet("영업 파트너 후보", partner, "경쟁사 제외 · 대학/담당지역에서 진행 중인 사업이 많은 순 (상위 300)")
    table("경쟁사 수주", comp_rows)
    summary_sheet("API 관련 업체", summary(api_rows), "API·게이트웨이·트래픽 제어·대기열 관련 사업을 수주한 업체")
    table("API 관련 사업", api_rows)
    # 대학별 요약
    if not univ_rows.empty:
        g = univ_rows.groupby("수요기관")
        us = pd.DataFrame({
            "대학": g["대학"].first(), "IT 사업 수": g.size(),
            "진행중·곧완료": g["상태"].agg(lambda x: int(x.isin(["진행중", "곧 완료"]).sum())),
            "금액 합계(억)": g["금액"].sum(min_count=1), "최근 일자": g["일자"].max(),
            "주요 업체": g["대표업체"].agg(lambda x: ", ".join(x.value_counts().index[:4])),
            "자사 관련": g["자사관련"].agg(lambda x: int((x == "Y").sum())),
            "경쟁사": g["경쟁사"].agg(lambda x: int((x == "Y").sum())),
        }).reset_index().rename(columns={"수요기관": "업체명"}).sort_values(["진행중·곧완료", "IT 사업 수"], ascending=False)
        us = us.rename(columns={"업체명": "대학(수요기관)"})
        ws = wb.create_sheet("대학별 요약")
        cols = list(us.columns)
        for i, k in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = {"대학(수요기관)": 34, "주요 업체": 50, "금액 합계(억)": 12}.get(k, 10)
        ws.append([c(ws, k, hf, navy) for k in cols])
        for r in us.to_dict("records"):
            ws.append([c(ws, eok(r[k]), fmt=EOK) if k == "금액 합계(억)" else r[k] for k in cols])
        ws.freeze_panes = "B2"
    table("대학 사업", univ_rows)
    table("통합 내역", d, helper=True)
    buf = io.BytesIO()
    wb.save(buf)
    meta = {"rows": len(d), "by_source": {k: int(v) for k, v in n.items()}, "companies": int(len(summ)),
            "univ_rows": len(univ_rows), "univ_private": int((univ_rows["대학"] == "사립").sum()),
            "competitor_rows": len(comp_rows), "api_rows": len(api_rows), "partners": int(len(partner))}
    return buf.getvalue(), meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="integrated.xlsx")
    ap.add_argument("--dlvr-cache", default="raw_cache.json")
    a = ap.parse_args()
    comp = store.load_competitors()[0]
    cons, ntce = load_contracts(comp)
    awards = load_awards(comp, ntce)
    dlvr = load_dlvr(comp, a.dlvr_cache)
    parts = [x for x in (cons, awards, dlvr) if not x.empty]
    if not parts:
        print("[SKIP] 자료 없음")
        return 1
    all_df = pd.concat(parts, ignore_index=True)
    data, meta = build(all_df)
    open(a.out, "wb").write(data)
    import base64
    fname = f"공공IT_수주통합현황_{datetime.now().strftime('%Y%m%d')}.xlsx"
    try:
        store.save_cache("integrated_excel", {"filename": fname, "b64": base64.b64encode(data).decode(), **meta})
        store.save_cache("integrated_excel_meta", {"filename": fname, "date": datetime.now().strftime("%Y-%m-%d"), "size": len(data), **meta})
    except Exception as e:
        print(f"[WARN] 대시보드용 저장 실패: {type(e).__name__}")
    print(f"[OK] 통합 엑셀: {len(data) / 1e6:.1f}MB · {json.dumps(meta, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
