# -*- coding: utf-8 -*-
"""오픈소스 임베딩 — BAAI/bge-m3.

과제 요건상 오픈소스 임베딩을 사용한다. 선정 근거는 `docs/AGENT-기술조사.md` §3-3.
sentence-transformers만으로 동작하도록 얇게 감싸, 추가 의존성을 만들지 않는다.
"""
from __future__ import annotations

from functools import lru_cache

from langchain_core.embeddings import Embeddings

MODEL_NAME = "BAAI/bge-m3"


@lru_cache(maxsize=2)
def _load(model_name: str, device: str):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name, device=device)


class BGEM3Embeddings(Embeddings):
    """bge-m3 dense 임베딩. 정규화된 벡터를 반환한다(코사인 = 내적)."""

    def __init__(self, model_name: str = MODEL_NAME, device: str = "cpu", batch_size: int = 8):
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size

    @property
    def _model(self):
        return _load(self.model_name, self.device)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vecs = self._model.encode(
            texts,
            normalize_embeddings=True,
            batch_size=self.batch_size,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        # bge-m3는 질의 접두사가 필요 없다(비대칭 프리픽스 미사용 모델).
        return self._model.encode(
            text, normalize_embeddings=True, show_progress_bar=False
        ).tolist()
