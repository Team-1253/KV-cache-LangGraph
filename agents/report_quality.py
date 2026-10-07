import json
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from agents.state import EvaluationState, evaluation_material

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "report_quality.md"


class ReportQuality(BaseModel):
    groundedness: bool
    neutrality: bool
    bias_control: bool
    perspective_coverage: bool
    feedback: str


def report_quality_agent(state: EvaluationState) -> dict:
    """보고서와 원자료를 대조해 네 품질 항목을 평가한다."""

    model = init_chat_model(
        "gpt-5.6-luna",
        model_provider="openai",
        reasoning_effort="low",
        use_responses_api=True,
        temperature=0,
        timeout=90,
        max_retries=2,
    )
    quality = model.with_structured_output(ReportQuality).invoke([
        SystemMessage(PROMPT_PATH.read_text(encoding="utf-8")),
        HumanMessage(json.dumps({
            "report": state["final_report"],
            "material": evaluation_material(state),
        }, ensure_ascii=False)),
    ])
    return {
        "report_quality": quality.model_dump(),
        # 재생성 실패로 대체 보고서가 나오더라도 재평가 시 횟수를 올린다.
        "report_retry_count": state.get("report_retry_count", 0)
        + int("report_quality" in state),
    }


def route_report_quality(state: EvaluationState) -> str:
    quality = state.get("report_quality", {})
    # Judge 실패 시 빈 결과가 남으므로 통과로 간주하거나 재생성하지 않는다.
    if not quality:
        return "end"
    passed = all(quality.get(key) is True for key in (
        "groundedness", "neutrality", "bias_control", "perspective_coverage",
    ))
    return "retry" if not passed and state.get("report_retry_count", 0) < 1 else "end"
