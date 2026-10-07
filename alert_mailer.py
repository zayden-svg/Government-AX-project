# alert_mailer.py
# 매일 아침 자동수집이 끝난 뒤 실행 — 메일 알림 등록자에게 '오늘의 영업 기회' 메일 발송
#   ① 오늘 새로 수집된 공고 중 AI 연관도 N점(기본 70) 이상 — 등록자가 고른 지역만
#   ② 경쟁사 수주 사업의 재발주 예상(향후 90일) — 다음 발주 전에 선제 영업
# 필요한 Secrets: SMTP_USER, SMTP_PASSWORD  (선택: SMTP_HOST, SMTP_PORT, MAIL_FROM, DASHBOARD_URL)
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

import common  # noqa: F401  (한국시간 고정)
from common import read_secret, match_regions, region_label, NATIONAL_LABEL
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
    return f"{n / 1e8:.1f}억" if n >= 1e7 else f"{n / 1e4:,.0f}만원"


def build_mail_html(sub, new_rows, reorder_rows, dashboard_url):
    regions_txt = ", ".join(sub["regions"]) if sub["regions"] else "전체 지역"
    if sub["regions"] and sub["include_national"]:
        regions_txt += f" + {NATIONAL_LABEL}"
    css_td = "padding:8px 10px;border-bottom:1px solid #E4E9F0;font-size:13px;vertical-align:top;"
    css_th = "padding:8px 10px;background:#191F28;color:#fff;font-size:12px;text-align:left;"

    def _table(headers, rows):
        head = "".join(f'<th style="{css_th}">{h}</th>' for h in headers)
        body = "".join("<tr>" + "".join(f'<td style="{css_td}">{c}</td>' for c in r) + "</tr>" for r in rows)
        return f'<table style="border-collapse:collapse;width:100%;border:1px solid #E4E9F0;">{head}{body}</table>'

    new_html = _table(
        ["점수", "공고명", "기관", "지역", "마감", "예산"],
        [[f'<b style="color:#B42318;">{int(r["_score"])}</b>',
          f'<a href="{escape(r["url"] or dashboard_url)}" style="color:#2D5BFF;text-decoration:none;font-weight:600;">{escape(r["title"])}</a>'
          + (f'<div style="color:#6B7684;font-size:12px;margin-top:3px;">{escape(r["ai_oneline"])}</div>' if r["ai_oneline"] else ""),
          escape(r["agency"]), escape(region_label(r["_regions"])), escape(r["due_date"] or "미정"),
          _fmt_money(r["budget"])] for r in new_rows]
    ) if new_rows else '<p style="color:#6B7684;font-size:13px;">오늘은 조건에 맞는 신규 공고가 없습니다.</p>'

    reorder_html = _table(
        ["예상 발주", "사업명", "기관", "수주업체", "금액", "계약 종료"],
        [[f'<b>{escape(r["expected"])}</b><div style="color:#6B7684;font-size:11px;">D-{max(int(r["d_day"]), 0)}</div>',
          escape(r["title"]), escape(r["agency"]), escape(r["company"]), _fmt_money(r["amount"]),
          escape(r["end_date"]) + (" (추정)" if r["end_est"] == "Y" else "")] for r in reorder_rows]
    ) if reorder_rows else ""

    return f"""
    <div style="font-family:'Malgun Gothic',Apple SD Gothic Neo,sans-serif;max-width:760px;color:#191F28;">
      <div style="background:#191F28;border-radius:10px;padding:16px 20px;color:#fff;">
        <div style="font-size:11px;letter-spacing:.1em;color:#9DB4FF;">GOV-TRACKER · 오늘의 영업 기회</div>
        <div style="font-size:20px;font-weight:800;margin-top:4px;">신규 고연관 공고 {len(new_rows)}건</div>
        <div style="font-size:12px;color:#C9D4E2;margin-top:4px;">기준: AI 연관도 {sub['min_score']}점 이상 · {escape(regions_txt)}</div>
      </div>
      <h3 style="font-size:15px;margin:20px 0 8px;">① 오늘 새로 올라온 고연관 공고</h3>
      {new_html}
      {'<h3 style="font-size:15px;margin:24px 0 8px;">② 경쟁사 수주 사업 — 재발주 예상 (향후 90일)</h3>' + reorder_html if reorder_html else ''}
      <p style="margin-top:22px;font-size:13px;"><a href="{escape(dashboard_url)}" style="color:#2D5BFF;">대시보드에서 전체 보기 →</a></p>
      <p style="font-size:11px;color:#9AA3AE;margin-top:16px;">
        재발주 예상 = 계약 종료일 − 60일 (낙찰 건은 계약기간 정보가 없어 1년으로 추정).
        수신 해제·조건 변경은 대시보드 상단 '📧 메일 알림'에서 할 수 있습니다.
      </p>
    </div>
    """


def send_mail(to_addr, subject, html):
    user = read_secret("SMTP_USER")
    pw = read_secret("SMTP_PASSWORD")
    host = read_secret("SMTP_HOST") or "smtp.gmail.com"
    port = int(read_secret("SMTP_PORT") or 587)
    sender = read_secret("MAIL_FROM") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Gov-Tracker", sender))
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(sender, [to_addr], msg.as_string())


def run(dry_run=False):
    if not dry_run and not (read_secret("SMTP_USER") and read_secret("SMTP_PASSWORD")):
        print("[메일] SMTP_USER / SMTP_PASSWORD 미설정 → 메일 발송을 건너뜁니다.")
        return 0
    subs = list_subscribers(active_only=True)
    if not subs:
        print("[메일] 등록된 수신자가 없습니다.")
        return 0

    df = load_active_postings()
    today = datetime.now().date()
    new_df = df[df["_created"].dt.date == today] if not df.empty else df
    reorder = load_reorder_candidates(horizon_days=90, include_solution=False)
    if not reorder.empty:
        reorder = reorder[reorder["d_day"] >= 0]          # 메일에는 앞으로 다가올 건만
    reorder_rows = reorder.head(10).to_dict("records") if not reorder.empty else []
    dashboard_url = read_secret("DASHBOARD_URL") or DASHBOARD_URL_DEFAULT

    sent = 0
    for sub in subs:
        rows = []
        if not new_df.empty:
            cand = new_df[new_df["_score"] >= sub["min_score"]].sort_values("_score", ascending=False)
            rows = [r for r in cand.to_dict("records")
                    if match_regions(r["_regions"], sub["regions"], sub["include_national"])][:20]
        if not rows and not reorder_rows:
            print(f"[메일] {sub['email']}: 보낼 내용 없음 → 생략")
            continue
        subject = f"[Gov-Tracker] {today:%m/%d} 신규 고연관 공고 {len(rows)}건" + \
                  (f" · 재발주 예상 {len(reorder_rows)}건" if reorder_rows else "")
        html = build_mail_html(sub, rows, reorder_rows, dashboard_url)
        if dry_run:
            print(f"[메일·테스트] {sub['email']}: {subject}")
            sent += 1
            continue
        try:
            send_mail(sub["email"], subject, html)
            sent += 1
            print(f"[메일] {sub['email']}: 발송 완료 ({subject})")
        except Exception as e:
            print(f"[메일] {sub['email']}: 발송 실패 — {type(e).__name__}: {e}")
    print(f"[메일] 총 {sent}명 발송")
    return sent


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
    sys.exit(0)
