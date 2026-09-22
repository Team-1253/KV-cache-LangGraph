# -*- coding: utf-8 -*-
"""기술 조사·TRL Node 단독 실행 스크립트 (담당 범위 검증용).

usage:
    python experiments/run_technical_research.py --no-llm   # 검색까지만 (LLM 호출 없음)
    python experiments/run_technical_research.py            # 조사 + TRL 전체 실행
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

SELECTED = {"SW": "DeepSeek-V2 MLA", "HW": "ITME"}
TARGET_DOMAIN = "데이터센터 / 클라우드 대규모 서빙"


def retrieval_only() -> None:
    from agents.technical_research import TECH_REGISTRY, _resolve_techs, _retrieve
    from rag.embeddings import BGEM3Embeddings
    from rag.retriever import TechRetriever

    emb = BGEM3Embeddings()
    for tid in _resolve_techs(SELECTED):
        meta = TECH_REGISTRY[tid]
        docs = _retrieve(TechRetriever(tid, meta["pdf"], embeddings=emb).build())
        tables = sum(d.metadata.get("has_table", False) for d in docs)
        print(f"\n[{meta['name']}] 근거 청크 {len(docs)}개 (표 포함 {tables}개)")
        print(f"  페이지: {sorted({d.metadata['page'] for d in docs})}")
        print(f"  예시: {docs[0].page_content[:110].replace(chr(10), ' ')}…")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    if args.no_llm:
        retrieval_only()
        return

    from agents.technical_research import technical_research_agent, trl_evaluation_node

    state = {"selected_technologies": SELECTED, "target_domain": TARGET_DOMAIN, "references": []}
    research = technical_research_agent(state)
    state.update(research)
    trl = trl_evaluation_node(state)

    out = ROOT / "outputs"
    out.mkdir(exist_ok=True)
    (out / "technical_result.json").write_text(
        json.dumps(research["technical_result"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "trl_result.json").write_text(
        json.dumps(trl["trl_result"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n저장 완료 → outputs/technical_result.json, outputs/trl_result.json")


if __name__ == "__main__":
    main()
