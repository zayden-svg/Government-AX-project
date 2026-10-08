# 일회성: 납품요구 API 요청값 형식 확인
import os, re, json, requests
from urllib.parse import unquote
k = (os.getenv("G2B_SERVICE_KEY") or "").strip(); k = unquote(k) if "%" in k else k
B = "https://apis.data.go.kr/1230000/at/ShoppingMallPrdctInfoService"
def go(op, **p):
    q = {"serviceKey": k, "type": "json", "pageNo": "1", "numOfRows": "3", **p}
    r = requests.get(f"{B}/{op}", params=q, timeout=60)
    t = re.sub(r"serviceKey=[^&\s]+", "***", r.text)
    try:
        b = r.json()["response"]["body"]; it = b.get("items") or []
        it = it.get("item", []) if isinstance(it, dict) else it
        it = [it] if isinstance(it, dict) else it
        print(op, p, "→ total", b.get("totalCount"), "| sample:", json.dumps(it[:1], ensure_ascii=False)[:900])
    except Exception:
        print(op, p, "→", r.status_code, t[:300])
for b, e in (("20260801", "20260831"), ("20260701", "20260930"), ("20260101", "20260331")):
    go("getDlvrReqDtlInfoList", inqryDiv="1", inqryBgnDate=b, inqryEndDate=e, prdctIdntNoNm="에스티씨랩")
for b, e in (("20250401", "20250430"), ("20190101", "20190131"), ("20170301", "20170331"), ("20150301", "20150331")):
    go("getDlvrReqDtlInfoList", inqryDiv="1", inqryBgnDate=b, inqryEndDate=e, prdctIdntNoNm="에스티씨랩")
    go("getDlvrReqDtlInfoList", inqryDiv="1", inqryBgnDate=b, inqryEndDate=e, prdctIdntNoNm="NetFUNNEL")
go("getDlvrReqDtlInfoList", inqryDiv="1", inqryBgnDate="20150301", inqryEndDate="20150301")
go("getDlvrReqDtlInfoList", inqryDiv="1", inqryBgnDate="20120301", inqryEndDate="20120301")
go("getDlvrReqDtlInfoList", inqryDiv="3", cntrctNo="002150153")
