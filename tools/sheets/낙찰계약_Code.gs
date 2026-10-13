/**
 * 공공·대학 낙찰·계약 현황 — 지금까지의 통합 엑셀 전체 + 매일 08시 자동 업데이트
 * 자료: Gov-Tracker가 매일 06:30 게시하는 나라장터·조달·대학 공개 자료 (sheets-feed)
 * 처음 한 번: [자동 업데이트] → [매일 자동 실행 켜기 (08시)]  (전체 내역 13만여 건은 몇 분에 걸쳐 나눠 채워짐)
 */
const FEED = 'https://raw.githubusercontent.com/zayden-svg/Government-AX-project/sheets-feed/';

function onOpen() {
  SpreadsheetApp.getUi().createMenu('자동 업데이트')
    .addItem('지금 업데이트', 'syncAll')
    .addItem('매일 자동 실행 켜기 (08시)', 'installTrigger')
    .addToUi();
}

function installTrigger() {
  ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'syncAll').forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('syncAll').timeBased().everyDays(1).atHour(8).inTimezone('Asia/Seoul').create();
  syncAll();
}

function fetch_(name) {
  const res = UrlFetchApp.fetch(FEED + name, {muteHttpExceptions: true});
  return res.getResponseCode() === 200 ? res : null;
}
function csv_(name) {
  const res = fetch_(name);
  if (!res) return null;
  if (/\.gz$/.test(name)) {
    const blob = Utilities.ungzip(res.getBlob().setContentType('application/x-gzip'));
    return Utilities.parseCsv(blob.getDataAsString('UTF-8'));
  }
  return Utilities.parseCsv(res.getContentText('UTF-8'));
}
function text_(name) { const r = fetch_(name); return r ? r.getContentText('UTF-8') : ''; }
const norm_ = s => String(s || '').replace(/\s+/g, '');
const NAVY = '#1B2A4A';

function header_(sh, row, cols) {
  sh.getRange(row, 1, 1, cols.length).setValues([cols]).setFontWeight('bold').setBackground(NAVY).setFontColor('#FFFFFF');
}

const NORM_FORMULA = "SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(LOWER(TRIM(B2)),\"주식회사\",\"\"),\"(주)\",\"\"),\"㈜\",\"\"),\"( 주 )\",\"\"),\"(주 )\",\"\"),\"( 주)\",\"\"),\"유한회사\",\"\"),\"(유)\",\"\"),\"사단법인\",\"\"),\"(사)\",\"\"),\"재단법인\",\"\"),\"(재)\",\"\"),\"합자회사\",\"\"),\"(합)\",\"\"),\"co.,ltd.\",\"\"),\"co.,ltd\",\"\"),\"co., ltd.\",\"\"),\"co., ltd\",\"\"),\"corporation\",\"\"),\"corp.\",\"\"),\"inc.\",\"\"),\" \",\"\"),\".\",\"\"),\",\",\"\"),\"-\",\"\"),\"·\",\"\"),\"_\",\"\"),\"(\",\"\"),\")\",\"\"),\"&\",\"\")";

const ORDER = ['대시보드', '업체검색', '경쟁사 수주', '신규 7일', '솔루션 연관', '대학 사업', '업체별 요약', '통합 내역'];
const TABS = [
  ['경쟁사 수주', 'competitor.csv', '경쟁 제품(DynaPath·에버세이프·xQueue 등) 계약 — 경쟁사가 따낸 무관한 사업은 제외'],
  ['신규 7일', 'new7.csv', '최근 7일 새로 잡힌 낙찰·계약·대학 사업'],
  ['솔루션 연관', 'solution.csv', '자사 솔루션(대기열·예약·매크로·API 트래픽)·API·경쟁 제품 관련 사업'],
  ['대학 사업', 'univ.csv', '대학 낙찰·계약·홈페이지 입찰·수의계약 공개'],
];

function syncAll() {
  const ss = SpreadsheetApp.getActive();
  const t0 = Date.now();
  const log = TABS.map(t => refreshTab_(ss, t[0], t[1], t[2]));
  log.push(summary_(ss));
  log.push(dashboard_(ss));
  searchSheet_(ss);
  order_(ss);
  PropertiesService.getScriptProperties().setProperty('allPart', '1');
  ss.toast(log.join(' · ') + ' — 통합 내역은 이어서 채웁니다', '업데이트', 10);
  importAll_(t0);
}

function order_(ss) {
  ORDER.forEach((n, i) => { const sh = ss.getSheetByName(n); if (sh) { ss.setActiveSheet(sh); ss.moveActiveSheet(i + 1); } });
  const st = ss.getSheetByName('시작하기'); if (st) { ss.setActiveSheet(st); ss.moveActiveSheet(ss.getNumSheets()); }
  const d = ss.getSheetByName('대시보드'); if (d) ss.setActiveSheet(d);
}

/** 통합 내역: 2만 건씩 나눠 채우고, 시간이 모자라면 1분 뒤 이어서 */
function continueAll() { importAll_(Date.now()); }
function importAll_(t0) {
  const ss = SpreadsheetApp.getActive();
  const props = PropertiesService.getScriptProperties();
  ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'continueAll').forEach(t => ScriptApp.deleteTrigger(t));
  const parts = Number(text_('all_parts.txt')) || 0;
  let p = Number(props.getProperty('allPart') || '1');
  let sh = ss.getSheetByName('통합 내역') || ss.insertSheet('통합 내역');
  while (p <= parts) {
    if (Date.now() - t0 > 4 * 60 * 1000) {
      props.setProperty('allPart', String(p));
      ScriptApp.newTrigger('continueAll').timeBased().after(60 * 1000).create();
      return;
    }
    const rows = csv_('all_' + p + '.csv.gz');
    if (!rows) break;
    const head = rows[0];
    const data = rows.slice(1).map(r => r.map((v, i) => head[i] === '금액(원)' ? (v === '' ? '' : Number(v)) : v));
    if (p === 1) {
      sh.clear();
      if (sh.getFilter()) sh.getFilter().remove();
      sh.getRange(1, 1).setValue('통합 내역 — 나라장터 계약·낙찰 + 쇼핑몰 납품 + 대학 (매일 08시 갱신). 원문 칸 주소를 누르면 원문').setFontWeight('bold');
      header_(sh, 2, head);
      const need = Number(text_('dash_meta.txt').split('\n')[0]) + 5;
      if (sh.getMaxRows() < need) sh.insertRowsAfter(sh.getMaxRows(), need - sh.getMaxRows());
    }
    const start = sh.getLastRow() + 1;
    if (data.length) {
      sh.getRange(start, 1, data.length, head.length).setValues(data);
      sh.getRange(start, head.indexOf('금액(원)') + 1, data.length, 1).setNumberFormat('#,##0');
    }
    p++;
  }
  props.setProperty('allPart', String(p));
  const h = sh.getRange(2, 1, 1, sh.getLastColumn()).getValues()[0];
  const hide = h.indexOf('검색용업체명') + 1;
  if (hide > 0) sh.hideColumns(hide);
  sh.setFrozenRows(2); sh.setFrozenColumns(5);
  if (!sh.getFilter()) sh.getRange(2, 1, sh.getLastRow() - 1, h.length).createFilter();
  ss.toast('통합 내역 ' + (sh.getLastRow() - 2).toLocaleString('ko-KR') + '건 완료', '업데이트 완료', 8);
}

function linkRows_(rows) {
  const head = rows[0];
  const iUrl = head.indexOf('원문'), iTitle = head.indexOf('사업명'), iAmt = head.indexOf('금액(원)');
  const keep = head.map((h, i) => i).filter(i => i !== iUrl && head[i] !== '검색용업체명');
  const out = rows.slice(1).map(r => keep.map(i => {
    if (i === iTitle && r[iUrl] && /^https?:/.test(r[iUrl]) && r[iUrl].length < 240)
      return '=HYPERLINK("' + r[iUrl].replace(/"/g, '%22') + '","' + String(r[i]).replace(/"/g, '""').slice(0, 200) + '")';
    if (i === iAmt) return r[i] === '' ? '' : Number(r[i]);
    return r[i];
  }));
  return {hdr: keep.map(i => head[i] === '사업명' ? '사업명 (누르면 원문)' : head[i]), out: out};
}

function refreshTab_(ss, name, file, note) {
  const rows = csv_(file);
  if (!rows) return name + ' 자료 없음';
  const sh = ss.getSheetByName(name) || ss.insertSheet(name);
  if (sh.getFilter()) sh.getFilter().remove();
  sh.clear();
  const t = linkRows_(rows);
  sh.getRange(1, 1).setValue(note + ' — ' + t.out.length.toLocaleString('ko-KR') + '건 (매일 08시 갱신)').setFontWeight('bold');
  header_(sh, 2, t.hdr);
  if (t.out.length) {
    sh.getRange(3, 1, t.out.length, t.hdr.length).setValues(t.out);
    const a = t.hdr.indexOf('금액(원)') + 1;
    if (a > 0) sh.getRange(3, a, t.out.length, 1).setNumberFormat('#,##0');
  }
  sh.setFrozenRows(2); sh.setFrozenColumns(5);
  sh.getRange(2, 1, Math.max(t.out.length, 1) + 1, t.hdr.length).createFilter();
  return name + ' ' + t.out.length + '건';
}

function summary_(ss) {
  const rows = csv_('summary.csv');
  if (!rows) return '업체별 요약 없음';
  const sh = ss.getSheetByName('업체별 요약') || ss.insertSheet('업체별 요약');
  if (sh.getFilter()) sh.getFilter().remove();
  sh.clear();
  const head = rows[0];
  const data = rows.slice(1).map(r => r.map((v, i) => /^(공공|대학|진행중|종료|솔루션|금액)/.test(head[i]) && v !== '' && !isNaN(v) ? Number(v) : v));
  header_(sh, 1, head);
  if (data.length) sh.getRange(2, 1, data.length, head.length).setValues(data);
  const a = head.indexOf('금액 합계') + 1;
  if (a > 0 && data.length) sh.getRange(2, a, data.length, 1).setNumberFormat('#,##0');
  sh.setFrozenRows(1); sh.setFrozenColumns(1);
  sh.getRange(1, 1, data.length + 1, head.length).createFilter();
  return '업체별 요약 ' + data.length + '곳';
}

/** 대시보드: 표 + 그래프(이름·건수 항상 표시) */
function dashboard_(ss) {
  const sh = ss.getSheetByName('대시보드') || ss.insertSheet('대시보드');
  sh.getCharts().forEach(c => sh.removeChart(c));
  sh.clear();
  const meta = text_('dash_meta.txt').split('\n');
  sh.getRange(1, 1).setValue('공공·대학 IT 수주 현황 대시보드').setFontSize(16).setFontWeight('bold');
  sh.getRange(2, 1).setValue('전체 ' + Number(meta[0] || 0).toLocaleString('ko-KR') + '건 · 매일 08시 자동 갱신 · 업체별 전체 사업은 [업체검색] 탭');
  let row = 4;
  const blocks = [
    ['① 자사 솔루션 연관 사업을 많이 수주한 업체 TOP 20 (에스티씨랩 제외)', 'dash_top.csv', Charts.ChartType.BAR, 22],
    ['② 월별 신규 사업 (최근 24개월)', 'dash_monthly.csv', Charts.ChartType.COLUMN, 26],
    ['③ 최근 6개월 신규 사업 분야', 'dash_field.csv', Charts.ChartType.BAR, 14],
    ['④ 경쟁사별 경쟁 제품 수주·납품', 'dash_comp.csv', Charts.ChartType.BAR, 10],
  ];
  blocks.forEach(b => {
    const rows = csv_(b[1]);
    if (!rows || rows.length < 2) return;
    sh.getRange(row, 1).setValue(b[0]).setFontWeight('bold');
    const data = rows.slice(1).map(r => r.map((v, i) => i > 0 && v !== '' && !isNaN(v) ? Number(v) : v));
    header_(sh, row + 1, rows[0]);
    sh.getRange(row + 2, 1, data.length, rows[0].length).setValues(data);
    const rng = sh.getRange(row + 1, 1, data.length + 1, 2);
    const series = {0: {dataLabel: 'value', color: b[1] === 'dash_comp.csv' ? '#C0392B' : '#2F5BD3'}};
    sh.insertChart(sh.newChart().setChartType(b[2]).addRange(rng).setNumHeaders(1)
      .setPosition(row, 5, 0, 0)
      .setOption('title', b[0]).setOption('legend', {position: 'none'}).setOption('series', series)
      .setOption('hAxis', {showTextEvery: 1, slantedText: b[2] === Charts.ChartType.COLUMN, textStyle: {fontSize: 10}})
      .setOption('vAxis', {textStyle: {fontSize: 10}})
      .setOption('width', b[2] === Charts.ChartType.COLUMN ? 900 : 620).setOption('height', Math.max(260, data.length * 22 + 80))
      .build());
    row += Math.max(data.length + 4, Math.ceil((Math.max(260, data.length * 22 + 80)) / 21) + 2);
  });
  const u = csv_('dash_univ.csv');
  if (u && u.length > 1) {
    sh.getRange(row, 1).setValue('⑤ 대학별 IT 사업 확보 현황 (대학알리미 전체 대학 기준)').setFontWeight('bold');
    header_(sh, row + 1, u[0]);
    sh.getRange(row + 2, 1, u.length - 1, u[0].length).setValues(u.slice(1).map(r => r.map((v, i) => i > 0 ? Number(v) : v)));
    row += u.length + 2;
    if (meta[1]) { sh.getRange(row, 1).setValue('4년제 0건: ' + meta[1]); row += 2; }
  }
  [['읽는 법', ''], ['종료', '진행중 / 종료 임박(90일 이내) / 종료 / 미상'], ['금액(원)', '원 단위. 미공개는 빈 칸'],
   ['사업종료일', '계약은 계약 종료일, 대학은 공고문·계약공개의 계약기간, 그 밖(낙찰·쇼핑몰 납품)은 일자 + 1년 추정'],
   ['경쟁사', '데브와이·스크립터스(DynaPath)·에버스핀(에버세이프)·소프트베이스(xQueue)·가온아이(에버세이프 판매) — 경쟁 제품 영역만'],
   ['지역', '수요기관 본사 시·도 (조달청 나라장터 사용자정보 기준, 매일 보완)']].forEach((r, i) => {
    sh.getRange(row + i, 1, 1, 2).setValues([r]);
    if (i === 0) sh.getRange(row, 1).setFontWeight('bold');
  });
  sh.setColumnWidth(1, 220);
  return '대시보드';
}

/** 업체검색: B2에 업체명 → (주)·주식회사·띄어쓰기·영문 표기 차이 무시하고 전부 */
function searchSheet_(ss) {
  const sh = ss.getSheetByName('업체검색') || ss.insertSheet('업체검색');
  const keep = sh.getRange(2, 2).getValue();
  sh.clear();
  sh.getRange(1, 1).setValue('업체 검색 — 노란 칸에 업체명 일부 입력 (종료 임박·진행중 먼저). (주)·주식회사·띄어쓰기·대소문자 무시').setFontWeight('bold');
  sh.getRange(2, 1).setValue('업체명 →').setFontWeight('bold');
  sh.getRange(2, 2).setValue(keep || '').setBackground('#FFF59D');
  sh.getRange(2, 4).setValue('인식한 검색어');
  sh.getRange(2, 5).setFormula('=' + NORM_FORMULA);
  sh.getRange(2, 6).setValue('결과');
  sh.getRange(2, 7).setFormula('=IF(E2="","",COUNTIF(\'통합 내역\'!O3:O,"*"&E2&"*"))');
  const h = ['기관특성', '대학구분', '업체명', '수요기관', '지역', '사업명', '제품', '금액(원)', '사업종료일', '종료', '종료 D-day', '원문', '일자', '출처'];
  header_(sh, 4, h);
  sh.getRange(5, 1).setFormula('=IF(E2="","",IFERROR(FILTER(\'통합 내역\'!A3:N,ISNUMBER(SEARCH(E2,\'통합 내역\'!O3:O))),"결과 없음"))');
  sh.getRange(5, 8, 3000, 1).setNumberFormat('#,##0');
  sh.setFrozenRows(4); sh.setFrozenColumns(5);
}
