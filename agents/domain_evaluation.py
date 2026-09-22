"""데이터센터 환경의 KV Cache 기술 도메인 평가 Agent."""

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from agents.state import EvaluationState


# ---------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PROMPT_PATH = PROJECT_ROOT / "prompts" / "domain_evaluation.md"

RUBRIC_PATH = PROJECT_ROOT / "data" / "3-4_domain_evaluation.json"


# JSON rubric의 criterion id와 Python 필드 이름을 연결한다.
CRITERION_FIELD_BY_ID = {
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


# ---------------------------------------------------------------------
# Structured Output Schema
# ---------------------------------------------------------------------


class Evidence(BaseModel):
    """평가 판단에 사용한 근거."""

    source: str = Field(
        description="근거 자료명 또는 식별자"
    )

    quote: str = Field(
        description="판정에 사용한 원문 근거"
    )


class CriterionResult(BaseModel):
    """개별 평가 기준의 결과."""

    status: Literal["EVALUATED", "NOT_VERIFIED"]

    score: Optional[int] = Field(
        default=None,
        ge=1,
        le=5,
        description="1~5점 평가 점수. NOT_VERIFIED이면 None",
    )

    rationale: str = Field(
        description="해당 점수를 부여한 이유"
    )

    evidence: list[Evidence] = Field(
        default_factory=list,
        description="판정을 뒷받침하는 근거",
    )

    @model_validator(mode="after")
    def validate_status_and_score(self):
        """status와 score의 일관성을 검증한다."""

        if self.status == "NOT_VERIFIED" and self.score is not None:
            raise ValueError(
                "NOT_VERIFIED 항목의 score는 None이어야 합니다."
            )

        if self.status == "EVALUATED" and self.score is None:
            raise ValueError(
                "EVALUATED 항목에는 1~5점의 score가 필요합니다."
            )

        return self


class TechnologyDomainEvaluation(BaseModel):
    """하나의 기술에 대한 데이터센터 도메인 평가."""

    technology: str

    # A. 도메인 적합성
    memory_efficiency: CriterionResult
    inference_performance: CriterionResult
    scalability: CriterionResult
    infrastructure_applicability: CriterionResult
    operational_cost_efficiency: CriterionResult

    # B. 근거 신뢰도
    evidence_maturity: CriterionResult
    independent_validation: CriterionResult
    workload_representativeness: CriterionResult
    recency_openness: CriterionResult

    # Python 코드에서 계산
    weighted_score: Optional[float] = None
    coverage: float = 0.0
    verdict: str = ""


class DomainResult(BaseModel):
    """도메인 평가 Agent의 최종 출력."""

    target_domain: str
    evaluations: list[TechnologyDomainEvaluation]


# ---------------------------------------------------------------------
# Rubric Loader
# ---------------------------------------------------------------------


def load_domain_rubric() -> dict[str, Any]:
    """data/3-4_domain_evaluation.json을 읽는다."""

    if not RUBRIC_PATH.exists():
        raise FileNotFoundError(
            f"Domain evaluation rubric not found: {RUBRIC_PATH}"
        )

    with RUBRIC_PATH.open("r", encoding="utf-8") as file:
        rubric = json.load(file)

    if not isinstance(rubric, dict):
        raise ValueError(
            "Domain evaluation rubric must be a JSON object."
        )

    if "criteria" not in rubric:
        raise ValueError(
            "Domain evaluation rubric has no 'criteria'."
        )

    if "scoring" not in rubric:
        raise ValueError(
            "Domain evaluation rubric has no 'scoring'."
        )

    return rubric


def get_domain_weights(
    rubric: Optional[dict[str, Any]] = None,
) -> dict[str, float]:
    """JSON rubric에서 9개 평가 항목의 가중치를 읽는다."""

    if rubric is None:
        rubric = load_domain_rubric()

    criteria = rubric.get("criteria")

    if not isinstance(criteria, list):
        raise ValueError(
            "rubric['criteria'] must be a list."
        )

    weights: dict[str, float] = {}

    for criterion in criteria:
        criterion_id = criterion.get("id")

        if criterion_id not in CRITERION_FIELD_BY_ID:
            raise ValueError(
                f"Unknown domain criterion id: {criterion_id}"
            )

        field_name = CRITERION_FIELD_BY_ID[criterion_id]

        if "weight" not in criterion:
            raise ValueError(
                f"Criterion {criterion_id} has no weight."
            )

        weights[field_name] = float(criterion["weight"])

    expected_fields = set(CRITERION_FIELD_BY_ID.values())
    loaded_fields = set(weights.keys())

    missing_fields = expected_fields - loaded_fields

    if missing_fields:
        raise ValueError(
            "Missing domain criterion weights: "
            + ", ".join(sorted(missing_fields))
        )

    total_weight = sum(weights.values())

    if abs(total_weight - 1.0) > 1e-9:
        raise ValueError(
            f"Domain weights must sum to 1.0, got {total_weight}"
        )

    return weights


def get_coverage_threshold(
    rubric: Optional[dict[str, Any]] = None,
) -> float:
    """JSON rubric에서 coverage 판정 기준을 읽는다."""

    if rubric is None:
        rubric = load_domain_rubric()

    scoring = rubric.get("scoring", {})

    if "coverage_threshold" not in scoring:
        raise ValueError(
            "Rubric scoring has no coverage_threshold."
        )

    threshold = float(scoring["coverage_threshold"])

    if not 0.0 <= threshold <= 1.0:
        raise ValueError(
            f"coverage_threshold must be between 0 and 1: {threshold}"
        )

    return threshold


def get_default_target_domain(
    rubric: Optional[dict[str, Any]] = None,
) -> str:
    """JSON rubric에 정의된 기본 평가 도메인을 반환한다."""

    if rubric is None:
        rubric = load_domain_rubric()

    target_domain = rubric.get("target_domain")

    if not target_domain:
        raise ValueError(
            "Domain rubric has no target_domain."
        )

    return str(target_domain)


# ---------------------------------------------------------------------
# Score Calculation
# ---------------------------------------------------------------------


def calculate_domain_score(
    evaluation: TechnologyDomainEvaluation,
) -> TechnologyDomainEvaluation:
    """가중 점수와 evidence coverage를 계산한다.

    가중치와 coverage threshold는
    data/3-4_domain_evaluation.json에서 읽는다.

    NOT_VERIFIED 항목은 점수와 coverage 계산에서 제외한다.
    """

    rubric = load_domain_rubric()

    domain_weights = get_domain_weights(rubric)
    coverage_threshold = get_coverage_threshold(rubric)

    weighted_sum = 0.0
    covered_weight = 0.0

    for criterion, weight in domain_weights.items():
        result = getattr(evaluation, criterion)

        if result.status == "NOT_VERIFIED":
            continue

        if result.score is None:
            continue

        weighted_sum += result.score * weight
        covered_weight += weight

    evaluation.coverage = round(covered_weight, 3)

    # 공식 rubric의 coverage threshold 미만이면 판정 보류
    if covered_weight < coverage_threshold:
        evaluation.weighted_score = None
        evaluation.verdict = "판정 보류 — 근거 불충분"

        return evaluation

    # NOT_VERIFIED 항목을 제외한 검증 항목의
    # 가중치 합으로 정규화한다.
    weighted_score = weighted_sum / covered_weight

    evaluation.weighted_score = round(weighted_score, 2)

    # 최종 판정
    if weighted_score >= 4.0 and covered_weight >= 0.8:
        evaluation.verdict = "적합 — 근거 충분"

    elif weighted_score >= 4.0:
        evaluation.verdict = "적합 — 근거 보강 필요"

    elif weighted_score >= 3.0:
        evaluation.verdict = "조건부 적합"

    elif weighted_score >= 2.0:
        evaluation.verdict = "제한적 적합"

    else:
        evaluation.verdict = "부적합"

    return evaluation


# ---------------------------------------------------------------------
# Prompt Loader
# ---------------------------------------------------------------------


def load_domain_prompt() -> str:
    """도메인 평가 프롬프트 파일을 읽는다."""

    if not PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"Domain evaluation prompt not found: {PROMPT_PATH}"
        )

    return PROMPT_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# LangGraph Node
# ---------------------------------------------------------------------


def domain_evaluation_agent(state: EvaluationState) -> dict:
    """데이터센터 환경에서 기술별 적용 적합성을 평가한다.

    입력 State:
        - technical_result
        - target_domain

    출력 State:
        - domain_result
        - references

    현재 구현:
        - Structured Output Schema
        - 공식 Domain Rubric 로딩
        - 가중치 로딩
        - coverage 계산
        - 최종 verdict 계산
        - Prompt 로딩

    추후 공통 RAG Tool이 연결되면:
        1. 기술별 Domain RAG 검색
        2. 검색 근거 + technical_result를 LLM에 전달
        3. Structured Output 생성
        4. calculate_domain_score() 적용
        5. domain_result + references 반환
    """

    technical_result = state.get("technical_result")

    if not technical_result:
        raise ValueError(
            "domain_evaluation_agent requires "
            "state['technical_result']"
        )

    rubric = load_domain_rubric()
    load_domain_prompt()

    target_domain = state.get(
        "target_domain"
    ) or get_default_target_domain(rubric)

    # TODO:
    # 1. 공통 RAG Tool을 이용해 기술별 관련 근거 검색
    # 2. technical_result + retrieved evidence + rubric을 LLM에 전달
    # 3. TechnologyDomainEvaluation Structured Output 생성
    # 4. 각 기술에 calculate_domain_score() 적용
    # 5. DomainResult 생성
    # 6. references 생성
    # 7. 아래 형태로 반환
    #
    # return {
    #     "domain_result": domain_result,
    #     "references": references,
    # }

    raise NotImplementedError(
        "Domain RAG/LLM integration is not connected yet. "
        f"target_domain={target_domain}"
    )