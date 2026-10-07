# doc_text.py
# 공고 첨부파일(한글 HWP·HWPX, PDF)에서 글자만 뽑아낸다.
#   - 상세페이지 본문에 예산·마감이 없고 "첨부 공고문 참조"인 기관(TIPA·KERIS·KISA 등) 대응
#   - 실패해도 빈 문자열을 돌려주고 수집은 계속 진행 (첨부 분석은 '보조 수단')
import io
import re
import struct
import zipfile
import zlib

import requests

MAX_BYTES = 15 * 1024 * 1024          # 15MB 넘는 첨부는 건너뜀
DOC_EXT_RE = re.compile(r"\.(hwpx|hwp|pdf)\b", re.I)
# 공고문 성격의 첨부를 먼저 읽기 위한 우선순위 단어 (서식·양식·동의서는 뒤로)
_GOOD_WORDS = ["공고", "공고문", "공고서", "안내", "제안요청", "과업지시", "모집", "공모", "규격", "시행"]
_BAD_WORDS = ["서식", "양식", "동의서", "신청서", "서약서", "확약서", "체크리스트", "별첨서류", "참고자료"]


def _hwpx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = sorted(n for n in z.namelist() if re.search(r"Contents/section\d+\.xml$", n, re.I))
        parts = []
        for n in names:
            xml = z.read(n).decode("utf-8", "ignore")
            xml = re.sub(r"</hp:p>", "\n", xml)
            xml = re.sub(r"<hp:lineBreak\s*/>", "\n", xml)
            xml = re.sub(r"<[^>]+>", "", xml)
            parts.append(xml)
    txt = "\n".join(parts)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'")):
        txt = txt.replace(a, b)
    return txt


# HWP 5.0 문단 텍스트 속 제어문자: 8글자(16바이트)를 차지하는 것과 1글자인 것
_HWP_CTRL_8 = set(range(1, 10)) | {11, 12} | set(range(14, 24))
_HWPTAG_PARA_TEXT = 67


def _hwp_para_text(buf):
    out, i, n = [], 0, len(buf) // 2
    while i < n:
        ch = struct.unpack_from("<H", buf, i * 2)[0]
        if ch in _HWP_CTRL_8:
            i += 8
            continue
        if ch in (10, 13):
            out.append("\n")
        elif ch == 9:
            out.append(" ")
        elif ch >= 32:
            out.append(chr(ch))
        i += 1
    return "".join(out)


def _hwp_text(data):
    import olefile
    ole = olefile.OleFileIO(io.BytesIO(data))
    try:
        header = ole.openstream("FileHeader").read()
        flags = struct.unpack_from("<I", header, 36)[0]
        compressed = bool(flags & 0x01)
        if flags & 0x02:                    # 암호가 걸린 문서
            return ""
        sections = sorted(
            ("/".join(p) for p in ole.listdir() if len(p) == 2 and p[0] == "BodyText" and p[1].startswith("Section")),
            key=lambda s: int(re.sub(r"\D", "", s) or 0),
        )
        if not sections and ole.exists("PrvText"):      # 배포용 문서 등: 미리보기 글자라도 사용
            return ole.openstream("PrvText").read().decode("utf-16-le", "ignore")
        texts = []
        for sec in sections:
            raw = ole.openstream(sec).read()
            if compressed:
                try:
                    raw = zlib.decompress(raw, -15)
                except zlib.error:
                    continue
            pos, size_all = 0, len(raw)
            while pos + 4 <= size_all:
                h = struct.unpack_from("<I", raw, pos)[0]
                pos += 4
                tag, size = h & 0x3FF, (h >> 20) & 0xFFF
                if size == 0xFFF:
                    if pos + 4 > size_all:
                        break
                    size = struct.unpack_from("<I", raw, pos)[0]
                    pos += 4
                if tag == _HWPTAG_PARA_TEXT:
                    texts.append(_hwp_para_text(raw[pos:pos + size]))
                pos += size
        txt = "\n".join(texts)
        if not txt.strip() and ole.exists("PrvText"):
            txt = ole.openstream("PrvText").read().decode("utf-16-le", "ignore")
        return txt
    finally:
        ole.close()


def _pdf_text(data, max_pages=12):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])


def bytes_to_text(data, name=""):
    """파일 내용(bytes) → 글자. 형식은 파일 앞부분(매직넘버)으로 판별"""
    if not data:
        return ""
    try:
        if data[:4] == b"PK\x03\x04":
            return _hwpx_text(data)
        if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            return _hwp_text(data)
        if data[:5] == b"%PDF-":
            return _pdf_text(data)
    except Exception as e:
        print(f"[WARN] 첨부 읽기 실패({name[:40]}): {type(e).__name__}: {str(e)[:80]}")
    return ""


def fetch_doc_text(url, session=None, headers=None, verify=True, timeout=25, name=""):
    """첨부 URL을 내려받아 글자만 돌려준다. 실패 시 ''"""
    try:
        req = session or requests
        r = req.get(url, headers=headers, timeout=timeout, verify=verify, stream=True)
        r.raise_for_status()
        buf = io.BytesIO()
        for chunk in r.iter_content(64 * 1024):
            buf.write(chunk)
            if buf.tell() > MAX_BYTES:
                return ""
        return bytes_to_text(buf.getvalue(), name or url)
    except Exception as e:
        print(f"[WARN] 첨부 내려받기 실패({(name or url)[:50]}): {type(e).__name__}")
        return ""


def rank_attachments(items):
    """[(이름, url), ...] → 공고문 성격 순으로 정렬, 문서 형식(hwp·hwpx·pdf)만"""
    docs = [(n, u) for n, u in items if u and DOC_EXT_RE.search(n or "")]

    def _score(item):
        n = item[0]
        s = 0
        s += 3 if any(w in n for w in _GOOD_WORDS) else 0
        s -= 4 if any(w in n for w in _BAD_WORDS) else 0
        s += 1 if re.search(r"\.hwpx?\b", n, re.I) else 0      # 한글 문서가 PDF보다 글자 추출이 정확
        return -s
    return sorted(docs, key=_score)
