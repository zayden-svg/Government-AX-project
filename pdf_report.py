import io
import os
from xml.sax.saxutils import escape as xml_escape
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE_DIR, "assets", "fonts")

# 웹 UI와 동일한 Pretendard 폰트를 PDF에도 적용 (fc-list 등 시스템 폰트 설치 여부에 의존하지 않음)
pdfmetrics.registerFont(TTFont("Pretendard", os.path.join(FONT_DIR, "Pretendard-Regular.ttf")))
pdfmetrics.registerFont(TTFont("Pretendard-Bold", os.path.join(FONT_DIR, "Pretendard-Bold.ttf")))

NAVY = colors.HexColor("#191F28")
ACCENT = colors.HexColor("#2D5BFF")
MUTED = colors.HexColor("#6B7684")
BODY = colors.HexColor("#374151")
BORDER = colors.HexColor("#E4E9F0")

TITLE_STYLE = ParagraphStyle("title", fontName="Pretendard-Bold", fontSize=17, leading=22, textColor=NAVY)
H_STYLE = ParagraphStyle("h", fontName="Pretendard-Bold", fontSize=12.5, leading=17, textColor=NAVY, spaceBefore=10, spaceAfter=4)
CAP_STYLE = ParagraphStyle("cap", fontName="Pretendard", fontSize=8.5, leading=12, textColor=MUTED)
BODY_STYLE = ParagraphStyle("body", fontName="Pretendard", fontSize=9.5, leading=14.5, textColor=BODY)
KPI_NUM_STYLE = ParagraphStyle("kpi_num", fontName="Pretendard-Bold", fontSize=19, leading=22, textColor=ACCENT, alignment=1)
KPI_LABEL_STYLE = ParagraphStyle("kpi_label", fontName="Pretendard", fontSize=8, leading=11, textColor=MUTED, alignment=1)


def _p(text, style=BODY_STYLE, markup=False):
    """공고 제목에 & < > 가 들어 있으면 PDF 생성이 통째로 실패하므로 기본으로 이스케이프한다."""
    body = str(text if text is not None else "")
    if not markup:
        body = xml_escape(body)
    return Paragraph(body.replace("\n", "<br/>"), style)


def _kpi_table(kpi_list):
    row_num = [_p(v, KPI_NUM_STYLE) for _, v in kpi_list]
    row_label = [_p(k, KPI_LABEL_STYLE) for k, _ in kpi_list]
    tbl = Table([row_num, row_label], colWidths=[(170 * mm) / len(kpi_list)] * len(kpi_list))
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return tbl


def _bullet_list(items, empty_text="해당 데이터가 부족합니다."):
    if not items:
        return [_p(empty_text, CAP_STYLE)]
    return [_p(f"· {it.get('text', '') if isinstance(it, dict) else it}") for it in items]


def _simple_table(headers, rows, col_widths):
    data = [[Paragraph(h, ParagraphStyle("th", fontName="Pretendard-Bold", fontSize=8.5, textColor=colors.white)) for h in headers]]
    for r in rows:
        data.append([_p(c, BODY_STYLE) for c in r])
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
    ]))
    return tbl


def build_daily_report_pdf(
    kpi_list,
    headline="",
    subtext="",
    issues=None,
    opp_biz=None,
    opp_rnd=None,
    urgent_rows=None,
    trend_rows=None,
):
    """통합보기(Ⅰ~Ⅵ) 데이터를 받아 리포트 형태 PDF 바이트를 반환."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=18 * mm, bottomMargin=16 * mm, leftMargin=16 * mm, rightMargin=16 * mm,
    )
    elements = []

    elements.append(_p("GOV-TRACKER · DAILY BRIEFING", CAP_STYLE))
    elements.append(_p("IT 동향 및 R&D 통합 보고서", TITLE_STYLE))
    elements.append(_p(f"발행 {datetime.now():%Y-%m-%d %H:%M}", CAP_STYLE))
    elements.append(Spacer(1, 6 * mm))

    elements.append(_kpi_table(kpi_list))
    elements.append(Spacer(1, 6 * mm))

    if headline:
        elements.append(_p("Ⅰ. 핵심 요약", H_STYLE))
        elements.append(_p(headline, ParagraphStyle("hl", parent=BODY_STYLE, fontName="Pretendard-Bold", fontSize=11)))
        if subtext:
            elements.append(_p(subtext))

    if issues:
        elements.append(_p("Ⅱ. 핵심 동향 — 이슈 테마", H_STYLE))
        rows = [[it.get("theme", ""), it.get("impact", "-"), f"{it.get('count', '-')}건", it.get("summary", "")] for it in issues]
        elements.append(_simple_table(["테마", "영향도", "건수", "요약"], rows, [32 * mm, 18 * mm, 15 * mm, 103 * mm]))

    if opp_biz is not None or opp_rnd is not None:
        elements.append(_p("Ⅳ. 기회 영역", H_STYLE))
        elements.append(_p("<b>사업부 — 지금 제안서·입찰</b>", BODY_STYLE, markup=True))
        for f in _bullet_list(opp_biz):
            elements.append(f)
        elements.append(Spacer(1, 2 * mm))
        elements.append(_p("<b>R&amp;D — 골든타임</b>", BODY_STYLE, markup=True))
        for f in _bullet_list(opp_rnd):
            elements.append(f)

    if urgent_rows:
        elements.append(_p("Ⅴ. 긴급 점검 사항", H_STYLE))
        rows = [[r.get("dday", "-"), r.get("title", ""), r.get("agency", ""), r.get("due", ""), r.get("score", "")] for r in urgent_rows]
        elements.append(_simple_table(["D-day", "제목", "기관", "마감", "점수"], rows, [15 * mm, 85 * mm, 30 * mm, 20 * mm, 18 * mm]))

    if trend_rows:
        elements.append(_p("Ⅵ. 최근 7일 트렌드 워치리스트", H_STYLE))
        rows = [[r.get("date", ""), r.get("keyword", ""), r.get("category", ""), f"{r.get('importance', '')}점"] for r in trend_rows]
        elements.append(_simple_table(["날짜", "키워드", "카테고리", "중요도"], rows, [25 * mm, 50 * mm, 50 * mm, 43 * mm]))

    elements.append(Spacer(1, 6 * mm))
    elements.append(HRFlowable(width="100%", color=BORDER))
    elements.append(_p("본 보고서는 AI 분석 결과를 포함하며, 제출 전 원문 공고의 접수 기간·자격 요건을 반드시 확인하십시오.", CAP_STYLE))

    doc.build(elements)
    return buf.getvalue()
