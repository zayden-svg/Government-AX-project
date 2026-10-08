# 수동 조사용: 주어진 주소들을 내려받아 저장 (+ 페이지 안 첨부파일 링크도 따라가 받음)
import os, re, sys, requests
from urllib.parse import urljoin
os.makedirs("out", exist_ok=True)
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
for i, u in enumerate([x.strip() for x in sys.argv[1].split(",,") if x.strip()]):
    try:
        if u.startswith("POST "):        # "POST 주소|키=값;키=값"
            url, _, body = u[5:].partition("|")
            r = requests.post(url, data=dict(kv.split("=", 1) for kv in body.split(";") if "=" in kv), headers=H, timeout=120)
        else:
            r = requests.get(u, headers=H, timeout=60)
        open(f"out/page_{i}.html", "wb").write(r.content)
        print(i, u, r.status_code, len(r.content))
        t = r.text
        links = set(re.findall(r"""(?:href|onclick)=["']([^"']*(?:down|Down|file|File|atch)[^"']*)["']""", t))
        for j, l in enumerate(sorted(links)[:15]):
            print("   link:", l[:200])
        for j, (a, b) in enumerate(re.findall(r"fn_?[a-zA-Z]*[Dd]own[a-zA-Z]*\(\s*'([^']*)'\s*,\s*'([^']*)'", t)[:5]):
            print("   js-down:", a, b)
    except Exception as e:
        print(i, u, "ERR", e)
