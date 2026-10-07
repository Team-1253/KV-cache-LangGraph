# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.state import EvaluationState
from rag.retriever import TechRetriever, format_chunks

MODEL_NAME = "gpt-4.1-mini"
PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "technical_research.md"
)
NOT_VERIFIED = "NOT_VERIFIED"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TRL_RUBRIC_PATH = DATA_DIR / "3-1_technology_readiness.json"

# 실행 날짜에 따라 TRL 해석의 시간 맥락이 달라지지 않도록 기준일을 고정한다.
AS_OF = date(2026, 9, 22)

# State에는 기술 선택만 담고, 논문 경로와 인용 정보는 기술·도메인 평가가 공유한다.
TECH_REGISTRY: dict[str, dict[str, str]] = {
    "deepseek_v2_mla": {
        "camp": "SW",
        "name": "DeepSeek-V2 (MLA)",
        "published": "2024-06",
        "pdf": str(DATA_DIR / "DeepSeekV2_2405.04434v5.pdf"),
        "citation": (
            "DeepSeek-AI(2024). DeepSeek-V2: A Strong, Economical, and Efficient "
            "Mixture-of-Experts Language Model. arXiv, 2405.04434."
        ),
        "url": "https://arxiv.org/pdf/2405.04434",
    },
    "itme": {
        "camp": "HW",
        "name": "ITME (CXL-Hybrid Tiered Memory Expansion)",
        "published": "2026-06",
        "pdf": str(DATA_DIR / "ITME_2606.12556v2.pdf"),
        "citation": (
            "Jang, H. et al.(2026). ITME: Inference Tiered Memory Expansion with "
            "Disaggregated CXL-Hybrid Memories. arXiv, 2606.12556."
        ),
        "url": "https://arxiv.org/pdf/2606.12556",
    },
}

# 기술마다 같은 관점을 조사하도록 한국어 고정 질의를 사용한다.
ASPECT_QUERIES: list[str] = [
    "이 기술의 핵심 접근 방식은 무엇인가",
    "어떤 문제를 해결하려고 하는가",
    "어떤 방식으로 동작하는가 구조와 절차",
    "KV 캐시를 구체적으로 어떻게 처리하는가",
    "어떤 하드웨어와 모델에서 평가했는가 실험 환경",
    "적용 전제 조건과 대상 워크로드는 무엇인가",
    "성능 개선 수치와 비교 대상 baseline",
    "메모리 절감량 또는 용량 확장 수치",
    "한계점과 제약 조건",
    "성능이 저하되거나 불리해지는 조건",
    "프로토타입 구현의 성능 격차와 최적화가 필요한 부분",
]


def _elapsed_months(published: str, as_of: date = AS_OF) -> int:
    """문헌 발표 월부터 평가 기준 월까지의 경과 개월을 계산한다."""

    y, m = (int(x) for x in published.split("-")[:2])
    return (as_of.year - y) * 12 + (as_of.month - m)


def _prompt_sections() -> dict[str, str]:
    """프롬프트 파일의 섹션 경계는 `## [SECTION]` 형식을 따라야 한다."""

    text = PROMPT_PATH.read_text(encoding="utf-8")
    parts = re.split(r"^## \[([A-Z_]+)\]\s*$", text, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


class _Source(BaseModel):
    """기술 조사에서 인용한 PDF 청크 ID와 페이지."""

    chunk_id: str = Field(description="근거 청크의 id. 제공된 <chunk id=...> 값 그대로")
    page: int = Field(description="근거 청크의 page 값")


class _Evidenced(BaseModel):
    """원문 출처가 연결된 기술 설명."""

    text: str
    source: _Source


class _Claim(BaseModel):
    """논문이 주장한 효과와 비교 대상, 출처."""

    text: str = Field(description="논문이 주장하는 효과")
    baseline: str = Field(
        description="무엇 대비인가. 원문에 없으면 항목을 출력하지 말 것"
    )
    source: _Source


class _Measurement(BaseModel):
    """논문의 측정 지표·값·비교 대상·실험 조건과 출처."""

    metric: str = Field(description="지표명 (예: TTFT speedup, KV cache reduction)")
    value: str = Field(description="측정값 (단위 포함)")
    baseline: str = Field(description="비교 기준. 필수")
    condition: str = Field(description="실험 조건 (하드웨어·모델·워크로드·구간)")
    source: _Source


class _Limit(BaseModel):
    """기술의 한계와 그 판단 근거, 출처."""

    text: str
    basis: str = Field(description="명시적 한계면 원문 근거, 암묵적 한계면 역산 근거")
    source: _Source


class _Extraction(BaseModel):
    """논문에서 추출한 개요, 작동 방식, 적용 범위, 주장·측정값과 명시적 한계."""

    overview: str
    mechanism: list[_Evidenced] = Field(default_factory=list)
    scope: list[_Evidenced] = Field(default_factory=list)
    claims: list[_Claim] = Field(default_factory=list)
    measurements: list[_Measurement] = Field(default_factory=list)
    limits_explicit: list[_Limit] = Field(default_factory=list)


class _ImplicitLimits(BaseModel):
    """논문 근거에서 추론한 암묵적 한계 목록."""

    limits_implicit: list[_Limit] = Field(default_factory=list)


class TrlComponent(BaseModel):
    """구성요소 하나의 TRL 추정과 핵심 여부, 근거 출처."""

    component: str
    trl: int = Field(ge=1, le=9)
    evidence: str
    is_critical: bool
    reference_ids: list[str] = Field(
        description="근거로 사용한 입력 source.reference_id 목록"
    )


class TrlAssessment(BaseModel):
    """기술 구성요소별 TRL 추정과 전체 판단 이유."""

    components: list[TrlComponent]
    rationale: str


def _resolve_techs(selected: dict | None) -> list[str]:
    """선택 이름과 SW·HW 구분을 등록된 기술 ID에 대응시킨다."""

    if not selected:
        return list(TECH_REGISTRY)

    selections = [str(token).lower() for pair in selected.items() for token in pair]
    matches = []
    for tech_id, meta in TECH_REGISTRY.items():
        keywords = ("deepseek", "mla") if tech_id == "deepseek_v2_mla" else ("itme",)
        if (
            tech_id in selections
            or meta["camp"].lower() in selections
            or any(keyword in token for keyword in keywords for token in selections)
        ):
            matches.append(tech_id)

    return matches or list(TECH_REGISTRY)


# 공용 인덱스는 유지하면서, 근거 추출 문맥에 섞인 목차·참고문헌 잡음만 제외한다.
_DOT_LEADER = re.compile(r"(?:\.\s*){4,}")
_BIB_LINE = re.compile(r"^\s*\[\d+\]\s")


def _is_noise(doc: Document) -> bool:
    """목차나 참고문헌 목록이 주를 이루는 청크를 판별한다."""

    lines = [l for l in doc.page_content.split("\n") if l.strip()]
    if not lines:
        return True

    toc = sum(bool(_DOT_LEADER.search(l)) for l in lines)
    bib = sum(bool(_BIB_LINE.match(l)) for l in lines)
    return toc / len(lines) >= 0.3 or bib / len(lines) >= 0.5


def _retrieve(retriever: TechRetriever, per_query_k: int = 4) -> list[Document]:
    """관점별 검색 결과에서 잡음과 중복을 제거하고 원문 순서로 정렬한다."""

    seen: dict[str, Document] = {}
    for q in ASPECT_QUERIES:
        for d in retriever.search(q, k=per_query_k):
            if _is_noise(d):
                continue
            seen.setdefault(d.metadata["chunk_id"], d)

    return sorted(
        seen.values(), key=lambda d: (d.metadata["page"], d.metadata["chunk_id"])
    )


_PLACEHOLDER = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "없음",
    "미상",
    "불명",
    "not specified",
    "unknown",
}
FACT_FIELDS = (
    "mechanism",
    "scope",
    "claims",
    "measurements",
    "limits_explicit",
    "limits_implicit",
)


def technical_research_agent(state: EvaluationState) -> dict:
    """논문에서 기술 사실과 한계를 추출해 검색 청크의 출처를 연결한다."""

    prompts = _prompt_sections()
    model = init_chat_model(
        MODEL_NAME, model_provider="openai", temperature=0, max_retries=2
    )

    from rag.embeddings import BGEM3Embeddings

    embeddings = BGEM3Embeddings()

    results, references = {}, {}

    for tech_id in _resolve_techs(state.get("selected_technologies")):

        meta = TECH_REGISTRY[tech_id]
        docs = _retrieve(
            TechRetriever(tech_id, meta["pdf"], embeddings=embeddings).build()
        )
        doc_by_id = {doc.metadata["chunk_id"]: doc for doc in docs}
        context = format_chunks(docs)

        extracted = (
            model.with_structured_output(_Extraction)
            .invoke(
                [
                    SystemMessage(prompts["SYSTEM"]),
                    HumanMessage(
                        prompts["EXTRACT"].format(
                            tech_name=meta["name"], context=context
                        )
                    ),
                ]
            )
            .model_dump()
        )

        implicit = model.with_structured_output(_ImplicitLimits).invoke(
            [
                SystemMessage(prompts["SYSTEM"]),
                HumanMessage(
                    prompts["IMPLICIT_LIMITS"].format(
                        tech_name=meta["name"], context=context
                    )
                ),
            ]
        )
        extracted["limits_implicit"] = [
            item.model_dump() for item in implicit.limits_implicit
        ]
        extracted["overview"] = extracted["overview"] or NOT_VERIFIED

        dropped = {}
        for field in FACT_FIELDS:
            kept = []
            for item in extracted[field]:
                doc = doc_by_id.get(item["source"]["chunk_id"])
                if doc is None:
                    continue

                # 비교 대상과 실험 조건이 없는 수치는 기술 간 비교 근거로 쓰지 않는다.
                if field == "measurements" and any(
                    item[key].strip().lower() in _PLACEHOLDER
                    for key in ("baseline", "condition")
                ):
                    continue

                ref_id = f"technical-{doc.metadata['chunk_id']}"
                item["source"].update(reference_id=ref_id, page=doc.metadata["page"])
                references[ref_id] = {
                    "id": ref_id,
                    "tech_id": tech_id,
                    "perspective": "technical",
                    "title": meta["citation"],
                    "url": meta["url"],
                    "date": meta["published"],
                    "page": doc.metadata["page"],
                    "content": doc.page_content,
                    "metadata": {"kind": "paper", "chunk_id": doc.metadata["chunk_id"]},
                }
                kept.append(item)

            dropped[field] = len(extracted[field]) - len(kept)
            extracted[field] = kept

        counts = {field: len(extracted[field]) for field in FACT_FIELDS}
        level = (
            "strong"
            if counts["measurements"] >= 3 and counts["limits_explicit"]
            else "limited"
        )
        results[tech_id] = {
            "tech_id": tech_id,
            "camp": meta["camp"],
            "title": meta["name"],
            **extracted,
            "evidence_level": level if sum(counts.values()) else NOT_VERIFIED,
            "retrieval": {
                "chunks_used": len(docs),
                "pages": sorted({d.metadata["page"] for d in docs}),
                "counts": counts,
                "dropped_ungrounded": dropped,
            },
        }
        print(f"[technical] {tech_id}: {len(docs)} 청크, 추출 {counts}")

    return {"technical_result": results, "references": list(references.values())}


def trl_evaluation_node(state: EvaluationState) -> dict:
    """기술 조사 근거로 구성요소별 TRL을 추정하고 성숙도 구간을 반환한다."""

    prompts = _prompt_sections()
    rubric = TRL_RUBRIC_PATH.read_text(encoding="utf-8")
    model = init_chat_model(
        MODEL_NAME, model_provider="openai", temperature=0, max_retries=2
    )

    references = {ref["id"]: ref for ref in state.get("references", [])}
    evaluations = {}

    for tech_id, profile in state.get("technical_result", {}).items():
        # 제안자의 효과 주장만으로 성숙도를 높게 추정하지 않도록 claims를 근거에서 제외한다.
        facts = {
            field: profile[field]
            for field in ("scope", "measurements", "limits_explicit", "limits_implicit")
        }
        facts["overview"] = profile["overview"]

        assessment = model.with_structured_output(TrlAssessment).invoke(
            [
                SystemMessage(prompts["TRL_SYSTEM"]),
                HumanMessage(
                    prompts["TRL_USER"].format(
                        tech_name=profile["title"],
                        profile=json.dumps(facts, ensure_ascii=False),
                        rubric=rubric,
                    )
                ),
            ]
        )

        allowed_ids = {
            item["source"]["reference_id"]
            for field in facts
            if field != "overview"
            for item in facts[field]
        }
        criteria, verified_components = [], []
        for index, component in enumerate(assessment.components, 1):
            evidence = [
                {"reference_id": ref_id, "text": component.evidence}
                for ref_id in dict.fromkeys(component.reference_ids)
                if ref_id in allowed_ids and ref_id in references
            ]
            if evidence:
                verified_components.append(component)
            criteria.append(
                {
                    "id": f"TRL-{index}",
                    "name": component.component,
                    "status": "VERIFIED" if evidence else "NOT_VERIFIED",
                    "score": component.trl if evidence else None,
                    "rationale": component.evidence,
                    "evidence": evidence,
                    "metadata": {"is_critical": component.is_critical},
                }
            )

        critical = [c.trl for c in verified_components if c.is_critical]
        # 핵심 구성요소의 근거 공백은 다른 구성요소의 높은 TRL로 보완할 수 없다.
        unknown_critical = any(
            c.is_critical and not item["evidence"]
            for c, item in zip(assessment.components, criteria)
        )
        score = (
            [
                min(critical or [c.trl for c in verified_components]),
                max(c.trl for c in verified_components),
            ]
            if verified_components and not unknown_critical
            else None
        )
        coverage = len(verified_components) / len(criteria) if criteria else 0

        meta = TECH_REGISTRY[tech_id]
        evaluations[tech_id] = {
            "tech_id": tech_id,
            "technology": profile["title"],
            "perspective": "trl",
            "status": (
                "VERIFIED"
                if coverage == 1
                else "PARTIAL" if coverage else "NOT_VERIFIED"
            ),
            "score": score,
            "score_scale": "1-9",
            "coverage": coverage,
            "summary": assessment.rationale,
            "verdict": (
                "공개 논문 근거 기반 추정" if score else "판정 보류 — 근거 불충분"
            ),
            "criteria": criteria,
            "metadata": {
                "published": meta["published"],
                "as_of": AS_OF.isoformat(),
                "elapsed_months": _elapsed_months(meta["published"]),
                "evidence_scope": "paper_only",
                "range_derivation": "하한=핵심 구성요소 최저 단계, 상한=전체 구성요소 최고 단계",
            },
        }

    return {"trl_result": evaluations, "references": []}
