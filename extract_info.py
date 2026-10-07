# extract_info.py
# 공고 본문(상세페이지·첨부 공고문)에서 '예산'과 '마감일'을 뽑는 공통 규칙
#   기관마다 표기가 제각각이라 실제 수집처 원문에서 확인한 표현을 모두 규칙으로 등록했다.
#   예) NIPA  "○ 사업금액: 150,000,000원(부가가치세 포함)" / "○ 제안서ㆍ가격 전자입찰서 제출 마감일시: 2026/09/18 10:00"
#       NIPA  "사업예산/기초가격: 40,000,000원" / "② 접수마감일시: 2026.09.22. 10:00" / "계약기간: 계약 체결일 ~ 2026. 12. 16."
#       KISA  "o 예산액 : 70,000,000원(부가세 포함)" / "o 공개기간 : 2026년 10월 7일 ~ 2026년 10월 13일"
#       AIHub "다. 사업예산 : 총 1,200백만원(정부지원금 600백만원 X 2개 분야)" / "공고기간 2026.8.12(수)~ 9.11(금)"
#       NTIS  "공고금액 : 1,575.9억원" / "마감일 : 2026.11.02"
#       KIAT  "지원규모 … 10억원 이내/년" / "공고접수기간/상태 2026-09-21 ~ 2027-01-28"
#       TIPA  "□ 접수기간 : ‘26. 6. 30.(화) ~ ’26. 7. 24.(금) 14:00 까지" / "사업화 지원 프로그램 최대 1.5억원"
#       IRIS  표 머리글 "총 연구비(억원)" 아래 숫자 / 접수기간 2026-09-23 ~ 2026-10-08
import re
from datetime import date, datetime, timedelta

# ------------------------------------------------------------------
# 금액
# ------------------------------------------------------------------
_UNIT = {"조": 10**12, "억": 10**8, "천만": 10**7, "백만": 10**6, "만": 10**4, "천": 10**3, "": 1}
_NUM = r"\d[\d,]*(?:\.\d+)?"
_UNITS = r"(?:조|억|천만|백만|만|천)"
# 금액 덩어리: '1억 5,000만원', '50억 원', '1,200백만원', '350,000천원', '150,000,000원', '1,575.9억원'
_AMOUNT_RE = re.compile(rf"(?<![\d.,])((?:{_NUM}\s*{_UNITS}\s*)+(?:{_NUM}\s*)?|{_NUM}\s*)원")
_TOKEN_RE = re.compile(rf"({_NUM})\s*({_UNITS})?")


def _amount_value(chunk):
    total = 0.0
    for num, unit in _TOKEN_RE.findall(chunk):
        try:
            total += float(num.replace(",", "")) * _UNIT[unit or ""]
        except ValueError:
            return 0
    return int(round(total))


def find_amounts(s):
    """문자열 안의 모든 '…원' 금액 → [(원 단위 정수, 시작위치)]"""
    out = []
    for m in _AMOUNT_RE.finditer(s or ""):
        v = _amount_value(m.group(1))
        if v > 0:
            out.append((v, m.start()))
    return out


# (라벨 정규식, 화면 표시용 이름, 찾는 범위(글자수), 같은 줄 안에서만 찾을지)
#   위에서부터 우선 적용: 입찰·용역 금액 → R&D 지원 규모 → 상금·혜택형
_BUDGET_RULES = [
    (r"사업\s*금액", "사업금액", 60, True),
    (r"사업\s*예산", "사업예산", 70, True),
    (r"배정\s*예산", "배정예산", 60, True),
    (r"예산\s*액", "예산액", 60, True),
    (r"소요\s*예산", "소요예산", 60, True),
    (r"총\s*사업\s*비", "총사업비", 60, True),
    (r"기초\s*(?:금액|가격)", "기초금액", 60, True),
    (r"예정\s*가격", "예정가격", 60, True),
    (r"추정\s*(?:가격|금액)", "추정가격", 60, True),
    (r"계약\s*금액", "계약금액", 60, True),
    (r"공고\s*금액", "공고금액", 60, True),
    (r"예산\s*(?:규모|금액)", "예산규모", 60, True),
    (r"사업\s*규모", "사업규모", 60, True),
    (r"(?:용역|구매)\s*(?:비|예산)", "용역비", 60, True),
    # R&D·지원사업
    (r"정부\s*지원\s*(?:연구\s*개발\s*비|금)", "정부지원금", 90, False),
    (r"정부\s*출연\s*금", "정부출연금", 90, False),
    (r"지원\s*(?:규모|금액|한도|예산)", "지원규모", 130, False),
    (r"총\s*연구\s*(?:개발\s*)?비", "총연구비", 90, False),
    (r"연구\s*개발\s*비", "연구개발비", 90, False),
    (r"과제\s*당", "과제당", 50, False),
    (r"사업\s*비", "사업비", 50, True),
    (r"예\s*산", "예산", 40, True),
    # 상금·혜택형 (우수성과·경진대회·포상 공모)
    (r"(?:총\s*)?(?:우수\s*)?(?:상금|포상금|시상금)", "상금", 90, False),
    (r"(?:지원\s*)?혜택", "지원혜택", 130, False),
    (r"지원\s*내용", "지원내용", 130, False),
    (r"인센티브", "인센티브", 90, False),
]
_BUDGET_RULES = [(re.compile(p), name, win, same_line) for p, name, win, same_line in _BUDGET_RULES]
_REWARD_NAMES = {"상금", "지원혜택", "지원내용", "인센티브"}


def _window(text, pos, width, same_line):
    w = text[pos:pos + width]
    if same_line:
        lead = re.match(r"[\s:：=\-·/()\[\]]*", w)     # "공고금액 :\n 1,575.9억원"처럼 라벨 뒤 줄바꿈은 허용
        start = lead.end() if lead else 0
        nl = w.find("\n", start)
        if nl > 0:
            w = w[:nl]
    return w


# 예산이 아닌 금액(자격 조건·매출 기준 등)을 걸러내는 문맥
_NOT_BUDGET_AFTER = re.compile(r"^\s*(?:이상|이하|미만|초과|이내의\s*기업|규모의\s*기업)")
_NOT_BUDGET_LINE = re.compile(r"(매출|자본금|자산|투자\s*유치|수출액|과제매출|보증료|융자|대출|이자)")


def _is_budget_context(w, pos, amount_text_end):
    if _NOT_BUDGET_AFTER.match(w[amount_text_end:amount_text_end + 8]):
        return False
    line_start = w.rfind("\n", 0, pos) + 1
    return not _NOT_BUDGET_LINE.search(w[line_start:pos])


def extract_budget(text):
    """반환: (원 단위 금액 문자열, 라벨) — 못 찾으면 ('', '')"""
    if not text:
        return "", ""
    # NTIS처럼 숫자와 단위가 다른 줄에 찍히는 경우 ("공고금액 :\n1,575.9\n억원") → 한 줄로 붙임
    t = re.sub(rf"(\d)[ \t]*\n\s*({_UNITS}?\s*원)", r"\1\2", str(text))
    t = re.sub(rf"({_UNITS})[ \t]*\n\s*원", r"\1원", t)
    for rx, name, width, same_line in _BUDGET_RULES:
        min_amount = 300_000 if name in _REWARD_NAMES else 1_000_000
        for m in rx.finditer(t):
            w = _window(t, m.end(), width, same_line)
            for am in _AMOUNT_RE.finditer(w):
                v = _amount_value(am.group(1))
                if v >= min_amount and _is_budget_context(w, am.start(), am.end()):
                    return str(v), name
    return "", ""


def extract_budget_from_tables(html):
    """표 머리글이 '총 연구비(억원)' · '사업비(백만원)' · '지원금액(천원)'처럼 단위를 가진 경우 그 열의 숫자 합계.
    합계 행이 있으면 합계 행 값을 사용. 반환: (원 단위 문자열, 라벨)"""
    if not html:
        return "", ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return "", ""
    soup = BeautifulSoup(html, "html.parser")
    unit_map = {"억원": 10**8, "백만원": 10**6, "천원": 10**3, "만원": 10**4, "원": 1}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_cells = rows[0].find_all(["th", "td"])
        col, mult, label = None, None, ""
        for i, c in enumerate(header_cells):
            h = re.sub(r"\s+", "", c.get_text(" ", strip=True))
            if re.search(r"(연구비|연구개발비|사업비|지원금|지원규모|예산|금액|출연금)", h):
                um = re.search(r"\((억원|백만원|천원|만원|원)\)", h)
                if um:
                    col, mult, label = i, unit_map[um.group(1)], re.sub(r"\(.*?\)", "", h)[:8]
                    break
        if col is None:
            continue
        total, total_row = 0.0, None
        for r in rows[1:]:
            cells = r.find_all(["th", "td"])
            if len(cells) <= col:
                continue
            raw = cells[col].get_text(" ", strip=True)
            m = re.search(_NUM, raw)
            if not m:
                continue
            try:
                v = float(m.group(0).replace(",", ""))
            except ValueError:
                continue
            if re.search(r"합\s*계|총\s*계|소\s*계", r.get_text(" ", strip=True)):
                total_row = v
            else:
                total += v
        value = total_row if total_row is not None else total
        if value > 0:
            return str(int(round(value * mult))), label or "연구비"
    return "", ""


# ------------------------------------------------------------------
# 날짜
# ------------------------------------------------------------------
_YEAR = r"(?P<y>20\d{2}|[’'‘`´]\s?\d{2})"
_FULL_RE = re.compile(_YEAR + r"\s*[.\-/년]\s*(?P<m>\d{1,2})\s*[.\-/월]\s*(?P<d>\d{1,2})(?!\d)\s*일?")
_MD_RE = re.compile(r"(?<![\d.])(?P<m>\d{1,2})\s*[./월]\s*(?P<d>\d{1,2})(?![\d억만천])\s*일?")
_RANGE_SEP = r"[~∼～〜]"
_TAIL = r"(?:\s*\.?\s*(?:\([^)]{1,4}\))?\s*(?:\d{1,2}\s*:\s*\d{2}(?:\s*:\s*\d{2})?)?\s*(?:까지|시한)?)"


def _mk_date(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return None


def _year_of(ystr):
    y = re.sub(r"\D", "", ystr or "")
    if len(y) == 2:
        return 2000 + int(y)
    return int(y) if y else None


def _infer_year(m, d, ref):
    """연도 없이 '9.11(금)'만 있을 때: 기준일(시작일·등록일)과 가장 가까운 미래 쪽 연도"""
    ref = ref or date.today()
    cand = _mk_date(ref.year, m, d)
    if cand and cand < ref - timedelta(days=60):
        cand = _mk_date(ref.year + 1, m, d)
    return cand


def _first_full(s):
    m = _FULL_RE.search(s or "")
    if not m:
        return None, None
    return _mk_date(_year_of(m.group("y")), m.group("m"), m.group("d")), m


def _range_end(s, ref=None):
    """'A ~ B' 형태에서 B(끝 날짜). B에 연도가 없으면 A의 연도를 따름. '~ B'만 있어도 B."""
    s = s or ""
    start, sm = _first_full(s)
    if sm:
        if re.search(_RANGE_SEP, s[max(0, sm.start() - 4):sm.start()]):
            return start                          # '~ 2026.10.13' — 처음 나온 날짜가 곧 끝 날짜
        after = s[sm.end():sm.end() + 45]
        sep = re.search(_RANGE_SEP, after)
        if not sep or sep.start() > 25:
            return None
        rest = after[sep.end():]
    else:
        sep = re.search(_RANGE_SEP, s)
        if not sep:
            return None
        rest = s[sep.end():sep.end() + 40]
    rest = rest.lstrip()
    end, em = _first_full(rest)
    if end and em.start() <= 3:
        return end
    mm = _MD_RE.match(rest)
    if mm:
        base = start or ref
        return _infer_year(mm.group("m"), mm.group("d"), base) if base else None
    return None


_DEADLINE_RULES = [   # 한 날짜를 가리키는 '마감' 라벨
    r"(?:접수|제출|입찰|신청|응모|참가)\s*마감\s*(?:일시|일자|일|시한|기한)?",
    r"입찰서\s*제출\s*마감",
    r"과제\s*신청\s*마감\s*(?:일시)?",
    r"마감\s*(?:일시|일자|일|시한)",
    r"(?:제출|접수|신청)\s*기한",
]
_PERIOD_RULES = [     # 'A ~ B' 기간 라벨 → B
    r"(?:신청\s*[·및\s]*)?접수\s*기간", r"신청\s*기간", r"공모\s*기간", r"모집\s*기간", r"제출\s*기간",
    r"공고\s*기간", r"공개\s*기간", r"의견\s*(?:제출|등록)\s*기간", r"참가\s*신청", r"신청\s*[·및]\s*접수",
    r"접수\s*및\s*제출", r"접수\s*일정", r"공고\s*및\s*접수",
]
_END_RULES = [        # 사업(계약) 종료 시점
    r"납품\s*(?:기한|기일|일자)", r"계약\s*기간", r"사업\s*기간", r"수행\s*기간", r"과업\s*기간",
    r"용역\s*기간", r"협약\s*기간", r"(?:총\s*)?연구\s*(?:개발\s*)?기간",
]
_DEADLINE_RULES = [re.compile(p) for p in _DEADLINE_RULES]
_PERIOD_RULES = [re.compile(p) for p in _PERIOD_RULES]
_END_RULES = [re.compile(p) for p in _END_RULES]


def _to_date(v):
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _valid_due(d, reg):
    if not d:
        return False
    if reg and (d < reg - timedelta(days=2) or d > reg + timedelta(days=420)):
        return False
    if not reg and (d.year < 2020 or d.year > date.today().year + 2):
        return False
    return True


def extract_due(text, reg_date=None):
    """접수·제출 마감일. 반환: ('YYYY-MM-DD', 라벨) — 못 찾으면 ('', '')"""
    if not text:
        return "", ""
    t = str(text)
    reg = _to_date(reg_date)
    # ① '마감일시: 2026/09/18' 형 (가장 확실)
    for rx in _DEADLINE_RULES:
        for m in rx.finditer(t):
            w = t[m.end():m.end() + 70]
            d, dm = _first_full(w)
            if d and dm.start() <= 25 and _valid_due(d, reg):
                return d.isoformat(), "접수마감"
            md = _MD_RE.search(w[:25])
            if md and re.search(_RANGE_SEP, w[:md.start() + 1]):
                d2 = _infer_year(md.group("m"), md.group("d"), reg or date.today())
                if _valid_due(d2, reg):
                    return d2.isoformat(), "접수마감"
    # ② '접수기간: A ~ B' 형
    for rx in _PERIOD_RULES:
        for m in rx.finditer(t):
            w = t[m.end():m.end() + 170]
            d = _range_end(w, reg)
            if d and _valid_due(d, reg):
                return d.isoformat(), "접수기간"
    return "", ""


def extract_period_end(text):
    """납품기한·계약기간·사업기간의 끝 날짜. 반환: 'YYYY-MM-DD' 또는 ''"""
    if not text:
        return ""
    t = str(text)
    for rx in _END_RULES:
        for m in rx.finditer(t):
            w = t[m.end():m.end() + 90]
            d = _range_end(w)
            if d:
                return d.isoformat()
            d, dm = _first_full(w)
            if d and dm.start() <= 16:
                return d.isoformat()
    return ""


def extract_all(text, reg_date=None, html=None):
    """본문 글자(+표 HTML) → {'budget', 'budget_label', 'due_date', 'due_label', 'period_end'}"""
    budget, b_label = extract_budget(text)
    if not budget and html:
        budget, b_label = extract_budget_from_tables(html)
    due, d_label = extract_due(text, reg_date)
    return {"budget": budget, "budget_label": b_label, "due_date": due, "due_label": d_label,
            "period_end": extract_period_end(text)}


def clean_text(s, limit=4000):
    """본문 저장용: 공백 정리 + 길이 제한"""
    s = re.sub(r"[ \t 　]+", " ", str(s or ""))
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()[:limit]
