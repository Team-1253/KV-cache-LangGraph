"""기술 조사 Agent와 TRL 평가 Node."""

from agents.state import EvaluationState


def technical_research_agent(state: EvaluationState) -> dict:
    """원문 PDF에서 두 기술의 구조, 성능, 범위와 한계를 추출한다."""

    # TODO: PDF RAG와 구조화된 technical_result 생성을 구현한다.
    raise NotImplementedError


def trl_evaluation_node(state: EvaluationState) -> dict:
    """공개 근거를 이용해 기술별 TRL을 추정한다."""

    # TODO: TRL 1~9 판정과 근거 생성을 구현한다.
    raise NotImplementedError
