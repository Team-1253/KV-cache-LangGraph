# -*- coding: utf-8 -*-
"""PDF 로딩 — PyPDFLoader 확정.

로더 선정 근거(검색 성능 A/B)는 `docs/AGENT-기술조사.md` §3-1.
PDFPlumberLoader는 본 문서군에서 원문 일부를 소실하므로 사용하지 않는다.
"""
from __future__ import annotations

from pathlib import Path

from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document

from rag.chunking import chunk_page


def load_pdf_chunks(path: str | Path, tech_id: str) -> list[Document]:
    """PDF 1건 -> 표 보존 청크(Document) 리스트."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF를 찾을 수 없습니다: {path}")

    pages = PyPDFLoader(str(path)).load()
    chunks: list[Document] = []
    for doc in pages:
        page_no = int(doc.metadata.get("page", 0)) + 1
        for c in chunk_page(doc.page_content, tech_id, page_no):
            chunks.append(
                Document(
                    page_content=c["text"],
                    metadata={
                        "tech_id": tech_id,
                        "source": path.name,
                        "page": page_no,
                        "has_table": c["has_table"],
                    },
                )
            )

    for i, d in enumerate(chunks):
        d.metadata["chunk_id"] = f"{tech_id}-{i:04d}"
    return chunks
