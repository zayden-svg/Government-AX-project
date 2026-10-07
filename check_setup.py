# check_setup.py
# 자동수집 맨 앞에서 실행 — 설정(Secrets)과 접속 상태를 한눈에 보여준다. 키 값 자체는 절대 출력하지 않음.
#   DB 연결이 안 되면 여기서 바로 실패 처리(뒤 단계가 의미 없으므로), 나머지는 경고만 표시.
import sys

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import common  # noqa: F401  (한국시간 고정)
from common import read_secret

REQUIRED = ["DATABASE_URL"]
OPTIONAL = {
    "ANTHROPIC_API_KEY": "AI 분석·요약 (없으면 기관 기본값으로만 분류)",
    "G2B_SERVICE_KEY": "조달청 입찰·사전규격·낙찰·계약",
    "NAVER_CLIENT_ID": "네이버 뉴스",
    "NAVER_CLIENT_SECRET": "네이버 뉴스",
    "SMTP_USER": "메일 알림 발송",
    "SMTP_PASSWORD": "메일 알림 발송",
}
SITES = {
    "조달청 API": "https://apis.data.go.kr/1230000/ad/BidPublicInfoService",
    "IRIS": "https://www.iris.go.kr",
    "NTIS": "https://www.ntis.go.kr",
    "NIPA": "https://www.nipa.kr",
    "KERIS": "https://www.keris.or.kr",
    "TIPA": "https://www.tipa.or.kr/s040101",
    "KIAT(k-pass)": "https://k-pass.kr/notice/ancList.do",
    "KISA": "https://www.kisa.or.kr/403",
    "행정안전부": "https://www.mois.go.kr",
}


def main():
    ok = True
    print("=== 1) Secrets 등록 상태 (값은 표시하지 않음) ===")
    for name in REQUIRED:
        v = read_secret(name)
        print(f"  {'✅' if v else '❌'} {name}{'' if v else '  ← 필수! 없으면 수집 결과가 저장되지 않음'}")
        ok = ok and bool(v)
    for name, why in OPTIONAL.items():
        v = read_secret(name)
        print(f"  {'✅' if v else '⚠️'} {name} — {why}{'' if v else ' (미등록)'}")

    key = read_secret("ANTHROPIC_API_KEY")

    print("\n=== 2) DB 연결 ===")
    if read_secret("DATABASE_URL"):
        try:
            from sqlalchemy import text
            from db2 import get_engine, is_postgres
            with get_engine().begin() as conn:
                conn.execute(text("SELECT 1"))
            print(f"  ✅ 연결 성공 ({'Supabase(PostgreSQL)' if is_postgres() else 'SQLite'})")
        except Exception as e:
            ok = False
            print(f"  ❌ 연결 실패: {type(e).__name__}: {str(e)[:200]}")
            print("     → Supabase 비밀번호를 바꿨다면 DATABASE_URL도 새 비밀번호로 다시 등록하세요.")

    print("\n=== 3) Claude API 확인 (아주 짧은 시험 호출) ===")
    if key:
        try:
            from ai_utils import _call
            txt, err = _call("OK라고만 답해.", json_mode=False, max_tokens=5)
            if not err:
                print("  ✅ 정상 응답")
            elif "credit balance" in str(err):
                print("  ❌ 크레딧 잔액 부족 — platform.claude.com → Settings → Billing → Buy credits 후 다시 실행하세요. (AI 분석 없이 진행)")
            elif "authentication" in str(err).lower() or "401" in str(err):
                print("  ❌ API 키 인증 실패 — 키를 다시 발급해 Secrets에 등록하세요.")
            else:
                print(f"  ⚠️ 실패: {str(err)[:200]}")
        except Exception as e:
            print(f"  ⚠️ 실패: {type(e).__name__}: {e}")
    else:
        print("  ⚠️ 키 미등록 — AI 분석 없이 진행")

    print("\n=== 4) 수집 사이트 접속 (해외 서버 차단 여부 확인) ===")
    for name, url in SITES.items():
        try:
            r = requests.get(url, timeout=15, verify=("kisa" not in url), headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
            print(f"  ✅ {name}: 응답 {r.status_code} ({len(r.text):,}자)")
        except Exception as e:
            print(f"  ⚠️ {name}: 접속 실패 ({type(e).__name__}) — 이 사이트는 이번 실행에서 0건일 수 있음")

    print("\n결과:", "정상 — 수집을 시작합니다." if ok else "필수 설정 문제 — 위 ❌ 항목을 먼저 해결하세요.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
