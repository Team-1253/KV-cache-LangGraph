"""실제 입력의 키와 값을 읽어 평가를 종합한다."""

import json
import re
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage

from agents.resilient import dump_data, error_record, response_text, source_data
from agents.state import EvaluationState

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "evaluation_synthesis.md"


def _read_result(text: str):
    """JSON은 그대로 사용하고 자유 서술 응답도 버리지 않는다."""
    try:
        return json.loads(text)
    except ValueError:
        for block in re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL):
            try:
                return json.loads(block)
            except ValueError:
                continue
        return text


def evaluation_synthesis_agent(state: EvaluationState) -> dict:
    """키 이름, 중첩 구조, 인용 형식을 강제하지 않는다."""
    try:
        prompt = _PROMPT_PATH.read_text(encoding="utf-8")
        llm = init_chat_model(
            "gpt-4.1-nano", model_provider="openai", temperature=0,
            timeout=90, max_retries=1,
        )
        response = llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content="확보된 입력 자료:\n" + dump_data(source_data(state))),
        ])
        text = response_text(response)
        if not text:
            raise ValueError("종합 응답이 비어 있습니다.")
        return {"evaluation_result": _read_result(text)}
    except Exception as exc:
        error = error_record("evaluation_synthesis", exc)
        return {
            "evaluation_result": {
                "status": "INCOMPLETE",
                "note": "자동 종합에 실패했다. 보고서는 남아 있는 입력 자료를 직접 읽어 작성한다.",
                "error": error,
            },
            "run_errors": [error],
        }
