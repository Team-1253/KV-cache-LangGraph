"""평가 종합 Agent."""

import json
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage

from agents.state import EvaluationState

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "evaluation_synthesis.md"

_REQUIRED_INPUT_KEYS = (
    "trl_result",
    "market_result",
    "stakeholder_result",
    "domain_result",
)

_RESULT_KEYS = ("agreements", "disagreements", "tradeoffs", "implications")

# 목록별 세 번째 구간의 고정 표시. 형식 검사 전용이며 의미 판단은 LLM에 맡긴다.
_REQUIRED_SEGMENT = {
    "agreements": ("사실:",),
    "disagreements": ("사실:",),
    "tradeoffs": ("이득/비용:",),
    "implications": ("시사점:", "판단 보류/근거 공백:"),
}


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _extract_json_strict(content: str) -> str:
    """응답 전체가 JSON 하나여야 한다. 코드블록 뒤의 추가 텍스트는 거부한다."""
    text = content.strip()
    if not text:
        raise ValueError("종합 응답이 비어 있습니다.")
    if text.startswith("```"):
        if not text.endswith("```"):
            raise ValueError("JSON 코드 블록 뒤에 추가 텍스트가 있습니다.")
        inner = text[len("```") : -len("```")]
        if "```" in inner:
            raise ValueError("코드 블록은 하나만 허용됩니다.")
        inner = inner.strip()
        if inner.startswith("json"):
            inner = inner[len("json") :].strip()
        if not inner:
            raise ValueError("종합 응답이 비어 있습니다.")
        return inner
    if not (text.startswith("{") and text.endswith("}")):
        raise ValueError("종합 응답은 JSON 객체 하나여야 합니다.")
    return text


def _validate_result(parsed: object) -> dict[str, list[str]]:
    if not isinstance(parsed, dict):
        raise ValueError("종합 응답은 최상위 JSON 객체여야 합니다.")
    if set(parsed.keys()) != set(_RESULT_KEYS):
        raise ValueError(
            f"종합 응답의 키는 정확히 {list(_RESULT_KEYS)}이어야 합니다."
        )
    for key in _RESULT_KEYS:
        value = parsed[key]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"'{key}' 값은 문자열 목록이어야 합니다.")
        for item in value:
            if not item.strip():
                raise ValueError(f"'{key}' 항목에 빈 문자열이 있습니다.")
            segments = [seg.strip() for seg in item.split("|")]
            if len(segments) < 3 or "관점:" not in item or "근거:" not in item:
                raise ValueError(f"'{key}' 항목은 '기술명 | 관점: ... | ... | 근거: ...' 형식이어야 합니다.")
            if not segments[2].startswith(_REQUIRED_SEGMENT[key]):
                raise ValueError(f"'{key}' 항목의 세 번째 구간은 {list(_REQUIRED_SEGMENT[key])} 중 하나로 시작해야 합니다.")
    return {key: list(parsed[key]) for key in _RESULT_KEYS}


def evaluation_synthesis_agent(state: EvaluationState) -> dict:
    """네 관점의 일치, 불일치, trade-off와 시사점을 종합한다."""

    missing = [key for key in _REQUIRED_INPUT_KEYS if key not in state]
    if missing:
        raise ValueError(f"종합에 필요한 평가 결과가 없습니다: {missing}")

    system_prompt = _load_prompt()
    llm = init_chat_model("gpt-4.1-nano", model_provider="openai", temperature=0)

    state_json = json.dumps(state, ensure_ascii=False, default=str)
    response = llm.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"=== EvaluationState (JSON) ===\n{state_json}"),
        ]
    )

    try:
        parsed = json.loads(_extract_json_strict(str(response.content)))
    except json.JSONDecodeError as exc:
        raise ValueError(f"종합 응답이 올바른 JSON이 아닙니다: {exc}") from exc
    return {"evaluation_result": _validate_result(parsed)}
