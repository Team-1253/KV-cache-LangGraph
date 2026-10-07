import json
import re
from pathlib import Path
from typing import Literal

from langchain.chat_models import init_chat_model
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.state import EvaluationState
from agents.technical_research import TECH_REGISTRY
from rag.embeddings import BGEM3Embeddings
from rag.retriever import TechRetriever, format_chunks

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT / "prompts" / "domain_evaluation.md"
RUBRIC_PATH = ROOT / "data" / "3-4_domain_evaluation.json"
FIELDS = {
    "3-4-a": "memory_efficiency",
    "3-4-b": "inference_performance",
    "3-4-c": "scalability",
    "3-4-d": "infrastructure_applicability",
    "3-4-e": "operational_cost_efficiency",
    "3-4-f": "evidence_maturity",
    "3-4-g": "independent_validation",
    "3-4-h": "workload_representativeness",
    "3-4-i": "recency_openness",
}


class Evidence(BaseModel):
    """도메인 채점에 사용한 원문 인용과 검색 청크 ID."""

    source: str = Field(description="검색된 chunk_id")
    quote: str = Field(description="번역이나 요약이 아닌 원문 문구 그대로")


class CriterionResult(BaseModel):
    """도메인 평가 한 항목의 점수, 판단 이유와 원문 근거."""

    status: Literal["EVALUATED", "NOT_VERIFIED"]
    score: int = Field(ge=0, le=5)
    rationale: str
    evidence: list[Evidence] = Field(default_factory=list)


class DomainAssessment(BaseModel):
    """도메인 루브릭의 아홉 항목에 대한 모델 평가 결과."""

    memory_efficiency: CriterionResult
    inference_performance: CriterionResult
    scalability: CriterionResult
    infrastructure_applicability: CriterionResult
    operational_cost_efficiency: CriterionResult
    evidence_maturity: CriterionResult
    independent_validation: CriterionResult
    workload_representativeness: CriterionResult
    recency_openness: CriterionResult


_DOT_LEADER = re.compile(r"(?:\.\s*){4,}")
_BIB_LINE = re.compile(r"^\s*\[\d+\]\s")
_REFERENCE_HEADING = re.compile(r"^\s*(references|bibliography)\s*$", re.IGNORECASE)
_CITATION_START = re.compile(r"^\s*(?:[A-Z]\.\s+[A-Z][A-Za-z-]+,\s*){2,}")
_REFERENCE_TERMS = re.compile(
    r"\b(arxiv preprint|proceedings of|transactions on|conference on|"
    r"association for computational linguistics)\b",
    re.IGNORECASE,
)


def _is_noise_document(doc: Document) -> bool:
    """목차·참고문헌·기여자 정보가 주를 이루는 청크를 판별한다."""

    lines = [line.strip() for line in doc.page_content.splitlines() if line.strip()]
    if not lines:
        return True

    text = " ".join(lines)
    if any(
        term in text.casefold()
        for term in (
            "contributions and acknowledgments",
            "contributions and acknowledgements",
            "author contributions",
        )
    ):
        return True
    if any(_REFERENCE_HEADING.match(line) for line in lines[:3]):
        return True
    if _CITATION_START.match(lines[0]):
        return True

    count = len(lines)
    toc_ratio = sum(bool(_DOT_LEADER.search(line)) for line in lines) / count
    bib_ratio = sum(bool(_BIB_LINE.match(line)) for line in lines) / count
    citation_ratio = sum(bool(_CITATION_START.match(line)) for line in lines) / count
    reference_hits = len(_REFERENCE_TERMS.findall(text))

    return (
        toc_ratio >= 0.3
        or bib_ratio >= 0.5
        or citation_ratio >= 0.3
        or reference_hits >= 2
    )


def domain_evaluation_agent(state: EvaluationState) -> dict:
    """논문 근거를 대조해 도메인 항목을 채점하고 가중 점수와 판정을 반환한다."""

    rubric = json.loads(RUBRIC_PATH.read_text(encoding="utf-8"))
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    model = init_chat_model(
        "gpt-4.1-mini", model_provider="openai", temperature=0, max_retries=2
    )
    embeddings = BGEM3Embeddings()

    evaluations, references = {}, {}

    for tech_id, profile in state.get("technical_result", {}).items():
        retriever = TechRetriever(
            tech_id, TECH_REGISTRY[tech_id]["pdf"], embeddings=embeddings
        ).build()

        docs = {}
        for criterion in rubric["criteria"]:
            query = f"{criterion['question']} 관련 실험 결과, 수치, 제약 조건을 찾아라. 확인 항목: {', '.join(criterion['evidence'])}"
            for doc in retriever.search(query, k=3):
                if not _is_noise_document(doc):
                    docs.setdefault(doc.metadata["chunk_id"], doc)

        assessment = model.with_structured_output(DomainAssessment).invoke(
            [
                SystemMessage(prompt),
                HumanMessage(
                    f"평가 도메인: {state.get('target_domain') or rubric['target_domain']}\n"
                    f"기술 조사: {json.dumps(profile, ensure_ascii=False)}\n"
                    f"루브릭: {json.dumps(rubric, ensure_ascii=False)}\n"
                    f"원문 근거:\n{format_chunks(sorted(docs.values(), key=lambda d: (d.metadata['page'], d.metadata['chunk_id'])))}"
                ),
            ]
        )

        criteria, weighted_score, coverage = [], 0.0, 0.0
        for criterion in rubric["criteria"]:
            item = getattr(assessment, FIELDS[criterion["id"]])
            evidence = []

            if item.status == "EVALUATED" and str(item.score) in criterion["scores"]:
                for source in item.evidence:
                    doc = docs.get(source.source)
                    if doc is None or not source.quote.strip():
                        continue

                    # 모델이 만든 인용문을 근거로 채택하지 않도록 검색 원문과 대조한다.
                    if " ".join(source.quote.split()) not in " ".join(
                        doc.page_content.split()
                    ):
                        continue

                    ref_id = f"domain-{source.source}"
                    references[ref_id] = {
                        "id": ref_id,
                        "tech_id": tech_id,
                        "perspective": "domain",
                        "title": TECH_REGISTRY[tech_id]["citation"],
                        "url": TECH_REGISTRY[tech_id]["url"],
                        "date": TECH_REGISTRY[tech_id]["published"],
                        "page": doc.metadata["page"],
                        "content": doc.page_content,
                        "metadata": {"kind": "paper", "chunk_id": source.source},
                    }
                    evidence.append({"reference_id": ref_id, "text": source.quote})

            verified = bool(evidence)
            weight = float(criterion["weight"])
            coverage += weight if verified else 0
            weighted_score += item.score * weight if verified else 0

            criteria.append(
                {
                    "id": criterion["id"],
                    "name": criterion["question"],
                    "status": "VERIFIED" if verified else "NOT_VERIFIED",
                    "score": item.score if verified else None,
                    "rationale": (
                        item.rationale
                        if verified
                        else item.rationale
                        + " / 원문 근거 또는 루브릭 점수를 확인하지 못함"
                    ),
                    "evidence": evidence,
                    "metadata": {"weight": weight},
                }
            )

        # 근거가 적은 평가의 점수가 부풀지 않도록 coverage로 재정규화하지 않는다.
        score = round(weighted_score, 2)
        if coverage < float(rubric["scoring"]["coverage_threshold"]):
            verdict = "판정 보류 — 근거 불충분"
        elif score >= 4:
            verdict = "적합 — 근거 충분" if coverage >= 0.8 else "적합 — 근거 보강 필요"
        elif score >= 3:
            verdict = "조건부 적합"
        elif score >= 2:
            verdict = "제한적 적합"
        else:
            verdict = "부적합"

        evaluations[tech_id] = {
            "tech_id": tech_id,
            "technology": profile["title"],
            "perspective": "domain",
            "status": (
                "VERIFIED"
                if round(coverage, 3) == 1
                else "PARTIAL" if coverage else "NOT_VERIFIED"
            ),
            "score": score,
            "score_scale": "0-5",
            "coverage": round(coverage, 3),
            "summary": " / ".join(c["rationale"] for c in criteria),
            "verdict": verdict,
            "criteria": criteria,
            "metadata": {
                "rubric": RUBRIC_PATH.name,
                "coverage_method": "확인된 항목 가중치 합",
            },
        }

    return {"domain_result": evaluations, "references": list(references.values())}
