import operator
from typing import Annotated, Any, TypedDict


class Evidence(TypedDict, total=False):
    """평가 항목의 판단을 출처에 연결하는 근거와 선택적 수치 정보."""

    reference_id: str
    text: str
    metric: str
    value: str
    unit: str
    baseline: str
    condition: str


class Reference(TypedDict):
    """평가에 사용한 PDF·웹 출처의 원문과 인용 정보."""

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
    """관점별 루브릭 한 항목의 점수, 확인 상태와 근거."""

    id: str
    name: str
    status: str
    score: int | None
    rationale: str
    evidence: list[Evidence]
    metadata: dict[str, Any]


class PerspectiveResult(TypedDict):
    """한 기술에 대한 한 평가 관점의 공통 결과 형식."""

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
    """그래프 노드가 공유하는 입력, 관점별 결과, 출처와 보고서."""

    background_facts: dict[str, str]
    selected_technologies: dict[str, str]
    target_domain: str

    technical_result: dict[str, Any]
    trl_result: dict[str, PerspectiveResult]
    market_result: dict[str, PerspectiveResult]
    stakeholder_result: dict[str, PerspectiveResult]
    domain_result: dict[str, PerspectiveResult]

    evaluation_result: Any

    # 병렬 평가가 남긴 오류와 출처는 덮어쓰지 않고 누적한다.
    run_errors: Annotated[list[dict[str, str]], operator.add]
    references: Annotated[list[Reference], operator.add]

    final_report: str


def evaluation_material(state: EvaluationState) -> dict:
    """기술 조사와 관점별 평가를 사용된 출처와 함께 취합한다."""

    evaluations = [
        result
        for key in (
            "trl_result",
            "market_result",
            "stakeholder_result",
            "domain_result",
        )
        for result in state.get(key, {}).values()
    ]

    # 검색 후보가 최종 근거로 오인되지 않도록 결과에 연결된 출처만 전달한다.
    used_ids = {
        evidence["reference_id"]
        for result in evaluations
        for criterion in result["criteria"]
        for evidence in criterion["evidence"]
    }
    for profile in state.get("technical_result", {}).values():
        for field in (
            "mechanism",
            "scope",
            "claims",
            "measurements",
            "limits_explicit",
            "limits_implicit",
        ):
            used_ids.update(item["source"]["reference_id"] for item in profile[field])

    references = {
        ref["id"]: ref for ref in state.get("references", []) if ref["id"] in used_ids
    }

    return {
        "target_domain": state.get("target_domain", ""),
        "background_facts": state.get("background_facts", {}),
        "technical_result": state.get("technical_result", {}),
        "evaluations": evaluations,
        "references": list(references.values()),
        "run_errors": state.get("run_errors", []),
    }
