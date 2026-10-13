/**
 * 에스티씨랩 공공사업부 솔루션 도입 레퍼런스 — 매일 자동 업데이트
 * 자료: Gov-Tracker가 매일 06:30에 게시하는 공개 조달 자료 (sheets-feed)
 * 처음 한 번: 메뉴 [자동 업데이트] → [매일 자동 실행 켜기]
 */
const FEED = 'https://raw.githubusercontent.com/zayden-svg/Government-AX-project/sheets-feed/';
const REF_SHEETS = ['STCLab_레퍼런스', '웰컨_레퍼런스'];
const EMPTY_REGION = ['', '확인 필요', '수도권/전국', '미확인'];

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
  const log = [updateRegions_(ss), addNewReferences_(ss)];
  const upd = csv_('updated.txt');
  ss.toast(log.join(' · '), '업데이트 완료 (자료 기준 ' + (upd ? upd[0][0] : '-') + ')', 8);
}

/** ① 레퍼런스 지역: 비어 있거나 '확인 필요'인 기관을 조달청 사용자정보(수요기관 주소)로 채움 */
function updateRegions_(ss) {
  const rows = csv_('region.csv');
  if (!rows) return '지역표 없음';
  const map = {};
  rows.slice(1).forEach(r => { map[r[0]] = r[1]; });
  let n = 0;
  REF_SHEETS.forEach(name => {
    const sh = ss.getSheetByName(name);
    if (!sh) return;
    const last = sh.getLastRow();
    if (last < 4) return;
    const rng = sh.getRange(4, 2, last - 3, 2);
    const v = rng.getValues();
    v.forEach(row => {
      if (!row[0] || EMPTY_REGION.indexOf(String(row[1]).trim()) < 0) return;
      const k = norm_(row[0]);
      const hit = map[k] || map[k.replace(/\(.*?\)/g, '')] || map[(k.match(/\((.*?)\)/) || [])[1]];
      if (hit) { row[1] = hit; n++; }
    });
    rng.setValues(v);
  });
  return '지역 ' + n + '곳 채움';
}
/** ② 신규 레퍼런스: 자사 조달 납품 중 레퍼런스에 없는 건을 맨 아래에 추가 (노란 칸) */
function addNewReferences_(ss) {
  const sh = ss.getSheetByName('STCLab_레퍼런스');
  const rows = csv_('own_dlvr.csv');
  if (!sh || !rows) return '신규 레퍼런스 확인 불가';
  const last = sh.getLastRow();
  const k = sh.getRange(4, 11, Math.max(last - 3, 1), 1).getValues().map(r => String(r[0]));
  const have = {};
  k.forEach(t => { const m = t.match(/\((20\d{6})\)/); if (m) have[m[1] + '|' + t.replace(/\s/g, '').slice(0, 25)] = 1; });
  const haveDate = {};
  k.forEach(t => { const m = t.match(/\((20\d{6})\)/); if (m) haveDate[m[1]] = (haveDate[m[1]] || 0) + 1; });
  const today = Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd');
  const add = [];
  rows.slice(1).forEach(r => {
    const [org, region, date, qty, prod, title] = r;
    const key = date + '|' + ('[조달]' + title).replace(/\s/g, '').slice(0, 25);
    if (have[key]) return;
    if (haveDate[date] && k.some(t => t.indexOf('(' + date + ')') >= 0 && t.replace(/\s/g, '').indexOf(String(title).replace(/\s/g, '').slice(0, 12)) >= 0)) return;
    const mb = prod.indexOf('MBUSTER') >= 0;
    const ver = mb ? 'MB2.0' : (prod.indexOf('v4') >= 0 ? 'NF4' : 'NF3');
    const q = Number(qty) || 0;
    add.push(['', org, region || '확인 필요', '', ver === 'NF3' ? '● Renwal 타겟' : '최신버전 ', 1, '1/1',
              mb ? 'MB (엠버스터)' : 'NF-License', ver, date.slice(0, 4) + '년',
              '[조달] ' + title + ' (' + date + ')', '[조달구매내역 자동추가] ' + prod,
              mb ? ('도메인 ' + q + '개 (조달)') : ((q * 5000).toLocaleString('ko-KR') + '유저 (조달 ' + q + '개)'),
              '🆕 자동추가 ' + today]);
    have[key] = 1;
  });
  if (add.length) {
    const r0 = sh.getLastRow() + 1;
    sh.getRange(r0, 1, add.length, 14).setValues(add).setBackground('#FFF59D');
  }
  return '신규 레퍼런스 ' + add.length + '건';
}
