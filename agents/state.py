import operator
from typing import Annotated, TypedDict


class OrchestratorState(TypedDict, total=False):
    """전체 계획, Worker 결과, 보고서 위치와 품질 평가를 관리한다."""

    selected_technologies: dict[str, str]
    target_domain: str
    background_facts: dict[str, str]

    # plan 항목: task_id, worker, tech_ids, instruction, reason
    plan: list[dict]
    status: str
    # results 항목: task_id, agent, task, output(결과 dict), status
    results: Annotated[list[dict], operator.add]
    report_uri: str
    errors: Annotated[list, operator.add]

    run_id: str
    step_count: int  # 보고서 생성 횟수
    max_steps: int   # 보고서 생성 상한. 기본 2회(초안 + 수정 1회)
    # quality 항목: groundedness, neutrality, bias_control, perspective_coverage, feedback
    quality: dict


class WorkerState(TypedDict, total=False):
    """Send로 전달하는 작업 하나와 해당 작업의 자료."""

    task: dict
    task_input: dict
