"""형식이 달라도 자료를 보존하고 노드 실패를 다음 단계에 전달한다."""

import importlib
import json
from typing import Any


def dump_data(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError, RecursionError):
        return repr(value)


def response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts).strip()
    return "" if content is None else dump_data(content)


def error_record(stage: str, exc: Exception) -> dict[str, str]:
    # API 예외 본문에는 요청 내용이나 인증 정보가 포함될 수 있어 유형만 기록한다.
    kind = type(exc).__name__
    print(f"[{stage}] {kind}: 이 단계의 결과가 불완전합니다. 다음 단계로 진행합니다.")
    return {"stage": stage, "error_type": kind, "message": "단계 처리 실패. 확보된 자료로 계속 진행함."}


def source_data(state: dict) -> dict:
    return {key: value for key, value in state.items() if key != "final_report"}


def fallback_report(state: dict, reason: str) -> str:
    return (
        "# 기술 다관점 평가 보고서\n\n"
        "## SUMMARY\n\n"
        f"{reason}\n\n자동 작성이 완료되지 않아 확보된 입력 자료를 아래에 보존했다. "
        "실패하거나 누락된 평가를 정상 완료된 결과로 해석해서는 안 된다.\n\n"
        "## 확보된 자료\n\n<pre>\n"
        + _escaped_data(state)
        + "\n</pre>\n"
    )


def _escaped_data(state: dict) -> str:
    from html import escape

    return escape(dump_data(source_data(state)), quote=False)


def source_appendix(state: dict) -> str:
    return "\n\n## 부록: 확보된 원자료\n\n<pre>\n" + _escaped_data(state) + "\n</pre>\n"


def continuing_node(module_name: str, function_name: str, result_key: str):
    """모듈 로딩 및 노드 실행 실패도 기록하고 fan-in까지 진행한다."""
    def run(state):
        try:
            function = getattr(importlib.import_module(module_name), function_name)
            update = function(state)
            if not isinstance(update, dict) or result_key not in update:
                raise ValueError("노드 결과가 반환되지 않았습니다.")
            # references는 그래프에서 목록으로 합치기 위한 전송 형식만 맞춘다.
            if "references" in update and not isinstance(update["references"], list):
                update = {**update, "references": ([] if update["references"] is None
                                                  else [update["references"]])}
            return update
        except Exception as exc:
            error = error_record(function_name, exc)
            if result_key == "final_report":
                report_state = {**state, "run_errors": [*state.get("run_errors", []), error]}
                return {result_key: fallback_report(report_state, "보고서 작성 단계가 실패했다."),
                        "run_errors": [error]}
            return {result_key: {"status": "FAILED", "error": error}, "run_errors": [error]}

    run.__name__ = function_name
    return run
