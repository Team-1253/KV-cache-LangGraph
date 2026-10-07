"""노드 State와 네 평가 관점이 공유하는 출력 형식."""

import operator
from typing import Annotated, Any, TypedDict


class Evidence(TypedDict, total=False):
    reference_id: str
    text: str
    metric: str
    value: str
    unit: str
    baseline: str
    condition: str


class Reference(TypedDict):
    id: str
    tech_id: str
    perspective: str
    title: str
    url: str
    date: str
    page: int | None
    content: str
    metadata: dict[str, Any]


class Criterion(TypedDict):
    id: str
    name: str
    status: str
    score: int | None
    rationale: str
    evidence: list[Evidence]
    metadata: dict[str, Any]


class PerspectiveResult(TypedDict):
    tech_id: str
    technology: str
    perspective: str
    status: str
    score: float | list[int] | None
    score_scale: str
    coverage: float
    summary: str
    verdict: str
    criteria: list[Criterion]
    metadata: dict[str, Any]


class EvaluationState(TypedDict, total=False):
    background_facts: dict[str, str]
    selected_technologies: dict[str, str]
    target_domain: str

    technical_result: dict[str, Any]
    trl_result: dict[str, PerspectiveResult]
    market_result: dict[str, PerspectiveResult]
    stakeholder_result: dict[str, PerspectiveResult]
    domain_result: dict[str, PerspectiveResult]

    evaluation_result: Any
    run_errors: Annotated[list[dict[str, str]], operator.add]
    references: Annotated[list[Reference], operator.add]
    final_report: str


def evaluation_material(state: EvaluationState) -> dict:
    """취합·보고서에 같은 평가 목록과 실제 사용한 출처를 전달한다."""
    evaluations = [
        result
        for key in ("trl_result", "market_result", "stakeholder_result", "domain_result")
        for result in state.get(key, {}).values()
    ]
    used_ids = {
        evidence["reference_id"]
        for result in evaluations
        for criterion in result["criteria"]
        for evidence in criterion["evidence"]
    }
    for profile in state.get("technical_result", {}).values():
        for field in ("mechanism", "scope", "claims", "measurements", "limits_explicit", "limits_implicit"):
            used_ids.update(item["source"]["reference_id"] for item in profile[field])
    references = {ref["id"]: ref for ref in state.get("references", []) if ref["id"] in used_ids}
    return {
        "target_domain": state.get("target_domain", ""),
        "background_facts": state.get("background_facts", {}),
        "technical_result": state.get("technical_result", {}),
        "evaluations": evaluations,
        "references": list(references.values()),
        "run_errors": state.get("run_errors", []),
    }
