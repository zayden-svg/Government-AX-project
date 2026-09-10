import re
from playwright.sync_api import sync_playwright

IRIS_LIST_URL = "https://www.iris.go.kr/contents/retrieveBsnsAncmListView.do"
IRIS_VIEW_URL = "https://www.iris.go.kr/contents/retrieveBsnsAncmView.do"

# onclick="f_bsnsAncmListForm_view('ancmId','bsnsYy','sorgnBsnsCd','bsnsAncmSn','dDay','접수시작','접수마감')"
ONCLICK_RE = re.compile(
    r"f_bsnsAncmListForm_view\('([^']*)','([^']*)','([^']*)','([^']*)','([^']*)','([^']*)','([^']*)'\)"
)


def fetch_iris(limit=20, max_pages=3):
    """IRIS(범부처통합연구지원시스템) 사업공고 목록을 Playwright로 수집한다."""
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(IRIS_LIST_URL, timeout=30000)
        page.wait_for_timeout(3000)

        for page_no in range(1, max_pages + 1):
            if page_no > 1:
                # 화면 내 페이징 버튼을 클릭하는 것과 동일한 효과 (내부 함수 직접 호출)
                page.evaluate(f"f_bsnsAncmListForm_search({page_no})")
                page.wait_for_timeout(2000)

            items = page.locator("li:has(a[onclick*='f_bsnsAncmListForm_view'])")
            count = items.count()
            if count == 0:
                break

            for i in range(count):
                li = items.nth(i)

                inst_title = ""
                if li.locator(".inst_title").count() > 0:
                    inst_title = li.locator(".inst_title").first.inner_text().strip()

                link = li.locator("a[onclick*='f_bsnsAncmListForm_view']").first
                title = link.inner_text().strip()
                onclick = link.get_attribute("onclick") or ""

                m = ONCLICK_RE.search(onclick)
                if not m:
                    continue
                ancm_id, bsns_yy, sorgn_bsns_cd, bsns_ancm_sn, d_day, rcve_from, rcve_to = m.groups()

                etc_info = {}
                if li.locator(".etc_info span").count() > 0:
                    spans = li.locator(".etc_info span")
                    for j in range(spans.count()):
                        span_text = spans.nth(j).inner_text().strip()
                        for label in ["세부사업명", "통합공고명", "내역사업명", "사업공고명"]:
                            if span_text.startswith(label):
                                etc_info[label] = span_text[len(label):].strip()

                dept, org = None, None
                if ">" in inst_title:
                    parts = inst_title.split(">")
                    dept = parts[0].strip()
                    org = parts[1].strip()

                item = {
                    "agency": "IRIS",
                    "unique_key": f"IRIS:{ancm_id}",
                    "title": title,
                    "부처": dept,
                    "기관": org,
                    "세부사업명": etc_info.get("세부사업명"),
                    "통합공고명": etc_info.get("통합공고명"),
                    "내역사업명": etc_info.get("내역사업명"),
                    "사업공고명": etc_info.get("사업공고명"),
                    "접수시작일": rcve_from,
                    "접수마감일": rcve_to,
                    "url": f"{IRIS_VIEW_URL}?ancmId={ancm_id}",
                }
                results.append(item)

                if len(results) >= limit:
                    browser.close()
                    return results

        browser.close()

    return results


if __name__ == "__main__":
    data = fetch_iris(limit=10)
    print(f"\n총 {len(data)}건 수집됨\n")
    for d in data:
        print(d)
