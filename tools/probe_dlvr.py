# 납품요구 자료 위치 확인용 (일회성 조사): ① 쇼핑몰 품목정보 API 설명 페이지 ② 조달데이터허브 보고서 화면의 실제 데이터 요청 주소
import json, os, re, sys
from urllib.parse import unquote
import requests
out = "probe_out"; os.makedirs(out, exist_ok=True)
H = {"User-Agent": "Mozilla/5.0"}
for pid in ("15129471", "15129427", "15053481"):
    try:
        t = requests.get(f"https://www.data.go.kr/data/{pid}/openapi.do", headers=H, timeout=60).text
        open(f"{out}/openapi_{pid}.html", "w", encoding="utf-8").write(t)
        ops = sorted(set(re.findall(r"(get[A-Z][A-Za-z0-9]+)", t)))
        print(pid, "operations:", ops[:60])
        print(pid, "endpoints:", sorted(set(re.findall(r"https?://apis\.data\.go\.kr/[0-9A-Za-z/_]+", t)))[:20])
    except Exception as e:
        print(pid, "ERR", e)

key = unquote(os.getenv("G2B_SERVICE_KEY", "").strip())
def mask(s): return re.sub(r"serviceKey=[^&\s]+", "serviceKey=***", s)
cands = []
for t in open(f"{out}/openapi_15129471.html", encoding="utf-8").read().split():
    pass
base = "https://apis.data.go.kr/1230000/at/ShoppingMallPrdctInfoService"
for op in sorted(set(re.findall(r"(get[A-Z][A-Za-z0-9]+)", open(f"{out}/openapi_15129471.html", encoding="utf-8").read()))):
    cands.append(f"{base}/{op}")
for u in cands[:40]:
    try:
        r = requests.get(u, params={"serviceKey": key, "pageNo": 1, "numOfRows": 3, "type": "json",
                                    "inqryDiv": "1", "inqryBgnDate": "20260901", "inqryEndDate": "20260930"}, timeout=40)
        print("PROBE", u.rsplit("/", 1)[-1], r.status_code, mask(r.text[:400]).replace("\n", " "))
    except Exception as e:
        print("PROBE", u, "ERR", mask(str(e))[:200])

# 조달데이터허브 보고서 화면: 브라우저로 열어 데이터 요청(XHR) 주소 기록
from playwright.sync_api import sync_playwright
reqs = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    def on_resp(resp):
        try:
            if resp.request.resource_type in ("xhr", "fetch"):
                body = ""
                try: body = resp.text()[:1500]
                except Exception: pass
                reqs.append({"url": resp.url, "method": resp.request.method, "post": (resp.request.post_data or "")[:1500],
                             "status": resp.status, "body": body})
        except Exception:
            pass
    pg.on("response", on_resp)
    pg.goto("https://data.g2b.go.kr/link/AISC001_01/?reptNm=UI-ADOXAA-038R", timeout=90000)
    pg.wait_for_timeout(15000)
    pg.screenshot(path=f"{out}/hub.png", full_page=True)
    open(f"{out}/hub.html", "w", encoding="utf-8").write(pg.content())
    # 조회 버튼이 있으면 눌러 봄
    for sel in ["text=조회", "button:has-text('조회')", "#btnSearch"]:
        try:
            pg.click(sel, timeout=4000); pg.wait_for_timeout(12000); break
        except Exception:
            continue
    pg.screenshot(path=f"{out}/hub2.png", full_page=True)
    b.close()
json.dump(reqs, open(f"{out}/hub_xhr.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("XHR", len(reqs))
for r in reqs[:40]:
    print("XHR", r["method"], r["status"], r["url"][:200], "| post:", r["post"][:300])
