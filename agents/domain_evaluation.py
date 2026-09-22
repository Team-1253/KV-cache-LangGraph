"""데이터센터 환경의 KV Cache 기술 도메인 평가 Agent."""

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from agents.state import EvaluationState


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

    # Python 코드에서 계산하는 값
    weighted_score: Optional[float] = None
    coverage: float = 0.0
    verdict: str = ""


class DomainResult(BaseModel):
    """도메인 평가 Agent의 최종 출력."""

    target_domain: str
    evaluations: list[TechnologyDomainEvaluation]


# ---------------------------------------------------------------------
# Evaluation Weights
# ---------------------------------------------------------------------


DOMAIN_WEIGHTS = {
    "memory_efficiency": 0.15,
    "inference_performance": 0.16,
    "scalability": 0.14,
    "infrastructure_applicability": 0.13,
    "operational_cost_efficiency": 0.12,
    "evidence_maturity": 0.10,
    "independent_validation": 0.08,
    "workload_representativeness": 0.07,
    "recency_openness": 0.05,
}


# ---------------------------------------------------------------------
# Score Calculation
# ---------------------------------------------------------------------


def calculate_domain_score(
    evaluation: TechnologyDomainEvaluation,
) -> TechnologyDomainEvaluation:
    """가중 점수와 evidence coverage를 계산한다.

    NOT_VERIFIED 항목은 점수 계산에서 제외한다.
    coverage가 0.6 미만이면 최종 판정을 보류한다.
    """

    weighted_sum = 0.0
    covered_weight = 0.0

    for criterion, weight in DOMAIN_WEIGHTS.items():
        result = getattr(evaluation, criterion)

        if result.status == "NOT_VERIFIED":
            continue

        if result.score is None:
            continue

        weighted_sum += result.score * weight
        covered_weight += weight

    evaluation.coverage = round(covered_weight, 3)

    # 전체 가중치 중 60% 미만만 검증된 경우 판정 보류
    if covered_weight < 0.6:
        evaluation.weighted_score = None
        evaluation.verdict = "판정 보류 — 근거 불충분"

        return evaluation

    # NOT_VERIFIED를 제외한 검증 항목의 가중치 기준으로 정규화
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


PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "prompts"
    / "domain_evaluation.md"
)


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

    현재 단계에서는 평가 스키마, 가중치 계산,
    coverage 계산 및 Prompt 로딩까지 구현한다.

    실제 RAG retrieval과 LLM Structured Output 연결은
    팀 공통 Tool 인터페이스가 확정된 뒤 연결한다.
    """

    technical_result = state.get("technical_result")

    target_domain = state.get(
        "target_domain",
        "데이터센터",
    )

    if not technical_result:
        raise ValueError(
            "domain_evaluation_agent requires "
            "state['technical_result']"
        )

    # Prompt 파일 정상 로딩 확인
    load_domain_prompt()

    # TODO:
    # 1. 기술별 Domain RAG 검색
    # 2. 검색된 근거 + technical_result를 LLM에 전달
    # 3. TechnologyDomainEvaluation Structured Output 생성
    # 4. 각 기술에 calculate_domain_score() 적용
    # 5. DomainResult 생성
    # 6. references 생성
    # 7. domain_result와 references만 반환

    raise NotImplementedError(
        "Domain RAG/LLM integration is not connected yet. "
        f"target_domain={target_domain}"
    )