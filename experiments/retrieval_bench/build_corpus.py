# -*- coding: utf-8 -*-
"""PDF -> 청크(JSONL). 로더별로 코퍼스를 만들어 A/B 비교할 수 있게 한다.

usage: python build_corpus.py <loader>
  loader: pymupdf4llm | pypdf | pdfplumber
표는 분할하지 않는다.
"""
import json, re, sys, pathlib

DOCS = {
    "itme": "/Users/youngjun/Downloads/2606.12556v2.pdf",
    "dsv2": "/Users/youngjun/Downloads/2405.04434v5.pdf",
}
TARGET_TOK, CHARS_PER_TOK = 900, 3.6
MAX_CHARS = int(TARGET_TOK * CHARS_PER_TOK)
HERE = pathlib.Path(__file__).parent


# ── 로더별 페이지 텍스트 추출 ────────────────────────────────
def pages_pymupdf4llm(path):
    import pymupdf4llm, pymupdf
    n = pymupdf.open(path).page_count
    for p in range(n):
        yield p + 1, pymupdf4llm.to_markdown(path, pages=[p], show_progress=False)

def pages_pypdf(path):
    from pypdf import PdfReader
    for i, pg in enumerate(PdfReader(path).pages):
        yield i + 1, (pg.extract_text() or "")

def pages_pdfplumber(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        for i, pg in enumerate(pdf.pages):
            yield i + 1, (pg.extract_text() or "")

LOADERS = {"pymupdf4llm": pages_pymupdf4llm, "pypdf": pages_pypdf, "pdfplumber": pages_pdfplumber}


# ── 청킹: rag.chunking 을 단일 소스로 사용 ───────────────────
# (프로덕션 파이프라인과 동일한 청킹이어야 측정값이 유효하다)
sys.path.insert(0, str(HERE.parent.parent))
from rag.chunking import chunk_page  # noqa: E402


def main():
    loader = sys.argv[1] if len(sys.argv) > 1 else "pymupdf4llm"
    assert loader in LOADERS, f"unknown loader: {loader}"
    fn = LOADERS[loader]

    all_chunks = []
    for tech_id, path in DOCS.items():
        for page, text in fn(path):
            all_chunks += chunk_page(text, tech_id, page)
    for i, c in enumerate(all_chunks):
        c["chunk_id"] = f"{c['tech_id']}-{i:04d}"

    out = HERE / f"chunks_{loader}.jsonl"
    with out.open("w") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    tbl = sum(c["has_table"] for c in all_chunks)
    chars = sum(len(c["text"]) for c in all_chunks)
    print(f"[{loader}] 청크 {len(all_chunks)}개 (마크다운표 {tbl}) · 총 {chars:,}자 -> {out.name}")

if __name__ == "__main__":
    main()
