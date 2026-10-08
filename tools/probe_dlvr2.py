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
for div in ("1", "2", "3"):
    go("getDlvrReqDtlInfoList", inqryDiv=div, inqryBgnDate="20260901", inqryEndDate="20260901")
go("getDlvrReqDtlInfoList", inqryDiv="1", inqryBgnDate="202609010000", inqryEndDate="202609012359")
go("getDlvrReqDtlInfoList", inqryDiv="1", inqryBgnDate="20260101", inqryEndDate="20261007")
go("getDlvrReqDtlInfoList", inqryDiv="1", inqryBgnDate="20260101", inqryEndDate="20261007", prdctIdntNoNm="NetFUNNEL")
go("getDlvrReqDtlInfoList", inqryDiv="1", inqryBgnDate="20260101", inqryEndDate="20261007", dtilPrdctClsfcNoNm="통신소프트웨어")
go("getDlvrReqDtlInfoList", inqryDiv="2", cntrctNo="R26TA01343941")
go("getDlvrReqDtlInfoList", inqryDiv="2", cntrctNo="002460509")
go("getDlvrReqDtlInfoList", inqryDiv="3", cntrctNo="002460509")
go("getDlvrReqInfoList", inqryDiv="1", inqryBgnDate="20260901", inqryEndDate="20260901")
go("getDlvrReqInfoList", inqryDiv="2", cntrctNo="002460509")
go("getDlvrReqInfoList", inqryDiv="1", inqryBgnDate="20250101", inqryEndDate="20251231", cntrctNo="002460509")
