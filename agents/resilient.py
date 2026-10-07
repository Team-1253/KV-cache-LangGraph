"""노드 실패를 한 곳에서 기록하고, 보고서까지 확보한 자료를 보존한다."""

import json
from html import escape


def response_text(response) -> str:
    content = response.content
    if isinstance(content, str):
        return content.strip()
    return "\n".join(block["text"] for block in content if block.get("type") == "text").strip()


def error_record(stage: str, exc: Exception) -> dict:
    # API 예외 원문 대신 유형만 기록한다.
    print(f"[{stage}] {type(exc).__name__}: 이 단계의 결과가 불완전합니다.")
    return {"stage": stage, "error_type": type(exc).__name__, "message": "확보된 자료로 계속 진행함"}


def fallback_report(state: dict, reason: str) -> str:
    data = {key: value for key, value in state.items() if key != "final_report"}
    return (
        f"# 기술 다관점 평가 보고서\n\n## SUMMARY\n\n{reason}\n"
        "자동 작성을 완료하지 못했으며 아래 자료는 부분 결과다.\n\n"
        "## 확보된 원자료\n\n<pre>\n"
        + escape(json.dumps(data, ensure_ascii=False, indent=2, default=str), quote=False)
        + "\n</pre>\n"
    )


def continuing_node(function, result_key: str):
    def run(state):
        try:
            return function(state)
        except Exception as exc:
            error = error_record(function.__name__, exc)
            if result_key == "final_report":
                state = {**state, "run_errors": [*state.get("run_errors", []), error]}
                return {result_key: fallback_report(state, "보고서 작성 단계가 실패했다."), "run_errors": [error]}
            return {result_key: {}, "run_errors": [error]}
    return run
