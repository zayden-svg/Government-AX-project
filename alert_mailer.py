# alert_mailer.py
# 매일 아침 자동수집이 끝난 뒤 실행 — 메일 알림 등록자에게 '오늘의 영업 기회' 메일 발송
#   ① 오늘 새로 수집된 공고 중 AI 연관도 N점(기본 50) 이상 (막 등록한 사람은 진행 중 공고로 첫 메일)
#   ② 경쟁사 수주 사업의 재발주 예상(향후 90일) — 다음 발주 전에 선제 영업
# 필요한 Secrets: SMTP_USER, SMTP_PASSWORD  (선택: SMTP_HOST, SMTP_PORT, MAIL_FROM, DASHBOARD_URL)
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

import pandas as pd

import common  # noqa: F401  (한국시간 고정)
from common import read_secret, match_regions, region_label, NATIONAL_LABEL, owner_org
from postings_data import load_active_postings
from procurement_store import load_reorder_candidates
from store import list_subscribers

DASHBOARD_URL_DEFAULT = "https://government-ax-project-hlctf4bwddnqnq9y4zrrgm.streamlit.app"


def _fmt_money(v):
    try:
        n = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"
    if n >= 1e8:
        return f"{n / 1e8:,.1f}".rstrip("0").rstrip(".") + "억"
    return f"{n / 1e4:,.0f}만원"


# 메일 색상 — PDF 브리핑('공공 IT 데일리 브리핑')과 같은 팔레트, 메일 프로그램 호환을 위해 모두 인라인 스타일
_P = dict(paper="#F3F5F7", raised="#FFFFFF", ink="#1A2233", soft="#5B6472", faint="#8B93A1", line="#DBE0E6",
          accent="#1B4B6B", accent_soft="#DCE7EE", accent_ink="#0E3350", red="#B8402C", red_soft="#F6E4E0")
_FONT = "'Pretendard','Malgun Gothic','Apple SD Gothic Neo',sans-serif"
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def _dday(due_raw):
    try:
        d = datetime.strptime(str(due_raw or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return "미정", None
    left = (d - datetime.now().date()).days
    return ("D-DAY" if left == 0 else (f"D-{left}" if left > 0 else "마감")), left


def _section(num, title, sub, inner):
    return (f'<tr><td style="padding:22px 0 8px;"><span style="font-weight:800;color:{_P["accent"]};font-size:15px;">{num}</span>'
            f'<span style="font-size:15px;font-weight:700;margin-left:8px;">{title}</span>'
            f'<span style="font-size:12.5px;color:{_P["faint"]};margin-left:8px;">{sub}</span></td></tr>'
            f'<tr><td>{inner}</td></tr>')


def _rows_table(heads, rows, widths):
    th = "".join(f'<th style="text-align:left;font-size:11px;letter-spacing:.06em;color:{_P["faint"]};background:{_P["accent_soft"]};'
                 f'padding:9px 12px;font-weight:600;width:{w}%;">{h}</th>' for h, w in zip(heads, widths))
    body = "".join("<tr>" + "".join(f'<td style="padding:11px 12px;border-top:1px solid {_P["line"]};vertical-align:top;'
                                    f'font-size:13px;line-height:1.55;color:{_P["ink"]};">{c}</td>' for c in r) + "</tr>" for r in rows)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate;'
            f'background:{_P["raised"]};border:1px solid {_P["line"]};border-radius:8px;overflow:hidden;">'
            f'<tr>{th}</tr>{body}</table>')


def _empty(msg):
    return (f'<div style="font-size:13px;color:{_P["faint"]};background:{_P["raised"]};border:1px dashed #C3CAD3;'
            f'border-radius:6px;padding:12px 15px;">{msg}</div>')


def build_mail_html(sub, new_rows, reorder_rows, dashboard_url, welcome=False, headline=None, soon_rows=None,
                    pdf_names=None):
    now = datetime.now()
    regions_txt = ", ".join(sub["regions"]) if sub["regions"] else "전체 지역"
    if sub["regions"] and sub["include_national"]:
        regions_txt += f" + {NATIONAL_LABEL}"
    link_css = f"color:{_P['ink']};text-decoration:none;font-weight:600;"

    # I. 오늘의 헤드라인
    pts = [p for p in ((headline or {}).get("points") or []) if p][:3]
    hl_title = escape(str((headline or {}).get("headline") or ""))
    hl_items = "".join(f'<tr><td style="color:{_P["faint"]};font-size:13px;padding:4px 10px 4px 0;vertical-align:top;">{i:02d}</td>'
                       f'<td style="font-size:15px;line-height:1.55;padding:4px 0;">{escape(str(p))}</td></tr>'
                       for i, p in enumerate(pts, 1))
    hl_html = (f'<div style="background:{_P["raised"]};border:1px solid {_P["line"]};border-radius:8px;padding:18px 20px;">'
               f'<div style="font-size:13px;font-weight:800;letter-spacing:.08em;color:{_P["accent_ink"]};">오늘의 헤드라인</div>'
               + (f'<div style="font-size:17px;font-weight:800;margin:6px 0 8px;">{hl_title}</div>' if hl_title else "")
               + f'<table role="presentation" cellpadding="0" cellspacing="0">{hl_items}</table></div>') if pts else ""

    # ① 신규 고연관 공고
    new_tbl = _rows_table(
        ["공고명", "기관", "예산", "마감", "연관도"],
        [[f'<a href="{escape(r.get("url") or dashboard_url)}" style="{link_css}">{escape(r["title"])}</a>'
          + (f'<div style="color:{_P["faint"]};font-size:12px;margin-top:2px;">{escape(str(r.get("ai_oneline") or ""))}</div>' if r.get("ai_oneline") else ""),
          escape(owner_org(r["agency"], r.get("dept"))), _fmt_money(r.get("budget")),
          f'{escape(str(r.get("due_date") or "미정")[5:10] or "미정")}<div style="font-size:11px;color:{_P["faint"]};">{_dday(r.get("due_date"))[0]}</div>',
          f'<b style="color:{_P["red"] if int(r["_score"]) >= 70 else _P["accent"]};">{int(r["_score"])}점</b>']
         for r in new_rows], (44, 20, 11, 11, 14)) if new_rows else _empty("오늘은 기준 점수 이상인 신규 공고가 없습니다.")

    # ② 마감 임박 (7일 이내)
    soon_html = ""
    for r in (soon_rows or [])[:8]:
        dd, left = _dday(r.get("due_date"))
        hot = left is not None and left <= 3
        soon_html += (f'<tr><td style="padding:0 0 8px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                      f'style="background:{_P["raised"]};border:1px solid {_P["line"]};border-radius:6px;"><tr>'
                      f'<td style="width:70px;padding:11px 0 11px 14px;vertical-align:middle;"><span style="display:inline-block;font-weight:700;'
                      f'font-size:12.5px;color:{_P["red"] if hot else _P["accent_ink"]};background:{_P["red_soft"] if hot else _P["accent_soft"]};'
                      f'padding:4px 9px;border-radius:4px;white-space:nowrap;">{dd}</span></td>'
                      f'<td style="padding:11px 14px;"><a href="{escape(r.get("url") or dashboard_url)}" style="{link_css}font-size:14px;">'
                      f'{escape(r["title"])}</a><div style="font-size:12.5px;color:{_P["faint"]};margin-top:2px;">'
                      f'{escape(owner_org(r["agency"], r.get("dept")))} · {_fmt_money(r.get("budget"))} · 연관도 {int(r["_score"])}점</div></td>'
                      f'</tr></table></td></tr>')
    soon_html = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{soon_html}</table>' if soon_html
                 else _empty("7일 이내 마감인 관련 공고가 없습니다."))

    # ③ 재발주 예상
    reorder_html = _rows_table(
        ["예상 발주", "사업명", "기관", "수주업체", "금액"],
        [[f'<b>{escape(str(r["expected"]))}</b><div style="color:{_P["faint"]};font-size:11px;">D-{max(int(r["d_day"]), 0)}</div>',
          escape(r["title"]), escape(r["agency"]), escape(r["company"]), _fmt_money(r["amount"])] for r in reorder_rows],
        (14, 40, 18, 16, 12)) if reorder_rows else ""

    title = "메일 알림 등록 완료" if welcome else "공공 IT 데일리 브리핑"
    pdf_txt = (f'<div style="font-size:12.5px;color:{_P["soft"]};margin-top:6px;">📎 첨부: {escape(", ".join(pdf_names))}</div>'
               if pdf_names else "")
    return f"""<!doctype html><html><body style="margin:0;background:{_P['paper']};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_P['paper']};font-family:{_FONT};color:{_P['ink']};">
<tr><td align="center" style="padding:24px 12px 36px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:720px;">
  <tr><td style="padding-bottom:16px;border-bottom:2px solid {_P['ink']};">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="font-size:12.5px;color:{_P['faint']};">Gov-Tracker 메일 알림 · {now:%Y-%m-%d}</td>
      <td align="right"><span style="font-size:12px;color:{_P['accent_ink']};background:{_P['accent_soft']};padding:3px 9px;border-radius:3px;">
        {now.month}월 {now.day}일({WEEKDAY_KO[now.weekday()]}) 아침 수집 기준</span></td></tr></table>
    <div style="font-size:28px;font-weight:800;margin:12px 0 8px;line-height:1.2;">{title}</div>
    <div style="font-size:13px;color:{_P['soft']};">기준 <b style="color:{_P['ink']};">AI 연관도 {sub['min_score']}점 이상</b> · {escape(regions_txt)}
      · 신규 <b style="color:{_P['ink']};">{len(new_rows)}건</b> · 마감 임박 <b style="color:{_P['ink']};">{len(soon_rows or [])}건</b></div>
    {pdf_txt}
  </td></tr>
  {'<tr><td style="padding-top:20px;">' + hl_html + '</td></tr>' if hl_html else ''}
  {_section('①', '지금 진행 중인 고연관 공고' if welcome else '오늘 새로 올라온 고연관 공고', f'— {sub["min_score"]}점 이상', new_tbl)}
  {_section('②', '마감 임박', '— 7일 이내 · 연관도 40점 이상', soon_html)}
  {_section('③', '경쟁사 수주 사업 — 재발주 예상', '— 향후 90일', reorder_html) if reorder_html else ''}
  <tr><td style="padding-top:22px;">
    <a href="{escape(dashboard_url)}" style="display:inline-block;background:{_P['accent']};color:#FFFFFF;text-decoration:none;font-weight:700;
       font-size:13.5px;padding:10px 18px;border-radius:8px;">대시보드에서 전체 보기 →</a></td></tr>
  <tr><td style="padding-top:18px;border-top:1px solid {_P['line']};font-size:12px;color:{_P['faint']};line-height:1.6;">
    {'' if welcome else '매일 아침 8시 자동수집이 끝나면 발송됩니다. '}재발주 예상 = 계약 종료일 − 60일(낙찰 건은 계약기간 1년으로 추정).
    수신 해제는 대시보드 오른쪽 위 '📧 메일 알림'에서 할 수 있습니다.</td></tr>
</table></td></tr></table></body></html>"""


def send_mail(to_addr, subject, html, attachments=None):
    """attachments: [(파일명, bytes)] — PDF 브리핑 첨부"""
    from email.mime.application import MIMEApplication
    user = read_secret("SMTP_USER")
    pw = read_secret("SMTP_PASSWORD")
    host = read_secret("SMTP_HOST") or "smtp.gmail.com"
    port = int(read_secret("SMTP_PORT") or 587)
    sender = read_secret("MAIL_FROM") or user
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Gov-Tracker", sender))
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))
    for name, data in attachments or []:
        part = MIMEApplication(data, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", name))
        msg.attach(part)
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(sender, [to_addr], msg.as_string())


def _soon_rows(df, sub, days=7, min_score=40, limit=8):
    if df is None or df.empty:
        return []
    today = pd.Timestamp(datetime.now().date())
    cand = df[df["_due"].notna() & (df["_due"] >= today) & (df["_due"] <= today + pd.Timedelta(days=days))
              & (df["_score"] >= min_score)].sort_values(["_due", "_score"], ascending=[True, False])
    return [r for r in cand.to_dict("records")
            if match_regions(r["_regions"], sub["regions"], sub["include_national"])][:limit]


def _brief_extras():
    """메일에 함께 넣을 오늘의 헤드라인 + 사업부·R&D PDF (아침 배치가 저장한 것)"""
    import base64
    from store import load_cache
    headline = (load_cache("headline") or (None, None))[0]
    atts = []
    for key in ("briefing_pdf_biz", "briefing_pdf_rnd"):
        v = (load_cache(key) or (None, None))[0]
        if isinstance(v, dict) and v.get("b64"):
            atts.append((v.get("filename") or f"{key}.pdf", base64.b64decode(v["b64"])))
    return headline, atts


def smtp_ready():
    """메일 발송 계정(SMTP_USER·SMTP_PASSWORD)이 등록돼 있는지"""
    return bool(read_secret("SMTP_USER") and read_secret("SMTP_PASSWORD"))


def _pick_rows(df, sub, new_only=True, limit=20):
    """구독자 조건(점수·지역)에 맞는 공고. new_only=False면 오늘 신규가 아니어도 진행 중 공고 전체에서 고름"""
    if df is None or df.empty:
        return []
    cand = df
    if new_only:
        cand = cand[cand["_created"].dt.date == datetime.now().date()]
    cand = cand[cand["_score"] >= sub["min_score"]].sort_values("_score", ascending=False)
    return [r for r in cand.to_dict("records")
            if match_regions(r["_regions"], sub["regions"], sub["include_national"])][:limit]


def _reorder_rows():
    reorder = load_reorder_candidates(horizon_days=90, include_solution=False)
    if not reorder.empty:
        reorder = reorder[reorder["d_day"] >= 0]          # 메일에는 앞으로 다가올 건만
    return reorder.head(10).to_dict("records") if not reorder.empty else []


def _is_new_subscriber(sub):
    """어제 이후 등록한 사람 — 첫 메일은 오늘 신규가 없어도 진행 중 공고로 보냄"""
    try:
        created = datetime.strptime(str(sub.get("created_at") or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return (datetime.now().date() - created).days <= 1


def send_welcome(sub):
    """메일 알림 등록 직후 1회: 지금 진행 중인 고연관 공고로 '등록 완료' 메일 발송 (대시보드에서 호출)"""
    df = load_active_postings()
    rows = _pick_rows(df, sub, new_only=False, limit=10)
    reorder_rows = _reorder_rows()
    dashboard_url = read_secret("DASHBOARD_URL") or DASHBOARD_URL_DEFAULT
    headline, atts = _brief_extras()
    subject = f"[Gov-Tracker] 메일 알림 등록 완료 · 진행 중 고연관 공고 {len(rows)}건"
    send_mail(sub["email"], subject, build_mail_html(sub, rows, reorder_rows, dashboard_url, welcome=True,
                                                     headline=headline, soon_rows=_soon_rows(df, sub),
                                                     pdf_names=[a[0] for a in atts]), attachments=atts)
    return len(rows)


def run(dry_run=False):
    if not dry_run and not smtp_ready():
        print("[메일] SMTP_USER / SMTP_PASSWORD 미설정 → 메일 발송을 건너뜁니다. "
              "(GitHub Secrets에 등록하면 다음 실행부터 발송)")
        return 0
    subs = list_subscribers(active_only=True)
    if not subs:
        print("[메일] 등록된 수신자가 없습니다.")
        return 0

    df = load_active_postings()
    today = datetime.now().date()
    reorder_rows = _reorder_rows()
    dashboard_url = read_secret("DASHBOARD_URL") or DASHBOARD_URL_DEFAULT
    headline, atts = _brief_extras()

    sent = 0
    for sub in subs:
        rows = _pick_rows(df, sub, new_only=True)
        welcome = False
        if not rows and _is_new_subscriber(sub):
            rows, welcome = _pick_rows(df, sub, new_only=False, limit=10), True
        soon = _soon_rows(df, sub)
        if not rows and not reorder_rows and not soon:
            print(f"[메일] {sub['email']}: 보낼 내용 없음 → 생략")
            continue
        subject = (f"[Gov-Tracker] {today:%m/%d} {'진행 중' if welcome else '신규'} 고연관 공고 {len(rows)}건"
                   + (f" · 재발주 예상 {len(reorder_rows)}건" if reorder_rows else ""))
        html = build_mail_html(sub, rows, reorder_rows, dashboard_url, welcome=welcome, headline=headline,
                               soon_rows=soon, pdf_names=[a[0] for a in atts])
        if dry_run:
            print(f"[메일·테스트] {sub['email']}: {subject}")
            sent += 1
            continue
        try:
            send_mail(sub["email"], subject, html, attachments=atts)
            sent += 1
            print(f"[메일] {sub['email']}: 발송 완료 ({subject})")
        except Exception as e:
            print(f"[메일] {sub['email']}: 발송 실패 — {type(e).__name__}: {e}")
    print(f"[메일] 총 {sent}명 발송")
    return sent


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
    sys.exit(0)
