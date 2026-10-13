/**
 * 에스티씨랩 공공사업부 낙찰·계약 현황 — 매일 자동 업데이트
 * 자료: Gov-Tracker가 매일 06:30에 게시하는 공개 조달 자료 (sheets-feed)
 * 처음 한 번: 메뉴 [자동 업데이트] → [매일 자동 실행 켜기]
 */
const FEED = 'https://raw.githubusercontent.com/zayden-svg/Government-AX-project/sheets-feed/';
const REF_SHEETS = ['STCLab_레퍼런스', '웰컨_레퍼런스'];
const EMPTY_REGION = ['', '확인 필요', '수도권/전국', '미확인'];
const TABS = [   // [시트 이름, 파일, 설명]
  ['경쟁사 수주', 'competitor.csv', '경쟁 제품(DynaPath·에버세이프·xQueue 등) 계약'],
  ['신규 7일', 'new7.csv', '최근 7일 새로 잡힌 낙찰·계약·대학 사업'],
  ['솔루션 연관', 'solution.csv', '자사 솔루션·API·경쟁 제품 관련 사업'],
  ['대학 사업', 'univ.csv', '대학 낙찰·계약·홈페이지 입찰'],
  ['진행중 IT 계약', 'active.csv', '진행중·종료 임박 IT 사업 전체'],
];

function onOpen() {
  SpreadsheetApp.getUi().createMenu('자동 업데이트')
    .addItem('지금 업데이트', 'syncAll')
    .addItem('매일 자동 실행 켜기 (08시)', 'installTrigger')
    .addToUi();
}

function installTrigger() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('syncAll').timeBased().everyDays(1).atHour(8).create();
  syncAll();
  SpreadsheetApp.getActive().toast('매일 08시에 자동 업데이트됩니다.', '완료', 5);
}

function csv_(name) {
  const res = UrlFetchApp.fetch(FEED + name, {muteHttpExceptions: true});
  if (res.getResponseCode() !== 200) return null;
  return Utilities.parseCsv(res.getContentText('UTF-8'));
}
const norm_ = s => String(s || '').replace(/\s+/g, '');

function syncAll() {
  const ss = SpreadsheetApp.getActive();
  const log = TABS.map(t => refreshTab_(ss, t[0], t[1], t[2]));
  const upd = csv_('updated.txt');
  ss.toast(log.join(' · '), '업데이트 완료 (자료 기준 ' + (upd ? upd[0][0] : '-') + ')', 8);
}

/** ③ 낙찰·계약 시트: 매일 통째로 새로 고침 */
function refreshTab_(ss, name, file, note) {
  const rows = csv_(file);
  if (!rows) return name + ' 자료 없음';
  let sh = ss.getSheetByName(name) || ss.insertSheet(name);
  sh.clear();
  const head = rows[0];
  const iUrl = head.indexOf('원문'), iTitle = head.indexOf('사업명'), iAmt = head.indexOf('금액(원)');
  const showCols = head.map((h, i) => i).filter(i => i !== iUrl);
  const out = rows.slice(1).map(r => showCols.map(i => {
    if (i === iTitle && r[iUrl] && /^https?:/.test(r[iUrl]) && r[iUrl].length < 240)
      return '=HYPERLINK("' + r[iUrl].replace(/"/g, '%22') + '","' + String(r[i]).replace(/"/g, '""').slice(0, 200) + '")';
    if (i === iAmt) return r[i] === '' ? '' : Number(r[i]);
    return r[i];
  }));
  sh.getRange(1, 1).setValue(note + ' — ' + (rows.length - 1).toLocaleString('ko-KR') + '건 (매일 08시 자동 갱신, 사업명 누르면 원문)').setFontWeight('bold');
  const hdr = showCols.map(i => head[i] === '사업명' ? '사업명 (누르면 원문)' : head[i]);
  sh.getRange(2, 1, 1, hdr.length).setValues([hdr]).setFontWeight('bold').setBackground('#1B2A4A').setFontColor('#FFFFFF');
  if (out.length) {
    sh.getRange(3, 1, out.length, hdr.length).setValues(out);
    const a = hdr.indexOf('금액(원)') + 1;
    if (a > 0) sh.getRange(3, a, out.length, 1).setNumberFormat('#,##0');
  }
  sh.setFrozenRows(2);
  sh.setFrozenColumns(5);
  if (sh.getFilter()) sh.getFilter().remove();
  sh.getRange(2, 1, Math.max(out.length, 1) + 1, hdr.length).createFilter();
  return name + ' ' + out.length + '건';
}
