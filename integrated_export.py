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
COMMON_COLS = ["출처", "구분", "상태", "남은일수", "사업명", "제품", "사업명·제품", "수요기관", "기관구분", "대학", "지역", "담당지역", "업체명", "대표업체",
               "금액", "일자", "종료일", "종료추정", "자사관련", "자사관련단어", "경쟁사", "API관련", "원문", "검색용업체명", "_gk", "_key"]


SIDO_SHORT = {"서울특별시": "서울", "서울": "서울", "부산광역시": "부산", "부산": "부산", "대구광역시": "대구", "대구": "대구",
              "인천광역시": "인천", "인천": "인천", "광주광역시": "광주", "광주": "광주", "대전광역시": "대전", "대전": "대전",
              "울산광역시": "울산", "울산": "울산", "세종특별자치시": "세종", "세종": "세종", "경기도": "경기", "경기": "경기",
              "강원특별자치도": "강원", "강원도": "강원", "강원": "강원", "충청북도": "충북", "충북": "충북", "충청남도": "충남", "충남": "충남",
              "전북특별자치도": "전북", "전라북도": "전북", "전북": "전북", "전라남도": "전남", "전남": "전남",
              "경상북도": "경북", "경북": "경북", "경상남도": "경남", "경남": "경남", "제주특별자치도": "제주", "제주": "제주"}
CENTRAL_RE = re.compile(r"(부|처|청|위원회|감사원|국회|대법원|헌법재판소|법원행정처|대통령)$|^(기획재정부|교육부|과학기술정보통신부|외교부|통일부|법무부|국방부|행정안전부|문화체육관광부|농림축산식품부|산업통상자원부|보건복지부|환경부|고용노동부|여성가족부|국토교통부|해양수산부|중소벤처기업부)")


def _univ_index():
    """대학알리미 학교개황(data/univ_master.csv): 학교명 → (지역, 설립구분)"""
    import csv
    idx = {}
    try:
        for r in csv.DictReader(open("data/univ_master.csv", encoding="utf-8-sig")):
            nm = re.sub(r"\s+", "", r["학교명"])
            idx.setdefault(nm, (r["지역"], r["설립구분"]))
    except Exception:
        pass
    return dict(sorted(idx.items(), key=lambda x: -len(x[0])))


UNIV_IDX = _univ_index()


def univ_lookup(name):
    n = re.sub(r"\s+", "", str(name or ""))
    for k, v in UNIV_IDX.items():
        if k in n:
            return v
    return None


# 자주 나오는 공공기관 본사 소재지 (조달청 수요기관 지역표가 채워지기 전 임시 보완)
KNOWN_HQ = {"한국지능정보사회진흥원": "대구", "한국항공우주연구원": "대전", "한국건설기술연구원": "경기", "한국교육학술정보원": "대구",
            "한국과학기술정보연구원": "대전", "한국인터넷진흥원": "전남", "국방과학연구소": "대전", "국립보건연구원": "충북",
            "한국생산기술연구원": "충남", "한국지역정보개발원": "서울", "중소벤처기업진흥공단": "경남", "한국고용정보원": "충북",
            "한국교통안전공단": "경북", "각 수요기관": "중앙(전국)"}
CODE_REGION = {}


def load_code_region():
    try:
        with get_engine().begin() as conn:
            rows = conn.execute(text("SELECT code, region FROM dminstt_region WHERE region <> ''")).fetchall()
        CODE_REGION.update({str(c): r for c, r in rows})
    except Exception:
        pass
    print(f"[지역표] 수요기관 코드 {len(CODE_REGION):,}곳")


def resolve_region(name, hint="", code=""):
    """기관 소재 시·도 (짧은 이름). 순서: ① 원자료 지역 ② 대학 목록 ③ 기관명 속 지역명 ④ 중앙부처 ⑤ 미확인"""
    if code and str(code) in CODE_REGION:
        return CODE_REGION[str(code)]
    for k, v in KNOWN_HQ.items():
        if k in str(name or ""):
            return v
    h = str(hint or "").strip()
    if h:
        tok = h.split()[0]
        if tok in SIDO_SHORT:
            return SIDO_SHORT[tok]
    u = univ_lookup(name)
    if u and u[0]:
        return SIDO_SHORT.get(u[0], u[0])
    r = detect_regions(name)
    if r:
        return r[0]
    if CENTRAL_RE.search(str(name or "").strip()):
        return "중앙(전국)"
    return "미확인"


UNIV_EXCLUDE = re.compile(r"(병원|협의회|협회|진흥원|연구재단|장학재단|해양과학기술원|부설)")


def univ_type(name):
    n = str(name or "")
    if not UNIV_RE.search(n) or UNIV_EXCLUDE.search(n):
        return ""
    u = univ_lookup(n)
    if u:
        return "사립" if u[1] == "사립" else "국립·공립"
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
    hints = df["_지역힌트"] if "_지역힌트" in df else pd.Series([""] * len(df), index=df.index)
    codes = df["_기관코드"] if "_기관코드" in df else pd.Series([""] * len(df), index=df.index)
    df["지역"] = [resolve_region(n, h, c) for n, h, c in zip(df["수요기관"], hints, codes)]
    df["담당지역"] = df["지역"].map(lambda r: "Y" if r in MY_REGIONS else "")
    df["기관구분"] = df["대학"].map(lambda u: "대학" if u else "공공")
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
        "_기관코드": d["dminstt_raw"].map(lambda x: (re.findall(r"\[\d+\^([^\^\]]+)\^", str(x)) or [""])[0]),
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
        "_기관코드": a["dminstt_cd"],
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
        "_지역힌트": d["dminsttRgnNm"], "_기관코드": d["dminsttCd"],
    })
    out = _common(out, comp)
    # 경쟁사·비교군 '제품' 납품은 판매업체가 리셀러여도 경쟁사 수주로 표시
    kind = d["_구분"].tolist()
    out["경쟁사"] = ["Y" if (c == "Y" or k in ("경쟁사",)) else "" for c, k in zip(out["경쟁사"], kind)]
    out["API관련"] = ["Y" if (a == "Y" or "API" in str(f)) else "" for a, f in zip(out["API관련"], d["_제품군"])]
    out.loc[[k == "자사" for k in kind], "자사관련"] = "Y"
    return out


def load_univ_bids(comp):
    """사립대·전문대 홈페이지 입찰 게시판에서 모은 글 (univ_crawl.py)"""
    try:
        with get_engine().begin() as conn:
            u = pd.read_sql(text("SELECT * FROM univ_bids"), conn).fillna("")
    except Exception:
        return pd.DataFrame(columns=COMMON_COLS)
    if u.empty:
        return pd.DataFrame(columns=COMMON_COLS)
    ev = pd.to_datetime(u["date"], errors="coerce")
    out = pd.DataFrame({
        "출처": "대학 홈페이지", "구분": "입찰" + u["kind"], "사업명": u["title"], "제품": "",
        "수요기관": u["school"] + u["campus"].map(lambda c: "" if c in ("", "본교") else f"({c})"),
        "업체목록": u["winner"].map(lambda w: [w] if w else []), "금액": u["amount"].map(ce._won),
        "일자": u["date"],
        # 공고는 아직 진행 전(공고일+1년을 사업 종료 추정), 결과(낙찰)는 결과일+1년
        "종료일": (ev + pd.Timedelta(days=365)).dt.strftime("%Y-%m-%d"), "종료추정": "Y",
        "원문": u["url"], "_key": "대학|" + u["uniq_key"], "_지역힌트": u["region"],
    })
    return _common(out, comp)


def build(all_df):
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    def clean(v):
        return ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v
    all_df = all_df.copy()
    for col in all_df.columns:
        if not pd.api.types.is_numeric_dtype(all_df[col]):
            all_df[col] = all_df[col].map(clean)
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
            ("기관구분", "기관구분", 7), ("대학", "대학", 8), ("지역", "지역", 10), ("담당지역", "담당지역", 7), ("금액", "금액", 11), ("일자", "일자", 11),
            ("종료일", "종료일", 11), ("종료 추정", "종료추정", 7), ("자사관련", "자사관련", 7), ("자사관련 단어", "자사관련단어", 14),
            ("경쟁사", "경쟁사", 7), ("API 관련", "API관련", 7), ("금액(원, 정렬용)", "_won", 14)]
    EOK = '#,##0.0"억"'
    WON = '#,##0'

    def eok(v):
        return None if v is None or (isinstance(v, float) and pd.isna(v)) or v == "" else round(float(v) / 1e8, 2)

    def money(ws, v):
        """천만원 이상 → 'N.N억', 미만 → '9,500,000' (원 단위 콤마)"""
        if v is None or v == "" or (isinstance(v, float) and pd.isna(v)):
            return c(ws, None)
        v = float(v)
        return c(ws, round(v / 1e8, 2), fmt=EOK) if abs(v) >= 1e7 else c(ws, int(v), fmt=WON)

    def link(url, title):
        t = clean(str(title or "")).replace('"', '""')[:240]
        u = clean(str(url or "")).replace('"', "%22")
        # 엑셀 수식 한 칸은 255자 문자열 제한 → 주소가 너무 길면 링크 없이 제목만
        return f'=HYPERLINK("{u}","{t}")' if u.startswith("http") and len(u) < 250 else t

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
                    row.append(money(ws, v))
                    continue
                if k == "_won":
                    w = r.get("금액")
                    row.append(c(ws, None if w is None or w == "" or pd.isna(w) else int(w), fmt=WON))
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
            "금액 합계": g["금액"].sum(min_count=1),
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
        widths = {"업체명": 28, "주요 기관": 46, "출처 구성": 22, "다른 표기": 30, "금액 합계": 12}
        for i, k in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = widths.get(k, 10)
        if note:
            ws.append([c(ws, note, bold)])
        ws.append([c(ws, k, hf, navy) for k in cols])
        for r in s.to_dict("records"):
            ws.append([money(ws, r[k]) if k == "금액 합계" else r[k] for k in cols])
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
        ("출처", f"조달청 나라장터 낙찰 {int(n.get('낙찰', 0)):,}건 · 계약 {int(n.get('계약', 0)):,}건 · 쇼핑몰 납품(자사·경쟁사 솔루션) {int(n.get('쇼핑몰 납품', 0)):,}건 · 대학 홈페이지 입찰 {int(n.get('대학 홈페이지', 0)):,}건"),
        ("금액 표시", "천만원 이상은 '억'(예: 10.0억, 0.5억), 천만원 미만은 원 단위(예: 9,500,000). 정렬·합계는 맨 끝 '금액(원, 정렬용)' 칸 사용"),
        ("지역", "기관 소재 시·도: 원자료 지역 → 대학 목록(대학알리미) → 기관명 속 지역명 순으로 판단. 중앙부처는 '중앙(전국)'"),
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
        ("한계", "대학 홈페이지 수집은 게시판 자동 찾기 방식 — 못 찾은 학교는 '대학 수집 결과' 시트에 표시 (주소를 알려주면 보완)"),
    ]
    for a, b in rows:
        ws.append([a if not isinstance(a, str) else c(ws, a, bold), b])

    # 업체검색
    ws = wb.create_sheet("업체검색")
    show = ["출처", "상태", "남은일수", "구분", "사업명 (누르면 원문)", "제품", "업체명", "수요기관", "기관구분", "대학", "지역", "금액", "일자", "종료일", "경쟁사", "API 관련"]
    letters = {t: get_column_letter(i + 1) for i, (t, _, _) in enumerate(VIEW)}
    ucol = get_column_letter(len(VIEW) + 1)
    for i, w in enumerate([9, 9, 8, 10, 46, 30, 26, 26, 7, 8, 9, 11, 11, 11, 7, 7], 1):
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
            if t == "금액":       # 정렬용 원 단위 칸을 읽어 천만원 이상은 억, 미만은 원으로 표시
                wc = letters["금액(원, 정렬용)"]
                f = (f'=IFERROR(IF(INDEX({T}!{wc}:{wc},{pos})="","",IF(INDEX({T}!{wc}:{wc},{pos})>=10000000,'
                     f'TEXT(INDEX({T}!{wc}:{wc},{pos})/100000000,"#,##0.0")&"억",TEXT(INDEX({T}!{wc}:{wc},{pos}),"#,##0"))),"")')
                row.append(f)
                continue
            tail = "" if t == "남은일수" else '&""'
            f = f"=IFERROR(INDEX({T}!{col}:{col},{pos}){tail},\"\")"
            row.append(f)
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
            "금액 합계": g["금액"].sum(min_count=1), "최근 일자": g["일자"].max(),
            "주요 업체": g["대표업체"].agg(lambda x: ", ".join(x.value_counts().index[:4])),
            "자사 관련": g["자사관련"].agg(lambda x: int((x == "Y").sum())),
            "경쟁사": g["경쟁사"].agg(lambda x: int((x == "Y").sum())),
        }).reset_index().rename(columns={"수요기관": "업체명"}).sort_values(["진행중·곧완료", "IT 사업 수"], ascending=False)
        us = us.rename(columns={"업체명": "대학(수요기관)"})
        ws = wb.create_sheet("대학별 요약")
        cols = list(us.columns)
        for i, k in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = {"대학(수요기관)": 34, "주요 업체": 50, "금액 합계": 12}.get(k, 10)
        ws.append([c(ws, k, hf, navy) for k in cols])
        for r in us.to_dict("records"):
            ws.append([money(ws, r[k]) if k == "금액 합계" else r[k] for k in cols])
        ws.freeze_panes = "B2"
    table("대학 사업", univ_rows)
    # 대학 홈페이지 수집 결과 (학교별)
    rep, _ = store.load_cache("univ_crawl_report")
    if rep:
        ws = wb.create_sheet("대학 수집 결과")
        for i, w in enumerate([24, 10, 8, 8, 60, 40], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.append([c(ws, k, hf, navy) for k in ("학교", "결과", "3년 내 글", "IT 글", "입찰 게시판 주소", "홈페이지")])
        for r in sorted(rep, key=lambda x: (x.get("status") != "성공", -int(x.get("it_posts") or 0))):
            ws.append([clean(r.get("school")), r.get("status"), r.get("posts"), r.get("it_posts"), clean(r.get("board")), clean(r.get("home"))])
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
    load_code_region()
    cons, ntce = load_contracts(comp)
    awards = load_awards(comp, ntce)
    dlvr = load_dlvr(comp, a.dlvr_cache)
    univ = load_univ_bids(comp)
    parts = [x for x in (cons, awards, dlvr, univ) if not x.empty]
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
