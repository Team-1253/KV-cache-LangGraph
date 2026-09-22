# -*- coding: utf-8 -*-
"""FAISS 인덱스 — 기술별 네임스페이스 분리.

두 논문의 분량이 4배 차이나므로(ITME 13p vs DeepSeek-V2 52p) 공통 풀에서 top-k를
뽑으면 분량이 많은 쪽이 검색을 잠식한다. 기술별로 인덱스를 나눠 각각 top-k를 뽑는다.

※ 공용 모듈. 시장·도메인 평가 Agent도 재사용할 수 있다.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document

from rag.embeddings import BGEM3Embeddings
from rag.loader import load_pdf_chunks

INDEX_ROOT = Path(".cache/faiss_index")


def _fingerprint(docs: list[Document]) -> str:
    return hashlib.md5("\n".join(d.page_content for d in docs).encode()).hexdigest()


class TechRetriever:
    """기술 1건에 대한 인덱스 + 검색기."""

    def __init__(self, tech_id: str, pdf_path: str, k: int = 5, embeddings=None):
        self.tech_id = tech_id
        self.pdf_path = pdf_path
        self.k = k
        self.embeddings = embeddings or BGEM3Embeddings()
        self.index_dir = INDEX_ROOT / tech_id
        self.store: FAISS | None = None

    def build(self) -> "TechRetriever":
        """인덱스를 만들거나, 문서가 그대로면 캐시에서 로드한다."""
        docs = load_pdf_chunks(self.pdf_path, self.tech_id)
        fp = _fingerprint(docs)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        fp_file = self.index_dir / "fingerprint.txt"
        idx_dir = self.index_dir / "index"          # FAISS.save_local 은 디렉토리를 만든다
        idx_path = str(idx_dir)

        if (
            fp_file.exists()
            and fp_file.read_text().strip() == fp
            and (idx_dir / "index.faiss").exists()
        ):
            self.store = FAISS.load_local(
                idx_path, self.embeddings, allow_dangerous_deserialization=True
            )
            print(f"[{self.tech_id}] 캐시된 인덱스 로드 ({len(docs)}청크)")
        else:
            print(f"[{self.tech_id}] 인덱스 생성 중… {len(docs)}청크")
            self.store = FAISS.from_documents(docs, self.embeddings)
            self.store.save_local(idx_path)
            fp_file.write_text(fp)
        return self

    def search(self, query: str, k: int | None = None) -> list[Document]:
        if self.store is None:
            raise RuntimeError("build()를 먼저 호출하세요.")
        return self.store.similarity_search(query, k=k or self.k)


def format_chunks(docs: list[Document]) -> str:
    """LLM 입력용 포매팅. chunk_id를 노출해야 추출 결과에 출처를 달 수 있다."""
    return "\n".join(
        f'<chunk id="{d.metadata["chunk_id"]}" page="{d.metadata["page"]}" '
        f'table="{str(d.metadata.get("has_table", False)).lower()}">'
        f"{d.page_content}</chunk>"
        for d in docs
    )
