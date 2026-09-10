"""
사업분류표_생성.py
NetFUNNEL/MBUSTER 실제 낙찰 데이터 기반 사업유형 분류표를
새 엑셀 파일로 자동 생성하는 스크립트.
"""
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# ── 분류표 데이터 ──────────────────────────────────────────
rows = [
    ["트래픽·접속제어", "1차", "대기열", "NetFUNNEL", "식품안전정보원, 국립현대미술관, 온라인몰 대기열 등"],
    ["트래픽·접속제어", "1차", "순번대기", "NetFUNNEL", "부산항만공사, 청소년청년재단, 하남시, 남원시 등 다수"],
    ["트래픽·접속제어", "1차", "대기시스템", "NetFUNNEL", "한국청소년상담복지개발원, 울산교육연구정보원"],
    ["트래픽·접속제어", "1차", "대량접속제어", "NetFUNNEL", "경찰공제회, 국토안전관리원, 병무청, 한국장학재단 등 다수"],
    ["트래픽·접속제어", "1차", "접속자순차처리", "NetFUNNEL", "충북교육청, 영동군, 서울주택도시개발공사 등 다수"],
    ["트래픽·접속제어", "1차", "유량제어", "NetFUNNEL", "수원도시공사, 한국에너지공단, 인천교육청"],
    ["트래픽·접속제어", "1차", "트래픽", "NetFUNNEL", "수원도시공사, 국립광주과학관, 법무연수원"],
    ["트래픽·접속제어", "1차", "동시접속", "NetFUNNEL", "교육부 국사편찬위원회, 국토안전관리원, 국민권익위"],
    ["업무유형(예약·청약·접수·시험)", "1차", "청약", "NetFUNNEL", "한국토지주택공사, 한국부동산원, 부산도시공사, 충남개발공사"],
    ["업무유형(예약·청약·접수·시험)", "1차", "예약시스템", "NetFUNNEL", "울산 동구, 국립공원공단, 동해시, 서울시연수원, 청주시"],
    ["업무유형(예약·청약·접수·시험)", "1차", "원서접수", "NetFUNNEL", "한국지역정보개발원, 한국방송통신전파진흥원"],
    ["업무유형(예약·청약·접수·시험)", "1차", "수강신청", "NetFUNNEL", "한경국립대, 한국교원대"],
    ["업무유형(예약·청약·접수·시험)", "1차", "채용시스템", "NetFUNNEL", "인사혁신처, 한국수력원자력"],
    ["업무유형(예약·청약·접수·시험)", "1차", "능력검정시험", "NetFUNNEL", "교육부 국사편찬위원회 (3회 반복 발주)"],
    ["업무유형(예약·청약·접수·시험)", "1차", "자격시험", "NetFUNNEL", "한국산업인력공단(큐넷), TOPIK IBT"],
    ["매크로·부정접속방어", "1차", "매크로 탐지", "MBUSTER", "헌법재판소, 하남도시공사, 경찰청 등 다수"],
    ["매크로·부정접속방어", "1차", "매크로 차단", "MBUSTER", "하남시, 안양도시공사, 중앙대, 강원랜드"],
    ["매크로·부정접속방어", "1차", "부정접속", "MBUSTER", "한국인터넷진흥원 e프라이버시 클린서비스"],
    ["시스템구축·전환", "1차", "차세대", "NetFUNNEL", "온비드, 조폐공사, 농업e지, 고용24, 4대사회보험 등 9건 이상"],
    ["시스템구축·전환", "1차", "고도화", "NetFUNNEL", "충북교육청, 국민제안 통합플랫폼, 산림청 숲나들e 등 5건 이상"],
    ["시스템구축·전환", "1차", "통합플랫폼", "NetFUNNEL", "국민권익위, 4대사회보험 정보연계플랫폼"],
    ["시스템구축·전환", "1차", "통합시스템", "NetFUNNEL", "통합공공도서관시스템, 통합학사관리시스템"],
    ["보조신호(결합시유효)", "2차", "홈페이지 개편", "NetFUNNEL", "부산청년플랫폼, 도서관 홈페이지 개선"],
    ["보조신호(결합시유효)", "2차", "인프라 확충", "NetFUNNEL", "헌법재판소 데이터센터, 경상국립대"],
    ["보조신호(결합시유효)", "2차", "클라우드 전환", "NetFUNNEL", "한국지능정보사회진흥원 클라우드 네이티브 전환"],
    ["보조신호(결합시유효)", "2차", "노후장비 교체", "NetFUNNEL", "헌법재판소, 울산시설공단"],
    ["보조신호(결합시유효)", "2차", "안정화", "NetFUNNEL", "한국생산성본부(KPC자격), 한국방송통신전파진흥원"],
    ["보조신호(결합시유효)", "2차", "DR", "NetFUNNEL", "한국환경공단 DR센터 구축"],
]

headers = ["카테고리", "우선순위", "키워드", "추천솔루션", "실제사례근거"]

# ── 엑셀 파일 생성 ──────────────────────────────────────────
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "사업유형분류표"

# 헤더 작성 및 스타일
ws.append(headers)
header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
header_font = Font(color="FFFFFF", bold=True, size=11)
for cell in ws[1]:
    cell.fill = header_fill
    cell.font = header_font
    cell.alignment = Alignment(horizontal="center", vertical="center")

# 데이터 작성
tier1_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")  # 노란색(1차)
tier2_fill = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")  # 회색(2차)
thin_border = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin")
)

for row_data in rows:
    ws.append(row_data)
    current_row = ws.max_row
    fill = tier1_fill if row_data[1] == "1차" else tier2_fill
    for cell in ws[current_row]:
        cell.fill = fill
        cell.border = thin_border
        cell.alignment = Alignment(vertical="center", wrap_text=True)

# 열 너비 자동 조정
col_widths = [28, 8, 16, 14, 45]
for i, width in enumerate(col_widths, start=1):
    ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width

# 필터 및 틀 고정
ws.auto_filter.ref = f"A1:E{ws.max_row}"
ws.freeze_panes = "A2"

# ── 파일 저장 ──────────────────────────────────────────
output_file = "사업유형분류표_NetFUNNEL_MBUSTER.xlsx"
wb.save(output_file)
print(f"완료: '{output_file}' 파일이 생성되었습니다.")
print(f"총 {len(rows)}개 키워드 항목이 저장되었습니다.")
