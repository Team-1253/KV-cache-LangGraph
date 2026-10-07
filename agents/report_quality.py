import json
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from agents.resilient import error_record
from agents.state import OrchestratorState
from agents.synthesizer import evaluation_material

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "report_quality.md"


class ReportQuality(BaseModel):
    groundedness: bool
    neutrality: bool
    bias_control: bool
    perspective_coverage: bool
    feedback: str


def report_quality_agent(state: OrchestratorState) -> dict:
    """보고서와 원자료를 대조해 네 품질 항목을 평가한다."""

    try:
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
                "report": Path(state["report_uri"]).read_text(encoding="utf-8"),
                "material": evaluation_material(state),
            }, ensure_ascii=False)),
        ])
    except Exception as exc:
        return {
            "quality": {}, "status": "FAILED",
            "errors": [error_record("report_quality", exc)],
        }

    passed = all((quality.groundedness, quality.neutrality,
                  quality.bias_control, quality.perspective_coverage))
    return {
        "quality": quality.model_dump(),
        "status": state["status"] if passed or state["status"] == "FAILED" else "PARTIAL",
    }


def route_report_quality(state: OrchestratorState) -> str:
    quality = state.get("quality", {})
    # Judge 실패 시 빈 결과가 남으므로 통과로 간주하거나 재생성하지 않는다.
    if not quality:
        return "end"
    passed = all(quality.get(key) is True for key in (
        "groundedness", "neutrality", "bias_control", "perspective_coverage",
    ))
    return "retry" if not passed and state.get("step_count", 0) < state.get("max_steps", 2) else "end"
