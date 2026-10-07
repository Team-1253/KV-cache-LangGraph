import json
from html import escape


def error_record(stage: str, exc: Exception) -> dict:
    """단계와 예외 유형을 기록해 부분 결과의 실패 원인을 남긴다."""

    print(f"[{stage}] {type(exc).__name__}: 이 단계의 결과가 불완전합니다.")

    return {
        "stage": stage,
        "error_type": type(exc).__name__,
        "message": "확보된 자료로 계속 진행함",
    }


def fallback_report(state: dict, reason: str) -> str:
    """자동 작성 실패 시 확보된 자료를 부분 결과 보고서로 보존한다."""

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
