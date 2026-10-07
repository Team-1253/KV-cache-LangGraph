"""같은 형식의 관점별 평가와 출처를 모아 한 번에 종합한다."""

import json
import re
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage

from agents.resilient import response_text
from agents.state import EvaluationState, evaluation_material

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "evaluation_synthesis.md"


def evaluation_synthesis_agent(state: EvaluationState) -> dict:
    model = init_chat_model("gpt-4.1-nano", model_provider="openai", temperature=0, max_retries=2)
    response = model.invoke([
        SystemMessage(PROMPT_PATH.read_text(encoding="utf-8")),
        HumanMessage(json.dumps(evaluation_material(state), ensure_ascii=False)),
    ])
    text = response_text(response)
    if not text:
        raise ValueError("종합 응답이 비어 있습니다.")
    block = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    try:
        result = json.loads(block.group(1) if block else text)
    except ValueError:
        result = text
    return {"evaluation_result": result}
