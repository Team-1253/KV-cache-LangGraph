import json
from html import escape


def response_text(response) -> str:
    """문자열 또는 콘텐츠 블록 응답에서 텍스트를 꺼낸다."""

    content = response.content
    if isinstance(content, str):
        return content.strip()

    return "\n".join(
        block["text"] for block in content if block.get("type") == "text"
    ).strip()


def error_record(stage: str, exc: Exception) -> dict:
    """단계와 예외 유형을 기록해 부분 결과의 실패 원인을 남긴다."""

    print(f"[{stage}] {type(exc).__name__}: 이 단계의 결과가 불완전합니다.")

    return {
        "stage": stage,
        "error_type": type(exc).__name__,
        "message": "확보된 자료로 계속 진행함",
    }


def fallback_report(state: dict, reason: str) -> str:
    """자동 작성 실패 시 확보된 State를 부분 결과 보고서로 보존한다."""

    data = {key: value for key, value in state.items() if key != "final_report"}

    return (
        f"# 기술 다관점 평가 보고서\n\n## SUMMARY\n\n{reason}\n"
        "자동 작성을 완료하지 못했으며 아래 자료는 부분 결과다.\n\n"
        "## 확보된 원자료\n\n<pre>\n"
        + escape(
            json.dumps(data, ensure_ascii=False, indent=2, default=str), quote=False
        )
        + "\n</pre>\n"
    )


def continuing_node(function, result_key: str):
    """일부 관점의 실패가 다른 평가와 확보된 자료의 보고를 막지 않게 한다."""

    def run(state):
        """노드를 실행하고 실패 시 오류와 빈 결과 또는 대체 보고서를 반환한다."""

        try:
            return function(state)
        except Exception as exc:
            error = error_record(function.__name__, exc)

            if result_key == "final_report":
                state = {**state, "run_errors": [*state.get("run_errors", []), error]}
                return {
                    result_key: fallback_report(state, "보고서 작성 단계가 실패했다."),
                    "run_errors": [error],
                }

            return {result_key: {}, "run_errors": [error]}

    return run
