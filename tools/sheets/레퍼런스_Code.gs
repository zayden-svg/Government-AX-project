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
/**
 * STCLab 솔루션 도입 레퍼런스 — 지금까지 정리한 레퍼런스 전체 + 매일 08시 자동 업데이트
 * - 처음 실행 시 레퍼런스 3개 시트(종합대시보드·STCLab·웰컨)를 이 파일 아래 REF 자료로 만듦
 * - 지역 '확인 필요'·'수도권/전국': ① 조달청 나라장터 사용자정보 ② 기관 홈페이지 하단 주소 순으로 자동 기재
 * - 기관특성 빈칸: 기존 분류표(공공기관/공기업·대학교/교육기관·중앙행정기관·도시공사/시설공단·시청·교육청·문화/공공재단·도청·구청·군청·병원/의료원)로 자동 분류
 * - 새 자사 조달 납품 → STCLab_레퍼런스 맨 아래 노란 칸으로 자동 추가 (라이선스 1개 = 5,000유저)
 */
const REF_SHEETS = ['STCLab_레퍼런스', '웰컨_레퍼런스'];
const EMPTY_REGION = ['', '확인 필요', '수도권/전국', '미확인'];
const MAX_WEB = 40;   // 하루에 홈페이지를 열어 볼 최대 기관 수 (실행 시간 6분 제한)

function syncAll() {
  const ss = SpreadsheetApp.getActive();
  const log = [];
  if (PropertiesService.getScriptProperties().getProperty('refInit') !== 'done') log.push(initImport_(ss));
  const ag = agencies_();
  log.push(addNewReferences_(ss, ag));
  log.push(fillKinds_(ss, ag));
  log.push(updateRegions_(ss, ag));
  ss.toast(log.join(' · '), '업데이트 완료', 10);
}

/* ---------- 처음 한 번: 레퍼런스 시트 만들기 ---------- */
function initImport_(ss) {
  const data = JSON.parse(REF);
  Object.keys(data).forEach((name, idx) => {
    const d = data[name];
    const old = ss.getSheetByName(name);
    if (old && name === '종합대시보드' && old.getCharts().length) return;  // 직접 만든 대시보드(차트)는 보존
    if (old) ss.deleteSheet(old);          // 이전에 만들다 멈춘 시트는 지우고 새로
    const sh = ss.insertSheet(name, idx);
    const nr = d.v.length, nc = Math.max.apply(null, d.v.map(r => r.length));
    const vals = d.v.map(r => { const x = r.slice(); while (x.length < nc) x.push(''); return x; });
    if (sh.getMaxRows() < nr + 10) sh.insertRowsAfter(sh.getMaxRows(), nr + 10 - sh.getMaxRows());
    if (sh.getMaxColumns() < nc) sh.insertColumnsAfter(sh.getMaxColumns(), nc - sh.getMaxColumns());
    sh.getRange(1, 1, nr, nc).setValues(vals).setFontSize(9).setVerticalAlignment('middle');
    const bg = d.bg.map(c => Array(nc).fill(c || null));
    sh.getRange(1, 1, nr, nc).setBackgrounds(bg);
    (d.m || []).forEach(a1 => { try { sh.getRange(a1).merge(); } catch (e) {} });
    Object.keys(d.w || {}).forEach(col => {
      const n = col.split('').reduce((s, ch) => s * 26 + ch.charCodeAt(0) - 64, 0);
      if (n <= nc) sh.setColumnWidth(n, Math.round(d.w[col] * 7));
    });
    sh.getRange(1, 1).setFontSize(13).setFontWeight('bold');
    if (name !== '종합대시보드') {
      sh.getRange(3, 1, 1, nc).setFontWeight('bold').setFontColor('#FFFFFF').setBackground('#2F5597');
      sh.setFrozenRows(3);
      sh.getRange(3, 1, nr - 2, nc).createFilter();
    }
  });
  PropertiesService.getScriptProperties().setProperty('refInit', 'done');
  return '레퍼런스 시트 생성';
}

/* ---------- 기관 자료 (나라장터 사용자정보) ---------- */
function agencies_() {
  const rows = csv_('agency.csv');
  const map = {};
  (rows || []).slice(1).forEach(r => { map[r[0]] = {region: r[1], homepage: r[2], type: r[3]}; });
  return map;
}
function findAg_(ag, name) {
  const k = norm_(name);
  return ag[k] || ag[k.replace(/\(.*?\)/g, '')] || ag[(k.match(/\((.*?)\)/) || [])[1]] || null;
}

/* ---------- 기관특성 분류 (기존 분류표) ---------- */
function kindOf_(name, type) {
  const n = norm_(name), cls = String(type || '');
  if (/병원|의료원/.test(n)) return '병원/의료원';
  if (/교육청|교육지원청|교육연구정보원|교육정보원|교육연구원|교육정보기록원/.test(n)) return '교육청';
  if (/대학|학교|과학기술원|KAIST|UNIST|GIST|DGIST/.test(n) || /고등교육|초중등/.test(cls)) return '대학교/교육기관';
  if (/도시공사|시설공단|시설관리공단|도시관리공단|도시공단|도시개발공사|시설안전관리공단/.test(n)) return '도시공사/시설공단';
  if (/재단|문화회관|예술의전당/.test(n)) return '문화/공공재단';
  if (/도청$/.test(n) || /^(경기도|강원도|충청북도|충청남도|전라북도|전라남도|경상북도|경상남도|제주특별자치도|강원특별자치도|전북특별자치도)$/.test(n)) return '도청';
  if (/구청$/.test(n) || (/[가-힣]구$/.test(n) && !/연구$/.test(n))) return '구청';
  if (/군청$|[가-힣]군$/.test(n)) return '군청';
  if (/시청$|[가-힣]시$/.test(n)) return '시청';
  if (/^한국|공단$|공사$|진흥원$|협회$|원$/.test(n) && !/중앙행정/.test(cls)) return '공공기관/공기업';
  if (/(부|처|청|위원회|대법원|헌법재판소|국회사무처|감사원)$/.test(n) || /중앙행정/.test(cls)) return '중앙행정기관';
  return '공공기관/공기업';
}
function fillKinds_(ss, ag) {
  let n = 0;
  REF_SHEETS.forEach(name => {
    const sh = ss.getSheetByName(name);
    if (!sh || sh.getLastRow() < 4) return;
    const rng = sh.getRange(4, 2, sh.getLastRow() - 3, 3);
    const v = rng.getValues();
    let changed = false;
    v.forEach(r => {
      if (r[0] && !String(r[2]).trim()) { const a = findAg_(ag, r[0]); r[2] = kindOf_(r[0], a && a.type); n++; changed = true; }
    });
    if (changed) sh.getRange(4, 4, v.length, 1).setValues(v.map(r => [r[2]]));
  });
  return '기관특성 ' + n + '곳 분류';
}

/* ---------- 지역: 사용자정보 → 홈페이지 하단 주소 ---------- */
const SIDO = [[/^서울/, '서울'], [/^부산/, '부산'], [/^대구/, '대구'], [/^인천/, '인천'], [/^광주/, '광주'], [/^대전/, '대전'], [/^울산/, '울산'], [/^세종/, '세종'],
  [/^경기/, '경기'], [/^강원/, '강원'], [/^(충청북|충북)/, '충북'], [/^(충청남|충남)/, '충남'], [/^(전라북|전북)/, '전북'], [/^(전라남|전남)/, '전남'],
  [/^(경상북|경북)/, '경북'], [/^(경상남|경남)/, '경남'], [/^제주/, '제주']];
const sido_ = t => { for (const [r, v] of SIDO) if (r.test(String(t || '').trim())) return v; return ''; };
const ADDR_RE = /(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충청북도|충북|충청남도|충남|전라북도|전북|전라남도|전남|경상북도|경북|경상남도|경남|제주)[가-힣]{0,6}\s+[가-힣]{1,8}(시|군|구)\s/g;

function get_(url) {
  try {
    const r = UrlFetchApp.fetch(url, {muteHttpExceptions: true, followRedirects: true, validateHttpsCertificates: false,
      headers: {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124'}});
    if (r.getResponseCode() >= 400) return '';
    let t = r.getContentText('UTF-8');
    if ((t.match(/�/g) || []).length > 20) t = r.getContentText('EUC-KR');
    return t;
  } catch (e) { return ''; }
}
function addrFrom_(html) {
  const text = html.replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/gi, ' ').replace(/<[^>]+>/g, ' ').replace(/&nbsp;|&#160;/g, ' ').replace(/\s+/g, ' ');
  const pick = s => { const m = s.match(ADDR_RE); return m ? m[m.length - 1].trim() : ''; };
  return pick(text.slice(-4000)) || pick(text);   // 하단(푸터) 주소 우선
}
function searchHomepage_(name) {
  const urls = [];
  [get_('https://html.duckduckgo.com/html/?q=' + encodeURIComponent(name + ' 홈페이지')),
   get_('https://www.bing.com/search?q=' + encodeURIComponent(name + ' 공식 홈페이지'))].forEach(h => {
    (h.match(/uddg=([^&"]+)/g) || []).forEach(m => urls.push(decodeURIComponent(m.slice(5))));
    (h.match(/<a href="(https?:\/\/[^"]+)"/g) || []).forEach(m => urls.push(m.slice(9, -1)));
  });
  const good = urls.filter(u => /\.(go|or|ac|re|kr)\.kr|\.kr\//.test(u) && !/namu|wiki|blog|news|tistory|youtube|facebook|instagram|bing\.com|duckduckgo|microsoft/.test(u));
  try { return good.length ? good[0].match(/^https?:\/\/[^\/]+/)[0] : ''; } catch (e) { return ''; }
}

function resolveRegion_(ag, name, budget) {
  const a = findAg_(ag, name);
  if (a && a.region) return {region: a.region, src: '나라장터 사용자정보'};
  if (budget.n >= MAX_WEB) return null;
  budget.n++;
  let hp = a && a.homepage ? a.homepage : '';
  if (!hp) hp = searchHomepage_(String(name).replace(/\(.*?\)/g, ''));
  if (!hp) return null;
  const addr = addrFrom_(get_(/^https?:/.test(hp) ? hp : 'http://' + hp));
  const r = sido_(addr);
  return r ? {region: r, src: '홈페이지 하단 주소: ' + addr + ' (' + hp + ')'} : null;
}

function updateRegions_(ss, ag) {
  let n = 0, web = 0;
  const budget = {n: 0};
  REF_SHEETS.forEach(name => {
    const sh = ss.getSheetByName(name);
    if (!sh || sh.getLastRow() < 4) return;
    const rng = sh.getRange(4, 2, sh.getLastRow() - 3, 2);
    const v = rng.getValues();
    v.forEach((row, i) => {
      if (!row[0] || EMPTY_REGION.indexOf(String(row[1]).trim()) < 0) return;
      const hit = resolveRegion_(ag, row[0], budget);
      if (!hit) return;
      row[1] = hit.region; n++;
      if (hit.src !== '나라장터 사용자정보') web++;
      sh.getRange(4 + i, 3).setNote('지역 출처: ' + hit.src);
    });
    sh.getRange(4, 3, v.length, 1).setValues(v.map(r => [r[1]]));
  });
  return '지역 ' + n + '곳 기재 (홈페이지 ' + web + ')';
}

/* ---------- 새 자사 조달 납품 → 레퍼런스 추가 ---------- */
function addNewReferences_(ss, ag) {
  const sh = ss.getSheetByName('STCLab_레퍼런스');
  const rows = csv_('own_dlvr.csv');
  if (!sh || !rows) return '신규 레퍼런스 확인 불가';
  const last = sh.getLastRow();
  const k = sh.getRange(4, 11, Math.max(last - 3, 1), 1).getValues().map(r => String(r[0]).replace(/\s/g, ''));
  const today = Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd');
  const budget = {n: 0};
  const add = [];
  rows.slice(1).forEach(r => {
    const [org, region, date, qty, prod, title] = r;
    const t12 = String(title).replace(/\s/g, '').slice(0, 12);
    if (k.some(t => t.indexOf('(' + date + ')') >= 0 && t.indexOf(t12) >= 0)) return;
    const mb = prod.indexOf('MBUSTER') >= 0;
    const ver = mb ? 'MB2.0' : (prod.indexOf('v4') >= 0 ? 'NF4' : 'NF3');
    const q = Number(qty) || 0;
    const a = findAg_(ag, org);
    const reg = region || (resolveRegion_(ag, org, budget) || {}).region || '확인 필요';
    add.push(['', org, reg, kindOf_(org, a && a.type), ver === 'NF3' ? '● Renwal 타겟' : '최신버전 ', 1, '1/1',
              mb ? 'MB (엠버스터)' : 'NF-License', ver, date.slice(0, 4) + '년',
              '[조달] ' + title + ' (' + date + ')', '[조달구매내역 자동추가] ' + prod,
              mb ? ('도메인 ' + q + '개 (조달)') : ((q * 5000).toLocaleString('ko-KR') + '유저 (조달 ' + q + '개)'),
              '🆕 자동추가 ' + today]);
    k.push(('[조달]' + title + '(' + date + ')').replace(/\s/g, ''));
  });
  if (add.length) sh.getRange(sh.getLastRow() + 1, 1, add.length, 14).setValues(add).setBackground('#FFF59D');
  return '신규 레퍼런스 ' + add.length + '건';
}

// REF(레퍼런스 원자료)는 고객사 정보라 저장소에 올리지 않음
